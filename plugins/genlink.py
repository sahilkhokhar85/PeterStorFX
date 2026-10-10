
import re
import os
import json
import base64
import aiohttp
import asyncio
import html
import traceback
from datetime import datetime, timezone

from pyrogram import filters, Client
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid,
    UsernameInvalid,
    UsernameNotModified,
)

from config import (
    ADMINS,
    LOG_CHANNEL,
    PUBLIC_FILE_STORE,
    WEBSITE_URL,
)

from plugins.settings_db import get_settings
from plugins.admins_db import is_admin
from plugins.dbusers import db


PERMANENT_LINK_WORKER_URL = os.getenv("PERMANENT_LINK_WORKER_URL")
PERMANENT_LINK_ADMIN_KEY = os.getenv("PERMANENT_LINK_ADMIN_KEY")

links_col = db.db["permanent_links"]


def get_media_unique_id(message):
    for media_type in ("document", "video", "audio"):
        media = getattr(message, media_type, None)
        if media and media.file_unique_id:
            return media.file_unique_id
    return None


def get_message_cache_key(message):
    unique_id = get_media_unique_id(message)
    if unique_id:
        return f"media:{unique_id}"
    return f"message:{message.chat.id}:{message.id}"


def encode_start(value):
    return base64.urlsafe_b64encode(
        value.encode("ascii")
    ).decode().rstrip("=")


async def get_saved_link(cache_key):
    return await links_col.find_one({"_id": cache_key})


async def save_link(cache_key, share_link, permanent_link=None):
    now = datetime.now(timezone.utc)
    update = {
        "$set": {
            "share_link": share_link,
            "updated_at": now,
        },
        "$setOnInsert": {"created_at": now},
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
        print(f"LINK CACHE SAVE ERROR: {type(e).__name__}: {e}")

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

                data = json.loads(response_text)
                if data.get("success"):
                    return data.get("permanentUrl")

                print(f"Worker error: {data}")

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


async def allowed(_, __, message):
    if (
        message.from_user
        and await db.is_user_banned(message.from_user.id)
    ):
        return False

    settings = await get_settings()
    public_mode = settings.get("public_mode")

    if public_mode is None:
        public_mode = PUBLIC_FILE_STORE

    if public_mode:
        return True

    if message.from_user and (
        message.from_user.id in ADMINS
        or await is_admin(message.from_user.id)
    ):
        return True

    return False


def make_file_share_link(log_message_id):
    encoded = encode_start(f"file_{log_message_id}")
    return f"{WEBSITE_URL.rstrip('/')}?start={encoded}"


def reply_link_text(permanent_link, share_link):
    if permanent_link:
        return (
            "<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"🔗 ᴘᴇʀᴍᴀɴᴇɴᴛ ʟɪɴᴋ :- {permanent_link}</b>"
        )

    return (
        "<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
        f"🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>"
    )


@Client.on_message(
    (filters.document | filters.video | filters.audio)
    & filters.private
    & filters.create(allowed)
)
async def incoming_gen_link(bot, message):
    cache_key = get_message_cache_key(message)
    saved = await get_saved_link(cache_key)

    if saved and saved.get("permanent_url"):
        return await message.reply(
            reply_link_text(
                saved["permanent_url"],
                saved.get("share_link", ""),
            )
        )

    if saved and saved.get("share_link"):
        share_link = saved["share_link"]
    else:
        post = await message.copy(LOG_CHANNEL)
        share_link = make_file_share_link(post.id)

    permanent_link, share_link = await get_or_create_link(
        cache_key, share_link
    )
    await message.reply(reply_link_text(permanent_link, share_link))


@Client.on_message(
    filters.command(["link"]) & filters.create(allowed)
)
async def gen_link_s(bot, message):
    replied = message.reply_to_message

    if not replied:
        return await message.reply(
            "Reply to a message to get a shareable link."
        )

    cache_key = get_message_cache_key(replied)
    saved = await get_saved_link(cache_key)

    if saved and saved.get("permanent_url"):
        return await message.reply(
            reply_link_text(
                saved["permanent_url"],
                saved.get("share_link", ""),
            )
        )

    if saved and saved.get("share_link"):
        share_link = saved["share_link"]
    else:
        post = await replied.copy(LOG_CHANNEL)
        share_link = make_file_share_link(post.id)

    permanent_link, share_link = await get_or_create_link(
        cache_key, share_link
    )
    await message.reply(reply_link_text(permanent_link, share_link))


_BATCH_LINK_RE = re.compile(
    r"^(?:https?://)?"
    r"(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?:c/)?"
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

    chat_name = match.group(1)
    msg_id = int(match.group(2))

    if chat_name.isdigit():
        # Telegram private-channel links use /c/<internal-id>/<msg-id>.
        if "/c/" in text:
            chat_id = int(f"-100{chat_name}")
        else:
            return None
    else:
        chat_id = chat_name

    return chat_id, msg_id


@Client.on_message(
    filters.command(["batch"]) & filters.create(allowed)
)
async def gen_link_batch(bot, message):
    try:
        await message.reply(
            "<b>Forward the FIRST message of your batch with its "
            "forward tag, or send its message link. Send /cancel to stop.</b>"
        )

        ans1 = await bot.ask(message.chat.id, "", timeout=300)

        if (ans1.text or "").strip().split()[0:1] == ["/cancel"]:
            return await ans1.reply("Cancelled.")

        ref1 = _extract_batch_ref(ans1)
        if not ref1:
            return await ans1.reply(
                "❌ Couldn't read the first message. "
                "Forward it with the forward tag or send its link."
            )

        f_chat_id, f_msg_id = ref1

        await message.reply(
            "<b>Now forward the LAST message of the batch with its "
            "forward tag, or send its message link. Send /cancel to stop.</b>"
        )

        ans2 = await bot.ask(message.chat.id, "", timeout=300)

        if (ans2.text or "").strip().split()[0:1] == ["/cancel"]:
            return await ans2.reply("Cancelled.")

        ref2 = _extract_batch_ref(ans2)
        if not ref2:
            return await ans2.reply(
                "❌ Couldn't read the last message. "
                "Forward it with the forward tag or send its link."
            )

        l_chat_id, l_msg_id = ref2

        if str(f_chat_id) != str(l_chat_id):
            return await ans2.reply("❌ Chat IDs do not match.")

        if f_msg_id > l_msg_id:
            return await ans2.reply(
                "❌ First message ID is greater than the last message ID."
            )

        batch_cache_key = f"batch:{f_chat_id}:{f_msg_id}:{l_msg_id}"
        saved = await get_saved_link(batch_cache_key)

        if saved and saved.get("permanent_url"):
            return await ans2.reply(
                reply_link_text(
                    saved["permanent_url"],
                    saved.get("share_link", ""),
                )
            )

        try:
            await bot.get_chat(f_chat_id)
        except ChannelInvalid:
            return await ans2.reply(
                "❌ Private channel access error. "
                "Make sure the bot can access that channel."
            )
        except (UsernameInvalid, UsernameNotModified):
            return await ans2.reply("❌ Invalid channel.")
        except Exception as e:
            return await ans2.reply(
                f"❌ Channel access failed: {type(e).__name__}: {e}"
            )

        sts = await ans2.reply("⏳ Starting batch generation…")

        total = l_msg_id - f_msg_id + 1
        outlist = []
        valid_count = 0

        try:
            print(
                f"BATCH START: chat={f_chat_id}, "
                f"first={f_msg_id}, last={l_msg_id}, total={total}"
            )

            # Fetch the requested IDs directly. This avoids reverse=True
            # pagination ambiguity and doesn't depend on contiguous messages.
            for msg_id in range(f_msg_id, l_msg_id + 1):
                try:
                    msg = await bot.get_messages(f_chat_id, msg_id)
                except Exception as e:
                    print(
                        f"BATCH MESSAGE ERROR id={msg_id}: "
                        f"{type(e).__name__}: {e}"
                    )
                    continue

                if not msg or getattr(msg, "empty", False):
                    continue

                if getattr(msg, "service", None):
                    continue

                outlist.append({
                    "channel_id": f_chat_id,
                    "msg_id": msg.id,
                })
                valid_count += 1

                if valid_count % 10 == 0:
                    try:
                        await sts.edit(
                            f"⏳ Generating batch…\n"
                            f"Range: {total} messages\n"
                            f"Collected: {valid_count}"
                        )
                    except Exception:
                        pass

            print(f"BATCH COLLECTED: {valid_count}")

            if not outlist:
                return await sts.edit(
                    "❌ No messages found in that range. "
                    "Check the message IDs and bot's channel access."
                )

            filename = f"batchmode_{message.from_user.id}.json"

            try:
                with open(filename, "w", encoding="utf-8") as out:
                    json.dump(outlist, out)

                post = await bot.send_document(
                    LOG_CHANNEL,
                    filename,
                    file_name="Batch.json",
                    caption="⚠️ Batch Generated For Filestore.",
                )
            finally:
                if os.path.exists(filename):
                    os.remove(filename)

            encoded = encode_start(str(post.id))
            share_link = (
                f"{WEBSITE_URL.rstrip('/')}?start=BATCH-{encoded}"
            )

            permanent_link, share_link = await get_or_create_link(
                batch_cache_key, share_link
            )

            await sts.edit(
                "<b>⭕ Here is your link:\n\n"
                f"Contains <code>{valid_count}</code> messages.\n\n"
                + (
                    f"🔗 Permanent link: {html.escape(permanent_link)}</b>"
                    if permanent_link
                    else f"🔗 Original link: {html.escape(share_link)}</b>"
                )
            )

        except Exception as e:
            traceback.print_exc()
            try:
                await sts.edit(
                    f"❌ Batch failed: "
                    f"<code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>"
                )
            except Exception:
                pass

    except asyncio.TimeoutError:
        await message.reply(
            "⌛ Timed out waiting for the message. Run /batch again."
        )
    except Exception as e:
        traceback.print_exc()
        try:
            await message.reply(
                f"❌ Batch command failed: "
                f"<code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>"
            )
        except Exception:
            pass
