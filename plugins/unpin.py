"""/unpin: take pinned broadcast messages down again.

/unpin           lists the last pinned broadcasts / sends, tap the ones to unpin, then Confirm Unpin
/unpin user_id   the same, but only for that one user
"Unpin All Messages" removes EVERY pinned message in the chat (also pins made before pins were remembered).
"""
import asyncio
import html
import secrets
import time
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, MessageIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from plugins.admins_db import dynamic_admin_filter
from plugins.broadcast import (_RUN_LOCK, _bar_line, _dur, _user_label, _users, CONCURRENCY, _target_user_ids,
                               list_pin_batches, get_pin_records, delete_pin_records,
                               delete_pin_records_for_users, prune_pin_batches)

LIFETIME = 1800   # an open /unpin screen expires after 30 minutes
FLOOD_RETRIES = 5

_SESS = {}   # token -> {"admin", "user", "label", "batches", "sel", "t", "count"}


def _clean():
    now = time.time()
    for token in [t for t, s in _SESS.items() if now - s["t"] > LIFETIME]:
        _SESS.pop(token, None)


# ---------------- the list screen ----------------

def _text(sess) -> str:
    out = ["<b>📌 PINNED MESSAGES !!</b>"]
    if sess["user"]:
        out += ["", f"<b>👤 User:</b> {sess['label']}"]
    batches = sess["batches"]
    if not batches:
        out += ["", "<b>No Saved Pins Yet.</b>", "<b>(Pins Made Before This Update Aren't Listed.)</b>"]
        return "\n".join(out)
    out += ["", "<b>👇 Tap To Select What To Unpin</b>"]
    for i, b in enumerate(batches, 1):
        date = b["t"].strftime("%d %b")
        out += ["", f"<b>{i}. 📅 {date} · {b['n']} {_users(b['n'])}</b>",
                f"<b>💬 {html.escape(b['snippet'])}</b>"]
    out += ["", f"<b>✅ Selected: {len(sess['sel'])}</b>"]
    return "\n".join(out)


def _markup(token, sess):
    rows = []
    nums = [InlineKeyboardButton(("✅ " if i in sess["sel"] else "📌 ") + str(i + 1), callback_data=f"up_t:{token}:{i}")
            for i in range(len(sess["batches"]))]
    rows += [nums[i:i + 5] for i in range(0, len(nums), 5)]
    rows.append([InlineKeyboardButton("🧹 Unpin All Messages", callback_data=f"up_all:{token}")])
    if sess["batches"]:
        rows.append([InlineKeyboardButton("✅ Confirm Unpin", callback_data=f"up_go:{token}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"up_no:{token}")])
    return InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("unpin") & filters.private & dynamic_admin_filter("can_broadcast"))
async def unpin_cmd(bot, message):
    arg = message.command[1] if len(message.command) > 1 else ""
    user = None
    if arg:
        if not (arg.isdigit() and 5 <= len(arg) <= 16):
            return await message.reply_text(
                "<b>📌 Usage:</b>\n"
                "<code>/unpin</code>\n"
                "<code>/unpin user_id</code>"
            )
        user = int(arg)
    _clean()
    token = secrets.token_hex(4)
    sess = {"admin": message.from_user.id, "user": user, "t": time.time(), "sel": set(),
            "batches": await list_pin_batches(user), "label": await _user_label(bot, user) if user else ""}
    _SESS[token] = sess
    await message.reply_text(_text(sess), reply_markup=_markup(token, sess))


# ---------------- doing it ----------------

async def _flood(fn):
    for _ in range(FLOOD_RETRIES):
        try:
            return await fn()
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
    return await fn()


async def _unpin_user(bot, uid, items):
    """items = [(record_id, message_id)] to unpin, or None = unpin everything in that chat.
    Returns (reason, finished_record_ids)."""
    done = []
    try:
        if items is None:
            await _flood(lambda: bot.unpin_all_chat_messages(uid))
        else:
            for rec_id, mid in items:
                try:
                    await _flood(lambda mid=mid: bot.unpin_chat_message(uid, mid))
                except MessageIdInvalid:
                    pass   # the message is gone already, nothing left to unpin
                done.append(rec_id)
        return "Success", done
    except UserIsBlocked:
        return "Blocked", done
    except InputUserDeactivated:
        return "Deleted", done
    except Exception:
        return "Error", done


def _result_text(c, total, final, secs):
    ok = int(c["success"] * 100 / total) if total else 0
    lines = [
        "✅ UNPIN COMPLETED !!" if final else "📌 UNPINNING…",
        "",
        None,
        f"⏱ Time: {_dur(secs)}",
        "",
        "📊 SUMMARY",
        f"👥 Total: {total}",
        f"✅ Success: {c['success']} ({ok}%)",
        f"🚫 Blocked: {c['blocked']}",
        f"🗑 Deleted: {c['deleted']}",
        f"❌ Failed: {c['failed']}",
    ]
    return "\n".join("" if l == "" else (_bar_line(c["done"], total) if l is None else f"<b>{l}</b>") for l in lines)


async def _execute(bot, sts, sess, everything, admin):
    user = sess["user"]
    async with _RUN_LOCK:
        if everything:
            ids = [user] if user else await _target_user_ids()
            plan = {uid: None for uid in ids}
            chosen = []
        else:
            chosen = [sess["batches"][i] for i in sorted(sess["sel"])]
            plan = defaultdict(list)
            for doc in await get_pin_records([b["_id"] for b in chosen], user):
                plan[doc["user_id"]].append((doc["_id"], doc["message_id"]))
        total = len(plan)
        start = time.time()
        await sts.edit_text("<b>📌 UNPINNING…</b>")

        counts = {"done": 0, "success": 0, "blocked": 0, "deleted": 0, "failed": 0}
        finished_ids, finished_users, last = [], [], {}
        sem = asyncio.Semaphore(CONCURRENCY)

        async def _one(uid, items):
            async with sem:
                return uid, await _unpin_user(bot, uid, items)

        tasks = [asyncio.create_task(_one(uid, items)) for uid, items in plan.items()]
        for coro in asyncio.as_completed(tasks):
            uid, (reason, done) = await coro
            key = {"Success": "success", "Blocked": "blocked", "Deleted": "deleted"}.get(reason, "failed")
            counts[key] += 1
            counts["done"] += 1
            last[uid] = reason
            if reason != "Error":      # nothing more can be done for blocked / deleted users either
                finished_users.append(uid)
            finished_ids += done
            if not user and not counts["done"] % 20:
                try:
                    await sts.edit_text(_result_text(counts, total, False, time.time() - start))
                except Exception:
                    pass

        try:   # forget what is unpinned
            if everything:
                await delete_pin_records_for_users(finished_users)
            else:
                await delete_pin_records(finished_ids)
            await prune_pin_batches()
        except Exception:
            pass

        if user:
            reason = last.get(user, "Success")
            if reason == "Success":
                text = f"<b>✅ Unpinned For User</b> <code>{user}</code>"
            else:
                why = {"Blocked": "That User Has Blocked The Bot.", "Deleted": "That Account Is Deleted."}.get(
                    reason, "Telegram Refused It. Try Again Later.")
                text = f"<b>❌ Couldn't Unpin For User</b> <code>{user}</code>\n<b>{why}</b>"
        else:
            text = _result_text(counts, total, True, time.time() - start)
        await sts.edit_text(text)


# ---------------- buttons ----------------

@Client.on_callback_query(filters.regex(r"^up_(t|go|all|yes|back|no):[0-9a-f]{8}(:\d{1,3})?$") & dynamic_admin_filter("can_broadcast"))
async def unpin_buttons(bot, query):
    action, _, rest = query.data.partition(":")
    action = action[3:]    # "up_go" -> "go"
    token, _, idx = rest.partition(":")
    sess = _SESS.get(token)
    if not sess:
        await query.answer("This screen expired. Run /unpin again.", show_alert=True)
        try:
            await query.message.edit_text("<b>⌛ Expired. Run /unpin Again.</b>")
        except Exception:
            pass
        return
    if sess["admin"] != query.from_user.id:
        return await query.answer("This belongs to another admin.", show_alert=True)
    sess["t"] = time.time()

    async def show(text, markup):
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass

    if action == "no":
        _SESS.pop(token, None)
        await query.answer("Cancelled.")
        return await show("<b>❌ Cancelled. Nothing Was Unpinned.</b>", None)

    if action == "t":
        i = int(idx)
        if i >= len(sess["batches"]):
            return await query.answer()
        sess["sel"] ^= {i}
        await query.answer()
        return await show(_text(sess), _markup(token, sess))

    if action == "back":
        await query.answer()
        return await show(_text(sess), _markup(token, sess))

    if action == "all":
        await query.answer()
        if sess["user"]:
            who = f"For User {sess['label']}"
        else:
            n = len(await _target_user_ids())
            who = f"For {n} {_users(n)}"
        text = (f"<b>⚠️ Unpin Everything</b> {who}<b>?</b>\n\n"
                "<b>Every Pinned Message Goes, Even Old Ones.</b>")
        return await show(text, InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Unpin All", callback_data=f"up_yes:{token}")],
            [InlineKeyboardButton("↩️ Back", callback_data=f"up_back:{token}")],
        ]))

    # up_go (selected pins) and up_yes (everything) really do it
    everything = action == "yes"
    if not everything and not sess["sel"]:
        return await query.answer("Select at least one message first.", show_alert=True)
    if _RUN_LOCK.locked():
        return await query.answer("A broadcast or unpin is already running. Wait for it to finish.", show_alert=True)
    _SESS.pop(token, None)   # one confirm = one run
    await query.answer("Unpinning...")
    await _execute(bot, query.message, sess, everything, query.from_user)
