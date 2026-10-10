# Ban / Unban / Bot Status commands
# Added for the ChillFlizX bot

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


def _extract_target_id(message: Message):
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if len(message.command) >= 2:
        try:
            return int(message.command[1])
        except ValueError:
            return None
    return None


@Client.on_message(filters.command("ban") & dynamic_admin_filter())
async def ban_user_cmd(client, message: Message):
    target_id = _extract_target_id(message)
    if target_id is None:
        return await message.reply_text(
            "<b>Usage:</b> <code>/ban user_id</code>\nOr reply to that user's message with /ban"
        )
    from plugins.admins_db import is_admin
    if target_id in ADMINS or await is_admin(target_id):
        return await message.reply_text("<b>❌ You can't ban an admin.</b>")
    if not await db.is_user_exist(target_id):
        return await message.reply_text("<b>This user has never used the bot.</b>")
    await db.ban_user(target_id)
    await message.reply_text(f"<b>✅ User <code>{target_id}</code> has been banned from the bot.</b>")


@Client.on_message(filters.command("unban") & dynamic_admin_filter())
async def unban_user_cmd(client, message: Message):
    target_id = _extract_target_id(message)
    if target_id is None:
        return await message.reply_text(
            "<b>Usage:</b> <code>/unban user_id</code>\nOr reply to that user's message with /unban"
        )
    if not await db.is_user_exist(target_id):
        return await message.reply_text("<b>This user has never used the bot.</b>")
    await db.unban_user(target_id)
    await message.reply_text(f"<b>✅ User <code>{target_id}</code> has been unbanned.</b>")


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
