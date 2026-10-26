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
# Delete Link (Links -> Delete Link): send an old, UNUSED bot link (botserver?start=...). Its saved
# content is removed from the log channel so it stops working. A bot link that a domain link still
# uses is refused. Domain links and shortener links are never touched.
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
from Peter.utils import arolinks_edit
from plugins.genlink import (
    links_col, PERMANENT_LINK_WORKER_URL, PERMANENT_LINK_ADMIN_KEY,
    bold, encode_start, make_file_share_link, get_message_cache_key,
    get_media_unique_id, _extract_batch_ref, fetch_batch_messages,
    copy_batch_to_log, _file_info, _human_size, post_links_to_log,
    method_markup, start_manual, unused_col,
)

IDLE_SECONDS = 600

LIST_PAGE = 8
_LISTS = {}  # owner id -> {"kind": "empty"|"unused", "items": [...], "t": time}
_FLOW = {}   # owner id -> {"step", "t", "prompt", "doc", "old", "new"}
_DONE = {}   # token -> {"admin", "old_log_id", "label", "text"}  (what "Delete Old" removes)


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


def _item_label(message):
    """'1 File (name)' for media, '1 Message (text...)' for anything else."""
    name, _size = _file_info(message)
    kind = "File" if getattr(message, "media", None) else "Message"
    return f"1 {kind} ({name})" if name else f"1 {kind}"


async def _describe_old(bot, share_link):
    """Returns (label, log_id) for what the bot link gives now. log_id is the
    log-channel message that "Delete Old" removes: the stored message itself,
    or Batch.json for a batch (without it the old batch link stops working)."""
    if not share_link:
        return "Nothing (Deleted)", None
    param = _start_param(share_link)
    try:
        if param and param.startswith("BATCH-"):
            json_id = _b64(param[len("BATCH-"):])
            return "Batch", int(json_id) if json_id.isdigit() else None
        if param:
            kind, _, log_id = _b64(param).partition("_")
            if kind == "file" and log_id.isdigit():
                label = "1 File"
                try:
                    label = _item_label(await bot.get_messages(LOG_CHANNEL, int(log_id)))
                except Exception:
                    pass
                return label, int(log_id)
    except Exception:
        pass
    return "Unknown", None


LINKS_HEADER = "🔗 Links For The Message Above"


async def _delete_saved(bot, content_id):
    """Removes a saved message (or Batch.json) from the log channel together with the
    "Links For The Message Above" post that sits right under it (the post that replies
    to it). Returns how many messages were removed."""
    ids = [content_id]
    try:
        near = await bot.get_messages(LOG_CHANNEL, [content_id + 1, content_id + 2, content_id + 3])
        for m in near or []:
            if (m and not getattr(m, "empty", False)
                    and (getattr(m, "text", None) or "").startswith(LINKS_HEADER)
                    and getattr(m, "reply_to_message_id", None) == content_id):
                ids.append(m.id)
    except Exception as e:
        print(f"LINKS POST LOOKUP ERROR: {type(e).__name__}: {e}")
    await bot.delete_messages(LOG_CHANNEL, ids)
    return len(ids)


def _choose_view(label):
    return (
        bold(f"✅ Link Found\n\nCurrently Gives: {html.escape(label)}\n\nWhat Is The New Content?"),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Single Message", callback_data="lnk_pick:file"),
             InlineKeyboardButton("📦 Batch", callback_data="lnk_pick:batch")],
            [InlineKeyboardButton("🔗 Existing Bot Link", callback_data="lnk_pick:existing")],
            [InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel")],
        ]),
    )


async def _domain_doc(text):
    """(saved link doc, key) for a pasted domain link, or (None, None)."""
    m = re.search(r"https?://[^\s/]+/([A-Za-z0-9_-]+)/?\s*$", text or "")
    if not m:
        return None, None
    key = m.group(1)
    doc = await links_col.find_one({"permanent_url": {"$regex": f"/{re.escape(key)}/?$"}})
    return (doc, key) if doc else (None, None)


async def _exists(bot, log_id):
    try:
        saved = await bot.get_messages(LOG_CHANNEL, log_id)
    except Exception:
        return False
    return bool(saved) and not getattr(saved, "empty", False)


async def _existing_new(bot, state, link_text):
    """Checks an existing bot link and, if it can be used for this domain link, sets state["new"].
    Returns an error message (HTML) or None."""
    param = _start_param(link_text)
    if not param:
        return bold("❌ Send A Valid Bot Link.")
    if param == _start_param(state["doc"].get("share_link")):
        return bold("❌ This Bot Link Is Already Active On This Domain Link.")
    user_doc = await _link_in_use(param)
    if user_doc:
        return (bold("❌ This Bot Link Is Already Used By:\n\n")
                + f"<code>{html.escape(user_doc.get('permanent_url') or '-')}</code>")
    label, log_id = await _describe_old(bot, link_text)
    if not log_id or not await _exists(bot, log_id):
        return bold("❌ Nothing Is Saved For This Bot Link (Deleted Or Not Valid).")
    state["new"] = {
        "share": f"{WEBSITE_URL.rstrip('/')}?start={param}",
        "label": label,
        "cache_key": f"existing:{param}",
        "post_id": log_id,
        "info": [f"<b>🔗 Existing Bot Link:</b> <code>{html.escape(label)}</code>"],
    }
    return None


# ---------------- opening the flow ----------------

@Client.on_callback_query(filters.regex(r"^lnk_change$") & owner_only)
async def change_start(bot, query):
    await query.answer()
    prompt = await query.message.reply_text(
        bold("✏️ Change Link\n\nSend The Domain Link Of The Post You Want To Change."),
        reply_markup=_cancel_markup(),
    )
    _FLOW[query.from_user.id] = {
        "step": "link", "t": time.time(), "prompt": prompt,
        "doc": None, "old": None, "new": None,
    }


@Client.on_callback_query(filters.regex(r"^lnk_cancel$") & owner_only)
async def change_cancel(bot, query):
    _FLOW.pop(query.from_user.id, None)
    await query.answer("Cancelled.")
    try:
        await query.message.edit_text(bold("Cancelled."))
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^lnk_delete$") & owner_only)
async def delete_start(bot, query):
    await query.answer()
    prompt = await query.message.reply_text(
        bold("🗑 Delete Link\n\nSend The Bot Link You Want To Delete.\nOnly An Unused Link Can Be Deleted."),
        reply_markup=_cancel_markup(),
    )
    _FLOW[query.from_user.id] = {
        "step": "link", "t": time.time(), "prompt": prompt,
        "doc": None, "old": None, "new": None, "mode": "delete",
    }


@Client.on_callback_query(filters.regex(r"^lnk_pick:(file|batch|existing)$") & owner_only)
async def change_pick(bot, query):
    state = _FLOW.get(query.from_user.id)
    if not _fresh(state) or state["step"] != "choose":
        _FLOW.pop(query.from_user.id, None)
        return await query.answer("Expired. Open Change Link again.", show_alert=True)
    state["t"] = time.time()
    await query.answer()
    if query.data.endswith("existing"):
        state["step"] = "existing"
        await _show(state, bold("🔗 Send The Existing Bot Link."), _cancel_markup())
    elif query.data.endswith("file"):
        state["step"] = "file"
        await _show(state, bold("📄 Send The New Message."),
                    _cancel_markup())
    else:
        state["step"] = "method"
        await _show(state, bold("📦 Choose How To Make The New Batch"),
                    method_markup("lnk_bm:manual", "lnk_bm:range", "lnk_cancel"))


@Client.on_callback_query(filters.regex(r"^lnk_bm:(manual|range)$") & owner_only)
async def change_method(bot, query):
    state = _FLOW.get(query.from_user.id)
    if not _fresh(state) or state["step"] != "method":
        _FLOW.pop(query.from_user.id, None)
        return await query.answer("Expired. Open Change Link again.", show_alert=True)
    state["t"] = time.time()
    await query.answer()
    if query.data.endswith("range"):
        state["step"] = "batch1"
        return await _show(
            state,
            bold("📦 Send The First Message Of The New Batch.\n\n"
                 "Forward It With Tag, Or Send Its Link."),
            _cancel_markup(),
        )
    state["step"] = "manual"
    uid = query.from_user.id

    async def finish(b, admin_id, mstate, outlist, failed):
        await _finish_manual(b, admin_id, state, outlist, failed)

    await start_manual(query.from_user, state["prompt"], finish,
                       on_cancel=lambda: _FLOW.pop(uid, None))


# ---------------- collecting the messages ----------------
# group=-3: runs before every other handler, so while this flow is open nothing
# the owner sends here is treated as a normal file or command.

def _preview_text(state):
    return bold(
        "🔄 Change Link\n\n"
        f"Old: {html.escape(state['old']['label'])}\n"
        f"New: {html.escape(state['new']['label'])}"
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

    if step == "link" and state.get("mode") == "delete":
        param = _start_param(text)
        if not param:
            await message.reply_text(bold("❌ Send A Valid Bot Link."))
            return message.stop_propagation()
        user_doc = await _link_in_use(param)
        if user_doc:
            await message.reply_text(
                bold("❌ This Bot Link Is Still In Use By:\n\n")
                + f"<code>{html.escape(user_doc.get('permanent_url') or '-')}</code>\n\n"
                + bold("Use Change Link First, Then Delete The Old Bot Link.")
            )
            return message.stop_propagation()
        label, log_id = await _describe_old(bot, text)
        gone = True
        if log_id:
            try:
                saved = await bot.get_messages(LOG_CHANNEL, log_id)
                gone = (not saved) or bool(getattr(saved, "empty", False))
            except Exception:
                gone = True
        if not log_id or gone:
            await message.reply_text(bold("❌ Nothing Is Saved For This Bot Link (Already Deleted Or Not Valid)."))
            return message.stop_propagation()
        state.update(step="dconfirm", dparam=param, dlink=text, old={"label": label, "log_id": log_id})
        await _show(state, _delete_text(state), _delete_markup())
        return message.stop_propagation()

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
        text_, markup_ = _choose_view(label)
        await _show(state, text_, markup_)
        return message.stop_propagation()

    if step == "attach_domain":
        doc, key = await _domain_doc(text)
        if not doc:
            await message.reply_text(bold("❌ Send A Valid Domain Link That Is In The Database."))
            return message.stop_propagation()
        label, old_id = await _describe_old(bot, doc.get("share_link"))
        state.update(doc=doc, key=key, old={"label": label, "log_id": old_id})
        err = await _existing_new(bot, state, state["preset"])
        if err:
            await message.reply_text(err)
            return message.stop_propagation()
        state["step"] = "preview"
        await _show(state, _preview_text(state), _confirm_markup())
        return message.stop_propagation()

    if step == "free_link":
        doc, key = await _domain_doc(text)
        if not doc:
            await message.reply_text(bold("❌ Send A Valid Domain Link That Is In The Database."))
            return message.stop_propagation()
        label, log_id = await _describe_old(bot, doc.get("share_link"))
        if not log_id or not await _exists(bot, log_id):
            await message.reply_text(bold("❌ This Link Is Already Empty."))
            return message.stop_propagation()
        state.update(step="fconfirm", doc=doc, key=key, old={"label": label, "log_id": log_id})
        await _show(state, _free_text(state), _free_markup())
        return message.stop_propagation()

    if step == "existing":
        err = await _existing_new(bot, state, text)
        if err:
            await message.reply_text(err)
            return message.stop_propagation()
        state["step"] = "preview"
        await _show(state, _preview_text(state), _confirm_markup())
        return message.stop_propagation()

    if step == "file":
        state["step"] = "saving"          # an album's other items (or a double send) are ignored
        cache_key = get_message_cache_key(message)
        taken = await _link_taken(cache_key, state["doc"].get("permanent_url"))
        if taken:
            state["step"] = "file"
            await message.reply_text(
                bold("❌ This Message Already Has Its Own Link. Send A Different One.\n\n")
                + f"<code>{html.escape(taken)}</code>"
            )
            return message.stop_propagation()
        await _show(state, bold("⏳ Saving The Message..."))
        try:
            post = await copy_keep_cover(message, LOG_CHANNEL)
        except Exception as e:
            print(f"CHANGE LINK COPY ERROR: {type(e).__name__}: {e}")
            state["step"] = "file"
            await _show(state, bold("❌ Could Not Save That Message. Send Another One."), _cancel_markup())
            return message.stop_propagation()
        name, size = _file_info(message)
        icon = "🎬 File" if getattr(message, "media", None) else "💬 Message"
        info = [f"<b>{icon}:</b> <code>{html.escape(name or '-')}</code>"]
        if size:
            info.append(f"<b>📦 Size: {_human_size(size)}</b>")
        state["new"] = {
            "share": make_file_share_link(post.id),
            "label": _item_label(message),
            "cache_key": cache_key,
            "post_id": post.id,
            "info": info,
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
        source_chat = await bot.get_chat(chat_id)
        source_title = getattr(source_chat, "title", None) or str(source_chat.id)
        info = [
            f"<b>📦 Batch:</b> <code>{len(outlist)}</code> <b>files</b>",
            f"<b>📺 Source: {html.escape(source_title)} (</b><code>{source_chat.id}</code><b>), "
            f"messages {first_id} to {last_id}</b>",
        ]
        state["new"] = {"share": share, "label": f"Batch ({len(outlist)} Files)", "cache_key": cache_key,
                        "post_id": post.id, "info": info}
        state["step"] = "preview"
        await _show(state, _preview_text(state), _confirm_markup())
    except Exception as e:
        print(f"CHANGE LINK BATCH ERROR: {type(e).__name__}: {e}")
        state["step"] = "batch1"
        await _show(state, bold("❌ Could Not Save The Batch. Send The First Message Again."), _cancel_markup())


async def _finish_manual(bot, admin_id, state, outlist, failed):
    """Batch made by sending messages one by one: same Batch.json as /batch, but NO domain link
    and NO shortener link. Shows the preview like the range method."""
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
    info = [
        f"<b>📦 Batch:</b> <code>{len(outlist)}</code> <b>messages</b>",
        "<b>📺 Source: Sent One By One</b>",
    ]
    label = f"Batch ({len(outlist)} Files" + (f", {failed} Skipped" if failed else "") + ")"
    state["new"] = {"share": share, "label": label, "cache_key": f"batch:manual:{post.id}",
                    "post_id": post.id, "info": info}
    state["step"] = "preview"
    state["t"] = time.time()
    await _show(state, _preview_text(state), _confirm_markup())


# ---------------- lists: Empty Links / Unused Bot Links ----------------
# Both lists are built when opened (the log channel is checked then), shown 8 per page as buttons.

def _log_id_of(share_link):
    """The log-channel message a bot link delivers (the file, or Batch.json), or None."""
    param = _start_param(share_link)
    if not param:
        return None
    try:
        if param.startswith("BATCH-"):
            j = _b64(param[len("BATCH-"):])
            return int(j) if j.isdigit() else None
        kind, _, i = _b64(param).partition("_")
        return int(i) if kind == "file" and i.isdigit() else None
    except Exception:
        return None


async def _alive_ids(bot, ids):
    """Which of these log-channel messages still exist. A failed check raises (never 'all gone')."""
    alive, ids = set(), sorted(set(ids))
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        res = await bot.get_messages(LOG_CHANNEL, chunk)
        for m in res or []:
            if m and not getattr(m, "empty", False):
                alive.add(m.id)
    return alive


def _short(url):
    return re.sub(r"^https?://", "", url or "").rstrip("/")


async def _find_empty(bot):
    docs = await links_col.find({"permanent_url": {"$exists": True, "$ne": None}}).to_list(length=None)
    docs = [d for d in docs if d.get("permanent_url")]
    alive = await _alive_ids(bot, [i for i in (_log_id_of(d.get("share_link")) for d in docs) if i])
    out = []
    for d in docs:
        lid = _log_id_of(d.get("share_link"))
        if lid is None or lid not in alive:
            out.append(d)
    out.sort(key=lambda d: d["permanent_url"])
    return out


async def _find_unused(bot):
    recs = await unused_col.find({}).to_list(length=None)
    alive = await _alive_ids(bot, [r["log_id"] for r in recs if r.get("log_id")])
    out = []
    for r in recs:
        if r.get("log_id") not in alive or await _link_in_use(r["_id"]):
            await unused_col.delete_one({"_id": r["_id"]})      # gone, or now used by a domain link
            continue
        out.append(r)
    out.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc))
    return out


def _item_name(kind, item):
    return _short(item["permanent_url"]) if kind == "empty" else (item.get("label") or "Unknown")


def _list_text(kind, items):
    if kind == "empty":
        head, empty_msg = f"📭 Empty Links ({len(items)})", "No Empty Links."
    else:
        head, empty_msg = f"🤖 Unused Bot Links ({len(items)})", "No Unused Bot Links."
    return bold(f"{head}\n\n{empty_msg}" if not items else head)


def _list_markup(kind, items, page):
    start = page * LIST_PAGE
    rows = [[InlineKeyboardButton(_item_name(kind, items[i])[:60], callback_data=f"lnk_{kind}:i:{i}")]
            for i in range(start, min(len(items), start + LIST_PAGE))]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"lnk_{kind}:p:{page - 1}"))
    if start + LIST_PAGE < len(items):
        nav.append(InlineKeyboardButton("Next »", callback_data=f"lnk_{kind}:p:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Close", callback_data="lnk_close")])
    return InlineKeyboardMarkup(rows)


@Client.on_callback_query(filters.regex(r"^lnk_(empty|unused)$") & owner_only)
async def list_open(bot, query):
    kind = query.data.split("_", 1)[1]
    await query.answer("Checking...")
    try:
        items = await (_find_empty if kind == "empty" else _find_unused)(bot)
    except Exception as e:
        print(f"LINK LIST ERROR: {type(e).__name__}: {e}")
        return await query.message.reply_text(bold("❌ Could Not Check The Log Channel. Try Again."),
                                              reply_markup=_close_markup())
    _LISTS[query.from_user.id] = {"kind": kind, "items": items, "t": time.time()}
    await query.message.reply_text(_list_text(kind, items), reply_markup=_list_markup(kind, items, 0))


def _list_state(query, kind):
    st = _LISTS.get(query.from_user.id)
    if not st or st["kind"] != kind or time.time() - st["t"] > IDLE_SECONDS:
        return None
    st["t"] = time.time()
    return st


@Client.on_callback_query(filters.regex(r"^lnk_(empty|unused):(p|i):\d+$") & owner_only)
async def list_nav(bot, query):
    kind, what, n = query.data[len("lnk_"):].split(":")
    n = int(n)
    st = _list_state(query, kind)
    valid = (n * LIST_PAGE < max(len(st["items"]), 1)) if (st and what == "p") else bool(st)
    if not valid:
        return await query.answer("Expired. Open The List Again.", show_alert=True)
    await query.answer()
    items = st["items"]
    if what == "p":
        return await _edit(query, _list_text(kind, items), _list_markup(kind, items, n))
    if n >= len(items):
        return await query.answer("Expired. Open The List Again.", show_alert=True)
    item, back = items[n], InlineKeyboardButton("« Back", callback_data=f"lnk_{kind}:p:{n // LIST_PAGE}")
    if kind == "empty":
        await _edit(
            query,
            bold(f"📭 {_short(item['permanent_url'])}\n\nContent: Empty"),
            InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Reuse This Link", callback_data=f"lnk_reuse:{n}")], [back]]),
        )
    else:
        link = f"{WEBSITE_URL.rstrip('/')}?start={item['_id']}"
        await _edit(
            query,
            bold(f"🤖 {html.escape(item.get('label') or 'Unknown')}\n\nNot Used By Any Domain Link")
            + f"\n\n<code>{html.escape(link)}</code>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Attach To Domain Link", callback_data=f"lnk_uattach:{n}"),
                 InlineKeyboardButton("🗑 Delete", callback_data=f"lnk_udel:{n}")],
                [back],
            ]),
        )


@Client.on_callback_query(filters.regex(r"^lnk_reuse:\d+$") & owner_only)
async def reuse_empty(bot, query):
    st, n = _list_state(query, "empty"), int(query.data.split(":")[1])
    if not st or n >= len(st["items"]):
        return await query.answer("Expired. Open The List Again.", show_alert=True)
    await query.answer()
    doc = st["items"][n]
    key = doc["permanent_url"].rstrip("/").rsplit("/", 1)[-1]
    _FLOW[query.from_user.id] = {
        "step": "choose", "t": time.time(), "prompt": query.message, "doc": doc, "key": key,
        "old": {"label": "Empty", "log_id": None}, "new": None,
    }
    text_, markup_ = _choose_view("Empty")
    await _edit(query, text_, markup_)


@Client.on_callback_query(filters.regex(r"^lnk_uattach:\d+$") & owner_only)
async def unused_attach(bot, query):
    st, n = _list_state(query, "unused"), int(query.data.split(":")[1])
    if not st or n >= len(st["items"]):
        return await query.answer("Expired. Open The List Again.", show_alert=True)
    await query.answer()
    param = st["items"][n]["_id"]
    _FLOW[query.from_user.id] = {
        "step": "attach_domain", "t": time.time(), "prompt": query.message, "doc": None, "old": None,
        "new": None, "preset": f"{WEBSITE_URL.rstrip('/')}?start={param}",
    }
    await _edit(query, bold("🔗 Attach To Domain Link\n\nSend The Domain Link."), _cancel_markup())


@Client.on_callback_query(filters.regex(r"^lnk_udel:\d+$") & owner_only)
async def unused_delete(bot, query):
    st, n = _list_state(query, "unused"), int(query.data.split(":")[1])
    if not st or n >= len(st["items"]):
        return await query.answer("Expired. Open The List Again.", show_alert=True)
    await query.answer()
    param = st["items"][n]["_id"]
    link = f"{WEBSITE_URL.rstrip('/')}?start={param}"
    label, log_id = await _describe_old(bot, link)
    if not log_id:
        return await _edit(query, bold("❌ Nothing Is Saved For This Bot Link."), _close_markup())
    state = {"step": "dconfirm", "t": time.time(), "prompt": query.message, "doc": None, "new": None,
             "mode": "delete", "dparam": param, "dlink": link, "old": {"label": label, "log_id": log_id}}
    _FLOW[query.from_user.id] = state
    await _show(state, _delete_text(state), _delete_markup())


# ---------------- Free A Link ----------------
# Removes the content (and its links post) of a domain link and keeps the domain link and the
# shortener link, so the pair can be reused later (Empty Links -> Reuse This Link).

def _free_text(state):
    return (bold("🧹 Free This Link?\n\n")
            + f"<code>{html.escape(_short(state['doc']['permanent_url']))}</code>\n"
            + bold(f"Content: {state['old']['label']}\n\n"
                   "Content And Its Links Post Will Be Removed.\n"
                   "Domain Link And Shortener Stay For Reuse."))


def _free_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, Free It", callback_data="lnk_freeyes"),
        InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel"),
    ]])


@Client.on_callback_query(filters.regex(r"^lnk_free$") & owner_only)
async def free_start(bot, query):
    await query.answer()
    prompt = await query.message.reply_text(
        bold("🧹 Free A Link\n\nSend The Domain Link You Want To Free."),
        reply_markup=_cancel_markup(),
    )
    _FLOW[query.from_user.id] = {
        "step": "free_link", "t": time.time(), "prompt": prompt,
        "doc": None, "old": None, "new": None, "mode": "free",
    }


@Client.on_callback_query(filters.regex(r"^lnk_freeyes$") & owner_only)
async def free_confirm(bot, query):
    admin_id = query.from_user.id
    state = _FLOW.get(admin_id)
    if not _fresh(state) or state["step"] != "fconfirm":
        _FLOW.pop(admin_id, None)
        return await query.answer("Expired. Start Again.", show_alert=True)
    state["step"] = "freeing"
    await query.answer("Freeing...")
    try:
        await _delete_saved(bot, state["old"]["log_id"])
        result = bold("✅ Link Freed !!\n\nIt Is Now In Empty Links.")
    except Exception as e:
        print(f"FREE LINK ERROR: {type(e).__name__}: {e}")
        result = bold("❌ Could Not Free The Link.\nNothing Was Changed.")
    _FLOW.pop(admin_id, None)
    await _show(state, result, _close_markup())


# ---------------- delete link ----------------

async def _link_in_use(param):
    """The saved link (database) whose bot link has this start parameter, if any."""
    return await links_col.find_one({"share_link": {"$regex": f"[?&]start={re.escape(param)}$"}})


def _delete_text(state):
    return bold(
        "🗑 Delete This Bot Link?\n\n"
        f"Content: {html.escape(state['old']['label'])}\n"
        "Not Used By Any Domain Link"
    )


def _delete_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Delete", callback_data="lnk_delyes"),
        InlineKeyboardButton("❌ Cancel", callback_data="lnk_cancel"),
    ]])


@Client.on_callback_query(filters.regex(r"^lnk_delyes$") & owner_only)
async def delete_confirm(bot, query):
    admin_id = query.from_user.id
    state = _FLOW.get(admin_id)
    if not _fresh(state) or state["step"] != "dconfirm":
        _FLOW.pop(admin_id, None)
        return await query.answer("Expired. Open Delete Link again.", show_alert=True)
    state["step"] = "deleting"        # a second tap finds nothing to do
    await query.answer("Deleting...")
    # checked again: the link may have been put to use while this screen was open
    user_doc = await _link_in_use(state["dparam"])
    if user_doc:
        _FLOW.pop(admin_id, None)
        return await _show(state, bold("❌ This Bot Link Is In Use Now. Nothing Was Deleted."), _close_markup())
    await _show(state, bold("⏳ Deleting The Bot Link..."))
    try:
        removed = await _delete_saved(bot, state["old"]["log_id"])
        try:
            await unused_col.delete_one({"_id": state["dparam"]})
        except Exception:
            pass
        result = bold("✅ Bot Link Deleted !!")
    except Exception as e:
        print(f"DELETE LINK LOG ERROR: {type(e).__name__}: {e}")
        result = bold("❌ Could Not Delete The Saved Content.\nNothing Was Changed.")
    _FLOW.pop(admin_id, None)
    await _show(state, result, _close_markup())


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
    try:
        await unused_col.delete_one({"_id": _start_param(new["share"])})   # no longer "unused"
    except Exception:
        pass

    # Right under the new content in the log channel: its links (same post as /link and /batch make)
    await post_links_to_log(
        bot, new["post_id"], query.from_user, new["info"], doc["permanent_url"], new["share"],
    )

    # Arolinks has no API for this, so the bot edits the short link the way the
    # website does (see Peter/utils/arolinks_edit.py). If that is not possible
    # the manual steps are shown instead.
    alias = short_url.rstrip("/").rsplit("/", 1)[-1] if short_url else None
    auto, why = False, ""
    if alias and arolinks_edit.configured():
        await _show(state, bold("⏳ Updating Arolinks..."))
        auto, why = await arolinks_edit.set_destination(alias, new["share"])

    done = (bold("✅ Link Changed !!\n\n")
            + f"<code>{html.escape(doc['permanent_url'])}</code>\n"
            + bold(f"→ {new['label']}"))
    if auto:
        text = done
    else:
        # The short link could not be edited by the bot: show what to edit by hand.
        head = ""
        if alias and arolinks_edit.configured():
            head = f"⚠️ Arolinks Not Updated: {html.escape(why)}\n\n"
        text = done + "\n\n"
        if short_url:
            text += bold(head + "Edit This In Arolinks:") + f"\n<code>{html.escape(short_url)}</code>\n\n"
        else:
            text += bold(head + "Edit The Short Link In Arolinks.") + "\n\n"
        text += bold("Set The New URL To:") + f"\n<code>{html.escape(new['share'])}</code>"

    old_id = state["old"]["log_id"]
    if old_id:
        token = secrets.token_hex(4)
        _DONE[token] = {"admin": admin_id, "old_log_id": old_id,
                        "label": state["old"]["label"], "text": text,
                        "old_param": _start_param(doc.get("share_link"))}
        await _show(state, text, _choice_markup(token))
    else:
        await _show(state, text, _close_markup())


def _close_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Close", callback_data="lnk_close")]])


def _choice_markup(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Delete Old", callback_data=f"lnk_del:{token}"),
         InlineKeyboardButton("✅ Keep Old", callback_data=f"lnk_keep:{token}")],
        [InlineKeyboardButton("❌ Close", callback_data="lnk_close")],
    ])


def _job(query):
    job = _DONE.get(query.data.split(":", 1)[1])
    return job if job and job["admin"] == query.from_user.id else None


async def _edit(query, text, markup):
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


# "Delete Old" / "Keep Old" first ask again, so a wrong tap changes nothing.

@Client.on_callback_query(filters.regex(r"^lnk_del:[0-9a-f]{8}$") & owner_only)
async def delete_old_ask(bot, query):
    job = _job(query)
    if not job:
        return await query.answer("Expired.", show_alert=True)
    token = query.data.split(":", 1)[1]
    if job["label"].startswith("Batch"):
        what = "Old Batch List And Its Links Post\n(The Files Stay)"
    else:
        what = f"{html.escape(job['label'])} And Its Links Post"
    await query.answer()
    await _edit(
        query,
        bold(f"🗑 Delete Old Content?\n\n{what}"),
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Delete", callback_data=f"lnk_dely:{token}"),
                               InlineKeyboardButton("↩️ Back", callback_data=f"lnk_back:{token}")]]),
    )


@Client.on_callback_query(filters.regex(r"^lnk_keep:[0-9a-f]{8}$") & owner_only)
async def keep_old_ask(bot, query):
    job = _job(query)
    if not job:
        return await query.answer("Expired.", show_alert=True)
    token = query.data.split(":", 1)[1]
    await query.answer()
    await _edit(
        query,
        bold(f"✅ Keep Old Content?\n\n{html.escape(job['label'])}\nIts Old Bot Link Keeps Working"),
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Keep", callback_data=f"lnk_keepy:{token}"),
                               InlineKeyboardButton("↩️ Back", callback_data=f"lnk_back:{token}")]]),
    )


@Client.on_callback_query(filters.regex(r"^lnk_back:[0-9a-f]{8}$") & owner_only)
async def choice_back(bot, query):
    job = _job(query)
    if not job:
        return await query.answer("Expired.", show_alert=True)
    await query.answer()
    await _edit(query, job["text"], _choice_markup(query.data.split(":", 1)[1]))


@Client.on_callback_query(filters.regex(r"^lnk_dely:[0-9a-f]{8}$") & owner_only)
async def delete_old(bot, query):
    job = _job(query)
    if not job:
        return await query.answer("Expired.", show_alert=True)
    token = query.data.split(":", 1)[1]
    try:
        await _delete_saved(bot, job["old_log_id"])
    except Exception as e:
        await query.answer(f"Could not delete ({type(e).__name__}).", show_alert=True)
        return await _edit(query, job["text"], _choice_markup(token))
    _DONE.pop(token, None)
    await query.answer("Old content deleted.", show_alert=True)
    await _edit(query, bold("✅ Old Content Deleted !!"), _close_markup())


@Client.on_callback_query(filters.regex(r"^lnk_keepy:[0-9a-f]{8}$") & owner_only)
async def keep_old(bot, query):
    job = _job(query)
    if not job:
        return await query.answer("Expired.", show_alert=True)
    _DONE.pop(query.data.split(":", 1)[1], None)
    if job.get("old_param") and job.get("old_log_id"):
        try:
            await unused_col.update_one(
                {"_id": job["old_param"]},
                {"$set": {"label": job["label"], "log_id": job["old_log_id"], "created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception as e:
            print(f"UNUSED RECORD ERROR: {type(e).__name__}: {e}")
    await query.answer("Old content kept.")
    await _edit(query, bold("✅ Old Content Kept !!"), _close_markup())


@Client.on_callback_query(filters.regex(r"^lnk_close$") & owner_only)
async def close_msg(bot, query):
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
