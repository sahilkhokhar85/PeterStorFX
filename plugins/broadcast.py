
from pyrogram.errors import InputUserDeactivated, UserNotParticipant, FloodWait, UserIsBlocked, PeerIdInvalid
from plugins.dbusers import db
from pyrogram import Client, filters
from config import ADMINS
from plugins.admins_db import dynamic_admin_filter
import asyncio
import datetime
import time

# How many users we message at once. Telegram's flood limits are mostly
# per-chat, so sending to many *different* users concurrently is safe and
# turns a broadcast that used to take ages (one message at a time) into
# something that finishes in a fraction of the time.
CONCURRENCY = 25


async def broadcast_messages(user_id, message):
    try:
        await message.copy(chat_id=user_id)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await broadcast_messages(user_id, message)
    except InputUserDeactivated:
        await db.delete_user_unless_banned(int(user_id))
        return False, "Deleted"
    except UserIsBlocked:
        await db.delete_user_unless_banned(int(user_id))
        return False, "Blocked"
    except PeerIdInvalid:
        # NOTE: PeerIdInvalid does NOT mean the user blocked/deleted the bot.
        # It usually means the bot's local peer cache doesn't have this user yet
        # (common after a redeploy on Railway/Koyeb/Render). Deleting the user
        # here was wrongly shrinking the user count after every broadcast.
        return False, "Error"
    except Exception:
        return False, "Error"


@Client.on_message(filters.command("broadcast") & dynamic_admin_filter("can_broadcast") & filters.reply)
async def verupikkals(bot, message):
    users = await db.get_all_users()
    user_ids = [int(u['id']) for u in await users.to_list(length=None) if 'id' in u]
    b_msg = message.reply_to_message
    sts = await message.reply_text(text='**Broadcasting your messages...**')
    start_time = time.time()
    total_users = len(user_ids)
    done = 0
    blocked = 0
    deleted = 0
    failed = 0
    success = 0

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _send(uid):
        async with sem:
            return await broadcast_messages(uid, b_msg)

    tasks = [asyncio.create_task(_send(uid)) for uid in user_ids]
    for coro in asyncio.as_completed(tasks):
        ok, reason = await coro
        if ok:
            success += 1
        elif reason == "Blocked":
            blocked += 1
        elif reason == "Deleted":
            deleted += 1
        else:
            failed += 1
        done += 1
        if not done % 20:
            try:
                await sts.edit(f"Broadcast in progress:\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}")
            except:
                pass

    time_taken = datetime.timedelta(seconds=int(time.time()-start_time))
    await sts.edit(f"Broadcast Completed:\nCompleted in {time_taken} seconds.\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}")
