import time
import logging
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from Peter.bot import StreamBot
from Peter import StartTime, __version__
from Peter.utils.watchdog import is_bot_healthy
from Peter.server.streamer import (
    InvalidStreamHash,
    StreamFileNotFound,
    decode_stream_token,
    render_watch_page,
    stream_media,
)
from config import STREAM_MODE
from plugins.dbusers import db

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _readable_uptime(seconds: int) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


@routes.get("/", allow_head=True)
async def root_route_handler(_):
    """Health-check endpoint - keeps the hosting platform (Railway/Koyeb/
    Heroku/Render) aware the service is alive.

    Importantly this also reports whether the Telegram (Pyrogram) client
    is actually connected. Previously this always said "running" even
    when the bot itself had silently disconnected/hung (e.g. blocked on a
    slow MongoDB call) - the web server was fine, only the bot was dead,
    and the host had no way to know something was wrong. Returning a
    non-200 status when the bot is down lets Railway's healthcheck /
    restart policy actually catch and fix it automatically.
    """
    bot_connected = is_bot_healthy()
    payload = {
        "server_status": "running",
        "uptime": _readable_uptime(time.time() - StartTime),
        "telegram_bot": "@" + (StreamBot.username or ""),
        "telegram_bot_connected": bot_connected,
        "version": __version__,
    }
    return web.json_response(payload, status=200 if bot_connected else 503)


@routes.get(r"/dl/{token}/{name:.*}", allow_head=True)
async def download_route_handler(request: web.Request):
    """Fast Download / Watch Online byte-range streaming endpoint.
    {name} in the path is cosmetic only (nice-looking URL) - the real
    identity check is the token + hash in the query string."""
    if not STREAM_MODE:
        raise web.HTTPNotFound(text="Streaming is disabled on this bot.")
    try:
        chat_id, message_id, user_id = decode_stream_token(request.match_info["token"])
        if await db.is_user_banned(user_id):
            raise web.HTTPForbidden(text="Access denied.")
        secure_hash = request.rel_url.query.get("hash", "")
        return await stream_media(request, chat_id, message_id, secure_hash)
    except InvalidStreamHash as e:
        raise web.HTTPForbidden(text=e.message)
    except StreamFileNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid link.")
    except (AttributeError, BadStatusLine, ConnectionResetError):
        return web.Response(status=499)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(f"stream error: {e}")
        raise web.HTTPInternalServerError(text="Something went wrong.")


@routes.get(r"/watch/{token}", allow_head=True)
async def watch_route_handler(request: web.Request):
    if not STREAM_MODE:
        raise web.HTTPNotFound(text="Streaming is disabled on this bot.")
    try:
        chat_id, message_id, user_id = decode_stream_token(request.match_info["token"])
        if await db.is_user_banned(user_id):
            raise web.HTTPForbidden(text="Access denied.")
        secure_hash = request.rel_url.query.get("hash", "")
        html = await render_watch_page(chat_id, message_id, secure_hash, user_id)
        return web.Response(text=html, content_type="text/html")
    except InvalidStreamHash as e:
        raise web.HTTPForbidden(text=e.message)
    except StreamFileNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Invalid link.")
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(f"watch page error: {e}")
        raise web.HTTPInternalServerError(text="Something went wrong.")
