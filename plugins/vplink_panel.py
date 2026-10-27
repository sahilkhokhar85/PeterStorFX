# Vplink Links panel (Links -> Vplink Links), owners only.
#
# A domain link (chillflizx.in/KEY) opens a shortener link, which opens a bot link.
# The Worker keeps, for every domain link, its bot link ("destination") and its
# shortener link ("shortUrl"). This panel looks at the shortener links on vplink.in:
#
#   In Use  - the link is attached to a domain link
#   Spare   - the link exists on vplink.in but no domain link uses it
#   Broken  - attached, but it opens the wrong bot link (or it is missing on vplink.in)
#
# "Move All To Vplink" gives every domain link that still uses another shortener a
# vplink link: a Spare link first (its destination is edited and checked on the
# website), a new link only when no Spare is left. The Worker record is changed only
# after the check passed, so a stop or a crash never leaves a broken domain link.
# Domain links are never deleted.
#
# Needs: bot variables VPLINK_USERNAME and VPLINK_PASSWORD, and in the Worker
# (Cloudflare) SHORTENER_API_URL=https://vplink.in and SHORTENER_API_TOKEN=<vplink api key>
# (only used to create NEW links) plus the worker_v4 code (/admin/list, /admin/set-short).

import asyncio
import html
import json
import time

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMINS
from Peter.utils import shortsite
from plugins.genlink import PERMANENT_LINK_WORKER_URL, PERMANENT_LINK_ADMIN_KEY, bold

HOST = "vplink.in"
DOMAIN = "https://chillflizx.in/"
PAGE = 8
IDLE_SECONDS = 600
PROGRESS_GAP = 3.0      # seconds between two progress edits (Telegram flood limit)
MAX_IN_A_ROW = 5        # this many failures one after another stop the move

_STATE = {}   # owner id -> {"t", "scan", "kind"}
_RUN = {}     # owner id -> {"stop": bool, "task": Task}


async def _owner(_, __, update):
    user = getattr(update, "from_user", None)
    return bool(user and user.id in ADMINS)

owner_only = filters.create(_owner)


# ---------------- Worker ----------------

async def _worker(path, payload):
    """Returns the Worker's JSON. Raises RuntimeError with a short reason."""
    if not PERMANENT_LINK_WORKER_URL or not PERMANENT_LINK_ADMIN_KEY:
        raise RuntimeError("Worker URL or admin key is missing")
    url = f"{PERMANENT_LINK_WORKER_URL.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json", "X-Admin-Key": PERMANENT_LINK_ADMIN_KEY}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            async with session.post(url, json=payload, headers=headers) as r:
                body = await r.text()
                if r.status == 404 and path in ("/admin/list", "/admin/set-short"):
                    raise RuntimeError("Worker Is Old (Upload worker_v4)")
                if r.status != 200:
                    raise RuntimeError(f"Worker HTTP {r.status}")
                data = json.loads(body)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Worker {type(e).__name__}")
    if not data.get("success"):
        raise RuntimeError(str(data.get("error") or "Worker refused"))
    return data


async def worker_records():
    """Every domain link: [{key, destination, shortUrl}]."""
    out, cursor = [], None
    while True:
        data = await _worker("/admin/list", {"cursor": cursor} if cursor else {})
        out += data.get("items") or []
        cursor = data.get("cursor")
        if not cursor:
            return out


# ---------------- scan ----------------

def classify(vp_links, records):
    """Pure function. Returns the lists the panel shows.

    in_use  - [{"link": vplink row, "rec": record}]
    spare   - [vplink row]            (no domain link uses it)
    broken  - [{"link": row|None, "rec": record, "why": "wrong"|"missing"}]
    pending - [record]                (domain links that do not use vplink yet)
    """
    by_alias = {l["alias"]: l for l in vp_links}
    used = {}
    pending = []
    for rec in records:
        if not rec.get("destination"):
            continue
        short = rec.get("shortUrl") or ""
        if shortsite.host_of(short) == HOST:
            used[short.rstrip("/").rsplit("/", 1)[-1]] = rec
        else:
            pending.append(rec)
    in_use, broken = [], []
    for alias, rec in used.items():
        link = by_alias.get(alias)
        if link is None:
            broken.append({"link": None, "rec": rec, "why": "missing"})
        elif link["dest"] != rec["destination"]:
            broken.append({"link": link, "rec": rec, "why": "wrong"})
        else:
            in_use.append({"link": link, "rec": rec})
    spare = [l for l in vp_links if l["alias"] not in used]
    return {"in_use": in_use, "spare": spare, "broken": broken, "pending": pending}


async def scan():
    async with shortsite.Session(HOST) as s:
        vp = await s.all_links()
    records = await worker_records()
    return classify(vp, records)


# ---------------- texts / buttons ----------------

def _domain(rec):
    return f"chillflizx.in/{rec['key']}"


def _menu_text(sc):
    return bold(
        "🔗 Vplink Links\n\n"
        f"✅ In Use: {len(sc['in_use'])}\n"
        f"🟡 Spare: {len(sc['spare'])}\n"
        f"❌ Broken: {len(sc['broken'])}\n\n"
        f"🚚 Still On Another Shortener: {len(sc['pending'])}"
    )


def _menu_markup(sc):
    rows = [[InlineKeyboardButton("✅ In Use", callback_data="vpl_l:in_use:0"),
             InlineKeyboardButton("🟡 Spare", callback_data="vpl_l:spare:0")],
            [InlineKeyboardButton("❌ Broken", callback_data="vpl_l:broken:0")]]
    if sc["pending"]:
        rows.append([InlineKeyboardButton(f"🚚 Move All To Vplink ({len(sc['pending'])})", callback_data="vpl_move")])
    rows.append([InlineKeyboardButton("« Back", callback_data="adm_links"),
                 InlineKeyboardButton("❌ Close", callback_data="lnk_close")])
    return InlineKeyboardMarkup(rows)


def _label(kind, item):
    if kind == "spare":
        return item["alias"]
    rec = item["rec"]
    alias = (rec.get("shortUrl") or "").rstrip("/").rsplit("/", 1)[-1]
    return f"{alias} → {rec['key']}"


_TITLES = {"in_use": "✅ In Use", "spare": "🟡 Spare", "broken": "❌ Broken"}


def _list_text(kind, items):
    if not items:
        return bold(f"{_TITLES[kind]}\n\nNothing Here.")
    return bold(f"{_TITLES[kind]}: {len(items)}\n\n👇 Tap A Link")


def _list_markup(kind, items, page):
    rows = []
    for i in range(page * PAGE, min(len(items), (page + 1) * PAGE)):
        rows.append([InlineKeyboardButton(_label(kind, items[i]), callback_data=f"vpl_i:{kind}:{i}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"vpl_l:{kind}:{page - 1}"))
    if (page + 1) * PAGE < len(items):
        nav.append(InlineKeyboardButton("Next ›", callback_data=f"vpl_l:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("« Back", callback_data="vpl_menu"),
                 InlineKeyboardButton("❌ Close", callback_data="lnk_close")])
    return InlineKeyboardMarkup(rows)


def _code(text):
    return f"<code>{html.escape(text or '')}</code>"


def _item_view(kind, item, n):
    back = InlineKeyboardButton("« Back", callback_data=f"vpl_l:{kind}:{n // PAGE}")
    if kind == "spare":
        text = (bold("🟡 Spare Link\n\nNo Domain Link Uses It.") + f"\n\n{_code(item['short'])}\n"
                + bold("Opens:") + f"\n{_code(item['dest'])}")
        rows = [[InlineKeyboardButton("🙈 Hide From List", callback_data=f"vpl_hide:{n}")], [back]]
    elif kind == "in_use":
        text = (bold("✅ In Use\n\nDomain Link:") + f"\n{_code(_domain(item['rec']))}\n"
                + bold("Vplink:") + f"\n{_code(item['link']['short'])}\n"
                + bold("Bot Link:") + f"\n{_code(item['rec']['destination'])}")
        rows = [[back]]
    else:
        why = "Opens The Wrong Bot Link" if item["why"] == "wrong" else "Missing On Vplink"
        text = (bold(f"❌ Broken\n\n{why}\n\nDomain Link:") + f"\n{_code(_domain(item['rec']))}\n"
                + bold("Vplink:") + f"\n{_code(item['rec'].get('shortUrl'))}\n"
                + bold("Should Open:") + f"\n{_code(item['rec']['destination'])}")
        if item["why"] == "wrong":
            text += "\n" + bold("Opens Now:") + f"\n{_code(item['link']['dest'])}"
        rows = [[InlineKeyboardButton("🔧 Fix This Link", callback_data=f"vpl_fix:{n}")], [back]]
    return text, InlineKeyboardMarkup(rows)


async def _edit(query, text, markup=None):
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


def _state(query):
    st = _STATE.get(query.from_user.id)
    if not st or time.time() - st["t"] > IDLE_SECONDS:
        return None
    st["t"] = time.time()
    return st


EXPIRED = "Expired. Open Vplink Links Again."


# ---------------- menu / lists ----------------

async def _open(query, fresh=True):
    st = _STATE.get(query.from_user.id)
    if fresh or not st:
        await _edit(query, bold("⏳ Checking Vplink..."))
        try:
            sc = await scan()
        except Exception as e:
            why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
            print(f"VPLINK SCAN ERROR: {type(e).__name__}: {e}")
            return await _edit(query, bold(f"❌ Could Not Check Vplink.\n{html.escape(why)}"),
                               InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="adm_links")]]))
        st = _STATE[query.from_user.id] = {"t": time.time(), "scan": sc}
    st["t"] = time.time()
    await _edit(query, _menu_text(st["scan"]), _menu_markup(st["scan"]))


@Client.on_callback_query(filters.regex(r"^vpl_open$") & owner_only)
async def vpl_open(bot, query):
    await query.answer()
    await _open(query)


@Client.on_callback_query(filters.regex(r"^vpl_menu$") & owner_only)
async def vpl_menu(bot, query):
    await query.answer()
    await _open(query, fresh=False)


@Client.on_callback_query(filters.regex(r"^vpl_l:(in_use|spare|broken):\d+$") & owner_only)
async def vpl_list(bot, query):
    _, kind, page = query.data.split(":")
    st, page = _state(query), int(page)
    if not st:
        return await query.answer(EXPIRED, show_alert=True)
    items = st["scan"][kind]
    if page * PAGE >= max(len(items), 1):
        page = 0
    await query.answer()
    await _edit(query, _list_text(kind, items), _list_markup(kind, items, page))


@Client.on_callback_query(filters.regex(r"^vpl_i:(in_use|spare|broken):\d+$") & owner_only)
async def vpl_item(bot, query):
    _, kind, n = query.data.split(":")
    st, n = _state(query), int(n)
    if not st or n >= len(st["scan"][kind]):
        return await query.answer(EXPIRED, show_alert=True)
    await query.answer()
    text, markup = _item_view(kind, st["scan"][kind][n], n)
    await _edit(query, text, markup)


# ---------------- hide / fix ----------------

@Client.on_callback_query(filters.regex(r"^vpl_hide:\d+$") & owner_only)
async def vpl_hide(bot, query):
    st, n = _state(query), int(query.data.split(":")[1])
    if not st or n >= len(st["scan"]["spare"]):
        return await query.answer(EXPIRED, show_alert=True)
    link = st["scan"]["spare"][n]
    await query.answer("Hiding...")
    try:
        async with shortsite.Session(HOST) as s:
            await s.hide(link["alias"])
    except Exception as e:
        why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        return await query.answer(f"Could not hide: {why}", show_alert=True)
    st["scan"]["spare"].pop(n)
    items = st["scan"]["spare"]
    await _edit(query, _list_text("spare", items), _list_markup("spare", items, min(n // PAGE, max(len(items) - 1, 0) // PAGE)))


@Client.on_callback_query(filters.regex(r"^vpl_fix:\d+$") & owner_only)
async def vpl_fix(bot, query):
    st, n = _state(query), int(query.data.split(":")[1])
    if not st or n >= len(st["scan"]["broken"]):
        return await query.answer(EXPIRED, show_alert=True)
    item = st["scan"]["broken"][n]
    rec = item["rec"]
    await query.answer("Fixing...")
    try:
        async with shortsite.Session(HOST) as s:
            if item["why"] == "wrong":
                await s.set_destination(item["link"]["alias"], rec["destination"])
            else:
                await _make_new(s, rec)
    except Exception as e:
        why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        return await query.answer(f"Could not fix: {why}", show_alert=True)
    st["scan"]["broken"].pop(n)
    items = st["scan"]["broken"]
    await _edit(query, _list_text("broken", items), _list_markup("broken", items, min(n // PAGE, max(len(items) - 1, 0) // PAGE)))


# ---------------- move all ----------------

async def _make_new(session, rec):
    """A new vplink link for this domain link (the Worker creates it and saves it)."""
    data = await _worker("/admin/shorten-key", {"key": rec["key"]})
    short = data.get("shortenedUrl") or ""
    if shortsite.host_of(short) != HOST:
        raise RuntimeError("Worker Still Uses The Old Shortener (Set SHORTENER_API_URL)")
    alias = short.rstrip("/").rsplit("/", 1)[-1]
    if await session.get_destination(alias) != rec["destination"]:
        raise RuntimeError("New Link Opens The Wrong Page")
    return short


async def _move_one(session, rec, spares):
    """Moves one domain link. Returns "spare" or "new". Raises RuntimeError on failure.
    A Spare that cannot be edited is skipped (dropped for this run) and the next one is tried."""
    while spares:
        link = spares[0]
        try:
            await session.set_destination(link["alias"], rec["destination"])      # edit + check on the site
        except Exception:
            spares.pop(0)
            continue
        data = await _worker("/admin/set-short", {"key": rec["key"], "shortUrl": link["short"]})
        if data.get("shortUrl") != link["short"]:
            raise RuntimeError("Worker Did Not Save The Link")
        spares.pop(0)
        return "spare"
    await _make_new(session, rec)
    return "new"


def _progress_text(total, done, used_spare, made_new, failed, spare_left):
    return bold(
        "🚚 Moving To Vplink\n\n"
        f"Total: {total}\n"
        f"Done: {done}\n"
        f"Spare Used: {used_spare}\n"
        f"New Created: {made_new}\n"
        f"Failed: {failed}\n"
        f"Spare Left: {spare_left}"
    )


def _stop_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Stop", callback_data="vpl_stop")]])


async def run_move(sc, session, on_progress, stop_flag):
    """Moves every pending domain link. Returns a result dict."""
    pending, spares = list(sc["pending"]), list(sc["spare"])
    res = {"total": len(pending), "done": 0, "spare": 0, "new": 0, "failed": [], "stopped": False, "aborted": False}
    in_a_row = 0
    last = 0.0
    for rec in pending:
        if stop_flag():
            res["stopped"] = True
            break
        try:
            how = await _move_one(session, rec, spares)
            res["done"] += 1
            res[how] += 1
            in_a_row = 0
        except Exception as e:
            why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
            res["failed"].append((rec["key"], why))
            in_a_row += 1
            if in_a_row >= MAX_IN_A_ROW:
                res["aborted"] = True
                break
        if time.time() - last >= PROGRESS_GAP:
            last = time.time()
            await on_progress(res, len(spares))
    return res


def _result_text(res):
    head = "⏹ Stopped" if res["stopped"] else ("⚠️ Stopped After Repeated Errors" if res["aborted"] else "✅ Move Finished")
    text = (f"{head}\n\nTotal: {res['total']}\nDone: {res['done']}\n"
            f"Spare Used: {res['spare']}\nNew Created: {res['new']}\nFailed: {len(res['failed'])}")
    out = bold(text)
    if res["failed"]:
        lines = [f"{html.escape(k)}: {html.escape(w)}" for k, w in res["failed"][:6]]
        out += "\n\n" + bold("Failed:") + "\n" + "\n".join(lines)
    if res["stopped"] or res["aborted"] or res["failed"]:
        out += "\n\n" + bold("Open Vplink Links And Tap Move All Again To Continue.")
    return out


@Client.on_callback_query(filters.regex(r"^vpl_move$") & owner_only)
async def vpl_move(bot, query):
    st = _state(query)
    if not st:
        return await query.answer(EXPIRED, show_alert=True)
    if _RUN.get(query.from_user.id):
        return await query.answer("A Move Is Already Running.", show_alert=True)
    sc = st["scan"]
    pending, spare = len(sc["pending"]), len(sc["spare"])
    await query.answer()
    await _edit(
        query,
        bold(f"🚚 Move {pending} Domain Links To Vplink?\n\n"
             f"🟡 Spare Links To Reuse: {min(spare, pending)}\n"
             f"🆕 New Links To Create: {max(pending - spare, 0)}\n\n"
             "Domain Links Never Change."),
        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Start", callback_data="vpl_go"),
                               InlineKeyboardButton("❌ Cancel", callback_data="vpl_menu")]]),
    )


@Client.on_callback_query(filters.regex(r"^vpl_go$") & owner_only)
async def vpl_go(bot, query):
    admin = query.from_user.id
    st = _state(query)
    if not st:
        return await query.answer(EXPIRED, show_alert=True)
    if _RUN.get(admin):
        return await query.answer("A Move Is Already Running.", show_alert=True)
    sc = st["scan"]
    if not sc["pending"]:
        return await query.answer("Nothing To Move.", show_alert=True)
    run = _RUN[admin] = {"stop": False}
    await query.answer("Started")
    total = len(sc["pending"])
    await _edit(query, _progress_text(total, 0, 0, 0, 0, len(sc["spare"])), _stop_markup())

    async def on_progress(res, spare_left):
        await _edit(query, _progress_text(res["total"], res["done"], res["spare"], res["new"],
                                          len(res["failed"]), spare_left), _stop_markup())

    async def job():
        res = None
        try:
            async with shortsite.Session(HOST) as session:
                res = await run_move(sc, session, on_progress, lambda: run["stop"])
        except Exception as e:
            why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
            print(f"VPLINK MOVE ERROR: {type(e).__name__}: {e}")
            await _edit(query, bold(f"❌ Move Could Not Start.\n{html.escape(why)}"),
                        InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="vpl_menu")]]))
        finally:
            _RUN.pop(admin, None)
            _STATE.pop(admin, None)      # the numbers changed: scan again next time
        if res is not None:
            await _edit(query, _result_text(res),
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Vplink Links", callback_data="vpl_open"),
                                               InlineKeyboardButton("❌ Close", callback_data="lnk_close")]]))

    run["task"] = asyncio.create_task(job())


@Client.on_callback_query(filters.regex(r"^vpl_stop$") & owner_only)
async def vpl_stop(bot, query):
    run = _RUN.get(query.from_user.id)
    if not run:
        return await query.answer("Nothing Is Running.", show_alert=True)
    run["stop"] = True
    await query.answer("Stopping After This Link...")
