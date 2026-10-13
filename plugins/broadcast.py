from pyrogram.errors import InputUserDeactivated, UserNotParticipant, FloodWait, UserIsBlocked, PeerIdInvalid
from plugins.dbusers import db
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from config import ADMINS
from plugins.admins_db import dynamic_admin_filter
import asyncio
import datetime
import secrets
import time

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
        f"1 Album Of {len(units[0])} Items" if n == 1 else f"{n} Messages")
    order = " In This Order" if n > 1 else ""
    if target:
        who = f"ONLY To User <code>{target}</code>"
        ask = "Is This Exactly What You Want To Send?"
        yes = "✅ Confirm Send"
    else:
        who = f"To {len(await _target_user_ids())} Users"
        ask = "Is This Exactly What You Want To Broadcast?"
        yes = "✅ Confirm Broadcast"
    await bot.send_message(
        chat_id,
        f"<b>👆 Preview: {what} Will Be Sent {who}{order}.</b>\n\n"
        f"<b>{ask}</b>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(yes, callback_data=f"bc_yes:{token}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"bc_no:{token}"),
        ]]),
    )


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
        "<b>📣 Send The Messages You Want To Broadcast, One By One.</b>\n"
        "Text, photo, video, file, sticker, a whole album, anything works.\n\n"
        "<b>/done</b> When Finished (You Will Get A Preview First)\n"
        "<b>/cancel</b> To Stop"
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
            "<b>Usage:</b>\n"
            "<code>/send user_id</code>, Then Send Your Messages, Then /done\n"
            "Or Reply To A Message With <code>/send user_id</code>"
        )
    target = int(arg)
    if await db.is_user_banned(target):
        return await message.reply_text(
            "<b>🚫 This User Is Banned.</b>\nUse /unban First If You Really Want To Message Them."
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
        f"<b>📨 Send The Messages For User</b> <code>{target}</code><b>, One By One.</b>\n"
        "Text, photo, video, file, sticker, a whole album, anything works.\n\n"
        "<b>/done</b> When Finished (You Will Get A Preview First)\n"
        "<b>/cancel</b> To Stop"
    )


async def _ack(admin_id, chat_id, bot, delay):
    if delay:
        await asyncio.sleep(delay)
    state = _WAITING.get(admin_id)
    if not state:
        return
    units = _to_units(state["msgs"])
    await bot.send_message(
        chat_id,
        f"<b>✅ Added. {len(units)} Message(s) Ready.</b>\n"
        "Send More, Or <b>/done</b> To Preview, <b>/cancel</b> To Stop.",
    )


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
        await message.reply_text("<b>Cancelled.</b>")
        return message.stop_propagation()

    if first_word == "/done":
        if state["ack"]:
            state["ack"].cancel()
        if not state["msgs"]:
            await message.reply_text("<b>Nothing Added Yet. Send Your Message Or /cancel.</b>")
            return message.stop_propagation()
        _WAITING.pop(user.id, None)
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


@Client.on_callback_query(filters.regex(r"^bc_(yes|no):[0-9a-f]{8}$") & dynamic_admin_filter("can_broadcast"))
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


async def _run_broadcast(bot, sts, units):
    async with _RUN_LOCK:
        user_ids = await _target_user_ids()
        start_time = time.time()
        total_users = len(user_ids)
        done = 0
        blocked = 0
        deleted = 0
        failed = 0
        success = 0
        await sts.edit_text("<b>Broadcasting your messages...</b>")

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _send(uid):
            async with sem:
                return await broadcast_messages(bot, uid, units)

        tasks = [asyncio.create_task(_send(uid)) for uid in user_ids]
        for coro in asyncio.as_completed(tasks):
            ok, reason = await coro
            if ok:
                success += 1
            elif reason == "Blocked":
                blocked += 1
            elif reason == "Deleted":
                deleted += 1
            else:
                failed += 1
            done += 1
            if not done % 20:
                try:
                    await sts.edit_text(f"Broadcast in progress:\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}\nFailed: {failed}")
                except Exception:
                    pass

        time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
        await sts.edit_text(f"Broadcast Completed:\nCompleted in {time_taken} seconds.\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}\nFailed: {failed}")
