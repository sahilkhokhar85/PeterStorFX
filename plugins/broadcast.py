from pyrogram.errors import InputUserDeactivated, UserNotParticipant, FloodWait, UserIsBlocked, PeerIdInvalid
from plugins.dbusers import db
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMINS, BOT_ID, REPORT_CHANNEL
from plugins.admins_db import dynamic_admin_filter
import asyncio
import datetime
import html
import logging
import secrets
import time
from contextvars import ContextVar

logger = logging.getLogger(__name__)

# How many users we message at once. Telegram's flood limits are mostly
# per-chat, so sending to many *different* users concurrently is safe and
# turns a broadcast that used to take ages (one message at a time) into
# something that finishes in a fraction of the time.
CONCURRENCY = 25

# /broadcast flow:
#   1. /broadcast  -> send as many messages as you want, one by one
#                     (text, photo, video, file, sticker, a whole album, anything)
#   2. /done       -> bot sends you a PREVIEW of everything, in order, with Confirm / Cancel
#   3. only Confirm starts the broadcast.   /cancel at any time stops everything.
#   /broadcast as a REPLY to a message (or album) skips step 1-2 and goes straight to the preview.
#   At the preview you choose: "This Bot Only", or "All Bots" (every bot that shares this
#   database sends it to ITS OWN users, one click for all of them - see the end of this file).
#
# /send <user_id> works exactly the same way, but the messages go ONLY to that one user.
IDLE_SECONDS = 600        # the bot stops waiting if you send nothing for 10 minutes
ALBUM_QUIET = 1.2         # seconds of silence that mean "the album is complete"
MAX_MESSAGES = 50         # safety limit for one broadcast
JOB_LIFETIME = 1800       # an unconfirmed preview expires after 30 minutes

_WAITING = {}   # admin_id -> {"t": last activity, "msgs": [Message], "ack": Task|None}
_JOBS = {}      # token -> {"admin": id, "units": [[Message]], "t": created}
_BG = set()
_RUN_LOCK = asyncio.Lock()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return task


def _to_units(msgs):
    """Messages -> ordered list of units. A unit is one message, or one whole
    album (consecutive messages with the same media_group_id)."""
    units = []
    last_gid = None
    for m in sorted(msgs, key=lambda m: m.id):
        gid = getattr(m, "media_group_id", None)
        if gid and units and gid == last_gid:
            units[-1].append(m)
        else:
            units.append([m])
        last_gid = gid
    return units


async def _deliver_unit(client, user_id, unit):
    """Send one message, or one whole album, to user_id as a copy (no 'Forwarded from')."""
    if len(unit) == 1:
        await unit[0].copy(chat_id=user_id)
    else:
        await client.forward_messages(
            chat_id=user_id,
            from_chat_id=unit[0].chat.id,
            message_ids=[m.id for m in unit],
            drop_author=True,
        )


async def broadcast_messages(client, user_id, units):
    """Send every unit to one user, in order. A FloodWait retries only the unit
    that was waiting, so nothing is sent twice."""
    try:
        for unit in units:
            while True:
                try:
                    await _deliver_unit(client, user_id, unit)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value)
        return True, "Success"
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


async def _user_label(bot, uid):
    """'Name (id)' for the /send messages. The name comes from the users saved by any bot, else from
    Telegram; if neither knows it, only the id is shown."""
    name = ""
    try:
        doc = await db.col.find_one({"id": int(uid)})
        name = str((doc or {}).get("name") or "").strip()
    except Exception:
        pass
    if not name:
        try:
            u = await bot.get_users(int(uid))
            name = " ".join(x for x in (u.first_name, u.last_name) if x).strip()
        except Exception:
            pass
    if name:
        return f"<b>{html.escape(name)} (</b><code>{uid}</code><b>)</b>"
    return f"<code>{uid}</code>"


def _users(n):
    return "User" if n == 1 else "Users"


async def _make_preview(bot, admin_id, chat_id, units, target=None):
    """Show the admin exactly what will be sent (in order), then Confirm / Cancel."""
    try:
        for unit in units:
            await _deliver_unit(bot, chat_id, unit)   # the very same code the real broadcast uses
    except Exception as e:
        return await bot.send_message(
            chat_id,
            "<b>❌ One Of These Messages Can't Be Broadcast.</b>\n"
            f"<code>{type(e).__name__}</code>\n\n"
            "<b>Send /broadcast Again Without That Message.</b>",
        )

    _clean_old_jobs()
    token = secrets.token_hex(4)
    _JOBS[token] = {"admin": admin_id, "units": units, "target": target, "t": time.time()}
    n = len(units)
    what = "1 Message" if n == 1 and len(units[0]) == 1 else (
        f"1 Album · {len(units[0])} Items" if n == 1 else f"{n} Messages (In Order)")
    cancel = [InlineKeyboardButton("❌ Cancel", callback_data=f"bc_no:{token}")]
    if target:
        text = (
            "<b>👆 SEND PREVIEW !!</b>\n\n"
            f"<b>📦 Content: {what}</b>\n\n"
            f"<b>👤 User:</b> {await _user_label(bot, target)} <b>only</b>\n\n"
            "<b>⚠️ Check Once — Sending can't be undone.</b>"
        )
        buttons = [InlineKeyboardButton("✅ Confirm Send", callback_data=f"bc_yes:{token}")]
        return await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup([buttons, cancel]))

    mine = len(await _target_user_ids())
    buttons = [InlineKeyboardButton("✅ This Bot Only", callback_data=f"bc_yes:{token}")]
    try:
        online, offline = await _online_bots()
    except Exception:
        online, offline = [], []
    lines = [
        "👆 BROADCAST PREVIEW !!",
        "",
        f"📦 Content: {what}",
        "",
        f"✅ This Bot Only: {mine} {_users(mine)}",
    ]
    if len(online) > 1:
        rows, total = [], 0
        for d in online:
            count = await db.col.count_documents({"bot_id": d["_id"]})
            total += count
            rows.append(f"✅ @{html.escape(str(d.get('username') or d['_id']))} – {count} {_users(count)}")
        for d in offline:
            rows.append(f"⛔ @{html.escape(str(d.get('username') or d['_id']))} – Offline (Skipped)")
        lines.append(f"📣 All Bots: {total} {_users(total)} ({len(online)} online)")
        lines += ["", "🤖 ONLINE BOTS !!", ""] + rows
        buttons.append(InlineKeyboardButton(f"📣 All Bots ({len(online)})", callback_data=f"bc_all:{token}"))
    lines += ["", "⚠️ Check Once — Sending can't be undone."]
    # the whole message is bold (empty lines stay empty)
    text = "\n".join(f"<b>{l}</b>" if l else "" for l in lines)
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup([buttons, cancel]))


@Client.on_message(filters.command("broadcast") & dynamic_admin_filter("can_broadcast"))
async def broadcast_cmd(bot, message):
    admin_id = message.from_user.id
    old = _WAITING.pop(admin_id, None)
    if old and old.get("ack"):
        old["ack"].cancel()

    replied = message.reply_to_message
    if replied:
        msgs = [replied]
        if replied.media_group_id:
            try:
                msgs = await bot.get_media_group(replied.chat.id, replied.id)
            except Exception:
                msgs = [replied]
        return await _make_preview(bot, admin_id, message.chat.id, _to_units(msgs))

    _WAITING[admin_id] = {"t": time.time(), "msgs": [], "ack": None, "target": None}
    await message.reply_text(
        "<b>📣 Send Your Messages One By One.\n\n"
        "/done - Preview\n"
        "/cancel - Stop</b>"
    )


@Client.on_message(filters.command("send") & dynamic_admin_filter("can_broadcast"))
async def send_cmd(bot, message):
    """/send <user_id>: same flow as /broadcast, but only ONE user gets it."""
    admin_id = message.from_user.id
    old = _WAITING.pop(admin_id, None)
    if old and old.get("ack"):
        old["ack"].cancel()

    arg = message.command[1] if len(message.command) > 1 else ""
    if not (arg.isdigit() and 5 <= len(arg) <= 16):
        return await message.reply_text(
            "<b>📨 Usage:</b> <code>/send user_id</code>\n"
            "<b>Or Reply To A Message With It.</b>"
        )
    target = int(arg)
    if await db.is_user_banned(target):
        return await message.reply_text(
            "<b>🚫 This User Is Banned.\n/unban First To Message Them.</b>"
        )

    replied = message.reply_to_message
    if replied:
        msgs = [replied]
        if replied.media_group_id:
            try:
                msgs = await bot.get_media_group(replied.chat.id, replied.id)
            except Exception:
                msgs = [replied]
        return await _make_preview(bot, admin_id, message.chat.id, _to_units(msgs), target)

    _WAITING[admin_id] = {"t": time.time(), "msgs": [], "ack": None, "target": target}
    await message.reply_text(
        f"<b>📨 Send Your Messages To</b> {await _user_label(bot, target)}<b>.\n\n"
        "/done - Preview\n"
        "/cancel - Stop</b>"
    )


async def _ack(admin_id, chat_id, bot, delay):
    if delay:
        await asyncio.sleep(delay)
    state = _WAITING.get(admin_id)
    if not state:
        return
    units = _to_units(state["msgs"])
    old = state.get("ack_msg")
    state["ack_msg"] = await bot.send_message(
        chat_id,
        f"<b>✅ {len(units)} Message(s) Ready.\n\n"
        "Send More Or Tap Done.</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done", callback_data="bcw_done"),
            InlineKeyboardButton("❌ Cancel", callback_data="bcw_cancel"),
        ]]),
    )
    await _drop(old)


async def _drop(msg):
    """Delete a bot message (the old 'Ready' message), ignoring any error."""
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass


# group=-2 so it runs before every other handler: while the bot is collecting
# the broadcast, whatever the admin sends is captured here (an album's items
# included) and does not trigger other features such as link generation.
@Client.on_message(filters.private & filters.incoming, group=-2)
async def _capture_broadcast_message(bot, message):
    user = message.from_user
    state = _WAITING.get(user.id) if user else None
    if not state:
        return
    if time.time() - state["t"] > IDLE_SECONDS:
        _WAITING.pop(user.id, None)
        return

    first_word = ((message.text or "").strip().split(maxsplit=1) or [""])[0].split("@")[0].lower()
    if first_word in ("/broadcast", "/send"):
        return   # lets the command restart the flow

    state["t"] = time.time()

    if first_word == "/cancel":
        _WAITING.pop(user.id, None)
        if state["ack"]:
            state["ack"].cancel()
        await _drop(state.get("ack_msg"))
        await message.reply_text("<b>Cancelled.</b>")
        return message.stop_propagation()

    if first_word == "/done":
        if state["ack"]:
            state["ack"].cancel()
        if not state["msgs"]:
            await message.reply_text("<b>Nothing Added Yet. Send Your Message Or /cancel.</b>")
            return message.stop_propagation()
        _WAITING.pop(user.id, None)
        await _drop(state.get("ack_msg"))
        await _make_preview(bot, user.id, message.chat.id, _to_units(state["msgs"]), state.get("target"))
        return message.stop_propagation()

    if len(state["msgs"]) >= MAX_MESSAGES:
        await message.reply_text(f"<b>Limit Reached ({MAX_MESSAGES} Messages). Send /done Or /cancel.</b>")
        return message.stop_propagation()

    state["msgs"].append(message)
    if state["ack"]:
        state["ack"].cancel()
    # a single message is acknowledged at once, an album after its last item arrived
    state["ack"] = _spawn(_ack(user.id, message.chat.id, bot, ALBUM_QUIET if message.media_group_id else 0))
    message.stop_propagation()


@Client.on_callback_query(filters.regex(r"^bcw_(done|cancel)$") & dynamic_admin_filter("can_broadcast"))
async def collect_buttons(bot, query):
    """The Done / Cancel buttons under the 'Ready' message (same as /done and /cancel)."""
    user_id = query.from_user.id
    state = _WAITING.get(user_id)
    if not state or time.time() - state["t"] > IDLE_SECONDS:
        _WAITING.pop(user_id, None)
        await query.answer("Nothing pending. Run /broadcast again.", show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if query.data == "bcw_cancel":
        _WAITING.pop(user_id, None)
        if state["ack"]:
            state["ack"].cancel()
        await query.answer("Cancelled.")
        try:
            await query.message.edit_text("<b>Cancelled.</b>")
        except Exception:
            pass
        return
    if not state["msgs"]:
        return await query.answer("Nothing added yet. Send your message first.", show_alert=True)
    _WAITING.pop(user_id, None)
    if state["ack"]:
        state["ack"].cancel()
    await query.answer()
    chat_id = query.message.chat.id
    await _drop(query.message)
    await _make_preview(bot, user_id, chat_id, _to_units(state["msgs"]), state.get("target"))


@Client.on_callback_query(filters.regex(r"^bc_(yes|all|no):[0-9a-f]{8}$") & dynamic_admin_filter("can_broadcast"))
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
        return await query.message.edit_text("<b>❌ Cancelled. Nothing Was Sent.</b>")

    if action == "bc_all":
        if job.get("target"):
            return await query.answer("A single-user send can't go to all bots.", show_alert=True)
        try:
            running = await _jobs().find_one({"status": "running"})
        except Exception:
            running = None
        if running:
            return await query.answer("An all-bots broadcast is already running. Wait for it to finish.", show_alert=True)
        _JOBS.pop(token, None)   # one confirm = one broadcast
        await query.answer("Preparing...")
        return await _start_all_job(bot, query.message, job["units"])

    if job.get("target"):
        _JOBS.pop(token, None)   # one confirm = one send
        await query.answer("Sending...")
        return await _run_direct(bot, query.message, job["target"], job["units"])

    if _RUN_LOCK.locked():
        return await query.answer("A broadcast is already running. Wait for it to finish.", show_alert=True)

    _JOBS.pop(token, None)   # one confirm = one broadcast (a second tap finds nothing)
    await query.answer("Broadcasting...")
    await _run_broadcast(bot, query.message, job["units"])


async def _run_direct(bot, sts, user_id, units):
    """Send to one user and tell the admin exactly what happened."""
    await sts.edit_text("<b>Sending...</b>")
    sent = 0
    try:
        for unit in units:
            while True:
                try:
                    await _deliver_unit(bot, user_id, unit)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value)
            sent += 1
    except UserIsBlocked:
        reason = "That User Has Blocked The Bot."
    except InputUserDeactivated:
        reason = "That Account Is Deleted."
    except PeerIdInvalid:
        reason = "The Bot Can't Find This User. Check The ID. The User Must Have Started The Bot."
    except Exception as e:
        reason = f"Failed ({type(e).__name__})."
    else:
        return await sts.edit_text(f"<b>✅ Sent To User</b> <code>{user_id}</code><b>. ({sent} Message(s))</b>")
    await sts.edit_text(
        f"<b>❌ Not Sent To User</b> <code>{user_id}</code>\n<b>{reason}</b>\n"
        f"Delivered Before The Error: {sent} Of {len(units)}"
    )


async def _send_all(bot, user_ids, units, on_progress=None, every=20):
    """Send `units` to every id in user_ids (CONCURRENCY at a time) and return the counts.
    on_progress(counts) is awaited every `every` finished users."""
    counts = {"done": 0, "success": 0, "blocked": 0, "deleted": 0, "failed": 0}
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _send(uid):
        async with sem:
            return await broadcast_messages(bot, uid, units)

    tasks = [asyncio.create_task(_send(uid)) for uid in user_ids]
    for coro in asyncio.as_completed(tasks):
        ok, reason = await coro
        if ok:
            counts["success"] += 1
        elif reason == "Blocked":
            counts["blocked"] += 1
        elif reason == "Deleted":
            counts["deleted"] += 1
        else:
            counts["failed"] += 1
        counts["done"] += 1
        if on_progress and not counts["done"] % every:
            try:
                await on_progress(counts)
            except Exception:
                pass
    return counts


def _bot_text(c, total, final, secs):
    ok = int(c["success"] * 100 / total) if total else 0
    return _bold([
        "✅ BROADCAST COMPLETED !!" if final else "📣 BROADCASTING…",
        "",
        _bar_line(c["done"], total),
        f"⏱ Time: {_dur(secs)}",
        "",
        "📊 SUMMARY",
        f"👥 Total: {total}",
        f"✅ Success: {c['success']} ({ok}%)",
        f"🚫 Blocked: {c['blocked']}",
        f"🗑 Deleted: {c['deleted']}",
        f"❌ Failed: {c['failed']}",
    ])


async def _run_broadcast(bot, sts, units):
    async with _RUN_LOCK:
        user_ids = await _target_user_ids()
        start_time = time.time()
        total_users = len(user_ids)
        await sts.edit_text("<b>📣 BROADCASTING…</b>")

        async def _progress(c):
            await sts.edit_text(_bot_text(c, total_users, False, time.time() - start_time))

        c = await _send_all(bot, user_ids, units, _progress)
        await sts.edit_text(_bot_text(c, total_users, True, time.time() - start_time))


# ======================================================================
# ONE CLICK -> EVERY BOT
# ======================================================================
# Every bot (one Railway service each) shares this MongoDB but has its own
# users. "All Bots" works like this:
#   1. the bot you pressed the button on copies your messages ONCE into
#      STAGE_CHAT (REPORT_CHANNEL; every bot is an admin there) and writes a
#      job into MongoDB (broadcast_jobs) with one "part" per online bot;
#   2. every bot checks for jobs every few seconds, takes its own part, and
#      sends the staged messages to ITS OWN users;
#   3. the bot you pressed the button on shows the combined progress, then the
#      final result, and finally deletes the staged copies.
# Which bots are "online" comes from a heartbeat every bot writes (bot_registry).

STAGE_CHAT = REPORT_CHANNEL
ONLINE_WINDOW = 180      # a bot counts as online if its heartbeat is newer than this (seconds)
HEARTBEAT_EVERY = 60
POLL_EVERY = 4
CLAIM_WAIT = 600         # a bot that did not take its part within this time is skipped
STALL_AFTER = 300        # a running part that reported nothing for this long counts as lost
WATCH_EVERY = 3
_ZERO = {"total": 0, "done": 0, "success": 0, "blocked": 0, "deleted": 0, "failed": 0}


def _jobs():
    return db.db.broadcast_jobs


def _registry():
    return db.db.bot_registry


async def _heartbeat(client):
    try:
        await _registry().update_one(
            {"_id": BOT_ID},
            {"$set": {"username": client.me.username, "seen": time.time()}},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[ALLBOTS] heartbeat failed: {type(e).__name__}: {e}")


async def _online_bots():
    """(online, offline) registry documents. Offline = known bots with an old heartbeat."""
    cutoff = time.time() - ONLINE_WINDOW
    docs = await _registry().find({}).to_list(length=None)
    online = sorted((d for d in docs if d.get("seen", 0) >= cutoff), key=lambda d: str(d.get("username") or d["_id"]))
    offline = sorted((d for d in docs if d.get("seen", 0) < cutoff), key=lambda d: str(d.get("username") or d["_id"]))
    return online, offline


async def _stage_units(bot, units):
    """Copy the admin's messages into STAGE_CHAT (an album stays an album).
    Returns [{"ids": [...]}, ...] in the same order."""
    staged = []
    for unit in units:
        while True:
            try:
                if len(unit) == 1:
                    try:
                        from Peter.bot import NO_SIGN
                    except ImportError:
                        NO_SIGN = ContextVar("NO_SIGN", default=False)
                    tok = NO_SIGN.set(True)  # staged copy must be exactly what the admin sent
                    try:
                        sent = await unit[0].copy(STAGE_CHAT)
                    finally:
                        NO_SIGN.reset(tok)
                    ids = [sent.id]
                else:
                    res = await bot.forward_messages(
                        chat_id=STAGE_CHAT,
                        from_chat_id=unit[0].chat.id,
                        message_ids=[m.id for m in unit],
                        drop_author=True,
                    )
                    ids = [m.id for m in (res if isinstance(res, list) else [res])]
                break
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
        staged.append({"ids": ids})
    return staged


async def _start_all_job(bot, sts, units):
    try:
        online, _ = await _online_bots()
        if not online:
            return await sts.edit_text("<b>❌ No Bot Is Online Right Now. Try Again In A Minute.</b>")
        await sts.edit_text("<b>📦 Preparing The Broadcast For All Bots...</b>")
        staged = await _stage_units(bot, units)
    except Exception as e:
        logger.error(f"[ALLBOTS] could not start: {type(e).__name__}: {e}")
        return await sts.edit_text(f"<b>❌ Could Not Start The Broadcast.</b>\n<code>{type(e).__name__}</code>")

    token = secrets.token_hex(4)
    now = time.time()
    parts = {
        str(d["_id"]): {"state": "pending", "username": str(d.get("username") or ""), "beat": now, **_ZERO}
        for d in online
    }
    try:
        await _jobs().delete_many({"created": {"$lt": now - 2 * 86400}})
        await _jobs().insert_one({
            "_id": token, "created": now, "status": "running", "origin_bot": BOT_ID,
            "stage_chat": STAGE_CHAT, "units": staged, "parts": parts,
        })
    except Exception as e:
        logger.error(f"[ALLBOTS] could not save the job: {type(e).__name__}: {e}")
        return await sts.edit_text("<b>❌ Could Not Start The Broadcast (Database Error).</b>")
    _spawn(_watch_job(bot, sts, token))


def _bold(lines):
    """Every non-empty line bold. The progress bar line is <code> (monospace), which Telegram can't bold."""
    return "\n".join("" if not l else (l if l.startswith("<code>") else f"<b>{l}</b>") for l in lines)


def _bar_line(done, total):
    bar, pct = _bar(done, total)
    return f"<code>{bar}</code> <b>{pct}%</b>"


def _bar(done, total, width=10):
    pct = int(done * 100 / total) if total else 0
    filled = round(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled), pct


def _dur(sec):
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def _part_line(part):
    name = "@" + html.escape(part.get("username") or "?")
    state = part.get("state")
    if state == "pending":
        return f"⏸ {name} — Waiting"
    if state == "skipped":
        return f"⛔ {name} — Skipped (Did Not Respond)"
    if state == "lost":
        return f"⚠️ {name} — Stopped Midway ({part['done']}/{part['total']})"
    if state == "failed":
        return f"❌ {name} — Failed ({html.escape(part.get('error', 'error'))})"
    icon = "✅" if state == "done" else "⏳"
    line = f"{icon} {name} — {part['done']}/{part['total']}"
    extra = []
    if part.get("blocked"):
        extra.append(f"🚫{part['blocked']}")
    if part.get("deleted"):
        extra.append(f"🗑{part['deleted']}")
    if part.get("failed"):
        extra.append(f"❌{part['failed']}")
    return line + (" · " + " ".join(extra) if extra else "")


def _part_full(part):
    """Per-bot block for the final result: the same numbers as the summary, for one bot (2 lines)."""
    if part.get("state") != "done":
        return [_part_line(part)]
    name = "@" + html.escape(part.get("username") or "?")
    ok = int(part["success"] * 100 / part["total"]) if part["total"] else 0
    return [f"✅ {name}",
            f"   👥 {part['total']} · ✅ {part['success']} ({ok}%) · "
            f"🚫 {part['blocked']} · 🗑 {part['deleted']} · ❌ {part['failed']}"]


def _all_text(job, final, detail=True):
    parts = list(job["parts"].values())
    total = {k: sum(p.get(k, 0) for p in parts) for k in _ZERO}
    secs = _dur(job.get("finished", time.time()) - job["created"])
    if not final:
        return _bold(
            ["📣 ALL BOTS BROADCAST !!", "", f"⏳ Running: {total['done']}/{total['total']}", "",
             _bar_line(total["done"], total["total"]), ""]
            + [_part_line(p) for p in parts] + ["", f"⏱ Time: {secs}"]
        )
    issues = any(p["state"] in ("skipped", "lost", "failed") for p in parts)
    ok_pct = int(total["success"] * 100 / total["total"]) if total["total"] else 0
    lines = [
        "⚠️ BROADCAST COMPLETED WITH ISSUES !!" if issues else "✅ BROADCAST COMPLETED !!",
        "",
        _bar_line(total["done"], total["total"]),
        f"⏱ Time: {secs} · 🤖 Bots: {len(parts)}",
        "",
        "📊 SUMMARY",
        f"👥 Total: {total['total']}",
        f"✅ Success: {total['success']} ({ok_pct}%)",
        f"🚫 Blocked: {total['blocked']}",
        f"🗑 Deleted: {total['deleted']}",
        f"❌ Failed: {total['failed']}",
    ]
    if issues and not detail:
        bad = sum(1 for p in parts if p["state"] in ("skipped", "lost", "failed"))
        lines += ["", f"⚠️ {bad} Bot(s) Did Not Finish — Tap Bot-wise"]
    if detail:
        lines += ["", "🤖 BOT-WISE STATUS !!", ""]
        for i, p in enumerate(parts):
            if i:
                lines.append("")
            lines += _part_full(p)
    return _bold(lines)


def _final_markup(token, detail):
    toggle = (InlineKeyboardButton("🔼 Hide", callback_data=f"bcv_hide:{token}") if detail
              else InlineKeyboardButton("📋 Bot-wise", callback_data=f"bcv_show:{token}"))
    return InlineKeyboardMarkup([[toggle, InlineKeyboardButton("❌ Close", callback_data=f"bcv_close:{token}")]])


@Client.on_callback_query(filters.regex(r"^bcv_(show|hide|close):[0-9a-f]{8}$") & dynamic_admin_filter("can_broadcast"))
async def broadcast_view(bot, query):
    action, token = query.data.split(":")
    if action == "bcv_close":
        await query.answer()
        try:
            return await query.message.delete()
        except Exception:
            return
    try:
        job = await _jobs().find_one({"_id": token})
    except Exception:
        job = None
    if not job:
        return await query.answer("This result has expired.", show_alert=True)
    detail = action == "bcv_show"
    await query.answer()
    try:
        await query.message.edit_text(_all_text(job, True, detail), reply_markup=_final_markup(token, detail))
    except Exception:
        pass


async def _watch_job(bot, sts, token):
    """Runs on the bot you pressed the button on: shows combined progress and cleans up."""
    last = ""
    while True:
        await asyncio.sleep(WATCH_EVERY)
        try:
            job = await _jobs().find_one({"_id": token})
            if not job:
                return
            now = time.time()
            for bid, part in job["parts"].items():
                if part["state"] == "pending" and now - job["created"] > CLAIM_WAIT:
                    await _jobs().update_one({"_id": token, f"parts.{bid}.state": "pending"},
                                             {"$set": {f"parts.{bid}.state": "skipped"}})
                elif part["state"] == "running" and now - part.get("beat", now) > STALL_AFTER:
                    await _jobs().update_one({"_id": token, f"parts.{bid}.state": "running"},
                                             {"$set": {f"parts.{bid}.state": "lost"}})
            job = await _jobs().find_one({"_id": token})
            final = all(p["state"] not in ("pending", "running") for p in job["parts"].values())
            if final:
                job["finished"] = time.time()
                await _jobs().update_one({"_id": token}, {"$set": {"finished": job["finished"]}})
            text = _all_text(job, final, True)
            if text != last:
                last = text
                try:
                    await sts.edit_text(text, reply_markup=_final_markup(token, True) if final else None)
                except Exception:
                    pass
            if final:
                ids = [i for u in job["units"] for i in u["ids"]]
                for i in range(0, len(ids), 100):
                    try:
                        await bot.delete_messages(job["stage_chat"], ids[i:i + 100])
                    except Exception:
                        pass
                await _jobs().update_one({"_id": token}, {"$set": {"status": "done"}})
                return
        except Exception as e:
            logger.warning(f"[ALLBOTS] watcher error: {type(e).__name__}: {e}")


async def _run_part(client, job):
    """Runs on EVERY bot: send the staged messages to this bot's own users."""
    token = job["_id"]
    key = f"parts.{BOT_ID}"

    async def _save(c, state=None, total=None):
        upd = {f"{key}.{k}": v for k, v in c.items()}
        upd[f"{key}.beat"] = time.time()
        if total is not None:
            upd[f"{key}.total"] = total
        if state:
            upd[f"{key}.state"] = state
        await _jobs().update_one({"_id": token}, {"$set": upd})

    try:
        units = []
        for u in job["units"]:
            msgs = await client.get_messages(job["stage_chat"], u["ids"])
            msgs = [m for m in (msgs if isinstance(msgs, list) else [msgs]) if m and not getattr(m, "empty", False)]
            if len(msgs) != len(u["ids"]):
                raise RuntimeError("staged message missing")
            units.append(msgs)
        async with _RUN_LOCK:
            user_ids = await _target_user_ids()
            total = len(user_ids)
            await _save({"done": 0}, total=total)

            async def _progress(c):
                await _save(c)

            counts = await _send_all(client, user_ids, units, _progress, every=25)
            await _save(counts, state="done", total=total)
    except Exception as e:
        logger.error(f"[ALLBOTS] part failed: {type(e).__name__}: {e}")
        try:
            await _jobs().update_one({"_id": token}, {"$set": {
                f"{key}.state": "failed",
                f"{key}.error": f"{type(e).__name__}"[:60],
                f"{key}.beat": time.time(),
            }})
        except Exception:
            pass


async def cross_bot_loop(client):
    """Started once per bot (bot.py): heartbeat + picks up its part of all-bots broadcasts."""
    last_beat = 0.0
    while True:
        try:
            if time.time() - last_beat >= HEARTBEAT_EVERY:
                await _heartbeat(client)
                last_beat = time.time()
            job = await _jobs().find_one_and_update(
                {"status": "running", f"parts.{BOT_ID}.state": "pending"},
                {"$set": {f"parts.{BOT_ID}.state": "running", f"parts.{BOT_ID}.beat": time.time()}},
            )
            if job:
                _spawn(_run_part(client, job))
                continue   # look for another job right away
        except Exception as e:
            logger.warning(f"[ALLBOTS] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(POLL_EVERY)
