"""
Fast Download / Watch Online streaming support.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import math
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
try:
    from pyrogram.errors import InternalServerError
except ImportError:                      # older Pyrogram: the 500 errors have no common class
    from pyrogram.errors import Timeout as InternalServerError
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

from config import API_HASH, API_ID, BOT_TOKEN, STREAM_SECRET, URL

logger = logging.getLogger(__name__)

MEDIA_TYPES = ("document", "video", "audio", "photo", "animation", "voice", "video_note")
CHUNK_SIZE = 1024 * 1024
_RENEW_LOCK = asyncio.Lock()
_DC_LOCK = asyncio.Lock()
_POOL_LOCK = asyncio.Lock()
_POOL: Dict[int, list] = {}          # data centre -> connections used together (only the bot's own DC)
POOL_SIZE = 3
_GAP = 0.015                         # seconds between two GetFile requests (all streams of this bot)
_MIN_GAP, _MAX_GAP = 0.008, 1.0
_NEXT_SLOT = 0.0
_LAST_SLOWDOWN = 0.0
_OK_STREAK = 0
_PACE_LOCK = asyncio.Lock()
_GET_LIMIT = asyncio.Semaphore(24)     # GetFile requests in flight at the same time (all streams of this bot)
_FLOOD_UNTIL = 0.0                   # time until which Telegram asked us to wait
_LAST_RENEW: Dict[int, float] = {}
PREFETCH = 6   # parts asked from Telegram at the same time (3 MB in flight per open stream)

# ---- disk cache: the file is pulled from Telegram into a temporary file and served from there ----
CACHE_ON = os.getenv("STREAM_CACHE", "1") != "0"
CACHE_DIR = os.getenv("STREAM_CACHE_DIR", "/tmp/stream_cache")
CACHE_TTL = 24 * 3600                                                    # a cached file lives 24 hours
CACHE_MAX = int(float(os.getenv("STREAM_CACHE_GB", "10")) * 1024 ** 3)   # total cache size per bot
CACHE_MIN = int(float(os.getenv("STREAM_CACHE_MIN_MB", "8")) * 1024 ** 2)  # smaller files are streamed directly
CACHE_KEEP_FREE = 3 * 1024 ** 3                                          # always leave this much of the disk free
CACHE_PREFETCH = 20                                                      # parts asked from Telegram at once while filling
CACHE_WINDOW = 48 * 1024 ** 2                                            # a request this far ahead of the fill still waits for it
CACHE_IDLE = 90                                                          # stop filling when nobody has read for this long (seconds)
_CACHE: Dict[str, "_CacheEntry"] = {}
_CACHE_TASK = None
_CACHE_PURGED = False

# Logo shown at the top-left of the watch page (any public image link).
LOGO_URL = "https://files.catbox.moe/3yc512.png"


class StreamFileNotFound(Exception):
    message = "File not found. The link may be broken or the file was removed."


class InvalidStreamHash(Exception):
    message = "Invalid or expired link."


def _secret() -> bytes:
    # STREAM_SECRET if set, otherwise derived from BOT_TOKEN (no new variable needed).
    base = STREAM_SECRET or ("stream-link:" + BOT_TOKEN)
    return hashlib.sha256(base.encode("utf-8")).digest()


def _sign(payload: str) -> str:
    digest = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:12]).decode("ascii").rstrip("=")


def encode_stream_token(chat_id: int, message_id: int, user_id: int) -> str:
    """Signed link token: chat:msg:user + HMAC. The links do not expire, but they
    are tied to the user they were made for, so the web server can refuse a
    banned user and nobody can forge a link for another user or file."""
    raw_token = f"{chat_id}:{message_id}:{int(user_id)}".encode("ascii")
    payload = base64.urlsafe_b64encode(raw_token).decode("ascii").rstrip("=")
    return f"{payload}.{_sign(payload)}"


def decode_stream_token(token: str) -> Tuple[int, int, int]:
    """Returns (chat_id, message_id, user_id). Raises InvalidStreamHash for a
    forged, tampered or old-format token."""
    try:
        payload, _, sig = token.partition(".")
        if not payload or not sig or not hmac.compare_digest(sig, _sign(payload)):
            raise InvalidStreamHash
        padded = payload + "=" * (-len(payload) % 4)
        chat_s, msg_s, uid_s = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii").split(":", 2)
        return int(chat_s), int(msg_s), int(uid_s)
    except InvalidStreamHash:
        raise
    except Exception:
        raise InvalidStreamHash


def get_media_from_message(message) -> Any:
    for attr in MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            return media
    return None


def get_hash(media) -> str:
    return (getattr(media, "file_unique_id", "") or "")[:6]


def _dl_url(token: str, file_unique_id: str, file_name: str, force_download: bool) -> str:
    secure_hash = (file_unique_id or "")[:6]
    safe_name = urllib.parse.quote(file_name or "file")
    base = URL if URL.endswith("/") else URL + "/"
    query = f"hash={secure_hash}"
    if force_download:
        query += "&dl=1"
    return f"{base}dl/{token}/{safe_name}?{query}"


def build_stream_urls(chat_id: int, message_id: int, file_unique_id: str, file_name: str,
                      user_id: int) -> Tuple[str, str]:
    token = encode_stream_token(chat_id, message_id, user_id)
    download_url = _dl_url(token, file_unique_id, file_name, force_download=True)
    secure_hash = (file_unique_id or "")[:6]
    base = URL if URL.endswith("/") else URL + "/"
    watch_url = f"{base}watch/{token}?hash={secure_hash}"
    return download_url, watch_url


def humanbytes(size) -> str:
    if not size:
        return "0 B"
    power = 1024
    n = 0
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(size)
    while size >= power and n < len(units) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {units[n]}"



async def _pace():
    """Starts GetFile requests evenly spaced (not all at once), so Telegram does not answer FLOOD_WAIT."""
    global _NEXT_SLOT
    async with _PACE_LOCK:
        now = time.time()
        start = max(now, _NEXT_SLOT)
        _NEXT_SLOT = start + _GAP
    if start > now:
        await asyncio.sleep(start - now)


def _slow_down():
    """Telegram said FLOOD_WAIT: space the requests wider (once per burst of floods)."""
    global _GAP, _LAST_SLOWDOWN, _OK_STREAK
    _OK_STREAK = 0
    if time.time() - _LAST_SLOWDOWN > 1.0:
        _LAST_SLOWDOWN = time.time()
        _GAP = min(_GAP * 1.6, _MAX_GAP)


def _speed_up():
    """Many requests in a row worked: space them a little closer again."""
    global _GAP, _OK_STREAK
    _OK_STREAK += 1
    if _OK_STREAK >= 40:
        _OK_STREAK = 0
        _GAP = max(_GAP * 0.9, _MIN_GAP)



# ---------------------------------------------------------------------------
# Helper bots: other bots' tokens (env STREAM_HELPER_TOKENS, comma separated) are logged in inside
# this bot too, and a stream's parts are fetched through all of them at the same time. Telegram
# limits download speed per bot account, so every extra account adds its own share of speed.
# ---------------------------------------------------------------------------
HELPER_MAX = 3
_HELPERS = None            # list of ByteStreamer (one per started helper bot) once ready
_HELPER_TASK = None
_LANE_SESSIONS: Dict[Tuple[int, int], Any] = {}   # (helper index, data centre) -> connection
_LANE_TASKS: Dict[Tuple[int, int], Any] = {}


class _Lane:
    """One bot account used for a stream: its connections and the file location as THAT bot sees it."""
    __slots__ = ("sessions", "location", "flood_until", "name")

    def __init__(self, sessions, location, name):
        self.sessions, self.location, self.name, self.flood_until = sessions, location, name, 0.0


async def _start_helpers():
    global _HELPERS
    out = []
    tokens = []
    for t in re.split(r"[\s,]+", (os.getenv("STREAM_HELPER_TOKENS") or "").replace('"', "").replace("'", "")):
        if t and ":" in t and t != BOT_TOKEN and t not in tokens:
            tokens.append(t)
    # a user account (session string): Telegram gives user accounts much more download speed than bots
    ss = (os.getenv("STREAM_SESSION_STRING") or "").strip().strip("\"'")
    if ss:
        try:
            c = Client(f"streamuser_{BOT_TOKEN.split(':')[0]}", api_id=API_ID, api_hash=API_HASH,
                       session_string=ss, in_memory=True, no_updates=True)
            await asyncio.wait_for(c.start(), timeout=60)
            try:                                   # a user account must load its chats to see the log channel
                async for _ in c.get_dialogs(limit=300):
                    pass
            except Exception as e:
                logger.warning(f"[STREAM] user account could not load its chats: {type(e).__name__}")
            bs = ByteStreamer(c)
            bs.is_user = True
            out.append(bs)
        except Exception as e:
            logger.warning(f"[STREAM] user account did not start: {type(e).__name__}: {e}")
    for t in tokens[:HELPER_MAX]:
        try:
            c = Client(f"streamhelper_{BOT_TOKEN.split(':')[0]}_{t.split(':')[0]}", api_id=API_ID, api_hash=API_HASH,
                       bot_token=t, in_memory=True, no_updates=True)
            await asyncio.wait_for(c.start(), timeout=60)
            out.append(ByteStreamer(c))
        except Exception as e:
            logger.warning(f"[STREAM] helper bot {t.split(':')[0]} did not start: {type(e).__name__}")
    _HELPERS = out
    logger.warning(f"[STREAM] helper bots ready: {len(out)}")


async def _prep_lane(idx, helper, file_id):
    key = (idx, file_id.dc_id)
    try:
        _LANE_SESSIONS[key] = await helper.generate_media_session(file_id)
    except Exception as e:
        logger.warning(f"[STREAM] helper {idx} connection to dc={file_id.dc_id} failed: {type(e).__name__}")
        await asyncio.sleep(60)             # try again after a minute, not at once
    finally:
        _LANE_TASKS.pop(key, None)


class ByteStreamer:
    def __init__(self, client):
        self.client = client
        self.cached_file_ids: Dict[Tuple[int, int], FileId] = {}

    async def get_file_properties(self, chat_id: int, message_id: int) -> FileId:
        key = (chat_id, message_id)
        cached = self.cached_file_ids.get(key)
        if cached is not None:
            return cached
        return await self._generate_file_properties(chat_id, message_id)

    
    async def _generate_file_properties(self, chat_id: int, message_id: int) -> FileId:
        key = (chat_id, message_id)

        message = await self.client.get_messages(chat_id, message_id)
        if not message or message.empty:
            raise StreamFileNotFound

        media = get_media_from_message(message)
        if not media:
            raise StreamFileNotFound

        file_id = FileId.decode(media.file_id)
        file_id.file_size = getattr(media, "file_size", 0) or 0
        file_id.mime_type = getattr(media, "mime_type", "") or ""
        file_id.file_name = getattr(media, "file_name", "") or ""
        file_id.unique_id = getattr(media, "file_unique_id", "") or ""
        file_id.caption = (getattr(message, "caption", None) or "").strip()

        self.cached_file_ids[key] = file_id
        return file_id


    async def generate_media_session(self, file_id: FileId) -> Session:
        """One connection per data centre. Making it for a data centre other than the bot's own
        needs auth.ExportAuthorization, which Telegram limits hard and which can also hang:
        one task at a time, a time limit, and a FLOOD_WAIT is waited out (not failed)."""
        client = self.client
        media_session = client.media_sessions.get(file_id.dc_id)
        if media_session is not None:
            return media_session
        async with _DC_LOCK:
            media_session = client.media_sessions.get(file_id.dc_id)
            if media_session is not None:
                return media_session
            for attempt in range(4):
                try:
                    logger.warning(f"[STREAM] opening connection to dc={file_id.dc_id} attempt={attempt}")
                    return await asyncio.wait_for(self._open_media_session(file_id), timeout=40)
                except FloodWait as e:
                    logger.warning(f"[STREAM] dc={file_id.dc_id} connection flood wait={e.value}s")
                    if attempt == 3 or int(e.value) > 300:
                        raise
                    await asyncio.sleep(int(e.value) + 1)
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning(f"[STREAM] dc={file_id.dc_id} connection timed out attempt={attempt}")
                    if attempt == 3:
                        raise

    async def media_pool(self, file_id: FileId, base):
        """Several connections to the bot's OWN data centre (same login, so no extra Telegram
        permission is needed). Parts are spread over them, which is faster than one connection.
        For any other data centre the single connection is used."""
        dc = file_id.dc_id
        pool = _POOL.get(dc)
        if pool:
            return pool
        async with _POOL_LOCK:
            pool = _POOL.get(dc)
            if pool:
                return pool
            pool = [base]
            # Extra connections to the same data centre. For the bot's own data centre they reuse
            # its login; for another one each needs a Telegram permission (ExportAuthorization),
            # which Telegram limits, so a FLOOD_WAIT or any error simply stops adding more.
            for _ in range(POOL_SIZE - 1):
                try:
                    extra = await asyncio.wait_for(self._open_media_session(file_id, store=False), timeout=30)
                    pool.append(extra)
                except FloodWait as e:
                    logger.warning(f"[STREAM] dc={dc} extra connection flood wait={e.value}s: using {len(pool)}")
                    break
                except Exception as e:
                    logger.warning(f"[STREAM] extra connection for dc={dc} failed: {type(e).__name__}")
                    break
            _POOL[dc] = pool
            logger.warning(f"[STREAM] dc={dc} using {len(pool)} connection(s)")
            return pool

    async def _open_media_session(self, file_id: FileId, store: bool = True) -> Session:
        client = self.client
        media_session = None

        test_mode = await client.storage.test_mode()
        if file_id.dc_id != await client.storage.dc_id():
            media_session = Session(
                client,
                file_id.dc_id,
                await Auth(client, file_id.dc_id, test_mode).create(),
                test_mode,
                is_media=True,
            )
            await media_session.start()
            for _ in range(6):
                exported_auth = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id)
                )
                try:
                    await media_session.send(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id, bytes=exported_auth.bytes
                        )
                    )
                    break
                except AuthBytesInvalid:
                    continue
            else:
                await media_session.stop()
                raise AuthBytesInvalid
        else:
            media_session = Session(
                client,
                file_id.dc_id,
                await client.storage.auth_key(),
                test_mode,
                is_media=True,
            )
            await media_session.start()

        if store:
            client.media_sessions[file_id.dc_id] = media_session
        return media_session

    @staticmethod
    async def get_location(file_id: FileId):
        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    async def yield_file(self, file_id, offset, first_part_cut, last_part_cut, part_count, chunk_size, source=None, prefetch=None):
        """Sends the file part by part. Several parts are asked from Telegram at the same time and
        spread over all available bot accounts (this bot + helper bots) and connections."""
        global _HELPER_TASK
        media_session = await self.generate_media_session(file_id)
        location = await self.get_location(file_id)
        try:
            pool = await self.media_pool(file_id, media_session)
        except Exception:
            pool = [media_session]
        lanes = [_Lane(pool, location, "main")]

        # helper bots: used as soon as their connection to this data centre is ready
        if source and (os.getenv("STREAM_HELPER_TOKENS") or os.getenv("STREAM_SESSION_STRING")):
            if _HELPER_TASK is None:
                _HELPER_TASK = asyncio.ensure_future(_start_helpers())
            for idx, h in enumerate(_HELPERS or []):
                sess = _LANE_SESSIONS.get((idx, file_id.dc_id))
                if sess is None:
                    if (idx, file_id.dc_id) not in _LANE_TASKS:
                        _LANE_TASKS[(idx, file_id.dc_id)] = asyncio.ensure_future(_prep_lane(idx, h, file_id))
                    continue
                try:
                    hf = await asyncio.wait_for(h.get_file_properties(*source), timeout=15)
                    lanes.append(_Lane([sess], await h.get_location(hf), "user" if getattr(h, "is_user", False) else f"helper{idx}"))
                except Exception as e:
                    logger.warning(f"[STREAM] helper {idx} cannot read the file: {type(e).__name__}")
        logger.warning(f"[STREAM] start dc={file_id.dc_id} accounts={len(lanes)} offset_mb={offset // chunk_size} parts={part_count}")

        async def renew_session(failed):
            """The connection to Telegram's file server stopped answering: open a fresh one.
            Opening one for another data centre uses auth.ExportAuthorization, which Telegram
            limits hard (waits of minutes), so: at most once a minute per data centre, one task
            at a time, and the old connection is kept if the new one cannot be made."""
            nonlocal media_session
            dc = file_id.dc_id
            async with _RENEW_LOCK:
                cur = self.client.media_sessions.get(dc)
                if cur is not None and cur is not failed:
                    media_session = cur                      # another task already renewed it
                    return
                if time.time() - _LAST_RENEW.get(dc, 0) < 60:
                    return                                   # too soon: keep what we have
                _LAST_RENEW[dc] = time.time()
                self.client.media_sessions.pop(dc, None)
                try:
                    media_session = await self.generate_media_session(file_id)
                except Exception:
                    self.client.media_sessions[dc] = failed  # keep the old one
                    return
                try:
                    asyncio.ensure_future(failed.stop())
                except Exception:
                    pass

        async def get(i):
            # Part i goes to account (i % number of accounts); a retry goes to the next account.
            # Telegram's FLOOD_WAIT pauses only the account that got it.
            for attempt in range(12):
                lane = lanes[(i + attempt) % len(lanes)]
                wait = lane.flood_until - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                used = lane.sessions[(i // len(lanes)) % len(lane.sessions)]
                try:
                    await _pace()
                    async with _GET_LIMIT:
                        res = await used.send(
                            raw.functions.upload.GetFile(location=lane.location, offset=offset + i * chunk_size, limit=chunk_size)
                        )
                    _speed_up()
                    return res
                except FloodWait as e:
                    if int(e.value) >= 3 or attempt >= 3:
                        logger.warning(f"[STREAM] flood {lane.name} dc={file_id.dc_id} wait={e.value}s attempt={attempt} gap={_GAP:.2f}")
                    if attempt == 11:
                        raise
                    lane.flood_until = max(lane.flood_until, time.time() + min(int(e.value), 30) + 0.3)
                    _slow_down()
                except InternalServerError:
                    logger.warning(f"[STREAM] telegram 500 timeout {lane.name} dc={file_id.dc_id} attempt={attempt}")
                    if attempt >= 5:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
                except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError):
                    logger.warning(f"[STREAM] no answer {lane.name} dc={file_id.dc_id} attempt={attempt}")
                    if attempt >= 3:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
                    if lane.name != "main":
                        if len(lanes) > 1 and lane in lanes:
                            lanes.remove(lane)          # this helper stopped answering: leave it out
                    elif len(lane.sessions) > 1 and used in lane.sessions:
                        lane.sessions.remove(used)      # drop the connection that stopped answering
                        try:
                            asyncio.ensure_future(used.stop())
                        except Exception:
                            pass
                    else:
                        await renew_session(used)
                        lane.sessions[:] = [media_session]

        def fetch(i):
            return asyncio.ensure_future(get(i))

        ahead = min((prefetch or PREFETCH) + 3 * (len(lanes) - 1), part_count)
        tasks = {i: fetch(i) for i in range(ahead)}
        next_i = ahead
        try:
            for i in range(part_count):
                r = await tasks.pop(i)
                if next_i < part_count:
                    tasks[next_i] = fetch(next_i)
                    next_i += 1
                if not isinstance(r, raw.types.upload.File):
                    if i == 0:
                        raise StreamFileNotFound
                    break
                chunk = r.bytes
                if not chunk:
                    break

                current_part = i + 1
                if part_count == 1:
                    data = chunk[first_part_cut:last_part_cut]
                elif current_part == 1:
                    data = chunk[first_part_cut:]
                elif current_part == part_count:
                    data = chunk[:last_part_cut]
                else:
                    data = chunk

                if data:
                    yield data
        finally:
            for t in tasks.values():
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)


_streamer: Optional[ByteStreamer] = None


def get_streamer() -> ByteStreamer:
    global _streamer
    if _streamer is None:
        from Peter.bot import StreamBot
        _streamer = ByteStreamer(StreamBot)
    return _streamer


_WATCH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta name="theme-color" content="#0a0708" />
<meta name="color-scheme" content="dark" />
<meta name="supported-color-schemes" content="dark" />
<title>{file_name} | ChillFlizX</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0a0708; --surface: #151012; --line: rgba(255,255,255,.07);
    --text: #eceef6; --dim: #8a90a6; --accent: #ef2f3c; --accent-2: #ff6a3d;
  }}
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html {{ background: var(--bg); }}
  body {{
    margin: 0; min-height: 100vh; color: var(--text);
    font-family: Inter, 'Segoe UI', Roboto, system-ui, Arial, sans-serif;
    background:
      radial-gradient(900px 500px at 8% -10%, rgba(239,47,60,.16), transparent 60%),
      radial-gradient(800px 500px at 100% 105%, rgba(255,106,61,.10), transparent 60%),
      var(--bg);
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 18px 16px 40px; }}

  header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }}
  .logo {{ display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .2px; }}
  .mark {{
    width: 38px; height: 38px; border-radius: 10px; display: grid; place-items: center; overflow: hidden;
    color: #fff; font-weight: 800;
  }}
  .mark img {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
  .mark.noimg {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); }}
  .join {{
    color: var(--text); text-decoration: none; font-size: .82rem; font-weight: 600;
    padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line); background: rgba(255,255,255,.03);
  }}

  .player {{
    position: relative; border-radius: 18px; overflow: hidden; background: #000;
    border: 1px solid var(--line); box-shadow: 0 30px 80px -30px rgba(239,47,60,.35);
  }}
  .player video {{ display: block; width: 100%; max-height: 72vh; aspect-ratio: 16 / 9; background: #000; object-fit: contain; }}
  .player.audio {{
    background: linear-gradient(160deg, #1f1014, #0e0809); padding: 34px 22px 26px; text-align: center;
  }}
  .player.audio .disc {{
    width: 96px; height: 96px; margin: 0 auto 22px; border-radius: 50%; display: grid; place-items: center;
    background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 14px 40px -10px rgba(239,47,60,.55);
  }}
  .player.audio audio {{ width: 100%; }}

  .hint {{
    display: none; margin-top: 12px; padding: 11px 14px; border-radius: 12px; font-size: .85rem;
    color: #ffd9a0; background: rgba(255,170,60,.08); border: 1px solid rgba(255,170,60,.22);
  }}
  .hint.show {{ display: block; }}

  .details {{ margin-top: 20px; }}
  h1 {{ margin: 0; font-size: clamp(1.05rem, 2.4vw, 1.5rem); line-height: 1.35; font-weight: 650; word-break: break-word; }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
  .chip {{
    font-size: .76rem; font-weight: 600; letter-spacing: .3px; color: var(--dim);
    padding: 6px 11px; border-radius: 999px; background: rgba(255,255,255,.04); border: 1px solid var(--line);
  }}
  .chip b {{ color: var(--text); font-weight: 650; }}

  .actions {{ display: grid; grid-template-columns: 1.3fr 1fr; gap: 10px; margin-top: 22px; }}
  .btn {{
    display: flex; align-items: center; justify-content: center; gap: 9px; min-height: 50px; padding: 0 16px;
    border-radius: 13px; text-decoration: none; font-weight: 650; font-size: .94rem; cursor: pointer;
    color: var(--text); background: var(--surface); border: 1px solid var(--line); font-family: inherit;
    transition: transform .12s ease, border-color .15s ease;
  }}
  .btn:hover {{ border-color: rgba(255,255,255,.18); }}
  .btn:active {{ transform: scale(.98); }}
  .btn.primary {{ background: linear-gradient(120deg, #e11d2e, #ff4b3a); color: #fff; border: none; }}
  .btn svg {{ width: 18px; height: 18px; flex: none; }}

  .apps {{ margin-top: 22px; padding: 16px; border-radius: 16px; background: var(--surface); border: 1px solid var(--line); }}
  .apps h2 {{ margin: 0 0 12px; font-size: .78rem; letter-spacing: 1.2px; text-transform: uppercase; color: var(--dim); font-weight: 650; }}
  .app-row {{ display: flex; flex-wrap: wrap; gap: 9px; }}
  .app {{
    display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px; border-radius: 10px;
    text-decoration: none; color: var(--text); font-size: .86rem; font-weight: 600;
    background: rgba(255,255,255,.04); border: 1px solid var(--line);
  }}
  .app:hover {{ border-color: var(--accent); }}
  .app svg {{ width: 14px; height: 14px; color: var(--accent); }}

  footer {{ margin-top: 30px; text-align: center; color: var(--dim); font-size: .78rem; }}
  .toast {{
    position: fixed; left: 50%; bottom: 26px; transform: translate(-50%, 20px); opacity: 0; pointer-events: none;
    padding: 10px 18px; border-radius: 999px; font-size: .85rem; font-weight: 600; color: #fff; background: var(--accent);
    transition: all .25s ease;
  }}
  .toast.show {{ opacity: 1; transform: translate(-50%, 0); }}
  @media (max-width: 560px) {{ .actions {{ grid-template-columns: 1fr; }} .wrap {{ padding-top: 14px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><div class="mark"><img src="{logo_url}" alt="ChillFlizX" onerror="this.parentNode.classList.add('noimg');this.parentNode.textContent='C'"></div><span>ChillFlizX</span></div>
    <a class="join" href="https://t.me/ChillFlizX" target="_blank" rel="noopener">Join Channel</a>
  </header>

  <div class="player {media_class}">
    {media_tag}
  </div>
  <div class="hint" id="hint">This file can't play in the browser. Use <b>Open in app</b> below or download it.</div>

  <div class="details">
    <h1>{file_name}</h1>
    <div class="chips">
      <span class="chip"><b>{file_size}</b></span>
      <span class="chip"><b>{file_ext}</b></span>
      <span class="chip">{file_kind}</span>
    </div>
  </div>

  <div class="actions">
    <a class="btn primary" href="{download_url}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-5-5m5 5l5-5M4 20h16"/></svg>
      Fast Download
    </a>
    <button class="btn" type="button" id="copy" data-url="{inline_url}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/></svg>
      Copy Stream Link
    </button>
  </div>

  <div class="apps">
    <h2>Open in app</h2>
    <div class="app-row">
      <a class="app" href="intent:{inline_url}#Intent;action=android.intent.action.VIEW;type=video/*;package=com.mxtech.videoplayer.ad;end"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>MX Player</a>
      <a class="app" href="intent:{inline_url}#Intent;action=android.intent.action.VIEW;type=video/*;package=org.videolan.vlc;end"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>VLC</a>
      <a class="app" href="playit://playerv2/video?url={inline_url}"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>PLAYit</a>
      <a class="app" href="{inline_url}" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>Direct Link</a>
    </div>
  </div>

  <footer>ChillFlizX &middot; Enjoy your stream</footer>
</div>
<div class="toast" id="toast">Link copied</div>
<script>
  (function () {{
    var media = document.querySelector('video, audio');
    if (media) media.addEventListener('error', function () {{ document.getElementById('hint').classList.add('show'); }});
    var btn = document.getElementById('copy'), toast = document.getElementById('toast');
    function done(ok) {{
      toast.textContent = ok ? 'Link copied' : 'Could not copy';
      toast.classList.add('show'); setTimeout(function () {{ toast.classList.remove('show'); }}, 1800);
    }}
    btn.addEventListener('click', function () {{
      var url = btn.getAttribute('data-url');
      if (navigator.clipboard && window.isSecureContext) {{
        navigator.clipboard.writeText(url).then(function () {{ done(true); }}, function () {{ done(false); }});
      }} else {{
        var t = document.createElement('textarea'); t.value = url; document.body.appendChild(t); t.select();
        var ok = false; try {{ ok = document.execCommand('copy'); }} catch (e) {{}}
        document.body.removeChild(t); done(ok);
      }}
    }});
  }})();
</script>
</body>
</html>"""


class _CacheEntry:
    def __init__(self, key, path, size):
        self.key = key
        self.path = path
        self.size = size
        self.written = 0                 # bytes already on disk (always from the start of the file)
        self.done = False
        self.failed = False
        self.readers = 0
        self.created = time.time()
        self.last_active = time.time()
        self.cond = asyncio.Condition()
        self.task = None


def _cache_remove(entry):
    _CACHE.pop(entry.key, None)
    try:
        os.remove(entry.path)
    except OSError:
        pass


def _cache_used() -> int:
    return sum(e.size for e in _CACHE.values() if not e.failed)


async def _cache_cleaner():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for e in list(_CACHE.values()):
            if e.readers == 0 and (e.failed or (e.done and now - e.created > CACHE_TTL)):
                logger.warning(f"[CACHE] removed {e.key[:10]} (expired)" if not e.failed else f"[CACHE] removed {e.key[:10]} (failed)")
                _cache_remove(e)


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


async def _cache_fill(streamer, entry: _CacheEntry, file_id, source):
    part_count = math.ceil(entry.size / CHUNK_SIZE)
    last_cut = ((entry.size - 1) % CHUNK_SIZE) + 1
    t0 = time.time()
    fd = os.open(entry.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        async for chunk in streamer.yield_file(file_id, 0, 0, last_cut, part_count, CHUNK_SIZE,
                                               source=source, prefetch=CACHE_PREFETCH):
            await asyncio.to_thread(_write_all, fd, chunk)
            entry.written += len(chunk)
            async with entry.cond:
                entry.cond.notify_all()
            if entry.readers == 0 and time.time() - entry.last_active > CACHE_IDLE:
                logger.warning(f"[CACHE] stopped {entry.key[:10]}: nobody is reading")
                break
        entry.done = entry.written >= entry.size
        if entry.done:
            took = max(time.time() - t0, 0.1)
            logger.warning(f"[CACHE] done {entry.size / 1048576:.0f} MB in {took:.0f}s = {entry.size / 1048576 / took:.1f} MB/s")
        else:
            entry.failed = True
    except asyncio.CancelledError:
        entry.failed = True
        raise
    except Exception as e:
        logger.warning(f"[CACHE] fill failed {entry.key[:10]}: {type(e).__name__} {e}")
        entry.failed = True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        async with entry.cond:
            entry.cond.notify_all()


def _cache_entry(streamer, file_id, source, size: int, start: int):
    """Returns the cache entry that should serve this request, or None (= stream directly)."""
    global _CACHE_TASK, _CACHE_PURGED
    if not CACHE_ON or size < CACHE_MIN or size > CACHE_MAX:
        return None
    key = f"{file_id.unique_id}_{size}"
    e = _CACHE.get(key)
    if e is not None:
        if e.failed:
            if e.readers:
                return None
            _cache_remove(e)
            e = None
        elif e.done or start <= e.written + CACHE_WINDOW:
            return e
        else:
            return None
    if start > CACHE_WINDOW:
        return None                      # seeking far into a new file: stream directly
    try:
        if not _CACHE_PURGED:
            _CACHE_PURGED = True
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        if shutil.disk_usage(CACHE_DIR).free < size + CACHE_KEEP_FREE:
            return None
        while _cache_used() + size > CACHE_MAX:
            old = [x for x in _CACHE.values() if x.done and x.readers == 0]
            if not old:
                return None
            _cache_remove(min(old, key=lambda x: x.created))
        e = _CacheEntry(key, os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest()), size)
        os.close(os.open(e.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))   # exists before the first reader opens it
    except OSError as ex:
        logger.warning(f"[CACHE] disabled for this file: {ex}")
        return None
    _CACHE[key] = e
    if _CACHE_TASK is None:
        _CACHE_TASK = asyncio.ensure_future(_cache_cleaner())
    logger.warning(f"[CACHE] start {size / 1048576:.0f} MB key={key[:10]}")
    e.task = asyncio.ensure_future(_cache_fill(streamer, e, file_id, source))
    return e


async def _serve_cached(entry: _CacheEntry, response, start: int, end: int) -> int:
    """Sends bytes start..end from the cache file. Returns the next byte not sent
    (end + 1 when everything was sent); the caller streams the rest directly."""
    pos = start
    entry.readers += 1
    try:
        fd = os.open(entry.path, os.O_RDONLY)
    except OSError:
        entry.readers -= 1
        return pos
    try:
        stalled = time.time()
        while pos <= end:
            entry.last_active = time.time()
            avail = entry.written - pos
            if avail <= 0:
                if entry.done or entry.failed or time.time() - stalled > 60:
                    return pos
                async with entry.cond:
                    try:
                        await asyncio.wait_for(entry.cond.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                continue
            stalled = time.time()
            n = min(end + 1 - pos, avail, 4 * CHUNK_SIZE)
            data = await asyncio.to_thread(os.pread, fd, n, pos)
            if not data:
                return pos
            await response.write(data)
            pos += len(data)
        return pos
    finally:
        entry.readers -= 1
        entry.last_active = time.time()
        try:
            os.close(fd)
        except OSError:
            pass


async def _stream_direct(streamer, response, file_id, start: int, end: int, source):
    offset = start - (start % CHUNK_SIZE)
    first_part_cut = start - offset
    part_count = math.ceil((end + 1) / CHUNK_SIZE) - (offset // CHUNK_SIZE)
    last_part_cut = ((end % CHUNK_SIZE) + 1)
    async for chunk in streamer.yield_file(file_id, offset, first_part_cut, last_part_cut, part_count, CHUNK_SIZE, source=source):
        await response.write(chunk)


async def stream_media(request: web.Request, chat_id: int, message_id: int, secure_hash: str) -> web.StreamResponse:
    streamer = get_streamer()
    file_id = await streamer.get_file_properties(chat_id, message_id)

    if file_id.unique_id[:6] != secure_hash:
        raise InvalidStreamHash

    file_size = file_id.file_size
    if not file_size:
        raise StreamFileNotFound

    range_header = request.headers.get("Range")
    start, end = 0, file_size - 1

    if range_header:
        try:
            unit, _, value = range_header.partition("=")
            if unit.strip() != "bytes":
                raise ValueError
            first, _, last = value.partition("-")
            if not first:
                suffix_length = int(last)
                if suffix_length <= 0:
                    raise ValueError
                start = max(0, file_size - suffix_length)
            else:
                start = int(first)
                end = int(last) if last else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError
            end = min(end, file_size - 1)
        except (ValueError, TypeError):
            return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})

    length = end - start + 1
    file_name = download_filename(getattr(file_id, "caption", ""), file_id.file_name)
    ascii_name = file_name.encode("ascii", "ignore").decode().replace('"', "").strip() or "file"
    headers = {
        "Content-Type": file_id.mime_type or mimetypes.guess_type(file_id.file_name or "")[0] or "application/octet-stream",
        "Content-Length": str(length),
        "Content-Disposition": (
            ('attachment' if request.rel_url.query.get("dl") == "1" else 'inline')
            + f'; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(file_name)}'
        ),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await response.prepare(request)
    if request.method == "HEAD":
        return response

    source = (chat_id, message_id)
    try:
        pos = start
        entry = _cache_entry(streamer, file_id, source, file_size, start)
        if entry is not None:
            pos = await _serve_cached(entry, response, start, end)
        if pos <= end:
            await _stream_direct(streamer, response, file_id, pos, end, source)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        raise
    except Exception:
        logger.exception("Error while streaming media")
        raise

    await response.write_eof()
    return response


_CAPTION_EXTS = (
    "mkv", "mp4", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp",
    "mp3", "m4a", "flac", "wav", "ogg", "aac", "opus", "zip", "rar", "7z", "pdf", "apk",
)
_CAPTION_RE = re.compile(r"^(.*?\.(?:" + "|".join(_CAPTION_EXTS) + r"))(?![A-Za-z0-9])", re.I | re.S)


def title_from_caption(caption: str, fallback: str) -> str:
    """Page title = the caption up to and including the file extension; whatever
    comes after the extension is ignored. No caption -> the file name."""
    caption = (caption or "").strip()
    if caption:
        m = _CAPTION_RE.match(caption)
        if m:
            return " ".join(m.group(1).split())
        first_line = caption.splitlines()[0].strip()
        if first_line:
            return first_line
    return (fallback or "file").replace("_", " ")


def download_filename(caption: str, raw_name: str) -> str:
    """File name used for the saved/streamed file: caption up to the extension,
    cleaned so it is valid as a file name (falls back to the real file name)."""
    raw_name = raw_name or "file"
    title = title_from_caption(caption, raw_name)
    title = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title)
    title = " ".join(title.split()).strip(" .")
    if not title:
        return raw_name
    stem, dot, ext = title.rpartition(".")
    if not dot or len(ext) > 5 or not ext.isalnum():
        # no extension in the title -> keep the real one
        real_ext = raw_name.rsplit(".", 1)[-1] if "." in raw_name else ""
        stem, ext = title, real_ext
    stem = stem[:150].rstrip(" .")
    return f"{stem}.{ext}" if ext else stem


async def render_watch_page(chat_id: int, message_id: int, secure_hash: str, user_id: int) -> str:
    streamer = get_streamer()
    file_id = await streamer.get_file_properties(chat_id, message_id)

    if file_id.unique_id[:6] != secure_hash:
        raise InvalidStreamHash

    token = encode_stream_token(chat_id, message_id, user_id)
    download_url = _dl_url(token, file_id.unique_id, file_id.file_name, force_download=True)
    inline_url = _dl_url(token, file_id.unique_id, file_id.file_name, force_download=False)
    mime_type = file_id.mime_type or ""
    raw_name = file_id.file_name or "file"
    file_name = html.escape(title_from_caption(getattr(file_id, "caption", ""), raw_name))
    file_size = humanbytes(file_id.file_size)
    ext = raw_name.rsplit(".", 1)[-1].upper() if "." in raw_name else ""
    is_audio = mime_type.startswith("audio")
    safe_inline = html.escape(inline_url, quote=True)

    if is_audio:
        media_tag = (
            '<div class="disc"><svg width="40" height="40" viewBox="0 0 24 24" fill="#fff">'
            '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>'
            f'<audio controls preload="metadata" src="{safe_inline}"></audio>'
        )
    else:
        media_tag = f'<video controls playsinline preload="metadata" src="{safe_inline}"></video>'

    return _WATCH_PAGE.format(
        file_name=file_name,
        file_size=file_size,
        file_ext=html.escape(ext or "FILE"),
        file_kind="Audio" if is_audio else "Video",
        media_class="audio" if is_audio else "",
        media_tag=media_tag,
        download_url=html.escape(download_url, quote=True),
        inline_url=safe_inline,
        logo_url=html.escape(LOGO_URL, quote=True),
    )
