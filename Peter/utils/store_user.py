"""Optional user-account lane for storing one-by-one /batch files.

If STORE_SESSION (a Pyrogram session string) is set, the files an admin sent to the bot are
forwarded to the log channel by that user account instead of by the bot. User accounts have far
looser flood limits than bots. Works only when the files were sent by that same account and the
account is a member (admin) of the log channel. On ANY problem it returns None and the normal
bot path is used, so nothing breaks.
"""
import asyncio
import collections
import os

from pyrogram import Client
from pyrogram.errors import FloodWait

STORE_SESSION = os.environ.get("STORE_SESSION", "").strip()

_client = None
_lock = asyncio.Lock()


async def _get_client(api_id, api_hash):
    global _client
    async with _lock:
        if _client is None:
            c = Client(
                "store_user", api_id=api_id, api_hash=api_hash,
                session_string=STORE_SESSION, in_memory=True, no_updates=True,
                sleep_threshold=0,
            )
            await c.start()
            _client = c
        return _client


def enabled():
    return bool(STORE_SESSION)


async def forward_for_bot(api_id, api_hash, bot_username, log_chat, msgs, content_id, chunk=100, progress=None):
    """msgs: the bot-side messages, in sending order. Returns [{"channel_id","msg_id"}] or None."""
    if not STORE_SESSION or not msgs:
        return None
    try:
        user = await _get_client(api_id, api_hash)
        # the same messages as the user account sees them (ids differ from the bot's side)
        hist = []
        async for m in user.get_chat_history(bot_username, limit=len(msgs) * 3 + 50):
            if m.outgoing:
                hist.append(m)
        hist.sort(key=lambda m: m.id)
        pool = collections.defaultdict(collections.deque)
        for m in hist:
            pool[content_id(m)].append(m)
        mapped = []
        for m in msgs:
            q = pool.get(content_id(m))
            if not q:
                print(f"STORE USER: no match for message {m.id}, using the bot path")
                return None
            mapped.append(q.popleft())
        out, done = [], []
        for i in range(0, len(mapped), chunk):
            part = mapped[i:i + chunk]
            while True:
                try:
                    res = await user.forward_messages(
                        log_chat, bot_username, [m.id for m in part], drop_author=True)
                    break
                except FloodWait as e:
                    await asyncio.sleep(int(e.value) + 1)
            res = res if isinstance(res, list) else [res]
            done.extend(res)
            ok = len(res) == len(part) and all(
                content_id(a) == content_id(b) for a, b in zip(part, res))
            if not ok:
                print("STORE USER: forward result did not match, undoing and using the bot path")
                try:
                    await user.delete_messages(log_chat, [m.id for m in done])
                except Exception:
                    pass
                return None
            out.extend({"channel_id": log_chat, "msg_id": m.id} for m in res)
            if progress:
                await progress(len(out), len(mapped))
        return out
    except Exception as e:
        print(f"STORE USER ERROR: {type(e).__name__}: {e}")
        return None
