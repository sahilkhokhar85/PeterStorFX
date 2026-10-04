import re
import os
import json
import base64
import aiohttp

from pyrogram import filters, Client, enums
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid,
    UsernameInvalid,
    UsernameNotModified
)

from config import (
    ADMINS,
    LOG_CHANNEL,
    PUBLIC_FILE_STORE,
    WEBSITE_URL,
    WEBSITE_URL_MODE
)

from plugins.settings_db import get_settings
from plugins.admins_db import is_admin
from plugins.dbusers import db


# ============================================================
# CLOUDFLARE PERMANENT LINK WORKER
# ============================================================

PERMANENT_LINK_WORKER_URL = os.getenv("PERMANENT_LINK_WORKER_URL")
PERMANENT_LINK_ADMIN_KEY = os.getenv("PERMANENT_LINK_ADMIN_KEY")


async def create_permanent_link(destination):
    """
    Sends the original File Store URL to the Cloudflare Worker.
    Worker creates the current shortener URL internally and returns
    a permanent Worker URL.
    """

    if not PERMANENT_LINK_WORKER_URL:
        print("PERMANENT LINK ERROR: PERMANENT_LINK_WORKER_URL is missing")
        return None

    if not PERMANENT_LINK_ADMIN_KEY:
        print("PERMANENT LINK ERROR: PERMANENT_LINK_ADMIN_KEY is missing")
        return None

    api_url = f"{PERMANENT_LINK_WORKER_URL.rstrip('/')}/admin/create-link"

    payload = {
        "destination": destination
    }

    headers = {
        "Content-Type": "application/json",
        "X-Admin-Key": PERMANENT_LINK_ADMIN_KEY
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                api_url,
                json=payload,
                headers=headers
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
                    print("PERMANENT LINK ERROR: Worker returned invalid JSON")
                    return None

                if data.get("success"):
                    permanent_url = data.get("permanentUrl")

                    if permanent_url:
                        print(
                            f"PERMANENT LINK CREATED: {permanent_url}"
                        )
                        return permanent_url

                print(
                    f"PERMANENT LINK ERROR: Unexpected Worker response: {data}"
                )

    except Exception as e:
        print(
            f"PERMANENT LINK REQUEST ERROR: "
            f"{type(e).__name__}: {e}"
        )

    return None

# ============================================================
# ACCESS CONTROL
# ============================================================

async def allowed(_, __, message):
    # banned users must not be able to generate links / use /batch either
    if message.from_user and await db.is_user_banned(message.from_user.id):
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


# ============================================================
# AUTOMATIC FILE LINK GENERATION
# ============================================================

@Client.on_message(
    (filters.document | filters.video | filters.audio)
    & filters.private
    & filters.create(allowed)
)
async def incoming_gen_link(bot, message):

    post = await message.copy(LOG_CHANNEL)

    file_id = str(post.id)

    string = "file_" + file_id

    outstr = base64.urlsafe_b64encode(
        string.encode("ascii")
    ).decode().strip("=")

    share_link = f"{WEBSITE_URL.rstrip('/')}?start={outstr}"

    permanent_link = await create_permanent_link(share_link)

    if permanent_link:
        await message.reply(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"🔗 ᴘᴇʀᴍᴀɴᴇɴᴛ ʟɪɴᴋ :- {permanent_link}</b>"
        )
    else:
        await message.reply(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>"
        )


# ============================================================
# /LINK
# ============================================================

@Client.on_message(
    filters.command(["link"])
    & filters.create(allowed)
)
async def gen_link_s(bot, message):

    replied = message.reply_to_message

    if not replied:
        return await message.reply(
            "Reply to a message to get a shareable link."
        )

    post = await replied.copy(LOG_CHANNEL)

    file_id = str(post.id)

    string = "file_" + file_id

    outstr = base64.urlsafe_b64encode(
        string.encode("ascii")
    ).decode().strip("=")

    share_link = f"{WEBSITE_URL.rstrip('/')}?start={outstr}"

    permanent_link = await create_permanent_link(share_link)

    if permanent_link:
        await message.reply(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"🔗 ᴘᴇʀᴍᴀɴᴇɴᴛ ʟɪɴᴋ :- {permanent_link}</b>"
        )
    else:
        await message.reply(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>"
        )


# ============================================================
# BATCH HELPERS
# ============================================================

_BATCH_LINK_RE = re.compile(
    r"(https://)?"
    r"(t\.me/|telegram\.me/|telegram\.dog/)"
    r"(c/)?"
    r"(\d+|[a-zA-Z_0-9]+)/"
    r"(\d+)$"
)


def _extract_batch_ref(msg):
    """
    Pull (chat_id, message_id) either from a forwarded channel post
    or from a t.me link.
    """

    if msg.forward_from_chat and msg.forward_from_message_id:
        return (
            msg.forward_from_chat.id,
            msg.forward_from_message_id
        )

    text = (msg.text or "").strip()

    if text:
        match = _BATCH_LINK_RE.match(text)

        if match:
            chat_id = match.group(4)
            msg_id = int(match.group(5))

            if chat_id.isnumeric():
                chat_id = int("-100" + chat_id)

            return chat_id, msg_id

    return None


# ============================================================
# /BATCH
# ============================================================

@Client.on_message(
    filters.command(["batch"])
    & filters.create(allowed)
)
async def gen_link_batch(bot, message):

    await message.reply(
        "<b>Forward The Batch First Message From your Batch Channel "
        "(With Forward Tag).. or Give Me Batch First Message link "
        "from your batch channel</b>"
    )

    ans1 = await bot.ask(message.chat.id, "")

    if ans1.text and ans1.text.strip() == "/cancel":
        return await ans1.reply("Cancelled.")

    ref1 = _extract_batch_ref(ans1)

    if not ref1:
        return await ans1.reply(
            "<b>❌ Couldn't read that. Forward the first message "
            "(with forward tag) or send its link, then run /batch again.</b>"
        )

    f_chat_id, f_msg_id = ref1

    await message.reply(
        "<b>Forward The Batch Last Message From Your Batch Channel "
        "(With Forward Tag).. or Give Me Batch last message link "
        "from your batch channel</b>"
    )

    ans2 = await bot.ask(message.chat.id, "")

    if ans2.text and ans2.text.strip() == "/cancel":
        return await ans2.reply("Cancelled.")

    ref2 = _extract_batch_ref(ans2)

    if not ref2:
        return await ans2.reply(
            "<b>❌ Couldn't read that. Forward the last message "
            "(with forward tag) or send its link, then run /batch again.</b>"
        )

    l_chat_id, l_msg_id = ref2

    message = ans2

    if str(f_chat_id) != str(l_chat_id):
        return await message.reply("Chat ids not matched.")

    try:
        chat_id = (await bot.get_chat(f_chat_id)).id

    except ChannelInvalid:
        return await message.reply(
            "This may be a private channel / group. "
            "Make me an admin over there to index the files."
        )

    except (UsernameInvalid, UsernameNotModified):
        return await message.reply(
            "Invalid Link specified."
        )

    except Exception as e:
        return await message.reply(
            f"Errors - {e}"
        )

    sts = await message.reply(
        "**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ ғᴏʀ ʏᴏᴜʀ ᴍᴇssᴀɢᴇ**.\n"
        "**ᴛʜɪs ᴍᴀʏ ᴛᴀᴋᴇ ᴛɪᴍᴇ ᴅᴇᴘᴇɴᴅɪɴɢ ᴜᴘᴏɴ "
        "ɴᴜᴍʙᴇʀ ᴏғ ᴍᴇssᴀɢᴇs**"
    )

    FRMT = (
        "**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ...**\n"
        "**ᴛᴏᴛᴀʟ ᴍᴇssᴀɢᴇs:** {total}\n"
        "**ᴅᴏɴᴇ:** {current}\n"
        "**ʀᴇᴍᴀɪɴɪɴɢ:** {rem}\n"
        "**sᴛᴀᴛᴜs:** {sts}"
    )

    outlist = []

    # File store without DB channel
    og_msg = 0
    tot = 0

    async for msg in bot.iter_messages(
        f_chat_id,
        l_msg_id,
        f_msg_id
    ):
        tot += 1

        if og_msg % 20 == 0:
            try:
                await sts.edit(
                    FRMT.format(
                        total=l_msg_id - f_msg_id,
                        current=tot,
                        rem=((l_msg_id - f_msg_id) - tot),
                        sts="Saving Messages"
                    )
                )
            except:
                pass

        if msg.empty or msg.service:
            continue

        file = {
            "channel_id": f_chat_id,
            "msg_id": msg.id
        }

        og_msg += 1
        outlist.append(file)

    with open(
        f"batchmode_{message.from_user.id}.json",
        "w+"
    ) as out:
        json.dump(outlist, out)

    post = await bot.send_document(
        LOG_CHANNEL,
        f"batchmode_{message.from_user.id}.json",
        file_name="Batch.json",
        caption="⚠️ Batch Generated For Filestore."
    )

    os.remove(
        f"batchmode_{message.from_user.id}.json"
    )

    string = str(post.id)

    file_id = base64.urlsafe_b64encode(
        string.encode("ascii")
    ).decode().strip("=")

    share_link = (
        f"{WEBSITE_URL.rstrip('/')}"
        f"?start=BATCH-{file_id}"
    )

    permanent_link = await create_permanent_link(
        share_link
    )

    if permanent_link:
        await sts.edit(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"Contains `{og_msg}` files.\n\n"
            f"🔗 ᴘᴇʀᴍᴀɴᴇɴᴛ ʟɪɴᴋ :- {permanent_link}</b>"
        )
    else:
        await sts.edit(
            f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n"
            f"Contains `{og_msg}` files.\n\n"
            f"🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>"
        )
