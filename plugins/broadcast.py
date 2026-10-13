from pyrogram.errors import InputUserDeactivated, UserNotParticipant, FloodWait, UserIsBlocked, PeerIdInvalid
from plugins.dbusers import db
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMINS
from plugins.admins_db import dynamic_admin_filter
import asyncio
import datetime
import secrets
import time

# How many users we message at once. Telegram's flood limits are mostly
# per-chat, so sending to many *different* users concurrently is safe and
# turns a broadcast that used to take ages (one message at a time) into
# something that finishes in a fraction of the time.
CONCURRENCY = 25

# /broadcast flow:
#   1. /broadcast            -> bot waits for the NEXT thing you send
#                               (text, photo, video, file, sticker, a whole album, anything)
#      /broadcast (as reply) -> uses the message you replied to (whole album if it is one)
#   2. bot sends you a PREVIEW of exactly what will go out + Confirm / Cancel buttons
#   3. only Confirm starts the broadcast
WAIT_SECONDS = 300        # how long the bot waits for your message after /broadcast
ALBUM_QUIET = 1.2         # seconds of silence that mean "the album is complete"
JOB_LIFETIME = 1800       # an unconfirmed preview expires after 30 minutes

_WAITING = {}   # admin_id -> {"t": start time, "msgs": [Message], "task": asyncio.Task|None}
_JOBS = {}      # token -> {"admin": id, "msgs": [Message], "t": created}
_BG = set()
_RUN_LOCK = asyncio.Lock()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


async def _deliver(client, user_id, msgs):
    """Send one message, or one whole album, to user_id as a copy (no 'Forwarded from')."""
    if len(msgs) == 1:
        await msgs[0].copy(chat_id=user_id)
    else:
        await client.forward_messages(
            chat_id=user_id,
            from_chat_id=msgs[0].chat.id,
            message_ids=[m.id for m in msgs],
            drop_author=True,
        )


async def broadcast_messages(client, user_id, msgs):
    try:
        await _deliver(client, user_id, msgs)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await broadcast_messages(client, user_id, msgs)
    except InputUserDeactivated:
        await db.delete_user_unless_banned(int(user_id))
        return False, "Deleted"
    except UserIsBlocked:
        await db.delete_user_unless_banned(int(user_id))
        return False, "Blocked"
    except PeerIdInvalid:
        # NOTE: PeerIdInvalid does NOT mean the user blocked/deleted the bot.
        # It usually means the bot's local peer cache doesn't have this user yet
        # (common after a redeploy on Railway/Koyeb/Render). Deleting the user
        # here was wrongly shrinking the user count after every broadcast.
        return False, "Error"
    except Exception:
        return False, "Error"


async def _target_user_ids():
    """Every saved user except banned ones (banned users never get any message)."""
    users = await db.get_all_users()
    ids = [int(u["id"]) for u in await users.to_list(length=None) if "id" in u]
    return [i for i in ids if not await db.is_user_banned(i)]


def _clean_old_jobs():
    now = time.time()
    for token in [t for t, j in _JOBS.items() if now - j["t"] > JOB_LIFETIME]:
        _JOBS.pop(token, None)


async def _make_preview(bot, admin_id, chat_id, msgs):
    """Show the admin exactly what will be sent, then Confirm / Cancel buttons."""
    msgs = sorted(msgs, key=lambda m: m.id)
    try:
        await _deliver(bot, chat_id, msgs)   # the very same code path the real broadcast uses
    except Exception as e:
        return await bot.send_message(
            chat_id,
            "<b>❌ This Message Can't Be Broadcast.</b>\n"
            f"<code>{type(e).__name__}</code>\n\n"
            "<b>Send /broadcast Again With A Different Message.</b>",
        )

    _clean_old_jobs()
    token = secrets.token_hex(4)
    _JOBS[token] = {"admin": admin_id, "msgs": msgs, "t": time.time()}
    total = len(await _target_user_ids())
    kind = f"Album Of {len(msgs)} Items" if len(msgs) > 1 else "Message"
    await bot.send_message(
        chat_id,
        f"<b>👆 Preview: This {kind} Will Be Sent To {total} Users.</b>\n\n"
        "<b>Is This Exactly What You Want To Broadcast?</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Broadcast", callback_data=f"bc_yes:{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"bc_no:{token}"),
        ]]),
    )


@Client.on_message(filters.command("broadcast") & dynamic_admin_filter("can_broadcast"))
async def broadcast_cmd(bot, message):
    admin_id = message.from_user.id
    _WAITING.pop(admin_id, None)

    replied = message.reply_to_message
    if replied:
        msgs = [replied]
        if replied.media_group_id:
            try:
                msgs = await bot.get_media_group(replied.chat.id, replied.id)
            except Exception:
                msgs = [replied]
        return await _make_preview(bot, admin_id, message.chat.id, msgs)

    _WAITING[admin_id] = {"t": time.time(), "msgs": [], "task": None}
    await message.reply_text(
        "<b>📣 Send The Message You Want To Broadcast.</b>\n"
        "Text, photo, video, file, sticker, a whole album, anything works.\n\n"
        "<b>/cancel To Stop.</b>"
    )


async def _finalize_waiting(bot, admin_id, delay):
    if delay:
        await asyncio.sleep(delay)
    state = _WAITING.pop(admin_id, None)
    if not state or not state["msgs"]:
        return
    msgs = state["msgs"]
    await _make_preview(bot, admin_id, msgs[0].chat.id, msgs)


# group=-2 so it runs before every other handler: while the bot is waiting for
# the broadcast message, whatever the admin sends is captured here (an album's
# items included) and does not trigger other features such as link generation.
@Client.on_message(filters.private & filters.incoming, group=-2)
async def _capture_broadcast_message(bot, message):
    user = message.from_user
    state = _WAITING.get(user.id) if user else None
    if not state:
        return
    if time.time() - state["t"] > WAIT_SECONDS:
        _WAITING.pop(user.id, None)
        return

    first_word = ((message.text or "").strip().split(maxsplit=1) or [""])[0].split("@")[0].lower()
    if first_word == "/broadcast":
        return   # lets the command restart the flow

    if first_word == "/cancel":
        _WAITING.pop(user.id, None)
        await message.reply_text("<b>Broadcast Cancelled.</b>")
        return message.stop_propagation()

    if message.media_group_id:
        state["msgs"].append(message)
        if state["task"]:
            state["task"].cancel()
        state["task"] = _spawn(_finalize_waiting(bot, user.id, ALBUM_QUIET))
    else:
        state["msgs"] = [message]
        _spawn(_finalize_waiting(bot, user.id, 0))
    message.stop_propagation()


@Client.on_callback_query(filters.regex(r"^bc_(yes|no):[0-9a-f]{8}$") & dynamic_admin_filter("can_broadcast"))
async def broadcast_answer(bot, query):
    action, _, token = query.data.partition(":")
    job = _JOBS.get(token)

    if not job:
        await query.answer("This preview expired. Run /broadcast again.", show_alert=True)
        try:
            await query.message.edit_text("<b>⌛ Preview Expired. Run /broadcast Again.</b>")
        except Exception:
            pass
        return
    if job["admin"] != query.from_user.id:
        return await query.answer("This broadcast belongs to another admin.", show_alert=True)

    if action == "bc_no":
        _JOBS.pop(token, None)
        await query.answer("Cancelled.")
        return await query.message.edit_text("<b>❌ Broadcast Cancelled. Nothing Was Sent.</b>")

    if _RUN_LOCK.locked():
        return await query.answer("A broadcast is already running. Wait for it to finish.", show_alert=True)

    _JOBS.pop(token, None)   # one confirm = one broadcast (a second tap finds nothing)
    await query.answer("Broadcasting...")
    await _run_broadcast(bot, query.message, job["msgs"])


async def _run_broadcast(bot, sts, msgs):
    async with _RUN_LOCK:
        user_ids = await _target_user_ids()
        start_time = time.time()
        total_users = len(user_ids)
        done = 0
        blocked = 0
        deleted = 0
        failed = 0
        success = 0
        await sts.edit_text("<b>Broadcasting your messages...</b>")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _send(uid):
            async with sem:
                return await broadcast_messages(bot, uid, msgs)

        tasks = [asyncio.create_task(_send(uid)) for uid in user_ids]
        for coro in asyncio.as_completed(tasks):
            ok, reason = await coro
            if ok:
                success += 1
            elif reason == "Blocked":
                blocked += 1
            elif reason == "Deleted":
                deleted += 1
            else:
                failed += 1
            done += 1
            if not done % 20:
                try:
                    await sts.edit_text(f"Broadcast in progress:\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}\nFailed: {failed}")
                except Exception:
                    pass

        time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
        await sts.edit_text(f"Broadcast Completed:\nCompleted in {time_taken} seconds.\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}\nFailed: {failed}")
