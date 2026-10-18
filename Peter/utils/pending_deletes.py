# Persistent auto-delete.
#
# Every delivered file that must be auto-deleted is also written to MongoDB
# (collection `pending_deletes`) together with the time it must disappear.
# If the bot restarts / redeploys / is down when that time arrives,
# restore_pending_deletes() runs at the next start-up: overdue entries are
# deleted immediately, the others get their timer again.
#
# The normal in-memory timer is still used while the bot is running, so the
# delivery code itself works exactly as before.

import asyncio
import logging
import time

from pyrogram.errors import FloodWait
from config import BOT_ID
from plugins.dbusers import db

logger = logging.getLogger(__name__)

# Keep references to running timers so they can't be garbage-collected.
_TASKS = set()
# Don't hit Telegram with hundreds of deletes at once after a long downtime.
_SEM = asyncio.Semaphore(5)


def _col():
    return db.db.pending_deletes


def _spawn(coro):
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def _delete_messages(client, chat_id, ids):
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        for attempt in range(3):
            try:
                await client.delete_messages(chat_id, chunk)
                break
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
            except Exception as e:
                # already deleted by the user / message too old / chat blocked: nothing more to do
                logger.info(f"[AUTODEL] couldn't delete {len(chunk)} message(s) in {chat_id}: {type(e).__name__}")
                break


async def _execute(client, entry):
    """Delete the files, update the reminder message, and forget the entry."""
    async with _SEM:
        await _delete_messages(client, entry["chat_id"], entry["ids"])
        if entry.get("notice_id"):
            try:
                await client.edit_message_text(entry["chat_id"], entry["notice_id"], entry["notice_text"])
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
                try:
                    await client.edit_message_text(entry["chat_id"], entry["notice_id"], entry["notice_text"])
                except Exception:
                    pass
            except Exception:
                pass
    if entry.get("_id") is not None:
        try:
            await _col().delete_one({"_id": entry["_id"]})
        except Exception as e:
            logger.warning(f"[AUTODEL] couldn't remove finished entry: {type(e).__name__}: {e}")


async def run_delete(client, delay, chat_id, ids, notice_id, notice_text):
    """Called when files are delivered: save the job in MongoDB, wait, then delete.
    If MongoDB is unreachable the delete still happens (in memory only)."""
    entry = {
        "bot_id": BOT_ID,   # several bots share this collection: each one only touches its own jobs
        "chat_id": int(chat_id),
        "ids": [int(i) for i in ids],
        "notice_id": int(notice_id) if notice_id else None,
        "notice_text": notice_text,
        "delete_at": time.time() + delay,
    }
    try:
        res = await _col().insert_one(dict(entry))
        entry["_id"] = res.inserted_id
    except Exception as e:
        logger.warning(f"[AUTODEL] couldn't save the job to MongoDB, using memory only: {type(e).__name__}: {e}")
    await asyncio.sleep(delay)
    await _execute(client, entry)


async def _run_restored(client, entry):
    await asyncio.sleep(max(0, entry["delete_at"] - time.time()))
    await _execute(client, entry)


async def restore_pending_deletes(client):
    """Call once after the bot has started."""
    try:
        try:
            await _col().create_index("delete_at")
        except Exception:
            pass
        entries = await _col().find({"bot_id": BOT_ID}).to_list(length=None)
    except Exception as e:
        logger.warning(f"[AUTODEL] couldn't load pending deletes: {type(e).__name__}: {e}")
        return 0
    now = time.time()
    overdue = 0
    for entry in entries:
        if entry["delete_at"] <= now:
            overdue += 1
        _spawn(_run_restored(client, entry))
    logger.info(f"[AUTODEL] restored {len(entries)} pending delete(s), {overdue} already overdue")
    return len(entries)
