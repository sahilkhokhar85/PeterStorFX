"""
Fast Download / Watch Online streaming support.

This is a single-file, single-bot-client adaptation of the well known
Telegram "file-to-link" streaming technique (the same idea used by
DreamXBotz / FileStreamBot and friends): instead of re-downloading a
file to disk, Telegram's raw upload.GetFile API is called directly and
the bytes are streamed straight to the browser, chunk by chunk, with
proper HTTP Range support so a <video> tag can seek/play instantly and
a download manager can resume a partial download.

Design notes specific to this project (kept intentionally simpler than
a lot of the reference bots this technique is copied from):

- Single bot client only (no MULTI_CLIENT / work_loads load-balancing).
  This project runs one bot token, so there is nothing to balance.
- No separate BIN_CHANNEL / forwarding step. Every file this bot ever
  hands out is fetched by (chat_id, message_id) - that's exactly the
  same (channel, message id) pair genlink.py/commands.py already read
  the file from - so the stream link is built from data already in
  hand, with zero extra Telegram API calls.
- The (chat_id, message_id) pair is packed into one URL-safe token
  instead of a raw numeric path, since chat_id for a channel is a
  large negative number.
"""

import base64
import logging
import math
import mimetypes
import urllib.parse
import asyncio
from typing import Any, Dict, Optional, Tuple

from aiohttp import web
from pyrogram import raw, utils
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

from config import URL

logger = logging.getLogger(__name__)

MEDIA_TYPES = ("document", "video", "audio", "photo", "animation", "voice", "video_note")
CHUNK_SIZE = 1024 * 1024  # 1 MiB, must stay a multiple of 4096 (Telegram requirement)


class StreamFileNotFound(Exception):
    message = "File not found. The link may be broken or the file was removed."


class InvalidStreamHash(Exception):
    message = "Invalid or expired link."


# ---------------------------------------------------------------------------
# Token = base64("<chat_id>:<message_id>"), URL-safe, no padding.
# ---------------------------------------------------------------------------

def encode_stream_token(chat_id: int, message_id: int) -> str:
    raw_token = f"{chat_id}:{message_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw_token).decode("ascii").rstrip("=")


def decode_stream_token(token: str) -> Tuple[int, int]:
    padded = token + "=" * (-len(token) % 4)
    chat_id_str, message_id_str = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii").split(":", 1)
    return int(chat_id_str), int(message_id_str)


def get_media_from_message(message) -> Any:
    for attr in MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            return media
    return None


def get_hash(media) -> str:
    return (getattr(media, "file_unique_id", "") or "")[:6]


def _dl_url(chat_id: int, message_id: int, file_unique_id: str, file_name: str, force_download: bool) -> str:
    token = encode_stream_token(chat_id, message_id)
    secure_hash = (file_unique_id or "")[:6]
    safe_name = urllib.parse.quote(file_name or "file")
    base = URL if URL.endswith("/") else URL + "/"
    query = f"hash={secure_hash}"
    if force_download:
        query += "&dl=1"  # tells stream_media() to send Content-Disposition: attachment
    return f"{base}dl/{token}/{safe_name}?{query}"


def build_stream_urls(chat_id: int, message_id: int, file_unique_id: str, file_name: str) -> Tuple[str, str]:
    """Build (download_url, watch_url) for a file already sitting at
    (chat_id, message_id) - no network call, pure string building.
    download_url forces an actual download (dl=1); watch_url opens the
    HTML player page, whose <video>/<audio> src is a separate, inline
    (non-forced) link so it plays instead of downloading."""
    download_url = _dl_url(chat_id, message_id, file_unique_id, file_name, force_download=True)
    token = encode_stream_token(chat_id, message_id)
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
    """Holds cached FileId properties for a single Pyrogram client and
    yields raw media bytes straight from Telegram's DC servers."""

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
        self.cached_file_ids[(chat_id, message_id)] = file_id
        return file_id

    async def generate_media_session(self, file_id: FileId) -> Session:
        client = self.client
        media_session = client.media_sessions.get(file_id.dc_id, None)
        if media_session is not None:
            return media_session

        if file_id.dc_id != await client.storage.dc_id():
            media_session = Session(
                client, file_id.dc_id,
                await Auth(client, file_id.dc_id, await client.storage.test_mode()).create(),
                await client.storage.test_mode(), is_media=True,
            )
            await media_session.start()
            for _ in range(6):
                exported_auth = await client.invoke(raw.functions.auth.ExportAuthorization(dc_id=file_id.dc_id))
                try:
                    await media_session.send(
                        raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes)
                    )
                    break
                except AuthBytesInvalid:
                    continue
            else:
                await media_session.stop()
                raise AuthBytesInvalid
        else:
            media_session = Session(
                client, file_id.dc_id, await client.storage.auth_key(),
                await client.storage.test_mode(), is_media=True,
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
        media_session = await self.generate_media_session(file_id)
        location = await self.get_location(file_id)
        current_part = 1
        try:
            r = await media_session.send(raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size))
            if isinstance(r, raw.types.upload.File):
                while True:
                    chunk = r.bytes
                    if not chunk:
                        break
                    elif part_count == 1:
                        yield chunk[first_part_cut:last_part_cut]
                    elif current_part == 1:
                        yield chunk[first_part_cut:]
                    elif current_part == part_count:
                        yield chunk[:last_part_cut]
                    else:
                        yield chunk
                    current_part += 1
                    offset += chunk_size
                    if current_part > part_count:
                        break
                    r = await media_session.send(raw.functions.upload.GetFile(location=location, offset=offset, limit=chunk_size))
        except (TimeoutError, AttributeError):
            pass


# Created lazily (on first request) rather than at import time, since building
# it needs a running asyncio loop and this module is imported before the bot
# actually starts polling/idling.
_streamer: Optional[ByteStreamer] = None


def get_streamer() -> ByteStreamer:
    global _streamer
    if _streamer is None:
        from TechVJ.bot import StreamBot  # local import: avoids any import-order surprises
        _streamer = ByteStreamer(StreamBot)
    return _streamer



async def stream_media(request: web.Request, chat_id: int, message_id: int, secure_hash: str) -> web.StreamResponse:
    streamer = get_streamer()
    file_id = await streamer.get_file_properties(chat_id, message_id)

    if file_id.unique_id[:6] != secure_hash:
        raise InvalidStreamHash

    file_size = file_id.file_size
    if not file_size:
        raise StreamFileNotFound

    range_header = request.headers.get("Range")
    start = 0
    end = file_size - 1

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
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

    length = end - start + 1
    headers = {
        "Content-Type": (
            file_id.mime_type
            or mimetypes.guess_type(file_id.file_name or "")[0]
            or "application/octet-stream"
        ),
        "Content-Length": str(length),
        "Content-Disposition": (
            'attachment' if request.rel_url.query.get("dl") == "1" else 'inline'
        ) + f'; filename="{(file_id.file_name or "file").replace(chr(34), "")}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    }

    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response = web.StreamResponse(
        status=206 if range_header else 200,
        headers=headers,
    )

    await response.prepare(request)

    if request.method == "HEAD":
        return response

    offset = start - (start % CHUNK_SIZE)
    first_part_cut = start - offset
    last_part_cut = end - offset + 1
    part_count = math.ceil((end + 1) / CHUNK_SIZE) - math.floor(offset / CHUNK_SIZE)

    try:
        async for chunk in streamer.yield_file(
            file_id, offset, first_part_cut, last_part_cut,
            part_count, CHUNK_SIZE
        ):
            await response.write(chunk)
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        raise
    except Exception:
        logger.exception("Error while streaming media")
        raise

    await response.write_eof()
    return response


async def render_watch_page(chat_id: int, message_id: int, secure_hash: str) -> str:
    streamer = get_streamer()
    file_id = await streamer.get_file_properties(chat_id, message_id)

    if file_id.unique_id[:6] != secure_hash:
        raise InvalidStreamHash

    download_url, watch_url = build_stream_urls(chat_id, message_id, file_id.unique_id, file_id.file_name)
    inline_url = _dl_url(chat_id, message_id, file_id.unique_id, file_id.file_name, force_download=False)
    mime_type = file_id.mime_type or ""
    file_name = (file_id.file_name or "file").replace("_", " ")
    file_size = humanbytes(file_id.file_size)

    if mime_type.startswith("audio"):
        media_tag = f'<audio controls preload="metadata" src="{inline_url}"></audio>'
    else:
        media_tag = f'<video controls playsinline preload="metadata" src="{inline_url}"></video>'

    return _WATCH_PAGE.format(
        file_name=file_name, file_size=file_size, media_tag=media_tag,
        download_url=download_url, inline_url=inline_url,
    )
