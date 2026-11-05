"""Premium plans paid with FamPay (UPI), verified automatically from the FamApp emails in Gmail.

How it works
  1. /premium shows the plans. A plan button creates an order and sends a UPI QR for that exact amount.
  2. The user pays from any UPI app. FamApp mails "You received ₹X in your FamX account" to the Gmail
     below (IMAP). The poller matches that mail to the order by amount + time and checks the UTR was never used.
  3. The order is marked paid, premium is added (or extended) in MongoDB and the owner gets a DM.
Only ONE pending order per amount exists at a time (prices are whole rupees), so two payments can never be mixed.

Environment (all bots share the same ones):
  FAM_UPI_ID, FAM_PAYEE_NAME (optional), IMAP_USERNAME, IMAP_APP_PASSWORD,
  IMAP_HOST / IMAP_PORT / IMAP_SENDER_FILTER (optional), PAY_EXPIRY_MINUTES (optional, default 10)
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
EXPIRY_MIN = int(os.environ.get("PAY_EXPIRY_MINUTES", "10") or 10)
POLL_SECONDS = 5
LOOKBACK_HOURS = 6
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
        for num in ids[-40:]:
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
            pay = {"utr": u.group(1), "amount": amount, "ts": ts,
                   "name": (who.group(1).strip() if who else "")}
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
    # one pending order per amount: two payments of the same amount could not be told apart
    busy = await _orders_col().find_one({
        "status": "pending", "amount": str(plan["price"]), "expires": {"$gt": now}, "user": {"$ne": uid}})
    if busy:
        return await query.answer(
            f"Another Payment Of ₹{_rupees(plan['price'])} Is In Progress. Try Again In A Few Minutes.",
            show_alert=True)
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


# ---------------------------------------------------------------- verification
async def _activate(bot, order, pay):
    now = _now()
    doc = await _users_col().find_one({"_id": order["user"]})
    base = doc["expires"] if doc and doc.get("expires") and doc["expires"] > now else now
    until = base + datetime.timedelta(days=int(order["days"]))
    await _users_col().update_one(
        {"_id": order["user"]},
        {"$set": {"expires": until, "plan": order["plan_name"], "last_paid": now},
         "$setOnInsert": {"since": now}},
        upsert=True)
    try:
        await bot.edit_message_caption(
            order["chat"], order["msg"],
            f"<b>✅ Payment Received !!</b>\n\n<b>💎 Premium — {order['plan_name']}</b>\n"
            f"<b>📅 Active Till: {_fmt_date(until)}</b>", reply_markup=None)
    except Exception:
        pass
    who = f'<a href="tg://user?id={order["user"]}">{order["user"]}</a>'
    text = (
        "<b>💎 #PremiumPurchased</b>\n\n"
        f"<b>👤 User:</b> {who}\n"
        f"<b>📦 Plan: {order['plan_name']}</b>\n"
        f"<b>💰 Amount: ₹{_rupees(order['amount'])}</b>\n"
        f"<b>🧾 UTR:</b> <code>{pay['utr']}</code>\n"
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
        for o in live:
            amount = Decimal(o["amount"])
            lo = o["created"] - datetime.timedelta(minutes=2)
            hi = o["expires"] + datetime.timedelta(minutes=3)
            for pay in sorted(payments, key=lambda p: p["ts"]):
                if pay["amount"] != amount or not (lo <= pay["ts"] <= hi):
                    continue
                try:
                    await _utr_col().insert_one({"_id": pay["utr"], "order": o["_id"], "t": now})
                except DuplicateKeyError:
                    continue   # this payment already confirmed an order
                done = await _orders_col().find_one_and_update(
                    {"_id": o["_id"], "status": "pending"},
                    {"$set": {"status": "paid", "utr": pay["utr"], "paid": now}})
                if not done:
                    await _utr_col().delete_one({"_id": pay["utr"]})
                    break
                await _activate(bot, o, pay)
                break


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
