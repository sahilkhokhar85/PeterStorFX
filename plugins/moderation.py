# Ban / Unban / Bot Status commands
# Added for the ChillFlizX bot

import io
import re
import time
import os
import sys
import asyncio
import psutil
from pyrogram import Client, filters
from pyrogram.types import Message
from config import ADMINS
from plugins.dbusers import db
from plugins.admins_db import dynamic_admin_filter

BOT_START_TIME = time.time()


def get_readable_time(seconds: int) -> str:
    periods = [('day', 86400), ('hour', 3600), ('minute', 60), ('second', 1)]
    result = []
    for name, secs in periods:
        val, seconds = divmod(seconds, secs)
        if val:
            result.append(f"{val} {name}{'s' if val != 1 else ''}")
    return ' '.join(result) if result else '0 seconds'


_ID_RE = re.compile(r"\d{5,16}")
_MAX_ID_FILE = 5 * 1024 * 1024  # 5 MB is plenty for a list of ids


def _ids_from_text(text):
    return {int(x) for x in _ID_RE.findall(text or "")}


async def _collect_target_ids(client, message: Message):
    """Ids can come from: the command text (many ids, any separator), a .txt/.csv
    file (sent with the command as caption, or replied to), or a replied-to user."""
    ids = set()
    text = message.text or message.caption or ""
    parts = text.split(None, 1)
    if len(parts) > 1:
        ids |= _ids_from_text(parts[1])

    for doc_msg in (message, message.reply_to_message):
        doc = getattr(doc_msg, "document", None) if doc_msg else None
        if doc and (doc.file_size or 0) <= _MAX_ID_FILE:
            try:
                data = await client.download_media(doc, in_memory=True)
                ids |= _ids_from_text(bytes(data.getbuffer()).decode("utf-8", errors="ignore"))
            except Exception as e:
                await message.reply_text(f"<b>❌ Couldn't read that file:</b> <code>{type(e).__name__}</code>")

    if not ids and message.reply_to_message and message.reply_to_message.from_user:
        ids.add(message.reply_to_message.from_user.id)
    return ids


@Client.on_message(filters.command("ban") & dynamic_admin_filter())
async def ban_user_cmd(client, message: Message):
    ids = await _collect_target_ids(client, message)
    if not ids:
        return await message.reply_text(
            "<b>Usage:</b>\n"
            "<code>/ban user_id</code>\n"
            "<code>/ban id1 id2 id3 ...</code> (many ids at once)\n"
            "Send or reply with a .txt file of ids using /ban\n"
            "Or reply to a user's message with /ban\n\n"
            "Ids don't need to have used the bot. They are blocked forever and get no reply."
        )
    from plugins.admins_db import get_all_admins
    protected = set(ADMINS) | {int(a["_id"]) for a in await get_all_admins()}
    skipped = ids & protected
    ids -= protected
    new = await db.ban_users(ids) if ids else 0
    text = (
        f"<b>✅ Banned {new} new id(s).</b>\n"
        f"Already banned: <code>{len(ids) - new}</code>\n"
    )
    if skipped:
        text += f"Skipped admins: <code>{len(skipped)}</code>\n"
    text += f"Total banned now: <code>{await db.total_banned_count()}</code>"
    await message.reply_text(text)


@Client.on_message(filters.command("unban") & dynamic_admin_filter())
async def unban_user_cmd(client, message: Message):
    ids = await _collect_target_ids(client, message)
    if not ids:
        return await message.reply_text(
            "<b>Usage:</b>\n"
            "<code>/unban user_id</code>\n"
            "<code>/unban id1 id2 id3 ...</code>\n"
            "Or reply to a user's message / a .txt file of ids with /unban"
        )
    removed = await db.unban_users(ids)
    await message.reply_text(
        f"<b>✅ Unbanned {removed} id(s).</b>\n"
        f"Not in the ban list: <code>{len(ids) - removed}</code>\n"
        f"Total banned now: <code>{await db.total_banned_count()}</code>"
    )


@Client.on_message(filters.command("banlist") & dynamic_admin_filter())
async def banlist_cmd(client, message: Message):
    """Send the whole ban list as a .txt file (handy as a backup)."""
    ids = await db.get_banned_ids()
    if not ids:
        return await message.reply_text("<b>The ban list is empty.</b>")
    buf = io.BytesIO("\n".join(str(i) for i in ids).encode("utf-8"))
    buf.name = "banned_ids.txt"
    await message.reply_document(buf, caption=f"<b>🚫 Banned ids: {len(ids)}</b>")


@Client.on_message(filters.command("delreq") & dynamic_admin_filter())
async def delreq_cmd(client, message: Message):
    """Manual safety net: wipes every recorded 'Join Request Mode' record so
    everyone has to send a fresh request. Individual leaves are already
    detected and cleared automatically - use this only if you suspect some
    got missed and want to force a clean reset for all channels at once."""
    from plugins.settings_db import clear_all_join_requests
    count = await clear_all_join_requests()
    await message.reply_text(f"<b>✅ Cleared {count} join-request record(s).</b> Everyone will need to send a fresh join request.")


@Client.on_message(filters.command(["status", "stats"]) & dynamic_admin_filter())
async def bot_status(client, message: Message):
    total_users = await db.total_users_count()
    total_banned = await db.total_banned_count()
    try:
        cpu = await asyncio.to_thread(psutil.cpu_percent, 0.5)
        ram = psutil.virtual_memory().percent
    except Exception:
        cpu = ram = 0
    uptime = get_readable_time(int(time.time() - BOT_START_TIME))
    text = (
        "<b>🤖 BOT STATUS</b>\n\n"
        f"👤 Users - <code>{total_users}</code>\n"
        f"🚫 Ban Users - <code>{total_banned}</code>\n"
        f"⚙️ CPU - <code>{cpu}%</code>\n"
        f"💾 RAM - <code>{ram}%</code>\n"
        f"⚡ Uptime - <code>{uptime}</code>"
    )
    await message.reply_text(text)


async def do_restart(chat_id_notify=None, client=None):
    """Restarts the bot process in place (works on Railway/Koyeb/Render)."""
    if client and chat_id_notify:
        try:
            await client.send_message(chat_id_notify, "<b>✅ Bot restarted successfully!</b>")
        except Exception:
            pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


@Client.on_message(filters.command("restart") & dynamic_admin_filter())
async def restart_bot_cmd(client, message: Message):
    await message.reply_text("<b>♻️ Restarting bot, please wait...</b>")
    os.execv(sys.executable, [sys.executable] + sys.argv)
