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
import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from pyrogram import raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

from config import BOT_TOKEN, STREAM_SECRET, URL

logger = logging.getLogger(__name__)

MEDIA_TYPES = ("document", "video", "audio", "photo", "animation", "voice", "video_note")
CHUNK_SIZE = 1024 * 1024
PREFETCH = 3   # parts asked from Telegram at the same time (3 MB in flight per open stream)

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
        client = self.client
        media_session = client.media_sessions.get(file_id.dc_id)
        if media_session is not None:
            return media_session

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

    async def yield_file(self, file_id, offset, first_part_cut, last_part_cut, part_count, chunk_size):
        """Sends the file part by part. Up to PREFETCH parts are asked from Telegram at the same
        time, so the speed is not limited by waiting for each 1 MB part one after another."""
        media_session = await self.generate_media_session(file_id)
        location = await self.get_location(file_id)

        async def get(i):
            # Telegram sometimes answers FLOOD_WAIT (a second or two) to GetFile: wait and ask again
            for attempt in range(6):
                try:
                    return await media_session.send(
                        raw.functions.upload.GetFile(location=location, offset=offset + i * chunk_size, limit=chunk_size)
                    )
                except FloodWait as e:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(min(int(e.value), 10) + 0.3)

        def fetch(i):
            return asyncio.ensure_future(get(i))

        ahead = min(PREFETCH, part_count)
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

    offset = start - (start % CHUNK_SIZE)
    first_part_cut = start - offset
    part_count = math.ceil((end + 1) / CHUNK_SIZE) - (offset // CHUNK_SIZE)

    # Last chunk ke andar kitne bytes bhejne hain
    last_part_cut = ((end % CHUNK_SIZE) + 1)

    try:
        async for chunk in streamer.yield_file(
            file_id, offset, first_part_cut, last_part_cut, part_count, CHUNK_SIZE
        ):
            await response.write(chunk)
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
