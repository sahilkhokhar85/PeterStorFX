import os
import logging
import random
import asyncio
from validators import domain
from Script import script
from plugins.dbusers import db
from pyrogram import Client, filters, enums
from plugins.users_api import get_user, update_user_info
from pyrogram.errors import ChatAdminRequired, FloodWait
from pyrogram.types import *
from utils import verify_user, check_token, check_verification, get_token
from plugins.settings_db import get_settings
from plugins.force_sub import not_joined_channels, force_sub_join_buttons, get_missing_and_buttons
from TechVJ.server.streamer import build_stream_urls
from config import *
import re
import json
import base64
logger = logging.getLogger(__name__)

BATCH_FILES = {}


def get_size(size):
    """Get size in readable format"""

    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units):
        i += 1
        size /= 1024.0
    return "%.2f %s" % (size, units[i])

def formate_file_name(file_name):
    if not file_name:
        return file_name
    chars = ["[", "]", "(", ")"]
    for c in chars:
        file_name = file_name.replace(c, "")  # was discarding the result before - a no-op bug
    file_name = ' '.join(filter(lambda x: not x.startswith('http') and not x.startswith('@') and not x.startswith('www.'), file_name.split()))
    return file_name


def stream_download_buttons(chat_id, message_id, media):
    """Fast Download / Watch Online buttons only for supported file types."""
    if not STREAM_MODE:
        return []

    try:
        # Stream buttons only for actual downloadable/streamable files
        if not hasattr(media, "file_name") or not media.file_name:
            return []

        download_url, watch_url = build_stream_urls(
            chat_id,
            message_id,
            getattr(media, "file_unique_id", ""),
            media.file_name,
        )

        return [[
            InlineKeyboardButton(
                '🚀 ꜰᴀsᴛ ᴅᴏᴡɴʟᴏᴀᴅ',
                url=download_url
            ),
            InlineKeyboardButton(
                '🖥 ᴡᴀᴛᴄʜ ᴏɴʟɪɴᴇ',
                url=watch_url
            ),
        ]]

    except Exception:
        logger.error(
            "Failed to build stream/download buttons",
            exc_info=True
        )
        return []


async def _forward_accessed_to_log(client, refs):
    """Forward the actual files/videos a user just accessed (batch) into
    LOG_CHANNEL, grouped and chunked, so admins can see/open the real files
    instead of only reading their names. Runs as a background task."""
    by_channel = {}
    for ch, mid in refs:
        by_channel.setdefault(ch, []).append(mid)
    for ch, ids in by_channel.items():
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                await client.forward_messages(LOG_CHANNEL, ch, chunk)
            except FloodWait as e:
                await asyncio.sleep(e.value)
                try:
                    await client.forward_messages(LOG_CHANNEL, ch, chunk)
                except Exception:
                    pass
            except Exception:
                pass
            await asyncio.sleep(0.5)


async def _forward_single_to_log(client, msg):
    """Forward the single file/video a user just accessed into LOG_CHANNEL
    so admins can see the real file, not just its name. Background task."""
    try:
        await client.forward_messages(LOG_CHANNEL, msg.chat.id, msg.id)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await client.forward_messages(LOG_CHANNEL, msg.chat.id, msg.id)
        except Exception:
            pass
    except Exception:
        pass


# Keep references to background tasks so they can't be garbage-collected mid-run.
_BG_TASKS = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


async def _delete_batch_later(delay, filesarr, notice):
    """Auto-delete a delivered batch after `delay` seconds. Runs as a background
    task so the /start handler (and its Pyrogram worker) is freed immediately
    instead of sleeping for the whole auto-delete time."""
    await asyncio.sleep(delay)
    await asyncio.gather(*[x.delete() for x in filesarr], return_exceptions=True)
    try:
        await notice.edit_text("<b>Your All Files/Videos is successfully deleted!!!</b>")
    except Exception:
        pass


async def _delete_single_later(delay, del_msg, notice):
    """Same as above, for a single delivered file."""
    await asyncio.sleep(delay)
    try:
        await del_msg.delete()
    except Exception:
        pass
    try:
        await notice.edit_text("<b>Your File/Video is successfully deleted!!!</b>")
    except Exception:
        pass


@Client.on_message(filters.command("start") & filters.incoming)
async def start(client, message):
    username = client.me.username
    user_doc = await db.get_user(message.from_user.id)
    if not user_doc:
        await db.add_user(message.from_user.id, message.from_user.first_name)
        await client.send_message(LOG_CHANNEL, script.LOG_TEXT.format(message.from_user.id, message.from_user.mention))
    elif user_doc.get('banned', False):
        return await message.reply_text("<b>🚫 You are banned from using this bot.</b>")

    settings = await get_settings()
    missing_channels, fsub_buttons = await get_missing_and_buttons(client, message.from_user.id, settings)
    if missing_channels:
        buttons = fsub_buttons
        param = message.command[1] if len(message.command) == 2 else "-"
        buttons.append([InlineKeyboardButton("🔄 ᴛʀʏ ᴀɢᴀɪɴ", callback_data=f"fsub_verify:{param}")])
        fsub_text = settings.get("force_sub_message") or "<b>Please join our channel(s) to use this bot.</b>"
        fsub_photo = settings.get("force_sub_photo")
        if fsub_photo:
            try:
                return await message.reply_photo(
                    fsub_photo,
                    caption=fsub_text,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            except Exception:
                pass  # bad/expired file_id -> fall back to text-only below
        return await message.reply_text(
            fsub_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )

    if len(message.command) != 2:
        buttons = [[
            InlineKeyboardButton('👨‍💻 ᴍᴀɪɴᴛᴀɪɴᴇʀ', url='https://t.me/RudraBlaze'),
            InlineKeyboardButton('📢 ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ', url='https://t.me/ChillFlizX')
            ],[
            InlineKeyboardButton('💁‍♂️ ʜᴇʟᴘ', callback_data='help'),
            InlineKeyboardButton('💎 ᴀʙᴏᴜᴛ', callback_data='about')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        me = client.me
        start_caption = settings.get("start_message") or script.START_TXT
        try:
            start_caption = start_caption.format(message.from_user.mention, me.mention)
        except (IndexError, KeyError):
            pass
        start_photo = settings.get("start_photo") or random.choice(PICS)
        try:
            await message.reply_photo(
                photo=start_photo,
                caption=start_caption,
                reply_markup=reply_markup
            )
        except Exception:
            # bad/expired custom file_id -> fall back to a default random pic
            await message.reply_photo(
                photo=random.choice(PICS),
                caption=start_caption,
                reply_markup=reply_markup
            )
        return

    
    data = message.command[1]
    try:
        pre, file_id = data.split('_', 1)
    except:
        file_id = data
        pre = ""
    if data.split("-", 1)[0] == "verify":
        userid = data.split("-", 2)[1]
        token = data.split("-", 3)[2]
        if str(message.from_user.id) != str(userid):
            return await message.reply_text(
                text="<b>Invalid link or Expired link !</b>",
                protect_content=True
            )
        is_valid = await check_token(client, userid, token)
        if is_valid == True:
            await message.reply_text(
                text=f"<b>Hey {message.from_user.mention}, You are successfully verified !\nNow you have unlimited access for all files till today midnight.</b>",
                protect_content=True
            )
            await verify_user(client, userid, token)
            return
        else:
            return await message.reply_text(
                text="<b>Invalid link or Expired link !</b>",
                protect_content=True
            )
    elif data.split("-", 1)[0] == "BATCH":
        try:
            if not await check_verification(client, message.from_user.id) and VERIFY_MODE == True:
                btn = [[
                    InlineKeyboardButton("Verify", url=await get_token(client, message.from_user.id, f"https://telegram.me/{username}?start="))
                ],[
                    InlineKeyboardButton("How To Open Link & Verify", url=VERIFY_TUTORIAL)
                ]]
                await message.reply_text(
                    text="<b>You are not verified !\nKindly verify to continue !</b>",
                    protect_content=True,
                    reply_markup=InlineKeyboardMarkup(btn)
                )
                return
        except Exception as e:
            return await message.reply_text(f"**Error - {e}**")
        sts = await message.reply("**🔺 ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ**")
        file_id = data.split("-", 1)[1]
        msgs = BATCH_FILES.get(file_id)
        if not msgs:
            decode_file_id = base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4)).decode("ascii")
            msg = await client.get_messages(LOG_CHANNEL, int(decode_file_id))
            media = getattr(msg, msg.media.value)
            file = await client.download_media(media.file_id)  # keep file_id intact - it is the BATCH_FILES cache key
            try: 
                with open(file) as file_data:
                    msgs=json.loads(file_data.read())
            except:
                try:
                    os.remove(file)
                except Exception:
                    pass
                await sts.edit("FAILED")
                return await client.send_message(LOG_CHANNEL, "UNABLE TO OPEN FILE.")
            os.remove(file)
            BATCH_FILES[file_id] = msgs
            
        filesarr = []
        titles = []
        accessed_refs = []  # (channel_id, msg_id) pairs actually delivered - used to
                             # forward the real files/videos into the access log below.

        # Fetch every source message in bulk (chunks of 200) instead of one
        # get_messages() call per file - this is the single biggest speedup
        # for large batches since each call previously cost a network round trip.
        by_channel = {}
        order = []
        for m in msgs:
            ch = int(m.get("channel_id"))
            mid = m.get("msg_id")
            by_channel.setdefault(ch, []).append(mid)
            order.append((ch, mid))

        fetched = {}
        for ch, ids in by_channel.items():
            for i in range(0, len(ids), 200):
                chunk = ids[i:i + 200]
                try:
                    results = await client.get_messages(ch, chunk)
                except Exception:
                    results = []
                for r in results or []:
                    if r:
                        fetched[(ch, r.id)] = r

        for channel_id, msgid in order:
            info = fetched.get((channel_id, msgid))
            if not info:
                continue
            if info.media:
                file_type = info.media
                file = getattr(info, file_type.value)
                f_caption = getattr(info, 'caption', '')
                if f_caption:
                    f_caption = f_caption.html
                old_title = getattr(file, "file_name", "")
                title = formate_file_name(old_title)
                size=get_size(int(file.file_size))
                batch_caption = settings.get("custom_caption") or BATCH_FILE_CAPTION
                if batch_caption:
                    try:
                        f_caption=batch_caption.format(file_name= '' if title is None else title, file_size='' if size is None else size, file_caption='' if f_caption is None else f_caption)
                    except:
                        f_caption=f_caption
                if f_caption is None:
                    f_caption = f"{title}"

                button = []
                
                for row in (settings.get("custom_buttons") or []):
                    button.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row])
                button.extend(stream_download_buttons(channel_id, msgid, file))
                reply_markup = InlineKeyboardMarkup(button)
                protect = settings.get("protect_content", False)
                try:
                    msg = await info.copy(chat_id=message.from_user.id, caption=f_caption, protect_content=protect, reply_markup=reply_markup)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    msg = await info.copy(chat_id=message.from_user.id, caption=f_caption, protect_content=protect, reply_markup=reply_markup)
                except:
                    continue
            else:
                title = "Unknown File"
                protect = settings.get("protect_content", False)

                button = []
                
                for row in (settings.get("custom_buttons") or []):
                    button.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row])
                reply_markup = InlineKeyboardMarkup(button)
                try:
                    msg = await info.copy(chat_id=message.from_user.id, protect_content=protect, reply_markup=reply_markup)
                except FloodWait as e:
                    await asyncio.sleep(e.value)
                    msg = await info.copy(chat_id=message.from_user.id, protect_content=protect, reply_markup=reply_markup)
                except:
                    continue
            filesarr.append(msg)
            titles.append(title)
            accessed_refs.append((channel_id, msgid))
            await asyncio.sleep(0.6)
        try:
            if titles:
                shown = titles[:30]
                files_list = "\n".join(f"{i+1}. <code>{t}</code>" for i, t in enumerate(shown))
                if len(titles) > 30:
                    files_list += f"\n... and {len(titles) - 30} more"
            else:
                files_list = "N/A"
            await client.send_message(
                LOG_CHANNEL,
                f"<b>📥 #FileAccess (Batch)</b>\n\n"
                f"👤 User: {message.from_user.mention} (<code>{message.from_user.id}</code>)\n"
                f"📦 Files Accessed: <code>{len(filesarr)}</code>\n\n"
                f"{files_list}\n\n"
                f"⬇️ ᴀᴄᴛᴜᴀʟ ꜰɪʟᴇꜱ ʙᴇʟᴏᴡ"
            )
            # Also forward the real accessed files/videos into the log channel
            # (not just their names) - runs in the background so it never
            # slows down the reply the user is waiting for.
            asyncio.create_task(_forward_accessed_to_log(client, accessed_refs))
        except:
            pass
        await sts.delete()
        if settings.get("auto_delete", True):
            del_minutes = max(1, settings.get("auto_delete_time", 1800) // 60)
            k = await client.send_message(chat_id = message.from_user.id, text=f"<b><u>❗️❗️❗️IMPORTANT❗️️❗️❗️</u></b>\n\nThis Movie File/Video will be deleted in <b><u>{del_minutes} minutes</u> 🫥 <i></b>(Due to Copyright Issues)</i>.\n\n<b><i>Please forward this File/Video to your Saved Messages and Start Download there</b>")
            _spawn(_delete_batch_later(settings.get("auto_delete_time", 1800), filesarr, k))
        return


    pre, decode_file_id = ((base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))).decode("ascii")).split("_", 1)
    if not await check_verification(client, message.from_user.id) and VERIFY_MODE == True:
        btn = [[
            InlineKeyboardButton("Verify", url=await get_token(client, message.from_user.id, f"https://telegram.me/{username}?start="))
        ],[
            InlineKeyboardButton("How To Open Link & Verify", url=VERIFY_TUTORIAL)
        ]]
        await message.reply_text(
            text="<b>You are not verified !\nKindly verify to continue !</b>",
            protect_content=True,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        return
    try:
        msg = await client.get_messages(LOG_CHANNEL, int(decode_file_id))
        real_file_types = ("document", "video", "audio", "photo", "animation", "voice", "video_note")
        if msg.media and msg.media.value in real_file_types:
            media = getattr(msg, msg.media.value)
            orig_caption = getattr(msg, 'caption', None)
            orig_caption = orig_caption.html if orig_caption else None
            title = orig_caption or formate_file_name(getattr(media, "file_name", ""))
            size=get_size(media.file_size)
            f_caption = f"<code>{title}</code>"
            single_caption = settings.get("custom_caption") or CUSTOM_FILE_CAPTION
            if single_caption:
                try:
                    f_caption=single_caption.format(file_name= '' if title is None else title, file_size='' if size is None else size, file_caption='' if title is None else title)
                except:
                    f_caption = f"<code>{title}</code>"

            button = []
            
            for row in (settings.get("custom_buttons") or []):
                button.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row])
            button.extend(stream_download_buttons(msg.chat.id, msg.id, media))
            reply_markup = InlineKeyboardMarkup(button)
            del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption, reply_markup=reply_markup, protect_content=settings.get("protect_content", False))
        else:
            title = "Unknown File"

            button = []
            
            for row in (settings.get("custom_buttons") or []):
                button.append([InlineKeyboardButton(b["text"], url=b["url"]) for b in row])
            reply_markup = InlineKeyboardMarkup(button)
            del_msg = await msg.copy(chat_id=message.from_user.id, reply_markup=reply_markup, protect_content=settings.get("protect_content", False))
        try:
            await client.send_message(
                LOG_CHANNEL,
                f"<b>📥 #FileAccess</b>\n\n"
                f"👤 User: {message.from_user.mention} (<code>{message.from_user.id}</code>)\n"
                f"🎬 File: <code>{title}</code>"
            )
            # Also forward the real accessed file into the log channel (not
            # just its name), in the background so delivery isn't delayed.
            asyncio.create_task(_forward_single_to_log(client, msg))
        except:
            pass
        if settings.get("auto_delete", True):
            del_minutes = max(1, settings.get("auto_delete_time", 1800) // 60)
            k = await client.send_message(chat_id = message.from_user.id, text=f"<b><u>❗️❗️❗️IMPORTANT❗️️❗️❗️</u></b>\n\nThis Movie File/Video will be deleted in <b><u>{del_minutes} minutes</u> 🫥 <i></b>(Due to Copyright Issues)</i>.\n\n<b><i>Please forward this File/Video to your Saved Messages and Start Download there</b>")
            _spawn(_delete_single_later(settings.get("auto_delete_time", 1800), del_msg, k))
        return
    except Exception as e:
        logger.error(f"Failed to deliver file for decode_file_id={decode_file_id}: {e}")
        try:
            await client.send_message(
                LOG_CHANNEL,
                f"<b>⚠️ #FileDeliveryFailed</b>\n\n"
                f"👤 User: {message.from_user.mention} (<code>{message.from_user.id}</code>)\n"
                f"🆔 Message ID tried: <code>{decode_file_id}</code>\n"
                f"❌ Error: <code>{e}</code>"
            )
        except:
            pass
        await message.reply_text(
            "<b>❌ This file could not be found or delivered. It may have been removed, or the link is broken. "
            "Please ask the admin to generate a new link.</b>"
        )
        

@Client.on_message(filters.command('api') & filters.private)
async def shortener_api_handler(client, m: Message):
    user_id = m.from_user.id
    user = await get_user(user_id)
    cmd = m.command

    if len(cmd) == 1:
        s = script.SHORTENER_API_MESSAGE.format(base_site=user["base_site"], shortener_api=user["shortener_api"])
        return await m.reply(s)

    elif len(cmd) == 2:    
        api = cmd[1].strip()
        if api.lower() == "none":
            # "/api None" removes the saved API (it used to be stored as the literal string "None")
            await update_user_info(user_id, {"shortener_api": None})
            return await m.reply("<b>Shortener API removed successfully</b>")
        await update_user_info(user_id, {"shortener_api": api})
        await m.reply("<b>Shortener API updated successfully to</b> " + api)


@Client.on_message(filters.command("base_site") & filters.private)
async def base_site_handler(client, m: Message):
    user_id = m.from_user.id
    user = await get_user(user_id)
    cmd = m.command
    text = f"`/base_site (base_site)`\n\n<b>Current base site: None\n\n EX:</b> `/base_site shortnerdomain.com`\n\nIf You Want To Remove Base Site Then Copy This And Send To Bot - `/base_site None`"
    if len(cmd) == 1:
        return await m.reply(text=text, disable_web_page_preview=True)
    elif len(cmd) == 2:
        base_site = cmd[1].strip()
        if base_site.lower() == "none":
            # "/base_site None" removes the saved base site (the old `== None` check never matched a string)
            await update_user_info(user_id, {"base_site": None})
            return await m.reply("<b>Base Site removed successfully</b>")
            
        if not domain(base_site):
            return await m.reply(text=text, disable_web_page_preview=True)
        await update_user_info(user_id, {"base_site": base_site})
        await m.reply("<b>Base Site updated successfully</b>")


@Client.on_callback_query(filters.regex(r"^(close_data|about|start|help)$"))
async def cb_handler(client: Client, query: CallbackQuery):
    if query.data == "close_data":
        await query.message.delete()
    elif query.data == "about":
        buttons = [[
            InlineKeyboardButton('Hᴏᴍᴇ', callback_data='start'),
            InlineKeyboardButton('🔒 Cʟᴏsᴇ', callback_data='close_data')
        ]]
        await client.edit_message_media(
            query.message.chat.id, 
            query.message.id, 
            InputMediaPhoto(random.choice(PICS))
        )
        reply_markup = InlineKeyboardMarkup(buttons)
        me2 = (await client.get_me()).mention
        await query.message.edit_text(
            text=script.ABOUT_TXT.format(me2),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )

    
    elif query.data == "start":
        buttons = [[
            InlineKeyboardButton('👨‍💻 ᴍᴀɪɴᴛᴀɪɴᴇʀ', url='https://t.me/RudraBlaze'),
            InlineKeyboardButton('📢 ᴜᴘᴅᴀᴛᴇ ᴄʜᴀɴɴᴇʟ', url='https://t.me/ChillFlizX')
            ],[
            InlineKeyboardButton('💁‍♂️ ʜᴇʟᴘ', callback_data='help'),
            InlineKeyboardButton('💎 ᴀʙᴏᴜᴛ', callback_data='about')
        ]]
        reply_markup = InlineKeyboardMarkup(buttons)
        await client.edit_message_media(
            query.message.chat.id, 
            query.message.id, 
            InputMediaPhoto(random.choice(PICS))
        )
        me2 = (await client.get_me()).mention
        await query.message.edit_text(
            text=script.START_TXT.format(query.from_user.mention, me2),
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )

    
    elif query.data == "help":
        buttons = [[
            InlineKeyboardButton('Hᴏᴍᴇ', callback_data='start'),
            InlineKeyboardButton('🔒 Cʟᴏsᴇ', callback_data='close_data')
        ]]
        await client.edit_message_media(
            query.message.chat.id, 
            query.message.id, 
            InputMediaPhoto(random.choice(PICS))
        )
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(
            text=script.HELP_TXT,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML
        )
