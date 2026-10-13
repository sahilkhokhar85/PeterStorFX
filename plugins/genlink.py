
import re
import os
import json
import base64
import aiohttp
import asyncio
import html
import traceback
from datetime import datetime, timezone

from pyrogram import filters, Client, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait
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
from TechVJ.utils.cover_copy import copy_keep_cover


# ============================================================
# CLOUDFLARE PERMANENT LINK WORKER
# ============================================================

PERMANENT_LINK_WORKER_URL = os.getenv("PERMANENT_LINK_WORKER_URL")
PERMANENT_LINK_ADMIN_KEY = os.getenv("PERMANENT_LINK_ADMIN_KEY")

links_col = db.db["permanent_links"]

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


def reply_link_text(permanent_link, share_link):
    if permanent_link:
        return (
            "<b>⭕ Here is your link:\n\n"
            f"🔗 Permanent link: "
            f"{html.escape(permanent_link)}</b>"
        )

    return (
        "<b>⭕ Here is your link:\n\n"
        f"🔗 Original link: "
        f"{html.escape(share_link)}</b>"
    )


async def build_link_reply(source_message):
    """Create (or reuse) the link for one media message and return the reply
    text. This is the only place that copies a file into LOG_CHANNEL."""
    cache_key = get_message_cache_key(source_message)
    saved = await get_saved_link(cache_key)

    if saved and saved.get("permanent_url"):
        return reply_link_text(
            saved["permanent_url"],
            saved.get("share_link", ""),
        )

    if saved and saved.get("share_link"):
        share_link = saved["share_link"]
    else:
        post = await copy_keep_cover(source_message, LOG_CHANNEL)
        share_link = make_file_share_link(post.id)

    permanent_link, share_link = await get_or_create_link(
        cache_key,
        share_link,
    )

    return reply_link_text(permanent_link, share_link)


# ============================================================
# FILE SENT TO THE BOT: ASK FIRST, THEN GENERATE
# ============================================================
# A file sent to the bot is NOT copied into LOG_CHANNEL automatically any
# more. The bot asks first, so files sent for other reasons (for example a
# .txt id list for /ban) never create a link or a log copy.

@Client.on_message(
    (filters.document | filters.video | filters.audio)
    & filters.private
    & filters.create(allowed)
)
async def incoming_gen_link(bot, message):
    # A file sent together with a command caption (e.g. "/ban" + id list)
    # is meant for that command, so never ask about it.
    caption = (message.caption or "").lstrip()
    if caption.startswith("/"):
        return

    await message.reply(
        bold("Generate A Link For This File?"),
        quote=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Generate Link",
                callback_data=f"gl_yes:{message.id}",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"gl_no:{message.id}",
            ),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^gl_(yes|no):\d+$"))
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
        text = await build_link_reply(source)
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

    await message.reply(await build_link_reply(replied))


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
    await bot.send_message(chat_id, bold(prompt))

    # Keep the original empty prompt: it does not add a third
    # user-facing instruction message.
    answer = await bot.ask(chat_id, "", timeout=300)

    answer_text = (answer.text or "").strip()

    if answer_text.split(maxsplit=1)[:1] == ["/cancel"]:
        await answer.reply(bold("Cancelled."))
        return None

    ref = _extract_batch_ref(answer)

    if not ref:
        await answer.reply(
            bold(
                "❌ Couldn't read that. Forward the message "
                "with its forward tag or send its Telegram link."
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
    return outlist


# ============================================================
# /BATCH
# ============================================================

@Client.on_message(
    filters.command(["batch"]) & filters.create(allowed)
)
async def gen_link_batch(bot, message):
    status = None

    try:
        first_ref = await ask_batch_reference(
            bot,
            message.chat.id,
            "Forward The Batch First Message From your Batch Channel "
            "(With Forward Tag).. or Give Me Batch First Message link "
            "from your batch channel",
        )

        if first_ref is None:
            return

        first_chat_id, first_msg_id = first_ref

        last_ref = await ask_batch_reference(
            bot,
            message.chat.id,
            "Forward The Batch Last Message From Your Batch Channel "
            "(With Forward Tag).. or Give Me Batch last message link "
            "from your batch channel",
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

        status = await message.reply(
            bold(
                "Generating link for your messages.\n"
                "This may take time depending on the number of messages."
            )
        )

        outlist = await fetch_batch_messages(
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

        filename = f"batchmode_{message.from_user.id}.json"

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

        permanent_link, share_link = await get_or_create_link(
            batch_cache_key,
            share_link,
        )

        if permanent_link:
            final_text = (
                "<b>⭕ Here is your link:\n\n"
                f"Contains {len(outlist)} messages.\n\n"
                f"🔗 Permanent link: "
                f"{html.escape(permanent_link)}</b>"
            )
        else:
            final_text = (
                "<b>⭕ Here is your link:\n\n"
                f"Contains {len(outlist)} messages.\n\n"
                f"🔗 Original link: "
                f"{html.escape(share_link)}</b>"
            )

        await status.edit(final_text)

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
