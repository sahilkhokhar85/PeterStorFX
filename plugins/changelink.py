# Change Link (owners only), opened from /settings -> Links.
#
# A post already shows a domain link (chillflizx.in/KEY) that opens a shortener
# link (Arolinks) that opens a bot link. This changes ONLY what the bot link
# delivers (a new file or a new batch). The domain link and the shortener link
# stay exactly the same and NO new shortener link is created.
#
# Flow (all buttons, no commands):
#   Links -> Change Link -> send the domain link -> File / Batch -> send the new
#   content -> preview -> Confirm.
# Confirm updates the Worker record (the shortener is kept) and the saved link in
# the database, then shows the shortener link and the new bot link so the
# destination can be edited in the Arolinks dashboard (Arolinks has no API for it).

import asyncio
import base64
import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid

from config import ADMINS, LOG_CHANNEL, WEBSITE_URL
from Peter.utils.cover_copy import copy_keep_cover
from plugins.genlink import (
    links_col, PERMANENT_LINK_WORKER_URL, PERMANENT_LINK_ADMIN_KEY,
    bold, encode_start, make_file_share_link, get_message_cache_key,
    get_media_unique_id, _extract_batch_ref, fetch_batch_messages,
    copy_batch_to_log, _file_info,
)

IDLE_SECONDS = 600

_FLOW = {}   # owner id -> {"step", "t", "prompt", "doc", "old", "new"}
_DONE = {}   # token -> {"admin": id, "old_log_id": int}


async def _owner(_, __, update):
    user = getattr(update, "from_user", None)
    return bool(user and user.id in ADMINS)

owner_only = filters.create(_owner)


def _cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel")]])


async def _show(state, text, markup=None):
    """Edit the single bot message the whole flow lives in."""
    try:
        await state["prompt"].edit_text(text, reply_markup=markup)
    except Exception:
        pass


def _fresh(state):
    return bool(state) and time.time() - state["t"] <= IDLE_SECONDS


# ---------------- reading the old content ----------------

def _start_param(share_link):
    m = re.search(r"[?&]start=([A-Za-z0-9_-]+)", share_link or "")
    return m.group(1) if m else None


def _b64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")


async def _describe_old(bot, share_link):
    """Returns (label, old_file_log_id or None) for what the bot link gives now."""
    param = _start_param(share_link)
    try:
        if param and param.startswith("BATCH-"):
            return "Batch", None
        if param:
            kind, _, log_id = _b64(param).partition("_")
            if kind == "file" and log_id.isdigit():
                label = "1 File"
                try:
                    msg = await bot.get_messages(LOG_CHANNEL, int(log_id))
                    name, _size = _file_info(msg)
                    if name:
                        label = f"1 File ({name})"
                except Exception:
                    pass
                return label, int(log_id)
    except Exception:
        pass
    return "Unknown", None


# ---------------- opening the flow ----------------

@Client.on_callback_query(filters.regex(r"^lnk_change$") & owner_only)
async def change_start(bot, query):
    state = _FLOW[query.from_user.id] = {
        "step": "link", "t": time.time(), "prompt": query.message,
        "doc": None, "old": None, "new": None,
    }
    await query.answer()
    await _show(
        state,
        bold("✏️ Change Link\n\nSend The Domain Link Of The Post You Want To Change."),
        _cancel_markup(),
    )


@Client.on_callback_query(filters.regex(r"^lnk_cancel$") & owner_only)
async def change_cancel(bot, query):
    _FLOW.pop(query.from_user.id, None)
    await query.answer("Cancelled.")
    try:
        await query.message.edit_text(bold("Cancelled."))
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^lnk_pick:(file|batch)$") & owner_only)
async def change_pick(bot, query):
    state = _FLOW.get(query.from_user.id)
    if not _fresh(state) or state["step"] != "choose":
        _FLOW.pop(query.from_user.id, None)
        return await query.answer("Expired. Open Change Link again.", show_alert=True)
    state["t"] = time.time()
    await query.answer()
    if query.data.endswith("file"):
        state["step"] = "file"
        await _show(state, bold("📄 Send The New File."), _cancel_markup())
    else:
        state["step"] = "batch1"
        await _show(
            state,
            bold("📦 Send The First Message Of The New Batch.\n\n"
                 "Forward It With Tag, Or Send Its Link."),
            _cancel_markup(),
        )


# ---------------- collecting the messages ----------------
# group=-3: runs before every other handler, so while this flow is open nothing
# the owner sends here is treated as a normal file or command.

def _preview_text(state):
    return bold(
        "👆 LINK CHANGE PREVIEW !!\n\n"
        "🌐 Domain Link: Same\n"
        "🔗 Shortener: Same\n\n"
        f"📦 Old Content: {html.escape(state['old']['label'])}\n"
        f"📦 New Content: {html.escape(state['new']['label'])}"
    )


def _confirm_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="lnk_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel"),
    ]])


async def _link_taken(cache_key, own_permanent):
    """A file/batch that already has a DIFFERENT domain link must not be overwritten."""
    existing = await links_col.find_one({"_id": cache_key})
    if existing and existing.get("permanent_url") and existing["permanent_url"] != own_permanent:
        return existing["permanent_url"]
    return None


@Client.on_message(filters.private & filters.incoming, group=-3)
async def collect(bot, message):
    user = message.from_user
    state = _FLOW.get(user.id) if user and user.id in ADMINS else None
    if not state:
        return
    if not _fresh(state):
        _FLOW.pop(user.id, None)
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        return   # commands still work
    state["t"] = time.time()
    step = state["step"]

    if step == "link":
        m = re.search(r"https?://[^\s/]+/([A-Za-z0-9_-]+)/?\s*$", text)
        if not m:
            await message.reply_text(bold("❌ Send A Valid Domain Link."))
            return message.stop_propagation()
        key = m.group(1)
        doc = await links_col.find_one({"permanent_url": {"$regex": f"/{re.escape(key)}/?$"}})
        if not doc:
            await message.reply_text(bold("❌ This Link Is Not In The Database."))
            return message.stop_propagation()
        label, old_id = await _describe_old(bot, doc.get("share_link"))
        state.update(step="choose", doc=doc, key=key,
                     old={"label": label, "log_id": old_id})
        await _show(
            state,
            bold(f"✅ Link Found\n\nCurrently Gives: {html.escape(label)}\n\nWhat Is The New Content?"),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 File", callback_data="lnk_pick:file"),
                 InlineKeyboardButton("📦 Batch", callback_data="lnk_pick:batch")],
                [InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel")],
            ]),
        )
        return message.stop_propagation()

    if step == "file":
        if not get_media_unique_id(message):
            await message.reply_text(bold("❌ Send A File (Document, Video Or Audio)."))
            return message.stop_propagation()
        cache_key = get_message_cache_key(message)
        taken = await _link_taken(cache_key, state["doc"].get("permanent_url"))
        if taken:
            await message.reply_text(
                bold("❌ This File Already Has Its Own Link. Send A Different File.\n\n")
                + f"<code>{html.escape(taken)}</code>"
            )
            return message.stop_propagation()
        await _show(state, bold("⏳ Saving The File..."))
        post = await copy_keep_cover(message, LOG_CHANNEL)
        name, _size = _file_info(message)
        state["new"] = {
            "share": make_file_share_link(post.id),
            "label": f"1 File ({name})" if name else "1 File",
            "cache_key": cache_key,
        }
        state["step"] = "preview"
        await _show(state, _preview_text(state), _confirm_markup())
        return message.stop_propagation()

    if step == "batch1":
        ref = _extract_batch_ref(message)
        if not ref:
            await message.reply_text(bold("❌ Couldn't Read That. Forward It With Tag Or Send Its Link."))
            return message.stop_propagation()
        state["first"] = ref
        state["step"] = "batch2"
        await _show(
            state,
            bold("📦 Send The Last Message Of The New Batch.\n\n"
                 "Forward It With Tag, Or Send Its Link."),
            _cancel_markup(),
        )
        return message.stop_propagation()

    if step == "batch2":
        ref = _extract_batch_ref(message)
        if not ref:
            await message.reply_text(bold("❌ Couldn't Read That. Forward It With Tag Or Send Its Link."))
            return message.stop_propagation()
        first_chat, first_id = state["first"]
        last_chat, last_id = ref
        if str(first_chat) != str(last_chat):
            await message.reply_text(bold("❌ Chat IDs Do Not Match."))
            return message.stop_propagation()
        if first_id < 1 or last_id < first_id:
            await message.reply_text(bold("❌ The First Message Must Come Before The Last One."))
            return message.stop_propagation()
        cache_key = f"batch:{first_chat}:{first_id}:{last_id}"
        taken = await _link_taken(cache_key, state["doc"].get("permanent_url"))
        if taken:
            await message.reply_text(
                bold("❌ This Batch Already Has Its Own Link. Send A Different Range.\n\n")
                + f"<code>{html.escape(taken)}</code>"
            )
            return message.stop_propagation()
        state["step"] = "working"
        asyncio.create_task(_build_batch(bot, user.id, state, first_chat, first_id, last_id, cache_key))
        return message.stop_propagation()

    # choose / preview / working: nothing to read, swallow so nothing else reacts
    return message.stop_propagation()


async def _build_batch(bot, admin_id, state, chat_id, first_id, last_id, cache_key):
    """Same saving steps as /batch, but NO domain link and NO shortener link."""
    try:
        try:
            await bot.get_chat(chat_id)
        except ChannelInvalid:
            state["step"] = "batch1"
            return await _show(state, bold("❌ Channel Access Failed. Send The First Message Again."), _cancel_markup())
        status = state["prompt"]
        await _show(state, bold("⏳ Saving The Batch..."))
        outlist, originals = await fetch_batch_messages(bot, chat_id, first_id, last_id, status)
        if not outlist:
            state["step"] = "batch1"
            return await _show(state, bold("❌ No Messages Found In This Range. Send The First Message Again."),
                               _cancel_markup())
        source_id = (await bot.get_chat(chat_id)).id
        if int(source_id) != int(LOG_CHANNEL):
            outlist, _kept = await copy_batch_to_log(bot, outlist, originals, status)
        filename = f"changelink_{admin_id}.json"
        try:
            with open(filename, "w", encoding="utf-8") as out:
                json.dump(outlist, out)
            post = await bot.send_document(
                LOG_CHANNEL, filename, file_name="Batch.json",
                caption=bold("⚠️ Batch Generated For Filestore."),
            )
        finally:
            if os.path.exists(filename):
                os.remove(filename)
        share = f"{WEBSITE_URL.rstrip('/')}?start=BATCH-{encode_start(str(post.id))}"
        state["new"] = {"share": share, "label": f"Batch ({len(outlist)} Files)", "cache_key": cache_key}
        state["step"] = "preview"
        await _show(state, _preview_text(state), _confirm_markup())
    except Exception as e:
        print(f"CHANGE LINK BATCH ERROR: {type(e).__name__}: {e}")
        state["step"] = "batch1"
        await _show(state, bold("❌ Could Not Save The Batch. Send The First Message Again."), _cancel_markup())


# ---------------- confirm ----------------

async def _worker_update(key, destination):
    """Returns (short_url, error). The Worker keeps the shortener link."""
    if not PERMANENT_LINK_WORKER_URL or not PERMANENT_LINK_ADMIN_KEY:
        return None, "Worker URL or admin key is missing."
    url = f"{PERMANENT_LINK_WORKER_URL.rstrip('/')}/admin/update"
    headers = {"Content-Type": "application/json", "X-Admin-Key": PERMANENT_LINK_ADMIN_KEY}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"key": key, "destination": destination}, headers=headers) as r:
                body = await r.text()
                if r.status != 200:
                    return None, f"Worker replied HTTP {r.status}."
                data = json.loads(body)
                if not data.get("success"):
                    return None, str(data.get("error") or "Worker refused.")
                return data.get("shortUrl"), None
    except Exception as e:
        return None, f"{type(e).__name__}"


@Client.on_callback_query(filters.regex(r"^lnk_confirm$") & owner_only)
async def change_confirm(bot, query):
    admin_id = query.from_user.id
    state = _FLOW.get(admin_id)
    if not _fresh(state) or state["step"] != "preview":
        _FLOW.pop(admin_id, None)
        return await query.answer("Expired. Open Change Link again.", show_alert=True)
    state["step"] = "applying"        # a second tap finds nothing to do
    await query.answer("Changing...")
    await _show(state, bold("⏳ Changing The Link..."))

    new = state["new"]
    doc = state["doc"]
    short_url, error = await _worker_update(state["key"], new["share"])
    if error:
        state["step"] = "preview"
        return await _show(
            state,
            bold(f"❌ Could Not Change The Link.\n{html.escape(error)}\n\nNothing Was Changed."),
            _confirm_markup(),
        )

    now = datetime.now(timezone.utc)
    await links_col.delete_one({"_id": doc["_id"]})
    await links_col.update_one(
        {"_id": new["cache_key"]},
        {"$set": {"share_link": new["share"], "permanent_url": doc["permanent_url"], "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    _FLOW.pop(admin_id, None)

    lines = ["✅ BOT LINK CHANGED !!", ""]
    if short_url:
        lines += ["Edit This In Arolinks:", "", "Short Link:"]
        text = bold("\n".join(lines)) + f"\n<code>{html.escape(short_url)}</code>\n\n"
    else:
        text = bold("\n".join(lines + ["Edit The Short Link In Arolinks."])) + "\n\n"
    text += bold("Set The New URL To:") + f"\n<code>{html.escape(new['share'])}</code>"

    rows = []
    old_id = state["old"]["log_id"]
    if old_id:
        token = secrets.token_hex(4)
        _DONE[token] = {"admin": admin_id, "old_log_id": old_id}
        rows.append([InlineKeyboardButton("🗑 Delete Old File", callback_data=f"lnk_del:{token}")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="lnk_close")])
    await _show(state, text, InlineKeyboardMarkup(rows))


@Client.on_callback_query(filters.regex(r"^lnk_del:[0-9a-f]{8}$") & owner_only)
async def delete_old(bot, query):
    job = _DONE.pop(query.data.split(":", 1)[1], None)
    if not job or job["admin"] != query.from_user.id:
        return await query.answer("Expired.", show_alert=True)
    try:
        await bot.delete_messages(LOG_CHANNEL, job["old_log_id"])
        await query.answer("Old file deleted.", show_alert=True)
    except Exception as e:
        return await query.answer(f"Could not delete ({type(e).__name__}).", show_alert=True)
    # keep the message, drop only the delete button
    try:
        rows = [[b for b in row if not b.callback_data.startswith("lnk_del:")]
                for row in query.message.reply_markup.inline_keyboard]
        await query.message.edit_reply_markup(InlineKeyboardMarkup([r for r in rows if r]))
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^lnk_close$") & owner_only)
async def close_msg(bot, query):
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
