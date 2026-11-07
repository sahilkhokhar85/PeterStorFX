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
TEST_PLAN = {"_id": "test", "name": "Test (1 Day)", "days": 1, "price": 1}

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


# ---------------------------------------------------------------- user flow
@Client.on_message(filters.command("premium") & filters.private)
async def premium_cmd(bot, message):
    if not enabled():
        return await message.reply_text("<b>Premium Is Not Available Right Now.</b>")
    await message.reply_text(await _home_text(message.from_user.id), reply_markup=await _plan_markup(message.from_user.id))


@Client.on_message(filters.private & filters.regex(r"^/start premium$"), group=-1)
async def start_premium(bot, message):
    """t.me/<bot>?start=premium (the Mini App's Premium button) opens the plans screen."""
    message.stop_propagation()
    await premium_cmd(bot, message)


@Client.on_callback_query(filters.regex(r"^prem_plan:[0-9A-Za-z_-]{1,20}$"))
async def plan_chosen(bot, query):
    if not enabled():
        return await query.answer("Premium Is Not Available Right Now.", show_alert=True)
    uid = query.from_user.id
    plan_id = query.data.split(":", 1)[1]
    plan = TEST_PLAN if (plan_id == "test" and uid in ADMINS) else await _plans_col().find_one({"_id": plan_id})
    if not plan:
        return await query.answer("This Plan Is No Longer Available.", show_alert=True)
    now = _now()
    # same-amount orders may run together: payments are told apart in _check_orders (or by UTR)
    # the user's own older pending order is replaced by this one
    async for old in _orders_col().find({"status": "pending", "user": uid}):
        await _close(bot, old, "cancelled", "<b>❌ Order Replaced.</b>")
    oid = secrets.token_hex(4)
    expires = now + datetime.timedelta(minutes=EXPIRY_MIN)
    try:
        sent = await bot.send_photo(
            query.message.chat.id, _qr_png(plan["price"], oid),
            caption=_order_caption(plan["name"], plan["price"]), reply_markup=_order_markup(oid))
    except Exception as e:
        logger.warning(f"[PREMIUM] could not send the QR: {type(e).__name__}: {e}")
        return await query.answer("Could Not Create The Order. Try Again.", show_alert=True)
    await _orders_col().insert_one({
        "_id": oid, "user": uid, "bot_id": BOT_ID, "chat": query.message.chat.id, "msg": sent.id,
        "plan": plan["_id"], "plan_name": plan["name"], "days": plan["days"], "amount": str(plan["price"]),
        "status": "pending", "created": now, "expires": expires,
    })
    await query.answer("Order Created.")
    _ensure_poller(bot)


async def _close(bot, order, status, text):
    """Closes a pending order and replaces its QR caption with `text`."""
    res = await _orders_col().find_one_and_update(
        {"_id": order["_id"], "status": "pending"}, {"$set": {"status": status}})
    if not res:
        return False
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
    return [{"id": p["_id"], "name": p["name"], "days": p["days"], "price": _rupees(p["price"])} for p in plans]


async def app_create_order(bot, uid, plan_id):
    """(order dict, None) or (None, error code: disabled / plan / busy / qr)."""
    if not enabled():
        return None, "disabled"
    plan = TEST_PLAN if (plan_id == "test" and uid in ADMINS) else await _plans_col().find_one({"_id": plan_id})
    if not plan:
        return None, "plan"
    now = _now()
    async for old in _orders_col().find({"status": "pending", "user": uid}):
        await _close(bot, old, "cancelled", "<b>❌ Order Replaced.</b>")
    oid = secrets.token_hex(4)
    uri = _upi_uri(plan["price"], oid)
    try:
        qr = _plain_qr_data_uri(uri)
    except Exception as e:
        logger.warning(f"[PREMIUM] app QR failed: {type(e).__name__}: {e}")
        return None, "qr"
    await _orders_col().insert_one({
        "_id": oid, "user": uid, "bot_id": BOT_ID, "chat": None, "msg": None, "via": "app",
        "plan": plan["_id"], "plan_name": plan["name"], "days": plan["days"], "amount": str(plan["price"]),
        "status": "pending", "created": now, "expires": now + datetime.timedelta(minutes=EXPIRY_MIN),
    })
    _ensure_poller(bot)
    return {"order": oid, "plan": plan["name"], "amount": _rupees(plan["price"]),
            "left": EXPIRY_MIN * 60, "upi": uri, "qr": qr}, None


async def app_order_status(uid, oid):
    o = await _orders_col().find_one({"_id": oid})
    if not o or o["user"] != uid:
        return None
    out = {"status": o["status"], "left": max(0, int((o["expires"] - _now()).total_seconds())),
           "needs_utr": bool(o.get("held")) and o["status"] == "pending"}
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
    async for o in _orders_col().find({"user": uid, "status": "paid"}).sort("paid", -1).limit(5):
        when = (o.get("paid") or o["created"]) + datetime.timedelta(hours=5, minutes=30)
        out["orders"].append({"plan": o["plan_name"], "amount": _rupees(o["amount"]),
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
    until = base + datetime.timedelta(days=int(order["days"]))
    await _users_col().update_one(
        {"_id": order["user"]},
        {"$set": {"expires": until, "plan": order["plan_name"], "last_paid": now},
         "$setOnInsert": {"since": now}},
        upsert=True)
    done_text = (f"<b>✅ Payment Received !!</b>\n\n<b>💎 Premium — {order['plan_name']}</b>\n"
                 f"<b>📅 Active Till: {_fmt_date(until)}</b>")
    if order.get("msg"):
        try:
            await bot.edit_message_caption(order["chat"], order["msg"], done_text, reply_markup=None)
        except Exception:
            pass
    elif order.get("via") == "app":
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
                await _close(bot, o, "expired", "<b>⌛ Order Expired. Use /premium To Create A New One.</b>")
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
