"""
Video-cover aware copy for Pyrofork 2.3.58 (Telegram TL layer 198).

Why this exists
---------------
Pyrofork's Message.copy() re-sends a video through send_cached_media(file_id),
which builds a bare raw.types.InputMediaDocument(id=...). That request carries
no `video_cover`, so the cover of the original video is silently lost on every
copy delivered to a user.

The parsed `Video` type does not expose the cover, but every parsed Message
keeps its raw TL message in `message.raw`, and raw.types.MessageMediaDocument
carries `video_cover` (+ `video_timestamp`). So this module:

  1. reads the cover / start timestamp from `message.raw.media`
  2. sends the copy itself with raw.functions.messages.SendMedia and an
     InputMediaDocument(..., video_cover=InputPhoto(...)), re-using the cover
     photo that already lives on Telegram's servers (no download / re-upload,
     no extra API call to find the cover).

Requires pyrofork >= 2.3.58. pyrofork 2.3.45 is layer 187 and has no
`video_cover` field at all.
"""

import logging

from pyrogram import raw, types, utils
from pyrogram.errors import FloodWait

logger = logging.getLogger(__name__)


def _cover_media(src):
    """Return src.raw.media if it is a document that carries a video cover,
    otherwise None."""
    media = getattr(getattr(src, "raw", None), "media", None)
    if not isinstance(media, raw.types.MessageMediaDocument):
        return None
    if not isinstance(getattr(media, "document", None), raw.types.Document):
        return None
    if not isinstance(getattr(media, "video_cover", None), raw.types.Photo):
        return None
    return media


def has_cover(src) -> bool:
    """True if `src` (a pyrogram Message) is a video that has a Telegram cover."""
    return _cover_media(src) is not None


async def copy_with_cover(src, chat_id, caption=None, reply_markup=None, protect_content=False):
    """Copy `src` to `chat_id` keeping its video cover (and start timestamp).

    Mirrors Message.copy() for the arguments the file-store uses:
      caption         None  -> keep the original caption (with its entities)
                      str   -> new caption (HTML / Markdown, same parsing as copy())
      reply_markup    InlineKeyboardMarkup or None (an empty keyboard is ignored)
      protect_content True  -> noforwards

    Returns the sent pyrogram Message, or None if `src` has no cover (nothing
    was sent - the caller should use the normal copy). Raises on Telegram
    errors; FloodWait is left for the caller to handle.
    """
    media = _cover_media(src)
    if media is None:
        return None

    client = src._client
    doc = media.document
    cover = media.video_cover

    input_media = raw.types.InputMediaDocument(
        id=raw.types.InputDocument(
            id=doc.id,
            access_hash=doc.access_hash,
            file_reference=doc.file_reference,
        ),
        video_cover=raw.types.InputPhoto(
            id=cover.id,
            access_hash=cover.access_hash,
            file_reference=cover.file_reference,
        ),
        video_timestamp=getattr(media, "video_timestamp", None),
        spoiler=getattr(media, "spoiler", None) or None,
    )

    if caption is None:
        # same as Message.copy(): original caption + its entities, no re-parsing surprises
        text_kwargs = await utils.parse_text_entities(
            client, src.caption or "", None, src.caption_entities
        )
    else:
        text_kwargs = await utils.parse_text_entities(client, caption, None, None)

    markup = None
    if reply_markup is not None and getattr(reply_markup, "inline_keyboard", None):
        markup = await reply_markup.write(client)

    r = await client.invoke(
        raw.functions.messages.SendMedia(
            peer=await client.resolve_peer(chat_id),
            media=input_media,
            random_id=client.rnd_id(),
            noforwards=protect_content or None,
            reply_markup=markup,
            **text_kwargs,
        )
    )

    users = {u.id: u for u in r.users}
    chats = {c.id: c for c in r.chats}
    for upd in r.updates:
        if isinstance(
            upd,
            (
                raw.types.UpdateNewMessage,
                raw.types.UpdateNewChannelMessage,
                raw.types.UpdateNewScheduledMessage,
            ),
        ):
            return await types.Message._parse(client, upd.message, users, chats)

    logger.warning("[COVER] SendMedia succeeded but no new-message update was returned")
    return None


async def copy_keep_cover(src, chat_id, **kw):
    """Drop-in for `src.copy(chat_id=chat_id, **kw)` that keeps the Telegram
    cover of videos. Use it wherever a message is copied and may be a video.

    Videos that carry a cover go through copy_with_cover. If that path fails
    for any non-flood reason we fall back to the normal copy, so the file is
    still delivered - just without the cover. FloodWait is re-raised so the
    caller's own FloodWait handling keeps working. Everything that is not a
    video with a cover is simply passed to src.copy() unchanged.
    """
    if has_cover(src):
        try:
            # like Message.copy(): when no reply_markup is passed, keep the original one
            markup = kw["reply_markup"] if "reply_markup" in kw else getattr(src, "reply_markup", None)
            sent = await copy_with_cover(
                src,
                chat_id,
                caption=kw.get("caption"),
                reply_markup=markup,
                protect_content=kw.get("protect_content") or False,
            )
            if sent is not None:
                return sent
        except FloodWait:
            raise
        except Exception as e:
            logger.warning(f"[COVER] cover copy failed, sending without cover: {type(e).__name__}: {e}")
    return await src.copy(chat_id=chat_id, **kw)
