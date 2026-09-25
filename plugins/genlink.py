import re
from pyrogram import filters, Client, enums
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, UsernameInvalid, UsernameNotModified
from config import ADMINS, LOG_CHANNEL, PUBLIC_FILE_STORE, WEBSITE_URL, WEBSITE_URL_MODE
from plugins.users_api import get_user, get_short_link
from plugins.settings_db import get_settings
from plugins.admins_db import is_admin
from plugins.dbusers import db
import re
import os
import json
import base64


async def allowed(_, __, message):
    # banned users must not be able to generate links / use /batch either
    if message.from_user and await db.is_user_banned(message.from_user.id):
        return False
    settings = await get_settings()
    public_mode = settings.get("public_mode")
    if public_mode is None:
        public_mode = PUBLIC_FILE_STORE  # fall back to env default if never set
    if public_mode:
        return True
    if message.from_user and (message.from_user.id in ADMINS or await is_admin(message.from_user.id)):
        return True
    return False


@Client.on_message((filters.document | filters.video | filters.audio) & filters.private & filters.create(allowed))
async def incoming_gen_link(bot, message):
    username = (await bot.get_me()).username
    file_type = message.media
    post = await message.copy(LOG_CHANNEL)
    file_id = str(post.id)
    string = 'file_'
    string += file_id
    outstr = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
    user_id = message.from_user.id
    user = await get_user(user_id)
    share_link = f"{WEBSITE_URL.rstrip('/')}?start={outstr}"
    if user["base_site"] and user["shortener_api"] != None:
        short_link = await get_short_link(user, share_link)
        await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🖇️ sʜᴏʀᴛ ʟɪɴᴋ :- {short_link}</b>")
    else:
        await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>")
        

@Client.on_message(filters.command(['link']) & filters.create(allowed))
async def gen_link_s(bot, message):
    username = (await bot.get_me()).username
    replied = message.reply_to_message
    if not replied:
        return await message.reply('Reply to a message to get a shareable link.')
    
    post = await replied.copy(LOG_CHANNEL)
    file_id = str(post.id)
    string = f"file_"
    string += file_id
    outstr = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
    user_id = message.from_user.id
    user = await get_user(user_id)
    share_link = f"{WEBSITE_URL.rstrip('/')}?start={outstr}"
    if user["base_site"] and user["shortener_api"] != None:
        short_link = await get_short_link(user, share_link)
        await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🖇️ sʜᴏʀᴛ ʟɪɴᴋ :- {short_link}</b>")
    else:
        await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>")
        


_BATCH_LINK_RE = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")


def _extract_batch_ref(msg):
    """Pull (chat_id, message_id) either from a forwarded channel post (needs
    the 'forward tag' i.e. forwarded normally, not 'forwarded without
    attribution') or from a t.me link the user typed/pasted."""
    if msg.forward_from_chat and msg.forward_from_message_id:
        return msg.forward_from_chat.id, msg.forward_from_message_id
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


@Client.on_message(filters.command(['batch']) & filters.create(allowed))
async def gen_link_batch(bot, message):
    username = (await bot.get_me()).username

    await message.reply(
        "<b>Forward The Batch First Message From your Batch Channel (With Forward Tag).. "
        "or Give Me Batch First Message link from your batch channel</b>"
    )
    ans1 = await bot.ask(message.chat.id, "")
    if ans1.text and ans1.text.strip() == "/cancel":
        return await ans1.reply("Cancelled.")
    ref1 = _extract_batch_ref(ans1)
    if not ref1:
        return await ans1.reply(
            "<b>❌ Couldn't read that. Forward the first message (with forward tag) "
            "or send its link, then run /batch again.</b>"
        )
    f_chat_id, f_msg_id = ref1

    await message.reply(
        "<b>Forward The Batch Last Message From Your Batch Channel (With Forward Tag).. "
        "or Give Me Batch last message link from your batch channel</b>"
    )
    ans2 = await bot.ask(message.chat.id, "")
    if ans2.text and ans2.text.strip() == "/cancel":
        return await ans2.reply("Cancelled.")
    ref2 = _extract_batch_ref(ans2)
    if not ref2:
        return await ans2.reply(
            "<b>❌ Couldn't read that. Forward the last message (with forward tag) "
            "or send its link, then run /batch again.</b>"
        )
    l_chat_id, l_msg_id = ref2
    message = ans2  # so status/reply messages below land after the last input

    if str(f_chat_id) != str(l_chat_id):
        return await message.reply("Chat ids not matched.")
    try:
        chat_id = (await bot.get_chat(f_chat_id)).id
    except ChannelInvalid:
        return await message.reply('This may be a private channel / group. Make me an admin over there to index the files.')
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply('Invalid Link specified.')
    except Exception as e:
        return await message.reply(f'Errors - {e}')

    
    sts = await message.reply("**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ ғᴏʀ ʏᴏᴜʀ ᴍᴇssᴀɢᴇ**.\n**ᴛʜɪs ᴍᴀʏ ᴛᴀᴋᴇ ᴛɪᴍᴇ ᴅᴇᴘᴇɴᴅɪɴɢ ᴜᴘᴏɴ ɴᴜᴍʙᴇʀ ᴏғ ᴍᴇssᴀɢᴇs**")

    FRMT = "**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ...**\n**ᴛᴏᴛᴀʟ ᴍᴇssᴀɢᴇs:** {total}\n**ᴅᴏɴᴇ:** {current}\n**ʀᴇᴍᴀɪɴɪɴɢ:** {rem}\n**sᴛᴀᴛᴜs:** {sts}"

    outlist = []


    # file store without db channel
    og_msg = 0
    tot = 0
    async for msg in bot.iter_messages(f_chat_id, l_msg_id, f_msg_id):
        tot += 1
        if og_msg % 20 == 0:
            try:
                await sts.edit(FRMT.format(total=l_msg_id-f_msg_id, current=tot, rem=((l_msg_id-f_msg_id) - tot), sts="Saving Messages"))
            except:
                pass
        if msg.empty or msg.service:
            continue
        file = {
            "channel_id": f_chat_id,
            "msg_id": msg.id
        }
        og_msg +=1
        outlist.append(file)


    with open(f"batchmode_{message.from_user.id}.json", "w+") as out:
        json.dump(outlist, out)
    post = await bot.send_document(LOG_CHANNEL, f"batchmode_{message.from_user.id}.json", file_name="Batch.json", caption="⚠️ Batch Generated For Filestore.")
    os.remove(f"batchmode_{message.from_user.id}.json")
    string = str(post.id)
    file_id = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
    user_id = message.from_user.id
    user = await get_user(user_id)
    share_link = f"{WEBSITE_URL.rstrip('/')}?start=BATCH-{file_id}"
    if user["base_site"] and user["shortener_api"] != None:
        short_link = await get_short_link(user, share_link)
        await sts.edit(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\nContains `{og_msg}` files.\n\n🖇️ sʜᴏʀᴛ ʟɪɴᴋ :- {short_link}</b>")
    else:
        await sts.edit(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\nContains `{og_msg}` files.\n\n🔗 ᴏʀɪɢɪɴᴀʟ ʟɪɴᴋ :- {share_link}</b>")
