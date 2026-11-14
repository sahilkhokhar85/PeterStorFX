"""Premium plans paid with FamPay (UPI), verified automatically from the FamApp emails in Gmail.

How it works
  1. /premium shows the plans. A plan button creates an order and sends a UPI QR for that exact amount.
  2. The user pays from any UPI app. FamApp mails "You received ₹X in your FamX account" to the Gmail
     below (IMAP). The poller matches that mail to the order by amount + time and checks the UTR was never used.
  3. The order is marked paid, premium is added (or extended) in MongoDB and the owner gets a DM.
Only ONE pending order per amount exists at a time (prices are whole rupees), so two payments can never be mixed.

Environment (all bots share the same ones):
  FAM_UPI_ID, FAM_PAYEE_NAME (optional), IMAP_USERNAME, IMAP_APP_PASSWORD,
  IMAP_HOST / IMAP_PORT / IMAP_SENDER_FILTER (optional), PAY_EXPIRY_MINUTES (optional, default 15)
"""
import asyncio
import datetime
import email
import imaplib
import io
import logging
import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from email import policy
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

import pytz
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, filters
from pyrogram.raw import functions, types as rtypes
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMINS, BOT_ID
from plugins.dbusers import db

logger = logging.getLogger(__name__)

UPI_ID = os.environ.get("FAM_UPI_ID", "").strip()
PAYEE = os.environ.get("FAM_PAYEE_NAME", "ChillFlizX").strip() or "ChillFlizX"
IMAP_USER = os.environ.get("IMAP_USERNAME", "").strip()
IMAP_PASS = os.environ.get("IMAP_APP_PASSWORD", "").replace(" ", "").strip()
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com").strip()
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or 993)
SENDER = os.environ.get("IMAP_SENDER_FILTER", "no-reply@famapp.in").strip()
EXPIRY_MIN = int(os.environ.get("PAY_EXPIRY_MINUTES", "15") or 15)
POLL_SECONDS = 5
LOOKBACK_HOURS = 24
IST = pytz.timezone("Asia/Kolkata")

# Owner-only test plan (₹1, 1 day): shown only to the ADMINS, never to users
TEST_PLAN = {"_id": "test", "name": "Test (1 Day)", "days": 1, "price": 1, "stars": 1}

# Telegram Stars price per plan (for users outside India); a plan document can override it with its own "stars" field
STARS_DEFAULT = {"30": 50, "90": 130, "365": 400}

# first start only; edit them later from the admin panel (step 2) or directly in MongoDB
DEFAULT_PLANS = [
    {"_id": "30", "name": "30 Days", "days": 30, "price": 49},
    {"_id": "90", "name": "90 Days", "days": 90, "price": 129},
    {"_id": "365", "name": "365 Days", "days": 365, "price": 399},
]


def enabled():
    return bool(UPI_ID and IMAP_USER and IMAP_PASS)


def _now():
    return datetime.datetime.utcnow()


def _plans_col():
    return db.db.premium_plans


def _users_col():
    return db.db.premium_users


def _orders_col():
    return db.db.premium_orders


def _utr_col():
    return db.db.premium_utrs


async def get_plans():
    docs = await _plans_col().find({}).sort("days", 1).to_list(length=20)
    if not docs:
        try:
            await _plans_col().insert_many(DEFAULT_PLANS)
        except Exception:
            pass
        docs = await _plans_col().find({}).sort("days", 1).to_list(length=20)
    return docs


async def premium_until(user_id):
    """Expiry (naive UTC) of the user's premium, or None when not premium (used by the next steps too)."""
    doc = await _users_col().find_one({"_id": int(user_id)})
    exp = (doc or {}).get("expires")
    return exp if exp and exp > _now() else None


async def is_premium(user_id):
    return await premium_until(user_id) is not None


def _fmt_date(dt):
    return pytz.utc.localize(dt).astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _rupees(x):
    x = Decimal(str(x))
    return f"{int(x)}" if x == x.to_integral() else f"{x:.2f}"


# ---------------------------------------------------------------- Gmail (IMAP)
_IN_SUBJECT = re.compile(r"you received ₹.* in your famx account", re.I)
_IN_BODY = re.compile(r"you have successfully received", re.I)
_OUT = re.compile(r"your payment of ₹.* is successful|you have successfully paid", re.I)
_AMOUNT = re.compile(r"₹\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
_UTR = re.compile(r"UTR\s*:?\s*([0-9]{9,})", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9]{8,40}")
_FROM = re.compile(r"received\s*₹\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?\s*from\s+([^\r\n<]+)", re.I)


def _text_of(msg):
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                txt = part.get_content()
            except Exception:
                txt = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            if part.get_content_type() == "text/html":
                txt = re.sub(r"<[^>]+>", " ", str(txt))
            parts.append(str(txt))
    return re.sub(r"[ \t ]+", " ", "\n".join(parts))


_SEEN = {}   # mail number -> parsed payment (or None): a mail is downloaded only once


def _fetch_payments():
    """Blocking: the recent incoming FamX payments [{utr, amount, ts (naive UTC), name}] from Gmail."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(hours=LOOKBACK_HOURS)).strftime("%d-%b-%Y")
    cli = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    out = []
    try:
        cli.login(IMAP_USER, IMAP_PASS)
        cli.select("INBOX", readonly=True)
        status, data = cli.search(None, "FROM", SENDER, "SINCE", since)
        ids = data[0].split() if status == "OK" and data and data[0] else []
        for num in ids[-80:]:
            if num in _SEEN:
                if _SEEN[num]:
                    out.append(_SEEN[num])
                continue
            st, fetched = cli.fetch(num, "(BODY.PEEK[])")
            if st != "OK":
                continue
            raw = next((p[1] for p in fetched if isinstance(p, tuple) and len(p) >= 2), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            subject = str(msg.get("Subject", ""))
            body = _text_of(msg)
            if _OUT.search(subject) or _OUT.search(body):
                _SEEN[num] = None
                continue
            if not (_IN_SUBJECT.search(subject) and _IN_BODY.search(body)):
                _SEEN[num] = None
                continue
            m = _AMOUNT.search(subject) or _AMOUNT.search(body)
            u = _UTR.search(body)
            if not m or not u:
                continue
            try:
                amount = Decimal(m.group(1).replace(",", ""))
                ts = parsedate_to_datetime(str(msg.get("Date")))
                ts = ts.astimezone(pytz.utc).replace(tzinfo=None) if ts.tzinfo else ts
            except (InvalidOperation, ValueError, TypeError):
                continue
            who = _FROM.search(body)
            toks = {t.upper() for t in _TOKEN.findall(body) if re.search(r"[0-9]", t)}
            toks.add(u.group(1).upper())
            pay = {"utr": u.group(1), "amount": amount, "ts": ts,
                   "name": (who.group(1).strip() if who else ""), "tokens": toks}
            _SEEN[num] = pay
            out.append(pay)
    finally:
        try:
            cli.logout()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- QR
def _upi_uri(amount, order):
    return "upi://pay?" + urlencode(
        {"pa": UPI_ID, "pn": PAYEE, "am": _rupees(amount), "cu": "INR", "tn": f"PREM{order}"}, quote_via=quote)


def _qr_png(amount, order):
    """The payment card (Peter/assets/pay_template.jpg with this order's QR); a plain QR if that fails."""
    uri = _upi_uri(amount, order)
    try:
        from Peter.utils.payqr import make_card
        return make_card(uri, f"Pay ₹{_rupees(amount)}")
    except Exception as e:
        logger.warning(f"[PREMIUM] card failed, sending a plain QR: {type(e).__name__}: {e}")
    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(uri)
    qr.make(fit=True)
    bio = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(bio, format="PNG")
    bio.seek(0)
    bio.name = "pay.png"
    return bio


# ---------------------------------------------------------------- order texts
def _order_caption(plan_name, amount):
    return (
        f"<b>💎 Premium — {plan_name}</b>\n\n"
        f"<b>💰 Amount: ₹{_rupees(amount)}</b>\n"
        f"<b>⏳ Valid For: {EXPIRY_MIN} Minutes</b>\n\n"
        f"<b>Scan The QR And Pay Exactly ₹{_rupees(amount)}.</b>\n"
        "<b>Premium Starts Automatically After The Payment.</b>"
    )


def _order_markup(oid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Check Payment", callback_data=f"prem_check:{oid}")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data=f"prem_cancel:{oid}")],
    ])


async def _plan_markup(user_id=None):
    plans = await get_plans()
    if user_id in ADMINS:
        plans = plans + [TEST_PLAN]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💎 {p['name']} — ₹{_rupees(p['price'])}", callback_data=f"prem_plan:{p['_id']}")]
        for p in plans
    ])


async def _home_text(user_id):
    until = await premium_until(user_id)
    status = f"<b>✅ Premium Active Till {_fmt_date(until)}</b>" if until else "<b>You Are Not A Premium Member.</b>"
    return f"<b>💎 Premium</b>\n\n{status}\n\n<b>Choose A Plan 👇</b>"


# ---------------------------------------------------------------- admin panel
_ADM_WAIT = {}      # admin id -> "grant" | "remove" | "check"  (the next text message is the answer)
_ADM_PAGE = 10


def _short_date(dt):
    return pytz.utc.localize(dt).astimezone(IST).strftime("%d %b %Y")


def _adm_home_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Grant Premium", callback_data="padm:grant"),
         InlineKeyboardButton("➖ Remove Premium", callback_data="padm:remove")],
        [InlineKeyboardButton("📋 Premium List", callback_data="padm:list:0"),
         InlineKeyboardButton("🔍 Check User", callback_data="padm:check")],
        [InlineKeyboardButton("🔒 Close", callback_data="padm:close")],
    ])


def _adm_cancel_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="padm:home")]])


def _adm_panel_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Admin Panel", callback_data="padm:home")]])


async def _adm_home_text():
    active = await _users_col().count_documents({"expires": {"$gt": _now()}})
    total = await _users_col().count_documents({})
    return (f"<b>💎 Premium Admin !!</b>\n\n"
            f"<b>👥 Active Users: {active}</b>\n"
            f"<b>📦 Total Ever: {total}</b>")


async def _adm_list(page):
    now = _now()
    q = {"expires": {"$gt": now}}
    total = await _users_col().count_documents(q)
    pages = max(1, (total + _ADM_PAGE - 1) // _ADM_PAGE)
    page = min(max(page, 0), pages - 1)
    docs = await _users_col().find(q).sort("expires", 1).skip(page * _ADM_PAGE).limit(_ADM_PAGE).to_list(length=_ADM_PAGE)
    lines = [f"<b>{page * _ADM_PAGE + i}.</b> <code>{d['_id']}</code> · {d.get('plan') or '-'} · {_short_date(d['expires'])}"
             for i, d in enumerate(docs, 1)]
    text = f"<b>📋 Active Premium ({page + 1}/{pages}) !!</b>\n\n" + ("\n".join(lines) if lines else "<b>No Active Premium Users !!</b>")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"padm:list:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"padm:list:{page + 1}"))
    rows = ([nav] if nav else []) + [[InlineKeyboardButton("🔙 Back", callback_data="padm:home")]]
    return text, InlineKeyboardMarkup(rows)


_RM_PAGE = 8


async def _adm_rm_list(bot, page):
    """Remove Premium: the active users as buttons (no typing the ID)."""
    q = {"expires": {"$gt": _now()}}
    total = await _users_col().count_documents(q)
    pages = max(1, (total + _RM_PAGE - 1) // _RM_PAGE)
    page = min(max(page, 0), pages - 1)
    docs = await _users_col().find(q).sort("expires", 1).skip(page * _RM_PAGE).limit(_RM_PAGE).to_list(length=_RM_PAGE)
    names = {}
    if docs:
        try:
            for u in await bot.get_users([d["_id"] for d in docs]):
                names[u.id] = (u.first_name or "").strip()
        except Exception:
            pass
    rows = []
    for d in docs:
        who = (names.get(d["_id"]) or str(d["_id"]))[:16]
        rows.append([InlineKeyboardButton(f"👤 {who} · {_short_date(d['expires'])}", callback_data=f"padm:rmsel:{d['_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"padm:rml:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"padm:rml:{page + 1}"))
    rows += ([nav] if nav else []) + [[InlineKeyboardButton("🔙 Back", callback_data="padm:home")]]
    head = f"<b>➖ Remove Premium ({page + 1}/{pages}) !!</b>\n\n"
    body = "<b>Tap A User To Remove.</b>" if docs else "<b>No Active Premium Users !!</b>"
    return head + body, InlineKeyboardMarkup(rows)


@Client.on_message(filters.command("padmin") & filters.private)
async def padmin_cmd(bot, message):
    if not message.from_user or message.from_user.id not in ADMINS:
        return
    _ADM_WAIT.pop(message.from_user.id, None)
    await message.reply_text(await _adm_home_text(), reply_markup=_adm_home_markup())


@Client.on_callback_query(filters.regex(r"^padm:(home|grant|remove|check|close|list:[0-9]{1,4}|rmyes:[0-9]{5,15}|rml:[0-9]{1,4}|rmsel:[0-9]{5,15}|gryes:[0-9]{5,15}:[0-9]{1,4})$"))
async def adm_cb(bot, query):
    uid = query.from_user.id
    if uid not in ADMINS:
        return await query.answer("Admins Only !!", show_alert=True)
    act = query.data.split(":", 1)[1]
    if act == "close":
        _ADM_WAIT.pop(uid, None)
        await query.answer()
        try:
            return await query.message.delete()
        except Exception:
            return
    if act == "home":
        _ADM_WAIT.pop(uid, None)
        await query.answer()
        return await query.message.edit_text(await _adm_home_text(), reply_markup=_adm_home_markup())
    if act in ("grant", "check"):
        _ADM_WAIT[uid] = act
        await query.answer()
        prompt = {
            "grant": "<b>➕ Grant Premium !!</b>\n\n<b>Send Telegram ID And Days.</b>\n<b>Example:</b> <code>123456789 30</code>",
            "check": "<b>🔍 Check User !!</b>\n\n<b>Send The Telegram ID.</b>",
        }[act]
        return await query.message.edit_text(prompt, reply_markup=_adm_cancel_markup())
    if act == "remove" or act.startswith("rml:"):
        _ADM_WAIT.pop(uid, None)
        await query.answer()
        text, markup = await _adm_rm_list(bot, int(act.split(":")[1]) if ":" in act else 0)
        return await query.message.edit_text(text, reply_markup=markup)
    if act.startswith("rmsel:"):
        tid = int(act.split(":")[1])
        await query.answer()
        until = await premium_until(tid)
        if not until:
            return await query.message.edit_text("<b>User Is Not Premium !!</b>", reply_markup=_adm_panel_button())
        doc = await _users_col().find_one({"_id": tid}) or {}
        return await query.message.edit_text(
            f"<b>➖ Remove Premium !!\n\n👤 User:</b> <code>{tid}</code>\n"
            f"<b>📦 Plan: {doc.get('plan') or '-'}\n📅 Till: {_fmt_date(until)}</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, Remove", callback_data=f"padm:rmyes:{tid}"),
                InlineKeyboardButton("❌ Cancel", callback_data="padm:rml:0")]]))
    if act.startswith("list:"):
        await query.answer()
        text, markup = await _adm_list(int(act.split(":")[1]))
        return await query.message.edit_text(text, reply_markup=markup)
    if act.startswith("gryes:"):
        _, tid, days = act.split(":")
        tid, days = int(tid), int(days)
        if not 1 <= days <= 3650:
            return await query.answer("Wrong Days.", show_alert=True)
        await query.answer()
        now = _now()
        doc = await _users_col().find_one({"_id": tid})
        base = doc["expires"] if doc and doc.get("expires") and doc["expires"] > now else now
        until = base + datetime.timedelta(days=days)
        await _users_col().update_one(
            {"_id": tid},
            {"$set": {"expires": until, "plan": "Admin Grant", "granted_by": uid, "granted": now},
             "$setOnInsert": {"since": now}},
            upsert=True)
        out = (f"<b>✅ Premium Granted !!\n\n👤 User:</b> <code>{tid}</code>\n"
               f"<b>📅 Days: +{days}\n📅 Active Till: {_fmt_date(until)}</b>")
        try:
            await bot.send_message(tid, f"<b>🎁 Premium Activated !!\n\n💎 +{days} Days Added\n"
                                        f"📅 Active Till: {_fmt_date(until)}</b>")
        except Exception:
            out += "\n\n<b>⚠️ Could Not Message The User !!</b>"
        return await query.message.edit_text(out, reply_markup=_adm_panel_button())
    if act.startswith("rmyes:"):
        tid = int(act.split(":")[1])
        await query.answer()
        until = await premium_until(tid)
        if not until:
            return await query.message.edit_text("<b>User Is Not Premium !!</b>", reply_markup=_adm_panel_button())
        await _users_col().update_one({"_id": tid}, {"$set": {"expires": _now()}})
        return await query.message.edit_text(
            f"<b>✅ Premium Removed !!</b>\n\n<b>👤 User:</b> <code>{tid}</code>", reply_markup=_adm_panel_button())


def _adm_waiting(_, __, m):
    u = m.from_user
    return bool(u and u.id in ADMINS and u.id in _ADM_WAIT and m.text and not m.text.startswith("/"))


@Client.on_message(filters.private & filters.create(_adm_waiting), group=-6)
async def adm_input(bot, message):
    try:
        await _adm_input(bot, message)
    finally:
        message.stop_propagation()      # raises, so it has to come after the work


async def _adm_input(bot, message):
    uid = message.from_user.id
    mode = _ADM_WAIT.get(uid)
    text = (message.text or "").strip()
    if mode == "grant":
        m = re.fullmatch(r"([0-9]{5,15})\s+([0-9]{1,4})", text)
        if not m or not 1 <= int(m.group(2)) <= 3650:
            return await message.reply_text(
                "<b>Wrong Format !!</b>\n<b>Send Like:</b> <code>123456789 30</code>", reply_markup=_adm_cancel_markup())
        _ADM_WAIT.pop(uid, None)
        tid, days = int(m.group(1)), int(m.group(2))
        now = _now()
        doc = await _users_col().find_one({"_id": tid})
        base = doc["expires"] if doc and doc.get("expires") and doc["expires"] > now else now
        until = base + datetime.timedelta(days=days)
        return await message.reply_text(
            f"<b>➕ Grant Premium !!\n\n👤 User:</b> <code>{tid}</code>\n"
            f"<b>📅 Days: +{days}\n📅 Active Till: {_fmt_date(until)}</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, Grant", callback_data=f"padm:gryes:{tid}:{days}"),
                InlineKeyboardButton("❌ Cancel", callback_data="padm:home")]]))
    if mode in ("remove", "check"):
        if not re.fullmatch(r"[0-9]{5,15}", text):
            return await message.reply_text("<b>Send Only The Telegram ID !!</b>", reply_markup=_adm_cancel_markup())
        _ADM_WAIT.pop(uid, None)
        tid = int(text)
        doc = await _users_col().find_one({"_id": tid})
        until = await premium_until(tid)
        if mode == "remove":
            if not until:
                return await message.reply_text("<b>User Is Not Premium !!</b>", reply_markup=_adm_panel_button())
            return await message.reply_text(
                f"<b>➖ Remove Premium !!</b>\n\n<b>👤 User:</b> <code>{tid}</code>\n"
                f"<b>📦 Plan: {doc.get('plan') or '-'}</b>\n<b>📅 Till: {_fmt_date(until)}</b>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, Remove", callback_data=f"padm:rmyes:{tid}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="padm:home")]]))
        if not doc:
            return await message.reply_text("<b>User Never Had Premium !!</b>", reply_markup=_adm_panel_button())
        paid = await _orders_col().count_documents({"user": tid, "status": "paid"})
        status = f"✅ Active Till {_fmt_date(until)}" if until else "❌ Not Active"
        last = _fmt_date(doc["last_paid"]) if doc.get("last_paid") else "-"
        return await message.reply_text(
            f"<b>🔍 User Info !!</b>\n\n<b>👤 User:</b> <code>{tid}</code>\n<b>💎 Status: {status}</b>\n"
            f"<b>📦 Plan: {doc.get('plan') or '-'}</b>\n<b>💳 Last Paid: {last}</b>\n"
            f"<b>🧾 Paid Orders: {paid}</b>", reply_markup=_adm_panel_button())


# ---------------------------------------------------------------- Telegram Stars (users outside India)
def _stars_for(plan):
    n = plan.get("stars") or STARS_DEFAULT.get(str(plan["_id"]))
    return int(n) if n else max(1, int(plan["price"]))


async def _plan_by_id(plan_id, uid):
    if plan_id == "test":
        return TEST_PLAN if uid in ADMINS else None
    return await _plans_col().find_one({"_id": plan_id})


def _stars_media(plan, uid):
    n = _stars_for(plan)
    media = rtypes.InputMediaInvoice(
        title=f"Premium — {plan['name']}"[:32],
        description=f"No-Ads Premium For {plan['days']} Days."[:255],
        invoice=rtypes.Invoice(currency="XTR", prices=[rtypes.LabeledPrice(label=str(plan["name"])[:30], amount=n)]),
        payload=f"ps:{uid}:{plan['_id']}".encode(),
        provider_data=rtypes.DataJSON(data="{}"),
    )
    return media, n


async def app_stars_link(bot, uid, plan_id):
    """({"link", "stars"}, None) or (None, error code) - the invoice the Mini App opens with openInvoice."""
    if not enabled():
        return None, "disabled"
    plan = await _plan_by_id(plan_id, uid)
    if not plan:
        return None, "plan"
    media, n = _stars_media(plan, uid)
    try:
        res = await bot.invoke(functions.payments.ExportInvoice(invoice_media=media))
    except Exception as e:
        logger.warning(f"[PREMIUM] stars link failed: {type(e).__name__}: {e}")
        return None, "stars"
    return {"link": res.url, "stars": n}, None


async def _stars_precheck(bot, upd):
    ok = False
    try:
        kind, uid, pid = upd.payload.decode().split(":", 2)
        plan = await _plan_by_id(pid, int(uid))
        ok = bool(kind == "ps" and plan and int(uid) == upd.user_id and upd.currency == "XTR"
                  and int(upd.total_amount) == _stars_for(plan))
    except Exception:
        ok = False
    if ok:
        await bot.invoke(functions.messages.SetBotPrecheckoutResults(query_id=upd.query_id, success=True))
    else:
        await bot.invoke(functions.messages.SetBotPrecheckoutResults(
            query_id=upd.query_id, error="This Payment Is No Longer Valid. Please Try Again."))


async def _stars_failed(bot, uid, charge, why):
    for admin in ADMINS:
        if isinstance(admin, int):
            try:
                await bot.send_message(
                    admin, f"<b>⚠️ Stars Payment Received But Premium Not Activated !!</b>\n\n"
                           f"<b>👤 User:</b> <code>{uid}</code>\n<b>🧾 Charge ID:</b> <code>{charge}</code>\n"
                           f"<b>❗ Reason: {why}</b>")
            except Exception:
                pass


async def _stars_paid(bot, act):
    charge = act.charge.id
    try:
        await db.db.premium_stars.insert_one({"_id": charge, "ts": _now()})
    except DuplicateKeyError:
        return                                   # this payment was already handled
    uid = 0
    try:
        kind, uid, pid = act.payload.decode().split(":", 2)
        uid = int(uid)
        plan = await _plan_by_id(pid, uid)
        if kind != "ps" or not plan:
            return await _stars_failed(bot, uid, charge, "Plan Not Found")
        now = _now()
        order = {"_id": secrets.token_hex(4), "user": uid, "bot_id": BOT_ID, "plan": plan["_id"],
                 "plan_name": plan["name"], "days": plan["days"], "amount": str(plan["price"]),
                 "status": "paid", "via": "stars", "stars": int(act.total_amount), "created": now,
                 "expires": now, "paid": now}
        await _orders_col().insert_one(order)
        name = "-"
        try:
            name = (await bot.get_users(uid)).first_name or "-"
        except Exception:
            pass
        await _activate(bot, order, {"utr": charge, "ts": now, "name": name, "stars": int(act.total_amount)})
        async for old in _orders_col().find({"status": "pending", "user": uid}):
            await _close(bot, old, "cancelled", "<b>✅ Paid With Stars.</b>")
    except Exception as e:
        logger.warning(f"[PREMIUM] stars payment failed: {type(e).__name__}: {e}")
        await _stars_failed(bot, uid, charge, f"{type(e).__name__}")


@Client.on_raw_update(group=-7)
async def stars_raw(bot, update, users, chats):
    try:
        if isinstance(update, rtypes.UpdateBotPrecheckoutQuery):
            await _stars_precheck(bot, update)
        elif isinstance(update, (rtypes.UpdateNewMessage, rtypes.UpdateNewChannelMessage)):
            act = getattr(update.message, "action", None)
            if isinstance(act, rtypes.MessageActionPaymentSentMe):
                await _stars_paid(bot, act)
    except Exception as e:
        logger.warning(f"[PREMIUM] stars update error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- user flow
@Client.on_message(filters.command("myplan") & filters.private)
async def myplan_cmd(bot, message):
    uid = message.from_user.id
    until = await premium_until(uid)
    username = (await bot.get_me()).username
    if until:
        import math
        doc = await _users_col().find_one({"_id": uid}) or {}
        left = max(1, math.ceil((until - _now()).total_seconds() / 86400))
        text = (f"<b>💎 My Plan !!\n\n"
                f"✅ Status: Premium Active\n"
                f"📦 Plan: {doc.get('plan') or 'Premium'}\n"
                f"📅 Active Till: {_fmt_date(until)}\n"
                f"⏳ Days Left: {left}</b>")
        label = "💎 Extend Premium"
    else:
        text = ("<b>💎 My Plan !!\n\n"
                "❌ Status: No Active Plan\n\n"
                "Get Premium To Skip The Short Links And Ads.</b>")
        label = "💎 Get Premium"
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, url=f"https://t.me/{username}/open")]]) if enabled() else None)


@Client.on_message(filters.private & filters.regex(r"^/start premium$"), group=-1)
async def start_premium(bot, message):
    """t.me/<bot>?start=premium shows the plan status; buying happens in the Mini App."""
    try:
        await myplan_cmd(bot, message)
    finally:
        message.stop_propagation()


async def _close(bot, order, status, text):
    """Closes a pending order and replaces its QR caption with `text`."""
    res = await _orders_col().find_one_and_update(
        {"_id": order["_id"], "status": "pending"}, {"$set": {"status": status}})
    if not res:
        return False
    try:
        from plugins import rewards
        await rewards.release(order)
    except Exception as e:
        logger.warning(f"[PREMIUM] points release failed: {type(e).__name__}: {e}")
    if order.get("msg"):
        try:
            await bot.edit_message_caption(order["chat"], order["msg"], text, reply_markup=None)
        except Exception:
            pass
    return True


@Client.on_callback_query(filters.regex(r"^prem_cancel:[0-9a-f]{8}$"))
async def cancel_order(bot, query):
    order = await _orders_col().find_one({"_id": query.data.split(":", 1)[1]})
    if not order or order["user"] != query.from_user.id:
        return await query.answer()
    if await _close(bot, order, "cancelled", "<b>❌ Order Cancelled.</b>"):
        return await query.answer("Order Cancelled.")
    await query.answer("This Order Is Already Closed.", show_alert=True)


_LAST_CHECK = {}


@Client.on_callback_query(filters.regex(r"^prem_check:[0-9a-f]{8}$"))
async def check_order(bot, query):
    oid = query.data.split(":", 1)[1]
    order = await _orders_col().find_one({"_id": oid})
    if not order or order["user"] != query.from_user.id:
        return await query.answer()
    if order["status"] == "paid":
        return await query.answer("Payment Received. Premium Is Active.", show_alert=True)
    if order["status"] != "pending":
        return await query.answer("This Order Is Closed.", show_alert=True)
    import time
    if time.time() - _LAST_CHECK.get(oid, 0) < 3:
        return await query.answer("Checking... Please Wait A Few Seconds.")
    _LAST_CHECK[oid] = time.time()
    await query.answer("Checking Your Payment...")
    await _check_orders(bot)
    fresh = await _orders_col().find_one({"_id": oid})
    if fresh and fresh["status"] == "pending":
        try:
            await query.answer("Not Received Yet. It Can Take A Minute After You Pay.", show_alert=True)
        except Exception:
            pass


# ---------------------------------------------------------------- Mini App helpers (Peter/server/miniapp.py)
def _plain_qr_data_uri(uri):
    import base64
    import qrcode
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    bio = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(bio, format="PNG")
    return "data:image/png;base64," + base64.b64encode(bio.getvalue()).decode()


async def app_plans(uid):
    plans = await get_plans()
    if uid in ADMINS:
        plans = plans + [TEST_PLAN]
    return [{"id": p["_id"], "name": p["name"], "days": p["days"], "price": _rupees(p["price"]),
             "stars": _stars_for(p)} for p in plans]


async def app_create_order(bot, uid, plan_id, promo=None, use_points=False):
    """(order dict, None) or (None, error code: disabled / plan / promo codes / qr / points)."""
    if not enabled():
        return None, "disabled"
    plan = TEST_PLAN if (plan_id == "test" and uid in ADMINS) else await _plans_col().find_one({"_id": plan_id})
    if not plan:
        return None, "plan"
    now = _now()
    from plugins import rewards
    q = {"base": int(plan["price"]), "promo": None, "promo_off": 0, "points": 0, "points_off": 0, "final": int(plan["price"])}
    if plan["_id"] != "test":
        q, err = await rewards.quote(uid, plan["price"], promo, use_points)
        if err:
            return None, err
    # Same plan, promo and points as a still-open app order: hand back that order (same QR and timer).
    async for old in _orders_col().find({"status": "pending", "user": uid, "via": "app"}):
        if (old.get("plan") == plan["_id"] and (old.get("promo") or None) == (q["promo"] or None)
                and bool(old.get("points_used")) == bool(q["points"]) and old["expires"] > now
                and not old.get("held") and not old.get("manual")):
            try:
                ruri = _upi_uri(int(old["amount"]), old["_id"])
                rqr = _plain_qr_data_uri(ruri)
            except Exception as e:
                logger.warning(f"[PREMIUM] app QR reuse failed: {type(e).__name__}: {e}")
                break
            _ensure_poller(bot)
            return {"order": old["_id"], "plan": old["plan_name"], "amount": _rupees(old["amount"]),
                    "base": old.get("base_price", int(old["amount"])), "promo": old.get("promo"),
                    "promo_off": old.get("promo_off", 0), "points": old.get("points_used", 0),
                    "points_off": old.get("points_off", 0),
                    "left": max(1, int((old["expires"] - now).total_seconds())), "upi": ruri, "qr": rqr}, None
    async for old in _orders_col().find({"status": "pending", "user": uid}):
        await _close(bot, old, "cancelled", "<b>❌ Order Replaced.</b>")
    oid = secrets.token_hex(4)
    uri = _upi_uri(q["final"], oid)
    try:
        qr = _plain_qr_data_uri(uri)
    except Exception as e:
        logger.warning(f"[PREMIUM] app QR failed: {type(e).__name__}: {e}")
        return None, "qr"
    if q["points"] and not await rewards.reserve(uid, q["points"]):
        return None, "points"
    await _orders_col().insert_one({
        "_id": oid, "user": uid, "bot_id": BOT_ID, "chat": None, "msg": None, "via": "app",
        "plan": plan["_id"], "plan_name": plan["name"], "days": plan["days"], "amount": str(q["final"]),
        "base_price": q["base"], "promo": q["promo"], "promo_off": q["promo_off"], "points_used": q["points"], "points_off": q["points_off"],
        "status": "pending", "created": now, "expires": now + datetime.timedelta(minutes=EXPIRY_MIN),
    })
    _ensure_poller(bot)
    return {"order": oid, "plan": plan["name"], "amount": _rupees(q["final"]), "base": q["base"],
            "promo": q["promo"], "promo_off": q["promo_off"], "points": q["points"], "points_off": q["points_off"],
            "left": EXPIRY_MIN * 60, "upi": uri, "qr": qr}, None


async def app_order_status(uid, oid):
    o = await _orders_col().find_one({"_id": oid})
    if not o or o["user"] != uid:
        return None
    out = {"status": o["status"], "left": max(0, int((o["expires"] - _now()).total_seconds())),
           "needs_utr": bool(o.get("held")) and o["status"] == "pending",
           "manual": bool(o.get("manual")) and o["status"] == "pending"}
    if o["status"] == "paid":
        until = await premium_until(uid)
        if until:
            out["until"] = (until + datetime.timedelta(hours=5, minutes=30)).strftime("%d %b %Y")
    return out


async def app_account(uid):
    """Premium state + the last paid orders, for the Mini App home / My Plan screen."""
    import math
    until = await premium_until(uid)
    out = {"premium": bool(until), "orders": []}
    plans = await get_plans()
    if plans:
        first = min(plans, key=lambda x: x.get("price", 0))
        out["from_price"] = _rupees(first.get("price"))
        out["from_days"] = first.get("days")
    if until:
        doc = await _users_col().find_one({"_id": uid}) or {}
        out["days"] = max(1, math.ceil((until - _now()).total_seconds() / 86400))
        out["until"] = (until + datetime.timedelta(hours=5, minutes=30)).strftime("%d %b %Y")
        out["plan"] = doc.get("plan") or "Premium"
    async for o in _orders_col().find({"user": uid, "status": "paid"}).sort("paid", -1).limit(20):
        when = (o.get("paid") or o["created"]) + datetime.timedelta(hours=5, minutes=30)
        out["orders"].append({"plan": o["plan_name"], "amount": _rupees(o["amount"]), "days": o.get("days"),
                              "date": when.strftime("%d %b %Y")})
    return out


async def app_claim(bot, uid, ident, order_id=None):
    code, _ = await claim_payment(bot, uid, ident, order_id)
    out = {"code": code}
    if code == "ok":
        st = await app_account(uid)
        out["until"] = st.get("until")
        out["plan"] = st.get("plan")
    return out


async def app_cancel_order(bot, uid, oid):
    o = await _orders_col().find_one({"_id": oid})
    if not o or o["user"] != uid:
        return False
    return await _close(bot, o, "cancelled", "<b>❌ Order Cancelled.</b>")


# ---------------------------------------------------------------- verification
async def _activate(bot, order, pay, tag="PremiumPurchased"):
    now = _now()
    doc = await _users_col().find_one({"_id": order["user"]})
    base = doc["expires"] if doc and doc.get("expires") and doc["expires"] > now else now
    bonus = 0
    if str(order.get("plan")) != "test":
        try:
            from plugins import rewards
            bonus = int((await rewards.perks(order["user"]))["bonus"])
        except Exception:
            bonus = 0
    until = base + datetime.timedelta(days=int(order["days"]) + bonus)
    await _users_col().update_one(
        {"_id": order["user"]},
        {"$set": {"expires": until, "plan": order["plan_name"], "last_paid": now},
         "$setOnInsert": {"since": now}},
        upsert=True)
    earned = {"points": 0, "tier_up": None}
    try:
        from plugins import rewards
        earned = await rewards.after_paid(bot, order)
    except Exception as e:
        logger.warning(f"[PREMIUM] rewards failed: {type(e).__name__}: {e}")
    done_text = (f"<b>✅ Payment Received !!</b>\n\n<b>💎 Premium — {order['plan_name']}</b>\n"
                 f"<b>📅 Active Till: {_fmt_date(until)}</b>")
    if bonus:
        done_text += f"\n<b>🎁 Bonus: +{bonus} Day{'s' if bonus > 1 else ''}</b>"
    if earned.get("points"):
        done_text += f"\n<b>⭐ Points Earned: +{earned['points']}</b>"
    if earned.get("tier_up"):
        done_text += f"\n<b>🏆 You Are Now {earned['tier_up']} !!</b>"
    if order.get("msg"):
        try:
            await bot.edit_message_caption(order["chat"], order["msg"], done_text, reply_markup=None)
        except Exception:
            pass
    elif order.get("via") in ("app", "stars"):
        try:
            await bot.send_message(order["user"], done_text)
        except Exception:
            pass
    who = f'<a href="tg://user?id={order["user"]}">{order["user"]}</a>'
    text = (
        f"<b>💎 #{tag}</b>\n\n"
        f"<b>👤 User:</b> {who}\n"
        f"<b>📦 Plan: {order['plan_name']}</b>\n"
        f"<b>💰 Amount: ₹{_rupees(order['amount'])}</b>\n"
        f"<b>🧾 UTR:</b> <code>{pay['utr']}</code>\n"
        f"<b>📧 Email Received: {_fmt_date(pay['ts'])}</b>\n"
        f"<b>🙍 Payer: {pay['name'] or '-'}</b>\n"
        f"<b>📅 Till: {_fmt_date(until)}</b>"
    )
    if pay.get("stars"):
        text = (
            f"<b>💎 #{tag} (Stars)</b>\n\n"
            f"<b>👤 User:</b> {who}\n"
            f"<b>📦 Plan: {order['plan_name']}</b>\n"
            f"<b>⭐ Stars: {pay['stars']}</b>\n"
            f"<b>🧾 Charge ID:</b> <code>{pay['utr']}</code>\n"
            f"<b>🙍 Name: {pay['name'] or '-'}</b>\n"
            f"<b>📅 Till: {_fmt_date(until)}</b>"
        )
    for admin in ADMINS:
        if isinstance(admin, int):
            try:
                await bot.send_message(admin, text)
            except Exception:
                pass


_CHECK_LOCK = asyncio.Lock()


async def _check_orders(bot):
    """Matches this bot's pending orders with the payment emails; also closes the expired ones."""
    async with _CHECK_LOCK:
        now = _now()
        pending = await _orders_col().find({"status": "pending", "bot_id": BOT_ID}).to_list(length=100)
        if not pending:
            return
        for o in pending:
            if o["expires"] <= now - datetime.timedelta(minutes=3):
                await _close(bot, o, "expired", "<b>⌛ Order Expired. Open Premium Plans To Create A New One.</b>")
        live = [o for o in pending if o["expires"] > now - datetime.timedelta(minutes=3)]
        if not live or not enabled():
            return
        try:
            payments = await asyncio.to_thread(_fetch_payments)
        except Exception as e:
            logger.warning(f"[PREMIUM] Gmail check failed: {type(e).__name__}: {e}")
            return
        def window(o):
            return (o["created"] - datetime.timedelta(minutes=1), o["expires"] + datetime.timedelta(minutes=3))

        async def settle(o, pay, tag="PremiumPurchased"):
            try:
                await _utr_col().insert_one({"_id": pay["utr"], "order": o["_id"], "t": _now()})
            except DuplicateKeyError:
                return False   # this payment already confirmed an order
            done = await _orders_col().find_one_and_update(
                {"_id": o["_id"], "status": "pending"},
                {"$set": {"status": "paid", "utr": pay["utr"], "paid": _now()}})
            if not done:
                await _utr_col().delete_one({"_id": pay["utr"]})
                return False
            await _activate(bot, o, pay, tag)
            return True

        used = {d["_id"] async for d in _utr_col().find({"_id": {"$in": [p["utr"] for p in payments]}})}
        groups = {}
        for o in live:
            groups.setdefault(o["amount"], []).append(o)
        for amount_s, orders in groups.items():
            amount = Decimal(amount_s)
            orders.sort(key=lambda o: o["created"])
            cands = [p for p in sorted(payments, key=lambda p: p["ts"])
                     if p["amount"] == amount and p["utr"] not in used
                     and any(window(o)[0] <= p["ts"] <= window(o)[1] for o in orders)]
            if not cands:
                continue
            if len(orders) == 1 or len(cands) >= len(orders):
                # every pending order of this amount is covered by a payment (or there is only one order):
                # who gets which payment cannot matter, all of them end up with the same plan
                left = list(orders)
                for pay in cands:
                    o = next((x for x in left if window(x)[0] <= pay["ts"] <= window(x)[1]), None)
                    if o and await settle(o, pay):
                        left.remove(o)
                continue
            # a payment came in but there are more orders than payments: it is not clear whose it is.
            # Nobody gets it automatically; the payer confirms it with the UTR / Transaction ID.
            fresh = [o for o in orders if not o.get("held")]
            if fresh:
                await _orders_col().update_many(
                    {"_id": {"$in": [o["_id"] for o in fresh]}, "status": "pending"}, {"$set": {"held": True}})
                uname = getattr(getattr(bot, "me", None), "username", None)
                for o in fresh:
                    if o.get("msg") and uname:
                        try:
                            await bot.edit_message_caption(
                                o["chat"], o["msg"],
                                f"<b>✅ Payment Detected</b>\n\n<b>💰 Amount: ₹{_rupees(o['amount'])}</b>\n\n"
                                "<b>Several People Are Paying The Same Amount. Open The Mini App And Enter Your "
                                "UTR / Transaction ID To Confirm Yours.</b>",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("📲 Confirm In Mini App", url=f"https://t.me/{uname}/open")],
                                    [InlineKeyboardButton("❌ Cancel Order", callback_data=f"prem_cancel:{o['_id']}")]]))
                        except Exception:
                            pass


# ---------------------------------------------------------------- UTR / Transaction ID claim
_CLAIM_TRIES = {}


async def claim_payment(bot, uid, ident, order_id=None):
    """The user types the UTR (bank apps) or the Transaction ID (FamApp) of a payment.
    Returns (code, info): ok / format / throttle / disabled / error / notfound / used / amount."""
    if not enabled():
        return "disabled", None
    ident = re.sub(r"[^A-Za-z0-9]", "", str(ident or "")).upper()
    if len(ident) < 8 or len(ident) > 40 or not re.search(r"[0-9]", ident):
        return "format", None
    now_t = time.time()
    tries = [t for t in _CLAIM_TRIES.get(uid, []) if now_t - t < 600]
    if len(tries) >= 6:
        return "throttle", None
    tries.append(now_t)
    _CLAIM_TRIES[uid] = tries
    async with _CHECK_LOCK:
        try:
            payments = await asyncio.to_thread(_fetch_payments)
        except Exception as e:
            logger.warning(f"[PREMIUM] claim: Gmail check failed: {type(e).__name__}: {e}")
            return "error", None
        pay = next((p for p in payments if ident in p.get("tokens", ())), None)
        if not pay:
            return "notfound", None
        if await _utr_col().find_one({"_id": pay["utr"]}):
            return "used", None
        q = {"user": uid, "status": "pending"}
        if order_id:
            q["_id"] = order_id
        order = None
        async for o in _orders_col().find(q).sort("created", -1):
            if Decimal(o["amount"]) == pay["amount"]:
                order = o
                break
        tag = "PremiumPurchased"
        if not order:
            # late claim: the order is gone (expired / cancelled), the plan is found from the amount
            plans = await get_plans()
            if uid in ADMINS:
                plans = plans + [TEST_PLAN]
            plan = next((p for p in plans if Decimal(str(p["price"])) == pay["amount"]), None)
            if not plan:
                return "amount", None
            now = _now()
            order = {"_id": secrets.token_hex(4), "user": uid, "bot_id": BOT_ID, "chat": None, "msg": None,
                     "via": "app", "plan": plan["_id"], "plan_name": plan["name"], "days": plan["days"],
                     "amount": str(plan["price"]), "status": "pending", "created": now, "expires": now}
            await _orders_col().insert_one(order)
            tag = "PremiumClaimed"
        try:
            await _utr_col().insert_one({"_id": pay["utr"], "order": order["_id"], "t": _now()})
        except DuplicateKeyError:
            return "used", None
        done = await _orders_col().find_one_and_update(
            {"_id": order["_id"], "status": "pending"},
            {"$set": {"status": "paid", "utr": pay["utr"], "paid": _now()}})
        if not done:
            await _utr_col().delete_one({"_id": pay["utr"]})
            return "error", None
    await _activate(bot, order, pay, tag)
    return "ok", None


_POLLER = None


def _ensure_poller(bot):
    global _POLLER
    if _POLLER is None or _POLLER.done():
        _POLLER = asyncio.create_task(payment_loop(bot))


async def payment_loop(bot):
    """Runs forever (started with the bot); costs nothing while there is no pending order."""
    while True:
        try:
            await _check_orders(bot)
        except Exception as e:
            logger.warning(f"[PREMIUM] poller error: {type(e).__name__}: {e}")
        await asyncio.sleep(POLL_SECONDS)


start_poller = _ensure_poller   # called once from bot.py so pending orders survive a restart
