# Ban / Unban / Bot Status commands
# Added for the ChillFlizX bot

import io
import re
import time
import os
import sys
import asyncio
import secrets
import psutil
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
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
    """Ids typed in the command: every 5-16 digit number counts."""
    return {int(x) for x in _ID_RE.findall(text or "")}


def _ids_from_file_text(text):
    """Ids from a .txt / .csv file, line by line.
    - a line of only numbers (one or many, any separator) -> all of them are ids
    - a CSV row like "123456789,Name,Last,username,reason" -> only the FIRST column
      is the id (so digits inside names / usernames are never banned by mistake)
    - header lines such as "id,firstname,..." are ignored"""
    ids = set()
    for line in (text or "").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        if re.fullmatch(r"[\d\s,;\t]+", line):
            ids |= {int(x) for x in _ID_RE.findall(line)}
            continue
        first = re.split(r"[,;\t ]", line, maxsplit=1)[0].strip()
        if re.fullmatch(r"\d{5,16}", first):
            ids.add(int(first))
    return ids


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
                ids |= _ids_from_file_text(bytes(data.getbuffer()).decode("utf-8", errors="ignore"))
            except Exception as e:
                await message.reply_text(f"<b>❌ Couldn't read that file:</b> <code>{type(e).__name__}</code>")

    if not ids and message.reply_to_message and message.reply_to_message.from_user:
        ids.add(message.reply_to_message.from_user.id)
    return ids


# Undo buttons: token -> {"admin": id, "ids": set, "t": created}. In memory only;
# after a restart an old button just says it expired.
_UNDO = {}
_UNDO_LIFETIME = 3600


def _undo_markup(action, ids, admin_id):
    """One button under the result: Undo Ban / Ban Again."""
    now = time.time()
    for k in [k for k, v in _UNDO.items() if now - v["t"] > _UNDO_LIFETIME]:
        _UNDO.pop(k, None)
    token = secrets.token_hex(4)
    _UNDO[token] = {"admin": admin_id, "ids": set(ids), "t": now, "action": action}
    label = "🔓 Undo Ban" if action == "ban" else "🚫 Ban Again"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"mod_undo:{token}")]])


@Client.on_message(filters.command("ban") & dynamic_admin_filter())
async def ban_user_cmd(client, message: Message):
    ids = await _collect_target_ids(client, message)
    if not ids:
        return await message.reply_text(
            "<b>🚫 Ban Users\n\n"
            "/ban 123456789\n"
            "/ban id1 id2 id3\n"
            "Reply to a user with /ban\n"
            "Reply with a .txt file of ids</b>"
        )
    from plugins.admins_db import get_all_admins
    protected = set(ADMINS) | {int(a["_id"]) for a in await get_all_admins()}
    skipped = ids & protected
    ids -= protected
    before = set(await db.get_banned_ids())
    new = await db.ban_users(ids) if ids else 0
    newly = ids - before
    text = (
        "<b>🚫 Ban Done\n\n"
        f"✅ Newly Banned: {new}\n"
        f"♻️ Already Banned: {len(ids) - new}\n"
    )
    if skipped:
        text += f"🛡 Skipped Admins: {len(skipped)}\n"
    text += f"\n👥 Total Banned: {await db.total_banned_count()}</b>"
    markup = _undo_markup("ban", newly, message.from_user.id) if newly else None
    await message.reply_text(text, reply_markup=markup)


@Client.on_message(filters.command("unban") & dynamic_admin_filter())
async def unban_user_cmd(client, message: Message):
    ids = await _collect_target_ids(client, message)
    if not ids:
        return await message.reply_text(
            "<b>🔓 Unban Users\n\n"
            "/unban 123456789\n"
            "/unban id1 id2 id3\n"
            "Reply to a user with /unban\n"
            "Reply with a .txt file of ids</b>"
        )
    before = set(await db.get_banned_ids())
    removed = await db.unban_users(ids)
    was = ids & before
    text = (
        "<b>✅ Unban Done\n\n"
        f"🔓 Unbanned: {removed}\n"
        f"⚠️ Not In Ban List: {len(ids) - removed}\n\n"
        f"👥 Total Banned: {await db.total_banned_count()}</b>"
    )
    markup = _undo_markup("unban", was, message.from_user.id) if was else None
    await message.reply_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^mod_undo:[0-9a-f]{8}$") & dynamic_admin_filter())
async def moderation_undo(client, query):
    job = _UNDO.get(query.data.split(":", 1)[1])
    if not job:
        await query.answer("Expired. Use /ban or /unban again.", show_alert=True)
        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass
        return
    if query.from_user.id != job["admin"]:
        return await query.answer("Only the admin who ran this can undo it.", show_alert=True)
    _UNDO.pop(query.data.split(":", 1)[1], None)
    ids = job["ids"]
    if job["action"] == "ban":
        changed = await db.unban_users(ids)
        head, line = "🔓 Ban Undone", f"🔓 Unbanned: {changed}"
    else:
        changed = await db.ban_users(ids)
        head, line = "🚫 Ban Restored", f"🚫 Banned Again: {changed}"
    await query.answer("Done.")
    await query.message.edit_text(
        f"<b>{head}\n\n{line}\n\n👥 Total Banned: {await db.total_banned_count()}</b>"
    )


@Client.on_message(filters.command("banlist") & dynamic_admin_filter())
async def banlist_cmd(client, message: Message):
    """Send the whole ban list as a .txt file (handy as a backup)."""
    ids = await db.get_banned_ids()
    if not ids:
        return await message.reply_text("<b>🚫 Ban List Is Empty.</b>")
    buf = io.BytesIO("\n".join(str(i) for i in ids).encode("utf-8"))
    buf.name = "banned_ids.txt"
    await message.reply_document(buf, caption=f"<b>🚫 Banned Ids: {len(ids)}</b>")


@Client.on_message(filters.command("status") & dynamic_admin_filter())
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
        "<b>🤖 BOT STATUS !!</b>\n\n"
        f"<b>👤 Users -</b> <code>{total_users}</code>\n"
        f"<b>🚫 Ban Users -</b> <code>{total_banned}</code>\n"
        f"<b>⚙️ CPU -</b> <code>{cpu}%</code>\n"
        f"<b>💾 RAM -</b> <code>{ram}%</code>\n"
        f"<b>⚡ Uptime -</b> <code>{uptime}</code>"
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


RESTART_CONFIRM_TEXT = "<b>♻️ Restart The Bot?\n\nIt Will Be Offline For A Few Seconds.</b>"
RESTART_CONFIRM_SECONDS = 120   # an old confirm message must not restart the bot later


@Client.on_message(filters.command("restart") & dynamic_admin_filter())
async def restart_bot_cmd(client, message: Message):
    # Ask first: a restart only happens after the Yes button.
    await message.reply_text(
        RESTART_CONFIRM_TEXT,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, Restart", callback_data=f"rst_yes:{int(time.time())}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"rst_no:{int(time.time())}"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^rst_(yes|no):\d+$") & dynamic_admin_filter())
async def restart_confirm_cb(client, query):
    answer, _, stamp = query.data.partition(":")
    if answer == "rst_no":
        await query.answer("Cancelled.")
        return await query.message.edit_text("<b>Cancelled.</b>")
    if time.time() - int(stamp) > RESTART_CONFIRM_SECONDS:
        await query.answer("Expired. Send /restart again.", show_alert=True)
        return await query.message.edit_text("<b>Expired. Send /restart Again.</b>")
    await query.answer("Restarting...")
    await query.message.edit_text("<b>♻️ Restarting bot, please wait...</b>")
    os.execv(sys.executable, [sys.executable] + sys.argv)
