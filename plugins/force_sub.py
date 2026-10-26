# Force Subscribe helper functions
#
# Join Request Mode design (mirrors a known-working reference implementation):
#   1. Admin adds a channel in "request" mode -> we create a
#      creates_join_request=True invite link and show it as the Join button.
#   2. User taps it -> Telegram fires an on_chat_join_request event -> we
#      record {user_id, channel_id} in the DB. That's the ONLY signal we
#      trust for "request mode" channels - no extra live API calls that can
#      silently misbehave.
#   3. Next /start, we check the DB record first (fast + reliable), then
#      fall back to a direct get_chat_member (covers users who were already
#      an actual channel member before this feature was even turned on).

import asyncio
import logging
import datetime
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from plugins.settings_db import (
    get_settings, force_sub_channel_id, force_sub_channel_mode,
    force_sub_channel_link, set_force_sub_link,
    record_join_request, clear_join_request, get_join_request,
)

logger = logging.getLogger(__name__)

# Caps how many get_chat_member calls can be in flight at once across all
# users. Without this, a burst of many users' records going stale at the
# same time (e.g. right after launch) fires dozens of Telegram API calls
# simultaneously, which invites FloodWait and can starve /start responses.
_verify_semaphore = asyncio.Semaphore(8)

# If a request has sat unapproved for this long, we stop trusting it blindly
# and re-verify against Telegram - this is what catches requests the admin
# quietly *declined* (Telegram sends no event at all for a plain decline,
# unlike ban/kick which we already detect via handle_member_left below).
JOIN_REQUEST_TTL_DAYS = 3


@Client.on_chat_join_request()
async def track_join_request(client: Client, update):
    """Record that this user sent a join request to this channel. This is
    the single source of truth for 'request' mode channels - no manual or
    auto approval needed, sending the request is enough."""
    logger.info(f"[FSUB] join_request event: user={update.from_user.id} chat={update.chat.id}")
    settings = await get_settings()
    entries = settings.get("force_sub_channels") or []
    matched = any(
        isinstance(entry, dict) and entry.get("mode") == "request" and force_sub_channel_id(entry) == update.chat.id
        for entry in entries
    )
    if not matched:
        logger.info(f"[FSUB] chat={update.chat.id} is not a registered 'request' mode force-sub channel - ignoring")
        return

    try:
        await record_join_request(update.from_user.id, update.chat.id)
        logger.info(f"[FSUB] recorded join request: user={update.from_user.id} chat={update.chat.id}")
    except Exception as e:
        logger.error(f"[FSUB] Couldn't save join request for {update.from_user.id} in {update.chat.id}: {e}")
        return

    try:
        await client.send_message(
            update.from_user.id,
            "✅ <b>Request received!</b> Tap /start again to continue.",
        )
    except Exception as e:
        logger.warning(f"[FSUB] Couldn't DM user {update.from_user.id} after join request: {e}")


@Client.on_chat_member_updated()
async def handle_member_left(client: Client, update):
    """If someone leaves/is removed from a 'Join Request Mode' channel after
    having passed the force-sub check, wipe their recorded request so they
    have to send a fresh one (and see the Join button again) to use the
    bot again."""
    new = update.new_chat_member
    old = update.old_chat_member
    logger.info(
        f"[FSUB] member_updated event: chat={update.chat.id} "
        f"old_status={old.status if old else None} new_status={new.status if new else None}"
    )
    # In this pyrofork build, a leave/removal shows up as new_chat_member
    # being None entirely (not a ChatMember object with status=LEFT) - so
    # "new is None" itself has to count as a leave, on top of the normal
    # LEFT/BANNED status check.
    left_or_banned = new is None or new.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED)
    if not left_or_banned:
        return

    user = (new.user if new else None) or (old.user if old else None)
    if not user:
        logger.info(f"[FSUB] member_updated for chat={update.chat.id} has no user info, skipping")
        return

    settings = await get_settings()
    entries = settings.get("force_sub_channels") or []
    matched = any(
        isinstance(entry, dict) and entry.get("mode") == "request" and force_sub_channel_id(entry) == update.chat.id
        for entry in entries
    )
    if matched:
        try:
            await clear_join_request(user.id, update.chat.id)
            logger.info(f"[FSUB] cleared join request record: user={user.id} chat={update.chat.id} (left/banned)")
        except Exception as e:
            logger.error(f"[FSUB] Couldn't clear join request for {user.id} in {update.chat.id}: {e}")
    else:
        logger.info(f"[FSUB] chat={update.chat.id} is not a registered 'request' mode force-sub channel - ignoring leave")


async def _reverify_stale(client: Client, user_id: int, ch):
    """Runs in the background (never blocks a user's /start). Confirms a
    stale join-request record is still good; clears it if the user is no
    longer actually in the channel (e.g. was declined/removed)."""
    try:
        async with _verify_semaphore:
            member = await client.get_chat_member(ch, user_id)
        if member.status not in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT):
            await record_join_request(user_id, ch)  # confirmed real member - refresh timestamp
        else:
            await clear_join_request(user_id, ch)
            logger.info(f"[FSUB] user={user_id} chat={ch} stale record expired ({member.status}) - needs fresh request")
    except UserNotParticipant:
        await clear_join_request(user_id, ch)
        logger.info(f"[FSUB] user={user_id} chat={ch} stale record expired (not a member/no pending request) - needs fresh request")
    except Exception as e:
        logger.warning(f"[FSUB] user={user_id} chat={ch} couldn't re-verify stale record (keeping it): {e}")


async def _channel_status(client: Client, entry, user_id: int, index: int = 1):
    """Check a single force-sub channel for this user.
    Returns (missing_entry_or_None, button_row_or_None)."""
    ch = force_sub_channel_id(entry)
    mode = force_sub_channel_mode(entry)

    if mode == "request":
        doc = await get_join_request(user_id, ch)
        if doc:
            requested_at = doc.get("requested_at")
            age_days = (datetime.datetime.utcnow() - requested_at).days if requested_at else 0
            if age_days < JOIN_REQUEST_TTL_DAYS:
                return None, None  # still fresh, trust it - no extra API call needed

            # Older than the grace period - a plain "Decline" from the admin
            # sends no event to the bot at all, so this is our only chance
            # to catch it. But we must NOT make the user's /start wait on
            # a live Telegram call here - if many records go stale at once
            # this used to block every /start behind a burst of API calls.
            # So: trust the cached record for *this* reply, and re-verify
            # in the background; a stale/declined user just gets caught
            # on their *next* check instead of this one.
            logger.info(f"[FSUB] user={user_id} chat={ch} DB record is {age_days}d old - re-verifying in background")
            asyncio.create_task(_reverify_stale(client, user_id, ch))
            return None, None

    try:
        async with _verify_semaphore:
            member = await client.get_chat_member(ch, user_id)
        logger.info(f"[FSUB] user={user_id} chat={ch} get_chat_member status={member.status}")
        if member.status not in (enums.ChatMemberStatus.BANNED, enums.ChatMemberStatus.LEFT):
            if mode == "request":
                # They're already an actual member (e.g. joined before this
                # channel was even set to request mode) - record it so we
                # don't need to hit get_chat_member for them again.
                try:
                    await record_join_request(user_id, ch)
                except Exception:
                    pass
            return None, None
    except UserNotParticipant:
        logger.info(f"[FSUB] user={user_id} chat={ch} -> UserNotParticipant")
    except Exception as e:
        # Bot not admin there / invalid channel -> don't lock everyone out
        logger.warning(f"[FSUB] user={user_id} chat={ch} get_chat_member error (not locking out): {e}")
        return None, None

    # Not satisfied -> build the Join button for this channel
    try:
        chat = await client.get_chat(ch)
    except Exception:
        return entry, None

    link = None
    if mode == "request":
        link = force_sub_channel_link(entry)
        if not link:
            try:
                invite = await client.create_chat_invite_link(ch, creates_join_request=True, name="Force Sub (Join Request)")
                link = invite.invite_link
                await set_force_sub_link(ch, link)
            except Exception as e:
                logger.warning(f"Couldn't create join-request link for {ch}: {e}")
                link = None
    else:
        link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else None)

    # Deliberately show a generic "Join Channel N" label instead of the
    # real channel name/title on the button - matches the requested design.
    row = [InlineKeyboardButton(f"🔔 Join Channel {index}", url=link)] if link else None
    return entry, row


async def not_joined_channels(client: Client, user_id: int, settings=None):
    """Return the list of force-sub channel entries this user has NOT joined."""
    missing, _buttons = await get_missing_and_buttons(client, user_id, settings)
    return missing


def _chunk_buttons(rows, per_row: int = 2):
    """Flatten single-button rows and regroup them so 2 join buttons sit
    side-by-side per line instead of stacking one per line - looks a lot
    cleaner than a long single column of buttons."""
    flat = [btn for row in rows for btn in row]
    return [flat[i:i + per_row] for i in range(0, len(flat), per_row)]


async def get_missing_and_buttons(client: Client, user_id: int, settings=None):
    """Single pass: returns (missing_entries, join_buttons) together, so we
    never check the same channel twice (once for the missing-list, once for
    the buttons) like the old two-function design used to."""
    if settings is None:
        settings = await get_settings()
    if not settings.get("force_sub"):
        return [], []
    entries = settings.get("force_sub_channels") or []
    results = await asyncio.gather(*[_channel_status(client, entry, user_id, i) for i, entry in enumerate(entries, start=1)])
    missing = [entry for entry, _row in results if entry is not None]
    buttons = [row for _entry, row in results if row is not None]
    return missing, _chunk_buttons(buttons)


async def force_sub_join_buttons(client: Client, entries, user_id: int = None, settings=None):
    """Build one 'Join <channel>' button per row for the given force-sub entries.
    Kept for backward compatibility - prefer get_missing_and_buttons() which
    avoids re-checking each channel a second time."""
    if user_id is None:
        async def _build_only(entry, i):
            _entry, row = await _channel_status(client, entry, 0, i)
            return row
        rows = await asyncio.gather(*[_build_only(entry, i) for i, entry in enumerate(entries, start=1)])
        return [row for row in rows if row is not None]

    rows = await asyncio.gather(*[_channel_status(client, entry, user_id, i) for i, entry in enumerate(entries, start=1)])
    return _chunk_buttons([row for _entry, row in rows if row is not None])


@Client.on_callback_query(filters.regex(r"^fsub_verify:"))
async def fsub_verify_callback(client: Client, callback_query):
    """Instant re-check when the user taps 'Try Again' - no need to reopen
    the bot chat, this edits the same message in place with the result
    right away (and shows a popup so it's obvious whether it worked)."""
    user_id = callback_query.from_user.id
    param = callback_query.data.split(":", 1)[1]

    settings = await get_settings()
    missing, buttons = await get_missing_and_buttons(client, user_id, settings)

    if missing:
        await callback_query.answer(
            f"❌ Still {len(missing)} channel(s) pending! Join/request all of them, then try again.",
            show_alert=True,
        )
        buttons = buttons + [[InlineKeyboardButton("🔄 Try Again", callback_data=f"fsub_verify:{param}")]]
        try:
            if callback_query.message.photo:
                await callback_query.message.edit_caption(callback_query.message.caption.html, reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await callback_query.message.edit_text(callback_query.message.text.html, reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            pass
        return

    await callback_query.answer("✅ Verified! Tap Continue Below.", show_alert=True)
    username = client.me.username
    open_url = f"https://t.me/{username}?start={param}" if param and param != "-" else f"https://t.me/{username}?start=true"
    success_text = "<b>✅ Access Granted !!\n\n👇 Tap Continue To Get Your Content.</b>"
    try:
        if callback_query.message.photo:
            await callback_query.message.edit_caption(success_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Continue", url=open_url)]]))
        else:
            await callback_query.message.edit_text(success_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Continue", url=open_url)]]))
    except Exception:
        pass
