"""Mini app endpoint: decides where the Mini App sends the user (premium: straight to the bot, others: shortener).

The page at https://<domain>/app (Cloudflare Worker) posts Telegram's signed "initData" here. We check the
signature with this bot's token (so a user id can't be faked), look the user up in the premium list and answer
with the link to open. Premium users get the bot link of the stored file; everybody else gets the shortener link.
"""
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
from urllib.parse import parse_qsl, parse_qs, urlparse, quote

import aiohttp
from aiohttp import web

from config import BOT_TOKEN

logger = logging.getLogger(__name__)

WORKER_URL = (os.getenv("PERMANENT_LINK_WORKER_URL") or "").rstrip("/")
ADMIN_KEY = os.getenv("PERMANENT_LINK_ADMIN_KEY") or ""
ALLOWED_ORIGIN = os.getenv("MINIAPP_ORIGIN") or WORKER_URL or "https://chillflizx.in"
MAX_AGE = 24 * 3600
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _cors():
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def check_init_data(init_data, token=BOT_TOKEN, max_age=MAX_AGE):
    """Returns the Telegram user id when the signed initData is genuine and fresh, else None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


async def _worker_record(key):
    """{destination, shortUrl} of a domain link from the Cloudflare Worker, or None."""
    if not WORKER_URL or not ADMIN_KEY:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.post(f"{WORKER_URL}/admin/get", json={"key": key},
                              headers={"X-Admin-Key": ADMIN_KEY}) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        logger.warning(f"[MINIAPP] worker lookup failed: {type(e).__name__}: {e}")
        return None


def _bot_link(destination, username):
    """https://t.me/<this bot>?start=<payload of the stored bot link>"""
    start = (parse_qs(urlparse(destination or "").query).get("start") or [""])[0]
    return f"https://t.me/{username}?start={quote(start)}" if start and username else None


async def miniapp_options(_):
    return web.Response(status=204, headers=_cors())


async def miniapp_open(request):
    headers = _cors()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400, headers=headers)
    key = str(body.get("key") or "")
    uid = check_init_data(str(body.get("initData") or ""))
    if not uid or not KEY_RE.match(key):
        return web.json_response({"ok": False}, status=403, headers=headers)

    rec = await _worker_record(key)
    if not rec or not rec.get("success"):
        return web.json_response({"ok": False}, status=404, headers=headers)

    until = None
    try:
        from plugins.premium import premium_until
        until = await premium_until(uid)
    except Exception as e:
        logger.warning(f"[MINIAPP] premium check failed: {type(e).__name__}: {e}")

    from Peter.bot import StreamBot
    username = getattr(StreamBot, "username", None)
    extra = {"premium_url": f"https://t.me/{username}?start=premium" if username else None}
    try:
        from plugins.premium import get_plans
        plans = await get_plans()
        if plans:
            first = min(plans, key=lambda x: x.get("price", 0))
            extra["from_price"] = first.get("price")
            extra["from_days"] = first.get("days")
    except Exception:
        pass

    if until:
        url = _bot_link(rec.get("destination"), username)
        if url:
            ist = until + datetime.timedelta(hours=5, minutes=30)
            left = max(1, math.ceil((until - datetime.datetime.utcnow()).total_seconds() / 86400))
            return web.json_response({"ok": True, "premium": True, "url": url, "days": left,
                                      "until": ist.strftime("%d %b %Y"), **extra}, headers=headers)
    # everybody else (and premium users when the bot link can't be built) goes through the shortener
    short = rec.get("shortUrl")
    if not short:
        return web.json_response({"ok": False}, status=404, headers=headers)
    return web.json_response({"ok": True, "premium": False, "url": short, **extra}, headers=headers)
