
import re
import os
import secrets
import json
import base64
import aiohttp
import asyncio
import html
import time
import traceback
from datetime import datetime, timezone

from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait, ListenerStopped
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid,
    UsernameInvalid,
    UsernameNotModified,
)

from config import (
    ADMINS,
    LOG_CHANNEL,
    WEBSITE_URL,
    WEBSITE_URL_MODE,
)

from plugins.admins_db import is_admin
from plugins.dbusers import db
from Peter.utils.cover_copy import copy_keep_cover
from Peter.utils import shortsite


# ============================================================
# CLOUDFLARE PERMANENT LINK WORKER
# ============================================================

PERMANENT_LINK_WORKER_URL = os.getenv("PERMANENT_LINK_WORKER_URL")
PERMANENT_LINK_ADMIN_KEY = os.getenv("PERMANENT_LINK_ADMIN_KEY")

links_col = db.db["permanent_links"]
unused_col = db.db["unused_bot_links"]   # bot links made with "Bot Link Only" (and old ones kept) that no domain link uses


async def remember_unused(param, label, log_id):
    """Never breaks link generation: a failed save only means the link is missing from the list."""
    try:
        await unused_col.update_one(
            {"_id": param},
            {"$set": {"label": label[:80], "log_id": log_id, "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as e:
        print(f"UNUSED RECORD ERROR: {type(e).__name__}: {e}")

# Fetch large ranges in chunks to reduce Telegram API requests.
BATCH_FETCH_SIZE = 200


def bold(text):
    return f"<b>{text}</b>"


def encode_start(value):
    return base64.urlsafe_b64encode(
        value.encode("ascii")
    ).decode().rstrip("=")


def get_media_unique_id(message):
    for media_type in ("document", "video", "audio"):
        media = getattr(message, media_type, None)
        if media and getattr(media, "file_unique_id", None):
            return media.file_unique_id
    return None


def get_message_cache_key(message):
    unique_id = get_media_unique_id(message)

    if unique_id:
        return f"media:{unique_id}"

    return f"message:{message.chat.id}:{message.id}"


async def get_saved_link(cache_key):
    return await links_col.find_one({"_id": cache_key})


async def save_link(cache_key, share_link, permanent_link=None):
    now = datetime.now(timezone.utc)

    update = {
        "$set": {
            "share_link": share_link,
            "updated_at": now,
        },
        "$setOnInsert": {
            "created_at": now,
        },
    }

    if permanent_link:
        update["$set"]["permanent_url"] = permanent_link

    try:
        await links_col.update_one(
            {"_id": cache_key},
            update,
            upsert=True,
        )
    except Exception as e:
        print(
            f"LINK CACHE SAVE ERROR: "
            f"{type(e).__name__}: {e}"
        )

    return await get_saved_link(cache_key)


async def create_permanent_link(destination):
    if not PERMANENT_LINK_WORKER_URL:
        print("PERMANENT LINK ERROR: Worker URL is missing")
        return None

    if not PERMANENT_LINK_ADMIN_KEY:
        print("PERMANENT LINK ERROR: Admin key is missing")
        return None

    api_url = (
        f"{PERMANENT_LINK_WORKER_URL.rstrip('/')}"
        "/admin/create-link"
    )

    headers = {
        "Content-Type": "application/json",
        "X-Admin-Key": PERMANENT_LINK_ADMIN_KEY,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                api_url,
                json={"destination": destination},
                headers=headers,
            ) as response:
                response_text = await response.text()

                print(
                    f"PERMANENT LINK WORKER RESPONSE: "
                    f"HTTP {response.status} | {response_text}"
                )

                if response.status != 200:
                    return None

                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError:
                    print("PERMANENT LINK ERROR: Invalid JSON")
                    return None

                if data.get("success"):
                    permanent_url = data.get("permanentUrl")

                    if permanent_url:
                        return permanent_url

                print(f"PERMANENT LINK ERROR: {data}")

    except Exception as e:
        print(
            f"PERMANENT LINK REQUEST ERROR: "
            f"{type(e).__name__}: {e}"
        )

    return None


async def get_or_create_link(cache_key, share_link):
    saved = await get_saved_link(cache_key)

    if saved and saved.get("permanent_url"):
        return (
            saved["permanent_url"],
            saved.get("share_link", share_link),
        )

    if saved and saved.get("share_link"):
        share_link = saved["share_link"]

    permanent_link = await create_permanent_link(share_link)

    if permanent_link:
        await save_link(cache_key, share_link, permanent_link)

        saved = await get_saved_link(cache_key)

        if saved and saved.get("permanent_url"):
            return saved["permanent_url"], share_link

        return permanent_link, share_link

    await save_link(cache_key, share_link)

    return None, share_link


# ============================================================
# ACCESS CONTROL
# ============================================================

async def allowed(_, __, message):
    if (
        message.from_user
        and await db.is_user_banned(message.from_user.id)
    ):
        return False

    # The bot is always private: only owners (ADMINS) and bot admins can
    # generate links / use /batch.
    if message.from_user and (
        message.from_user.id in ADMINS
        or await is_admin(message.from_user.id)
    ):
        return True

    return False


# ============================================================
# LINK HELPERS
# ============================================================

def make_file_share_link(log_message_id):
    encoded = encode_start(f"file_{log_message_id}")
    return f"{WEBSITE_URL.rstrip('/')}?start={encoded}"


def _file_log_id(share_link):
    """The log-channel message id inside a single-file bot link, or None."""
    m = re.search(r"[?&]start=([A-Za-z0-9_-]+)$", share_link or "")
    if not m:
        return None
    param = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(param + "=" * (-len(param) % 4)).decode()
    except Exception:
        return None
    kind, _, i = raw.partition("_")
    return int(i) if kind == "file" and i.isdigit() else None


async def _content_gone(bot, share_link):
    """True when the saved file behind this bot link was deleted from the log channel.
    If it cannot be checked, the content is assumed to be there."""
    lid = _file_log_id(share_link)
    if not lid:
        return False
    try:
        client = bot
        if client is None:
            from Peter.bot import StreamBot as client
        m = await client.get_messages(LOG_CHANNEL, lid)
    except Exception:
        return False
    return (not m) or bool(getattr(m, "empty", False))


async def _worker_call(path, payload):
    """JSON answer of a Worker admin endpoint. Raises RuntimeError with a short reason."""
    if not PERMANENT_LINK_WORKER_URL or not PERMANENT_LINK_ADMIN_KEY:
        raise RuntimeError("Worker URL or admin key is missing")
    url = f"{PERMANENT_LINK_WORKER_URL.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json", "X-Admin-Key": PERMANENT_LINK_ADMIN_KEY}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, json=payload, headers=headers) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"Worker HTTP {r.status}")
                data = json.loads(body)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Worker {type(e).__name__}")
    if not data.get("success"):
        raise RuntimeError(str(data.get("error") or "Worker refused"))
    return data


async def _refresh_stale(source_message, saved):
    """The saved content of this file was deleted from the log channel: store the file again
    and point the SAME domain link and shortener link at it. Returns (new_share_link, post_id).
    Raises RuntimeError (and removes the new copy) if the links could not be moved."""
    post = await copy_keep_cover(source_message, LOG_CHANNEL)
    new_share = make_file_share_link(post.id)
    permanent = saved.get("permanent_url")
    try:
        if permanent:
            key = permanent.rstrip("/").rsplit("/", 1)[-1]
            short = (await _worker_call("/admin/get", {"key": key})).get("shortUrl")
            host = shortsite.host_of(short) if short else ""
            if short and shortsite.site_for(short) and shortsite.configured(host):
                ok, why = await shortsite.set_destination_once(short, new_share)
                if not ok:
                    raise RuntimeError(f"Shortener Not Updated: {why}")
            await _worker_call("/admin/update", {"key": key, "destination": new_share})
        await links_col.update_one({"_id": saved["_id"]},
                                   {"$set": {"share_link": new_share, "updated_at": datetime.now(timezone.utc)}})
    except BaseException:
        try:
            await post.delete()
        except Exception:
            pass
        raise
    return new_share, post.id


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def link_text(permanent_link, share_link, count=None, note="", bot_only=False):
    """The "your link is ready" message. Bold text, the link itself in monospace
    (tap to copy). `count` is set for a batch, `note` is an optional warning line."""
    if bot_only:
        head, label, link = "✅ BOT LINK GENERATED !!", "🤖 Bot Link:", share_link
    elif permanent_link:
        head = "✅ BATCH LINK GENERATED !!" if count else "✅ LINK GENERATED !!"
        label, link = "🔗 Permanent Link:", permanent_link
    else:
        head = "✅ BATCH LINK GENERATED !!" if count else "✅ LINK GENERATED !!"
        label, link = "🖇 Original Link:", share_link
    if bot_only and count:
        head = "✅ BOT LINK GENERATED !!"
    lines = [head, ""]
    if count:
        lines += [f"📦 Contains: {_plural(count, 'Message')}", ""]
    if note:
        lines += [note, ""]
    lines.append(label)
    return "<b>" + "\n".join(lines) + f"</b>\n<code>{html.escape(link)}</code>"


def reply_link_text(permanent_link, share_link):
    return link_text(permanent_link, share_link)


def bot_link_text(share_link):
    """Reply for "Bot Link Only": just the bot link (no domain link, no shortener)."""
    return link_text(None, share_link, bot_only=True)


def _human_size(size):
    if not size:
        return "N/A"
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024


def _file_info(message):
    """(name, size) of the media in a message, for the report."""
    media_type = getattr(message, "media", None)
    obj = getattr(message, media_type.value, None) if media_type else None
    name = getattr(obj, "file_name", None)
    if not name:
        text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
        name = text[:60] if text else (media_type.value.title() if media_type else "Message")
    return name, getattr(obj, "file_size", None)


async def post_links_to_log(bot, reply_to_id, admin, info_lines, permanent_link, share_link):
    """Right under a file (or Batch.json) that was just stored in LOG_CHANNEL,
    post the links made for it, so the database channel shows
    "file -> its links". Only the permanent link and the original link are
    shown (the shortener link in the middle is never shown). A problem here
    must never break link generation, so every error is swallowed."""
    lines = ["<b>🔗 Links For The Message Above</b>", ""]
    lines += info_lines
    if admin:
        lines.append(f"<b>👤 By: {admin.mention} (</b><code>{admin.id}</code><b>)</b>")
    lines.append("")
    if permanent_link:
        lines.append(f"<b>🔗 Permanent: {html.escape(permanent_link)}</b>")
    if share_link:
        lines.append(f"<b>🖇 Original: {html.escape(share_link)}</b>")
    text = "\n".join(lines)
    try:
        try:
            await bot.send_message(
                LOG_CHANNEL, text,
                reply_to_message_id=reply_to_id,
                disable_web_page_preview=True,
            )
        except TypeError:
            await bot.send_message(LOG_CHANNEL, text, disable_web_page_preview=True)
    except Exception as e:
        print(f"LOG LINKS ERROR: {type(e).__name__}: {e}")


async def build_link_reply(source_message, bot=None, admin=None, bot_only=False):
    """Create (or reuse) the link for one media message and return the reply
    text. This is the only place that copies a file into LOG_CHANNEL. When a
    new file was stored and `bot` is given, its links are posted right under
    it in LOG_CHANNEL."""
    cache_key = get_message_cache_key(source_message)
    saved = await get_saved_link(cache_key)

    # The saved copy was deleted from the log channel (for example the channel was cleaned):
    # the old links would open nothing, so the file is stored again.
    if saved and saved.get("share_link") and await _content_gone(bot, saved["share_link"]):
        if bot_only:
            saved = None          # just a new bot link, the old pair is left alone
        else:
            try:
                new_share, new_id = await _refresh_stale(source_message, saved)
            except RuntimeError as e:
                return bold(f"❌ Could Not Refresh The Old Link.\n{html.escape(str(e))}")
            if bot and new_id:
                name, size = _file_info(source_message)
                await post_links_to_log(
                    bot, new_id, admin,
                    [
                        f"<b>🎬 File:</b> <code>{html.escape(name)}</code>",
                        f"<b>📦 Size: {_human_size(size)}</b>",
                    ],
                    saved.get("permanent_url"), new_share,
                )
            saved = dict(saved, share_link=new_share)

    if bot_only:
        # Only the bot link: no domain link, no shortener, nothing saved in the Worker/database.
        stored_id = None
        if saved and saved.get("share_link"):
            share_link = saved["share_link"]
        else:
            post = await copy_keep_cover(source_message, LOG_CHANNEL)
            share_link = make_file_share_link(post.id)
            stored_id = post.id
        if stored_id:
            name, size = _file_info(source_message)
            kind = "File" if getattr(source_message, "media", None) else "Message"
            await remember_unused(encode_start(f"file_{stored_id}"), f"1 {kind} ({name})", stored_id)
        if bot and stored_id:
            await post_links_to_log(
                bot, stored_id, admin,
                [
                    f"<b>🎬 File:</b> <code>{html.escape(name)}</code>",
                    f"<b>📦 Size: {_human_size(size)}</b>",
                ],
                None, share_link,
            )
        return bot_link_text(share_link)

    if saved and saved.get("permanent_url"):
        return reply_link_text(
            saved["permanent_url"],
            saved.get("share_link", ""),
        )

    stored_id = None
    if saved and saved.get("share_link"):
        share_link = saved["share_link"]
    else:
        post = await copy_keep_cover(source_message, LOG_CHANNEL)
        share_link = make_file_share_link(post.id)
        stored_id = post.id

    permanent_link, share_link = await get_or_create_link(
        cache_key,
        share_link,
    )

    if bot and stored_id:
        name, size = _file_info(source_message)
        await post_links_to_log(
            bot, stored_id, admin,
            [
                f"<b>🎬 File:</b> <code>{html.escape(name)}</code>",
                f"<b>📦 Size: {_human_size(size)}</b>",
            ],
            permanent_link, share_link,
        )

    return reply_link_text(permanent_link, share_link)


# ============================================================
# FILE SENT TO THE BOT: ASK FIRST, THEN GENERATE
# ============================================================
# A file sent to the bot is NOT copied into LOG_CHANNEL automatically any
# more. The bot asks first, so files sent for other reasons (for example a
# .txt id list you then reply /ban to) never create a link or a log copy.

@Client.on_message(
    (filters.document | filters.video | filters.audio)
    & filters.private
    & filters.create(allowed)
)
async def incoming_gen_link(bot, message):
    await message.reply(
        bold("Generate A Link For This File?"),
        quote=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Generate Link", callback_data=f"gl_yes:{message.id}")],
            [InlineKeyboardButton("🤖 Bot Link Only", callback_data=f"gl_bot:{message.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"gl_no:{message.id}")],
        ]),
    )


@Client.on_callback_query(filters.regex(r"^gl_(yes|no|bot):\d+$"))
async def gen_link_answer(bot, query):
    user_id = query.from_user.id

    if await db.is_user_banned(user_id) or not (
        user_id in ADMINS or await is_admin(user_id)
    ):
        return await query.answer("Not allowed.", show_alert=True)

    action, _, msg_id = query.data.partition(":")
    prompt = query.message

    if action == "gl_no":
        await query.answer("Cancelled.")
        return await prompt.edit_text(bold("Cancelled. No Link Was Created."))

    # Edit first so a double tap cannot create two links.
    await query.answer("Generating...")
    try:
        await prompt.edit_text(bold("⏳ Generating Your Link..."))
    except Exception:
        return

    try:
        source = await bot.get_messages(prompt.chat.id, int(msg_id))
        if not source or source.empty:
            return await prompt.edit_text(
                bold("❌ That File Is No Longer Available. Send It Again.")
            )
        text = await build_link_reply(source, bot, query.from_user, bot_only=(action == "gl_bot"))
    except FloodWait as e:
        return await prompt.edit_text(
            bold(f"❌ Telegram Is Busy. Try Again In {int(e.value)} Seconds.")
        )
    except Exception as e:
        print(f"GEN LINK ERROR: {type(e).__name__}: {e}")
        return await prompt.edit_text(
            bold("❌ Could Not Create The Link. Please Try Again.")
        )

    await prompt.edit_text(text)


# ============================================================
# /LINK
# ============================================================

@Client.on_message(
    filters.command(["link"]) & filters.create(allowed)
)
async def gen_link_s(bot, message):
    replied = message.reply_to_message

    if not replied:
        return await message.reply(
            bold("Reply to a message to get a shareable link.")
        )

    # Ask first. Confirm / Cancel are handled by gen_link_answer (the same
    # buttons as for a file sent to the bot).
    await message.reply(
        bold("Generate A Link For This Message?"),
        quote=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Generate Link", callback_data=f"gl_yes:{replied.id}")],
            [InlineKeyboardButton("🤖 Bot Link Only", callback_data=f"gl_bot:{replied.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"gl_no:{replied.id}")],
        ]),
    )


# ============================================================
# BATCH HELPERS
# ============================================================

_BATCH_LINK_RE = re.compile(
    r"^(?:https?://)?"
    r"(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(c/)?"
    r"([a-zA-Z0-9_]+)/"
    r"(\d+)/?$"
)


def _extract_batch_ref(msg):
    forward_chat = getattr(msg, "forward_from_chat", None)
    forward_id = getattr(msg, "forward_from_message_id", None)

    if forward_chat and forward_id:
        return forward_chat.id, int(forward_id)

    text = (msg.text or msg.caption or "").strip()
    match = _BATCH_LINK_RE.fullmatch(text)

    if not match:
        return None

    is_private_link = bool(match.group(1))
    chat_name = match.group(2)
    msg_id = int(match.group(3))

    if is_private_link:
        if not chat_name.isdigit():
            return None

        chat_id = int(f"-100{chat_name}")
    elif chat_name.isdigit():
        return None
    else:
        chat_id = chat_name

    return chat_id, msg_id


async def ask_batch_reference(bot, chat_id, prompt):
    # Exactly one prompt for each reference: FIRST and LAST.
    prompt_msg = await bot.send_message(
        chat_id,
        bold(prompt),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="bcb_cancel"),
        ]]),
    )

    # Keep the original empty prompt: it does not add a third
    # user-facing instruction message.
    try:
        answer = await bot.ask(chat_id, "", timeout=300)
    finally:
        # Remove the button once this step is over, so an old button
        # can never cancel the next step by mistake.
        try:
            await prompt_msg.edit_reply_markup(None)
        except Exception:
            pass

    answer_text = (answer.text or "").strip()

    if answer_text.split(maxsplit=1)[:1] == ["/cancel"]:
        await answer.reply(bold("Cancelled."))
        return None

    ref = _extract_batch_ref(answer)

    if not ref:
        await answer.reply(
            bold(
                "❌ Couldn't Read That. Forward It With Tag Or Send Its Link."
            )
        )
        return None

    return ref


async def get_messages_chunk(bot, chat_id, ids):
    """
    Fetch a chunk. FloodWait is retried after Telegram's requested
    delay. Other persistent errors are raised instead of silently
    dropping the chunk and producing an incomplete batch.
    """
    while True:
        try:
            messages = await bot.get_messages(
                chat_id,
                ids,
                replies=0,
            )

            if not isinstance(messages, list):
                messages = [messages] if messages else []

            return {
                msg.id: msg
                for msg in messages
                if msg and getattr(msg, "id", None)
            }

        except FloodWait as e:
            wait_seconds = int(e.value) + 1

            print(
                f"BATCH FLOOD WAIT: {wait_seconds} seconds"
            )

            await asyncio.sleep(wait_seconds)


async def fetch_batch_messages(
    bot,
    chat_id,
    first_id,
    last_id,
    status,
):
    outlist = []
    originals = {}
    total = last_id - first_id + 1
    processed = 0

    print(
        f"BATCH START: chat={chat_id}, first={first_id}, "
        f"last={last_id}, total={total}"
    )

    for chunk_start in range(
        first_id,
        last_id + 1,
        BATCH_FETCH_SIZE,
    ):
        chunk_end = min(
            chunk_start + BATCH_FETCH_SIZE - 1,
            last_id,
        )

        ids = list(range(chunk_start, chunk_end + 1))

        # Do not silently skip a failed chunk.
        by_id = await get_messages_chunk(bot, chat_id, ids)

        for msg_id in ids:
            msg = by_id.get(msg_id)

            # Deleted or unavailable messages can legitimately be absent.
            if not msg or getattr(msg, "empty", False):
                continue

            if getattr(msg, "service", None):
                continue

            outlist.append({
                "channel_id": chat_id,
                "msg_id": msg_id,
            })
            originals[msg_id] = msg

        processed += len(ids)

        try:
            await status.edit(
                bold(
                    "⏳ Generating batch...\n\n"
                    f"Range checked: {processed}/{total}\n"
                    f"Messages collected: {len(outlist)}"
                )
            )
        except Exception:
            pass

    print(f"BATCH COLLECTED: {len(outlist)}")
    return outlist, originals


# ============================================================
# BATCH FILES LIVE IN THE LOG CHANNEL (the database)
# ============================================================
# A batch made from any other channel is first copied into LOG_CHANNEL and the
# batch link points to those copies. So the link keeps working even if the
# source channel is deleted, restricted or the bot loses access to it.

FORWARD_CHUNK = 100   # Telegram forwards at most 100 messages per call


def _content_id(msg):
    """Something that stays the same between a message and its copy."""
    media = getattr(msg, "media", None)
    if media:
        obj = getattr(msg, media.value, None)
        uid = getattr(obj, "file_unique_id", None)
        if uid:
            return ("media", uid)
    return ("text", (getattr(msg, "text", None) or getattr(msg, "caption", None) or ""))


async def _forward_chunk(bot, from_chat, ids):
    while True:
        try:
            res = await bot.forward_messages(
                chat_id=LOG_CHANNEL,
                from_chat_id=from_chat,
                message_ids=ids,
                drop_author=True,
            )
            return res if isinstance(res, list) else [res]
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)


async def _copy_one(bot, original):
    while True:
        try:
            return await copy_keep_cover(original, LOG_CHANNEL)
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except Exception as e:
            print(f"BATCH COPY ERROR: {type(e).__name__}: {e}")
            return None


async def copy_batch_to_log(bot, outlist, originals, status):
    """Copy every batch message into LOG_CHANNEL. Returns (new_outlist, kept)
    where `kept` counts messages that could not be copied (they keep pointing
    at the source channel so the batch still works)."""
    if not outlist:
        return outlist, 0

    source_chat = outlist[0]["channel_id"]
    result = []
    kept = 0

    for start in range(0, len(outlist), FORWARD_CHUNK):
        chunk = outlist[start:start + FORWARD_CHUNK]
        ids = [entry["msg_id"] for entry in chunk]
        copies = None

        # Fast path: one forward call per 100 messages (keeps covers, captions, albums).
        try:
            res = await _forward_chunk(bot, source_chat, ids)
            ok = len(res) == len(chunk) and all(
                _content_id(originals[i]) == _content_id(c)
                for i, c in zip(ids, res)
            )
            if ok:
                copies = res
            else:
                # Not a clean one-to-one result: undo it and copy one by one.
                try:
                    await bot.delete_messages(LOG_CHANNEL, [m.id for m in res])
                except Exception:
                    pass
        except Exception as e:
            print(f"BATCH FORWARD ERROR: {type(e).__name__}: {e}")

        if copies is None:
            copies = [await _copy_one(bot, originals[i]) for i in ids]

        for entry, copy in zip(chunk, copies):
            if copy:
                result.append({"channel_id": LOG_CHANNEL, "msg_id": copy.id})
            else:
                result.append(entry)
                kept += 1

        try:
            await status.edit(
                bold(
                    "⏳ Saving files to the database channel...\n\n"
                    f"Done: {len(result)}/{len(outlist)}"
                )
            )
        except Exception:
            pass

    return result, kept


# ============================================================
# /BATCH
# ============================================================

@Client.on_callback_query(filters.regex(r"^bcb_cancel$") & filters.create(allowed))
async def batch_cancel_button(bot, query):
    """Cancel button under the batch prompts (same as /cancel)."""
    _MANUAL.pop(query.from_user.id, None)
    try:
        await bot.stop_listening(chat_id=query.message.chat.id)
    except Exception:
        traceback.print_exc()
    await query.answer("Cancelled.")
    try:
        await query.message.edit_text(bold("Cancelled."))
    except Exception:
        pass


# ---------------- link type question (batch) ----------------
_TYPE_WAIT = {}   # token -> (user id, future)


async def ask_link_type(bot, chat_id, user_id, panel=None):
    """Asks "Generate Link" or "Bot Link Only". Returns ("full" | "bot" | "no", message shown)."""
    token = secrets.token_hex(4)
    fut = asyncio.get_running_loop().create_future()
    _TYPE_WAIT[token] = (user_id, fut)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Generate Link", callback_data=f"bt:full:{token}")],
        [InlineKeyboardButton("🤖 Bot Link Only", callback_data=f"bt:bot:{token}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"bt:no:{token}")],
    ])
    text = bold("Choose The Link Type")
    try:
        if panel is not None:
            await panel.edit_text(text, reply_markup=markup)
            shown = panel
        else:
            shown = await bot.send_message(chat_id, text, reply_markup=markup)
        choice = await asyncio.wait_for(fut, timeout=300)
    except asyncio.TimeoutError:
        choice, shown = "no", panel
    finally:
        _TYPE_WAIT.pop(token, None)
    return choice, shown


@Client.on_callback_query(filters.regex(r"^bt:(full|bot|no):[0-9a-f]+$") & filters.create(allowed))
async def link_type_answer(bot, query):
    _, choice, token = query.data.split(":")
    waiting = _TYPE_WAIT.get(token)
    if not waiting or waiting[0] != query.from_user.id or waiting[1].done():
        return await query.answer("Expired. Start Again.", show_alert=True)
    await query.answer()
    waiting[1].set_result(choice)


# ---------------- method choice ----------------
# /batch first asks HOW the batch is made:
#   1) Send Messages One By One  - anything (files, text, photos...) sent or forwarded to the
#      bot, then Done. Each one is copied to the log channel in the order it was sent.
#   2) First & Last Message      - the old way (first + last message / link of a channel range).
# Change Link -> Batch asks the same question and uses the same collector.

MANUAL_IDLE = 600
_MANUAL = {}   # admin id -> {"items": {msg_id: message}, "panel", "t", "busy", "user", "finish", "on_cancel"}


def method_markup(manual_cb, range_cb, cancel_cb):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Send Messages One By One", callback_data=manual_cb)],
        [InlineKeyboardButton("🔗 First & Last Message", callback_data=range_cb)],
        [InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb)],
    ])


def _manual_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data="bm_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="bm_cancel"),
    ]])


def _manual_text(count):
    return bold(
        "📤 Send Or Forward The Messages One By One.\n"
        "Press Done When Finished.\n\n"
        f"📥 Received: {count}"
    )


async def start_manual(user, panel, finish, on_cancel=None, ask_type=False):
    """Open the one-by-one collector. `panel` is the bot message that shows the counter;
    `finish(bot, admin_id, state, outlist, failed)` is called after Done."""
    _MANUAL[user.id] = {
        "items": {}, "panel": panel, "t": time.time(), "busy": False, "user": user,
        "finish": finish, "on_cancel": on_cancel, "shown": 0, "task": None,
        "ask_type": ask_type, "bot_only": False,
    }
    try:
        await panel.edit_text(_manual_text(0), reply_markup=_manual_markup())
    except Exception:
        pass


async def _refresh_panel(state):
    await asyncio.sleep(1)           # one edit per second at most while files keep coming
    state["task"] = None
    count = len(state["items"])
    if state["busy"] or count == state["shown"]:
        return
    state["shown"] = count
    try:
        await state["panel"].edit_text(_manual_text(count), reply_markup=_manual_markup())
    except Exception:
        pass


@Client.on_message(filters.private & filters.incoming, group=-4)
async def manual_collect(bot, message):
    user = message.from_user
    state = _MANUAL.get(user.id) if user else None
    if not state:
        return
    if time.time() - state["t"] > MANUAL_IDLE and not state["busy"]:
        _MANUAL.pop(user.id, None)
        return
    text = (message.text or "").strip()
    if text.split(maxsplit=1)[:1] == ["/cancel"]:
        if not state["busy"]:
            _MANUAL.pop(user.id, None)
            if state["on_cancel"]:
                state["on_cancel"]()
            await message.reply(bold("Cancelled."))
            return message.stop_propagation()
        return
    if text.startswith("/"):
        return   # other commands still work
    state["t"] = time.time()
    if not state["busy"]:
        state["items"][message.id] = message
        if not state["task"]:
            state["task"] = asyncio.create_task(_refresh_panel(state))
    return message.stop_propagation()


@Client.on_callback_query(filters.regex(r"^bm_cancel$") & filters.create(allowed))
async def manual_cancel(bot, query):
    state = _MANUAL.get(query.from_user.id)
    if state and state["busy"]:
        return await query.answer("Saving... Please Wait.", show_alert=True)
    _MANUAL.pop(query.from_user.id, None)
    if state and state["on_cancel"]:
        state["on_cancel"]()
    await query.answer("Cancelled.")
    try:
        await query.message.edit_text(bold("Cancelled."))
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^bm_done$") & filters.create(allowed))
async def manual_done(bot, query):
    uid = query.from_user.id
    state = _MANUAL.get(uid)
    if not state:
        return await query.answer("Expired. Start Again.", show_alert=True)
    if state["busy"]:
        return await query.answer("Already Saving...", show_alert=True)
    if not state["items"]:
        return await query.answer("Send At Least One Message First.", show_alert=True)
    state["busy"] = True
    await query.answer()
    panel = state["panel"]
    if state.get("ask_type"):
        choice, _shown = await ask_link_type(bot, query.message.chat.id, uid, panel=panel)
        if choice == "no":
            _MANUAL.pop(uid, None)
            if state["on_cancel"]:
                state["on_cancel"]()
            try:
                await panel.edit_text(bold("Cancelled."))
            except Exception:
                pass
            return
        state["bot_only"] = (choice == "bot")
    msgs = [state["items"][k] for k in sorted(state["items"])]   # message ids grow in sending order
    outlist, failed = [], 0
    try:
        for i, m in enumerate(msgs, 1):
            copy = await _copy_one(bot, m)
            if copy:
                outlist.append({"channel_id": LOG_CHANNEL, "msg_id": copy.id})
            else:
                failed += 1
            if i % 10 == 0 or i == len(msgs):
                try:
                    await panel.edit_text(bold(
                        "⏳ Saving the messages to the database channel...\n\n"
                        f"Done: {i}/{len(msgs)}"
                    ))
                except Exception:
                    pass
        if not outlist:
            _MANUAL.pop(uid, None)
            if state["on_cancel"]:
                state["on_cancel"]()
            return await panel.edit_text(bold("❌ Could Not Save Any Of The Messages. Nothing Was Created."))
        _MANUAL.pop(uid, None)
        await state["finish"](bot, uid, state, outlist, failed)
    except Exception as e:
        traceback.print_exc()
        _MANUAL.pop(uid, None)
        if state["on_cancel"]:
            state["on_cancel"]()
        try:
            await panel.edit_text(f"<b>❌ Batch failed:</b>\n<code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>")
        except Exception:
            pass


async def _finish_manual_batch(bot, admin_id, state, outlist, failed):
    """/batch -> One By One: same Batch.json + links as the range method."""
    user, panel = state["user"], state["panel"]
    filename = f"batchmode_{admin_id}.json"
    try:
        with open(filename, "w", encoding="utf-8") as out:
            json.dump(outlist, out)
        post = await bot.send_document(
            LOG_CHANNEL, filename, file_name="Batch.json",
            caption=bold("⚠️ Batch Generated For Filestore."),
        )
    finally:
        if os.path.exists(filename):
            os.remove(filename)

    encoded_post = encode_start(str(post.id))
    share_link = f"{WEBSITE_URL.rstrip('/')}?start=BATCH-{encoded_post}"
    if state.get("bot_only"):
        permanent_link = None   # Bot Link Only: no domain link, no shortener
        await remember_unused(f"BATCH-{encoded_post}", f"Batch ({len(outlist)} Files)", post.id)
    else:
        permanent_link, share_link = await get_or_create_link(f"batch:manual:{post.id}", share_link)
    note = f"⚠️ Skipped: {_plural(failed, 'Message')}" if failed else ""
    final_text = link_text(permanent_link, share_link, count=len(outlist), note=note,
                           bot_only=bool(state.get("bot_only")))
    await post_links_to_log(
        bot, post.id, user,
        [f"<b>📦 Batch:</b> <code>{len(outlist)}</code> <b>messages"
         + (f" ({failed} skipped)" if failed else "") + "</b>",
         "<b>📺 Source: Sent One By One</b>"],
        permanent_link, share_link,
    )
    await panel.edit_text(final_text)


@Client.on_message(
    filters.command(["batch"]) & filters.create(allowed)
)
async def gen_link_batch(bot, message):
    _MANUAL.pop(message.from_user.id, None)
    await message.reply(
        bold("📦 Choose How To Make The Batch"),
        reply_markup=method_markup("bm_manual", "bm_range", "bcb_cancel"),
    )


@Client.on_callback_query(filters.regex(r"^bm_manual$") & filters.create(allowed))
async def batch_method_manual(bot, query):
    await query.answer()
    await start_manual(query.from_user, query.message, _finish_manual_batch, ask_type=True)


@Client.on_callback_query(filters.regex(r"^bm_range$") & filters.create(allowed))
async def batch_method_range(bot, query):
    await query.answer()
    try:
        await query.message.edit_reply_markup(None)
    except Exception:
        pass
    await _range_batch(bot, query.message, query.from_user)


async def _range_batch(bot, message, user):
    status = None

    try:
        first_ref = await ask_batch_reference(
            bot,
            message.chat.id,
            "📦 Send The First Message Of Your Batch.\n\n"
            "Forward It With Tag, Or Send Its Link.",
        )

        if first_ref is None:
            return

        first_chat_id, first_msg_id = first_ref

        last_ref = await ask_batch_reference(
            bot,
            message.chat.id,
            "📦 Send The Last Message Of Your Batch.\n\n"
            "Forward It With Tag, Or Send Its Link.",
        )

        if last_ref is None:
            return

        last_chat_id, last_msg_id = last_ref

        if str(first_chat_id) != str(last_chat_id):
            return await message.reply(
                bold("Chat IDs do not match.")
            )

        if first_msg_id < 1 or last_msg_id < 1:
            return await message.reply(
                bold("❌ Message IDs must be positive numbers.")
            )

        if first_msg_id > last_msg_id:
            return await message.reply(
                bold(
                    "❌ First message ID is greater than "
                    "the last message ID."
                )
            )

        batch_cache_key = (
            f"batch:{first_chat_id}:{first_msg_id}:{last_msg_id}"
        )

        saved = await get_saved_link(batch_cache_key)

        if saved and saved.get("permanent_url"):
            return await message.reply(
                reply_link_text(
                    saved["permanent_url"],
                    saved.get("share_link", ""),
                )
            )

        try:
            await bot.get_chat(first_chat_id)

        except ChannelInvalid:
            return await message.reply(
                bold(
                    "This may be a private channel or group. "
                    "Make sure the bot can access it."
                )
            )

        except (UsernameInvalid, UsernameNotModified):
            return await message.reply(
                bold("Invalid link specified.")
            )

        except Exception as e:
            print(
                f"BATCH CHAT ACCESS ERROR: "
                f"{type(e).__name__}: {e}"
            )

            return await message.reply(
                bold(
                    "❌ Channel access failed. Check the channel "
                    "reference and bot permissions."
                )
            )

        choice, shown = await ask_link_type(bot, message.chat.id, user.id)
        if choice == "no":
            try:
                if shown is not None:
                    await shown.edit_text(bold("Cancelled."))
            except Exception:
                pass
            return
        bot_only = (choice == "bot")

        status = shown
        await status.edit_text(
            bold(
                "Generating link for your messages.\n"
                "This may take time depending on the number of messages."
            )
        )

        outlist, originals = await fetch_batch_messages(
            bot,
            first_chat_id,
            first_msg_id,
            last_msg_id,
            status,
        )

        if not outlist:
            return await status.edit(
                bold(
                    "❌ No messages found in this range. "
                    "Check the message IDs and bot's channel access."
                )
            )

        # Files must live in the log channel, not in the source channel.
        kept = 0
        source_chat = await bot.get_chat(first_chat_id)
        source_id = source_chat.id
        source_title = getattr(source_chat, "title", None) or str(source_id)
        if int(source_id) != int(LOG_CHANNEL):
            outlist, kept = await copy_batch_to_log(
                bot, outlist, originals, status
            )

        filename = f"batchmode_{user.id}.json"

        try:
            with open(filename, "w", encoding="utf-8") as out:
                json.dump(outlist, out)

            post = await bot.send_document(
                LOG_CHANNEL,
                filename,
                file_name="Batch.json",
                caption=bold("⚠️ Batch Generated For Filestore."),
            )

        finally:
            if os.path.exists(filename):
                os.remove(filename)

        encoded = encode_start(str(post.id))

        share_link = (
            f"{WEBSITE_URL.rstrip('/')}"
            f"?start=BATCH-{encoded}"
        )

        if bot_only:
            permanent_link = None   # Bot Link Only: no domain link, no shortener
            await remember_unused(f"BATCH-{encoded}", f"Batch ({len(outlist)} Files)", post.id)
        else:
            permanent_link, share_link = await get_or_create_link(
                batch_cache_key,
                share_link,
            )

        note = (f"⚠️ {_plural(kept, 'Message')} Still Come From The Source Channel") if kept else ""
        final_text = link_text(permanent_link, share_link, count=len(outlist), note=note, bot_only=bot_only)

        await post_links_to_log(
            bot, post.id, user,
            [
                f"<b>📦 Batch:</b> <code>{len(outlist)}</code> <b>files"
                + (f" ({kept} kept in source)" if kept else "") + "</b>",
                f"<b>📺 Source: {html.escape(source_title)} (</b><code>{source_id}</code><b>), "
                f"messages {first_msg_id} to {last_msg_id}</b>",
            ],
            permanent_link, share_link,
        )

        await status.edit(final_text)

    except ListenerStopped:
        # Cancelled with the button; the button already said "Cancelled."
        return

    except asyncio.TimeoutError:
        await message.reply(
            bold("⌛ Timed out waiting for a message. Run /batch again.")
        )

    except Exception as e:
        traceback.print_exc()

        error_text = html.escape(
            f"{type(e).__name__}: {e}"
        )

        try:
            if status:
                await status.edit(
                    f"<b>❌ Batch failed:</b>\n"
                    f"<code>{error_text}</code>"
                )
            else:
                await message.reply(
                    f"<b>❌ Batch command failed:</b>\n"
                    f"<code>{error_text}</code>"
                )
        except Exception:
            pass
