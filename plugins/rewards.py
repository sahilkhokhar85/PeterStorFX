"""Rewards for premium buyers: tiers, points, referral, promo codes, and manual (screenshot) payments.

Tiers (by number of purchases of 30+ day plans)
  New Member            -
  Silver  (1 purchase)  1.2x referral points, +1 bonus day on every purchase
  Gold    (3 purchases) 1.5x referral points, +3 bonus days on every purchase
Points
  every paid order gives 1 point per Rs.10; 1 point = Rs.0.50 off at checkout (up to REDEEM_MAX_PCT of a plan)
  a referred friend's first purchase gives the inviter REF_POINTS (x tier multiplier)
Promo codes are made by the admins:  /promo add CODE 20 [max uses] [days]   /promo flat CODE 10   /promo del CODE   /promo list
Manual payments: the user uploads a screenshot in the Mini App, the admins get it with Approve / Reject buttons.
"""
import datetime
import io
import logging
import math
import os
import re
import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMINS
from plugins.dbusers import db

logger = logging.getLogger(__name__)

REDEEM_MAX_PCT = int(os.environ.get("REDEEM_MAX_PCT", "50") or 50)
REF_POINTS = int(os.environ.get("REF_POINTS", "20") or 20)
SILVER_AT, GOLD_AT = 1, 3
PERKS = {
    "new": {"name": "New Member", "mult": 1.0, "bonus": 0},
    "silver": {"name": "Silver", "mult": 1.2, "bonus": 1},
    "gold": {"name": "Gold", "mult": 1.5, "bonus": 3},
}
MANUAL_HOURS = 4


def _now():
    return datetime.datetime.utcnow()


def _col():
    return db.db.rewards


def _promo_col():
    return db.db.promo_codes


def _manual_col():
    return db.db.manual_payments


def _orders():
    return db.db.premium_orders


def tier_of(purchases):
    return "gold" if purchases >= GOLD_AT else "silver" if purchases >= SILVER_AT else "new"


async def get(uid):
    return await _col().find_one({"_id": int(uid)}) or {"_id": int(uid), "points": 0, "purchases": 0}


async def perks(uid):
    return PERKS[tier_of(int((await get(uid)).get("purchases", 0)))]


# ---------------------------------------------------------------- summary for the Mini App
async def summary(uid, username):
    d = await get(uid)
    p = int(d.get("purchases", 0))
    t = tier_of(p)
    nxt = "silver" if t == "new" else "gold" if t == "silver" else None
    need = (SILVER_AT if nxt == "silver" else GOLD_AT) if nxt else 0
    return {
        "tier": t, "tier_name": PERKS[t]["name"], "purchases": p,
        "next": PERKS[nxt]["name"] if nxt else None, "next_at": need,
        "next_perks": {"mult": PERKS[nxt]["mult"], "bonus": PERKS[nxt]["bonus"]} if nxt else None,
        "perks": {"mult": PERKS[t]["mult"], "bonus": PERKS[t]["bonus"]},
        "points": int(d.get("points", 0)),
        "ref_count": int(d.get("ref_count", 0)), "ref_points": int(d.get("ref_points", 0)),
        "ref_link": f"https://t.me/{username}?start=ref_{uid}" if username else None,
        "ref_each": int(round(REF_POINTS * PERKS[t]["mult"])),
        "max_pct": REDEEM_MAX_PCT,
    }


# ---------------------------------------------------------------- promo codes + points at checkout
async def check_promo(uid, code, base):
    """(discount in whole rupees, None) or (0, error code)."""
    code = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    if not code:
        return 0, "format"
    doc = await _promo_col().find_one({"_id": code})
    if not doc or not doc.get("active", True):
        return 0, "invalid"
    if doc.get("expires") and doc["expires"] <= _now():
        return 0, "expired"
    if doc.get("max_uses") and int(doc.get("used", 0)) >= int(doc["max_uses"]):
        return 0, "limit"
    if int(uid) in (doc.get("used_by") or []):
        return 0, "used"
    off = int(doc["flat"]) if doc.get("flat") else int(base * int(doc.get("pct", 0)) / 100)
    off = max(0, min(off, int(base) - 1))
    return (off, None) if off > 0 else (0, "invalid")


async def quote(uid, base, code=None, use_points=False):
    """{base, promo, promo_off, points, points_off, final} or (None, error code)."""
    base = int(base)
    out = {"base": base, "promo": None, "promo_off": 0, "points": 0, "points_off": 0}
    after = base
    if code:
        off, err = await check_promo(uid, code, base)
        if err:
            return None, err
        out["promo"] = re.sub(r"[^A-Za-z0-9]", "", code).upper()
        out["promo_off"] = off
        after -= off
    if use_points:
        bal = int((await get(uid)).get("points", 0))
        cap_rs = min(after * REDEEM_MAX_PCT // 100, after - 1)
        pts = min(bal, cap_rs * 2)
        pts -= pts % 2
        if pts > 0:
            out["points"] = pts
            out["points_off"] = pts // 2
            after -= pts // 2
    out["final"] = max(1, after)
    return out, None


async def reserve(uid, pts):
    if pts <= 0:
        return True
    res = await _col().update_one({"_id": int(uid), "points": {"$gte": int(pts)}}, {"$inc": {"points": -int(pts)}})
    return res.modified_count == 1


async def release(order):
    """Gives the reserved points back when an order is cancelled / expires."""
    pts = int(order.get("points_used") or 0)
    if pts <= 0:
        return
    done = await _orders().find_one_and_update(
        {"_id": order["_id"], "points_released": {"$ne": True}}, {"$set": {"points_released": True}})
    if done:
        await _col().update_one({"_id": int(order["user"])}, {"$inc": {"points": pts}}, upsert=True)


# ---------------------------------------------------------------- after a paid order
async def after_paid(bot, order):
    """Points, tier progress, referral reward, promo usage. Runs once per order. Returns what the buyer earned."""
    out = {"points": 0, "tier_up": None}
    if str(order.get("plan")) == "test":
        return out
    first = await _orders().find_one_and_update(
        {"_id": order["_id"], "rewarded": {"$ne": True}}, {"$set": {"rewarded": True}})
    if not first:
        return out
    uid = int(order["user"])
    before = tier_of(int((await get(uid)).get("purchases", 0)))
    earn = int(math.floor(int(float(order["amount"])) / 10))
    inc = {"points": earn}
    if int(order.get("days", 0)) >= 30:
        inc["purchases"] = 1
    await _col().update_one({"_id": uid}, {"$inc": inc, "$setOnInsert": {"joined": _now()}}, upsert=True)
    d = await get(uid)
    after = tier_of(int(d.get("purchases", 0)))
    out["points"] = earn
    if after != before:
        out["tier_up"] = PERKS[after]["name"]
    if order.get("promo"):
        await _promo_col().update_one({"_id": order["promo"]}, {"$inc": {"used": 1}, "$addToSet": {"used_by": uid}})
    ref = d.get("ref_by")
    if ref and not d.get("ref_rewarded"):
        got = await _col().find_one_and_update({"_id": uid, "ref_rewarded": {"$ne": True}}, {"$set": {"ref_rewarded": True}})
        if got:
            mult = PERKS[tier_of(int((await get(ref)).get("purchases", 0)))]["mult"]
            bonus = int(round(REF_POINTS * mult))
            await _col().update_one({"_id": int(ref)},
                                    {"$inc": {"points": bonus, "ref_count": 1, "ref_points": bonus}}, upsert=True)
            try:
                await bot.send_message(int(ref), f"<b>🎉 Your Friend Joined Premium !!\n\n🎁 You Got +{bonus} Points.</b>")
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- referral link: /start ref_<id>
@Client.on_message(filters.private & filters.regex(r"^/start ref_[0-9]{4,15}$"), group=-1)
async def start_ref(bot, message):
    """t.me/<bot>?start=ref_<inviter>: remembers the inviter (only for brand new users), then runs the normal /start."""
    try:
        uid = message.from_user.id
        ref = int(message.text.split("ref_", 1)[1])
        if ref != uid and not await db.is_user_exist(uid):
            await _col().update_one({"_id": uid}, {"$setOnInsert": {"ref_by": ref, "points": 0, "purchases": 0,
                                                                   "joined": _now()}}, upsert=True)
    except Exception as e:
        logger.warning(f"[REWARDS] referral bind failed: {type(e).__name__}: {e}")
    message.text = "/start"
    message.command = ["start"]


# ---------------------------------------------------------------- promo admin command
@Client.on_message(filters.command("promo") & filters.private & filters.user(ADMINS))
async def promo_cmd(bot, message):
    a = message.command[1:]
    usage = ("<b>🎟 Promo Codes</b>\n\n"
             "<code>/promo add CODE 20</code> — 20% off\n"
             "<code>/promo add CODE 20 100 7</code> — 20% off, 100 uses, 7 days\n"
             "<code>/promo flat CODE 10</code> — ₹10 off\n"
             "<code>/promo del CODE</code>\n<code>/promo list</code>")
    if not a:
        return await message.reply_text(usage)
    cmd = a[0].lower()
    if cmd == "list":
        docs = await _promo_col().find({}).to_list(length=50)
        if not docs:
            return await message.reply_text("<b>No Promo Codes Yet.</b>")
        lines = []
        for d in docs:
            off = f"₹{d['flat']}" if d.get("flat") else f"{d.get('pct')}%"
            lim = f"/{d['max_uses']}" if d.get("max_uses") else ""
            exp = (d["expires"] + datetime.timedelta(hours=5, minutes=30)).strftime("%d %b") if d.get("expires") else "No Expiry"
            lines.append(f"<code>{d['_id']}</code> — {off} off — Used {d.get('used', 0)}{lim} — {exp}")
        return await message.reply_text("<b>🎟 Promo Codes</b>\n\n" + "\n".join(lines))
    if cmd == "del" and len(a) >= 2:
        r = await _promo_col().delete_one({"_id": re.sub(r"[^A-Za-z0-9]", "", a[1]).upper()})
        return await message.reply_text("<b>✅ Deleted.</b>" if r.deleted_count else "<b>Code Not Found.</b>")
    if cmd in ("add", "flat") and len(a) >= 3 and a[2].isdigit():
        code = re.sub(r"[^A-Za-z0-9]", "", a[1]).upper()
        if len(code) < 3:
            return await message.reply_text(usage)
        doc = {"_id": code, "active": True, "used": 0, "used_by": [], "created": _now()}
        if cmd == "add":
            if not 1 <= int(a[2]) <= 90:
                return await message.reply_text("<b>Percent Must Be 1 To 90.</b>")
            doc["pct"] = int(a[2])
        else:
            doc["flat"] = int(a[2])
        if cmd == "add" and len(a) >= 4 and a[3].isdigit():
            doc["max_uses"] = int(a[3])
        if cmd == "add" and len(a) >= 5 and a[4].isdigit():
            doc["expires"] = _now() + datetime.timedelta(days=int(a[4]))
        await _promo_col().replace_one({"_id": code}, doc, upsert=True)
        return await message.reply_text(f"<b>✅ Promo Code Saved:</b> <code>{code}</code>")
    await message.reply_text(usage)


# ---------------------------------------------------------------- manual payment (screenshot)
_UPLOADS = {}


async def manual_submit(bot, uid, oid, data):
    """Returns ok / notfound / dup / big / format / throttle / error."""
    if not data.startswith(b"\xff\xd8") or len(data) < 500:
        return "format"
    if len(data) > 700_000:
        return "big"
    now_t = time.time()
    tries = [t for t in _UPLOADS.get(uid, []) if now_t - t < 3600]
    if len(tries) >= 5:
        return "throttle"
    o = await _orders().find_one({"_id": oid})
    if not o or o["user"] != uid or o["status"] != "pending":
        return "notfound"
    if await _manual_col().find_one({"_id": oid}):
        return "dup"
    tries.append(now_t)
    _UPLOADS[uid] = tries
    await _manual_col().insert_one({"_id": oid, "user": uid, "status": "open", "t": _now()})
    # keep the order open while the admins check it (auto verification still works in the meantime)
    await _orders().update_one({"_id": oid}, {"$set": {
        "manual": True, "expires": _now() + datetime.timedelta(hours=MANUAL_HOURS)}})
    who = f'<a href="tg://user?id={uid}">{uid}</a>'
    caption = (f"<b>🧾 #ManualPayment</b>\n\n<b>👤 User:</b> {who}\n<b>📦 Plan: {o['plan_name']}</b>\n"
               f"<b>💰 Amount: ₹{o['amount']}</b>\n<b>🆔 Order:</b> <code>{oid}</code>")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"mp_ok:{oid}"),
                                    InlineKeyboardButton("❌ Reject", callback_data=f"mp_no:{oid}")]])
    sent = 0
    for admin in ADMINS:
        if isinstance(admin, int):
            try:
                bio = io.BytesIO(data)
                bio.name = "payment.jpg"
                await bot.send_photo(admin, bio, caption=caption, reply_markup=markup)
                sent += 1
            except Exception as e:
                logger.warning(f"[REWARDS] manual send failed: {type(e).__name__}: {e}")
    if not sent:
        await _manual_col().delete_one({"_id": oid})
        return "error"
    return "ok"


@Client.on_callback_query(filters.regex(r"^mp_(ok|no):[0-9a-f]{8}$") & filters.user(ADMINS))
async def manual_decide(bot, query):
    from plugins import premium
    act, oid = query.data.split(":")
    got = await _manual_col().find_one_and_update({"_id": oid, "status": "open"}, {"$set": {"status": act}})
    if not got:
        return await query.answer("Already Handled.", show_alert=True)
    order = await _orders().find_one({"_id": oid})
    if not order:
        return await query.answer("Order Not Found.", show_alert=True)
    cap = query.message.caption or ""
    if act == "mp_no":
        await premium._close(bot, order, "cancelled", "<b>❌ Order Cancelled.</b>")
        try:
            await bot.send_message(order["user"], "<b>❌ Payment Not Verified.\n\nIf You Have Paid, Contact Support With Your Payment Details.</b>")
        except Exception:
            pass
        await query.message.edit_caption(cap + "\n\n❌ Rejected", reply_markup=None)
        return await query.answer("Rejected.")
    now = _now()
    before = await _orders().find_one_and_update(
        {"_id": oid, "status": {"$in": ["pending", "expired", "cancelled"]}},
        {"$set": {"status": "paid", "utr": "MANUAL-" + oid, "paid": now}})
    if not before:
        return await query.answer("Order Is Already Paid.", show_alert=True)
    if before["status"] != "pending" and int(before.get("points_used") or 0) > 0:
        await _col().update_one({"_id": int(before["user"])}, {"$inc": {"points": -int(before["points_used"])}})
    await premium._activate(bot, before, {"utr": "MANUAL-" + oid, "ts": now, "name": "-"}, "PremiumManual")
    await query.message.edit_caption(cap + "\n\n✅ Approved", reply_markup=None)
    await query.answer("Approved.")
