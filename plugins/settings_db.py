# Dynamic bot settings stored in MongoDB so admins can change them
# from chat (via /settings) without redeploying the bot.

import datetime
import motor.motor_asyncio
from config import DB_URI, DB_NAME, AUTO_DELETE_MODE, AUTO_DELETE_TIME

_client = motor.motor_asyncio.AsyncIOMotorClient(
    DB_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
)
_db = _client[DB_NAME]
_col = _db.bot_settings
_join_requests_col = _db.join_requests

DEFAULTS = {
    "_id": "settings",
    "force_sub": False,
    "force_sub_channels": [],          # list of channel ids or @usernames
    "force_sub_message": (
        "🔴 <b>ᴘʟᴇᴀꜱᴇ ᴊᴏɪɴ ᴍʏ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜꜱᴇ ᴛʜɪꜱ ʙᴏᴛ!</b> 🔴\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n\n"
        "🎬 <b>ʟᴀᴛᴇꜱᴛ ᴍᴏᴠɪᴇꜱ | ᴛᴠ ꜱʜᴏᴡꜱ | ᴡᴇʙ ꜱᴇʀɪᴇꜱ ᴘᴀᴀɴᴇ ᴋᴇ ʟɪʏᴇ</b>\n"
        "👉 <i>Please join all our upcoming channels first.</i>\n\n"
        "⚠️ <b>Channels join karne ke baad hi aap bot use kar sakte hain.</b>\n"
        "🔄 <i>Join karne ke baad \"Try Again\" par click karein aur episode mil jayenge.</i> ✅"
    ),
    "force_sub_photo": None,           # file_id of a photo to show with the force-sub prompt (None = text only)
    "protect_content": False,
    "auto_delete": True,
    "auto_delete_time": 1800,          # seconds
    "custom_caption": None,            # None -> fall back to config.CUSTOM_FILE_CAPTION
    "start_message": None,             # None -> fall back to Script.script.START_TXT
    "start_photo": None,                # file_id of a custom /start photo (None = random pic from config.PICS)
    "custom_buttons": [],              # list of rows -> [[{"text":..,"url":..}, ...], ...]
    "public_mode": None,               # None -> fall back to config.PUBLIC_FILE_STORE; True/False overrides it
    "last_used": None,                 # unix timestamp, updated whenever /settings is opened
}


def _apply_code_controlled(doc):
    """These options are no longer editable from the /settings panel. They are
    fixed here / in config.py, whatever an older value saved in MongoDB says."""
    doc = dict(doc)
    doc["start_message"] = None          # -> Script.script.START_TXT
    doc["start_photo"] = None            # -> random pic from config.PICS
    doc["custom_caption"] = None         # -> config CUSTOM_FILE_CAPTION / BATCH_FILE_CAPTION
    doc["custom_buttons"] = []           # no extra buttons
    doc["protect_content"] = False       # never protect content
    doc["auto_delete"] = AUTO_DELETE_MODE        # env AUTO_DELETE_MODE
    doc["auto_delete_time"] = AUTO_DELETE_TIME   # env AUTO_DELETE_TIME (seconds)
    doc["public_mode"] = False           # bot is always private
    return doc


async def get_settings():
    doc = await _col.find_one({"_id": "settings"})
    if not doc:
        doc = DEFAULTS.copy()
        await _col.insert_one(doc)
        return _apply_code_controlled(doc)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in doc:
            doc[k] = v
            changed = True
    if changed:
        await _col.update_one({"_id": "settings"}, {"$set": doc}, upsert=True)
    return _apply_code_controlled(doc)


async def update_setting(key, value):
    await _col.update_one({"_id": "settings"}, {"$set": {key: value}}, upsert=True)


async def touch_last_used():
    import time
    await update_setting("last_used", time.time())


def readable_ago(timestamp):
    import time
    if not timestamp:
        return "Never"
    seconds = int(time.time() - timestamp)
    if seconds < 60:
        return f"{seconds} Seconds"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} Minutes {secs} Seconds"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} Hours {minutes} Minutes"
    days, hours = divmod(hours, 24)
    return f"{days} Days {hours} Hours"


async def add_force_sub_channel(channel, mode="normal", link=None):
    settings = await get_settings()
    channels = settings.get("force_sub_channels") or []
    # drop any existing entry for this channel (dict or legacy plain id) before re-adding
    channels = [c for c in channels if (c.get("id") if isinstance(c, dict) else c) != channel]
    channels.append({"id": channel, "mode": mode, "link": link})
    await update_setting("force_sub_channels", channels)


async def set_force_sub_link(channel, link):
    """Cache the generated invite link (join-request or normal) for a channel
    so we don't need to create/fetch it again on every /start."""
    settings = await get_settings()
    channels = settings.get("force_sub_channels") or []
    changed = False
    for c in channels:
        if isinstance(c, dict) and c.get("id") == channel:
            c["link"] = link
            changed = True
    if changed:
        await update_setting("force_sub_channels", channels)


async def remove_force_sub_channel(channel):
    settings = await get_settings()
    channels = settings.get("force_sub_channels") or []
    channels = [c for c in channels if (c.get("id") if isinstance(c, dict) else c) != channel]
    await update_setting("force_sub_channels", channels)


def force_sub_channel_id(entry):
    """Works whether the stored entry is the new {'id':.., 'mode':..} dict
    or an older plain channel id (kept for backwards compatibility)."""
    return entry.get("id") if isinstance(entry, dict) else entry


def force_sub_channel_mode(entry):
    return entry.get("mode", "normal") if isinstance(entry, dict) else "normal"


def force_sub_channel_link(entry):
    return entry.get("link") if isinstance(entry, dict) else None


async def add_custom_button(text, url):
    settings = await get_settings()
    rows = settings.get("custom_buttons") or []
    # up to 2 buttons per row, like the reference UI
    if rows and len(rows[-1]) < 2:
        rows[-1].append({"text": text, "url": url})
    else:
        rows.append([{"text": text, "url": url}])
    await update_setting("custom_buttons", rows)


async def remove_custom_button(index):
    settings = await get_settings()
    flat = [btn for row in (settings.get("custom_buttons") or []) for btn in row]
    if 0 <= index < len(flat):
        flat.pop(index)
    # re-pack into rows of 2
    rows = [flat[i:i + 2] for i in range(0, len(flat), 2)]
    await update_setting("custom_buttons", rows)


async def clear_custom_buttons():
    await update_setting("custom_buttons", [])


# --- Join Request Mode tracking -------------------------------------------
# For channels in "request" mode we don't wait for the admin to manually
# approve the join request - the fact that the user tapped Join and sent a
# request is treated as enough. Recorded here (via the reliable
# on_chat_join_request event) as the primary signal, with a live Telegram
# check as a fallback for when this event was somehow missed.

async def record_join_request(user_id, channel_id):
    await _join_requests_col.update_one(
        {"user_id": int(user_id), "channel_id": int(channel_id)},
        {"$set": {"user_id": int(user_id), "channel_id": int(channel_id), "requested_at": datetime.datetime.utcnow()}},
        upsert=True,
    )


async def get_join_request(user_id, channel_id):
    """Returns the full record (with its timestamp) or None."""
    return await _join_requests_col.find_one({"user_id": int(user_id), "channel_id": int(channel_id)})


async def has_join_request(user_id, channel_id):
    doc = await _join_requests_col.find_one({"user_id": int(user_id), "channel_id": int(channel_id)})
    return doc is not None


async def clear_join_request(user_id, channel_id):
    """Wipe the recorded join request when a user leaves/is kicked from the
    channel, so they're required to request again next time."""
    await _join_requests_col.delete_one({"user_id": int(user_id), "channel_id": int(channel_id)})


async def clear_all_join_requests():
    """Manual safety net for admins: wipe every recorded join request so
    everyone has to send a fresh request. Useful if you ever suspect some
    individual leave-events were missed and records have gone stale."""
    result = await _join_requests_col.delete_many({})
    return result.deleted_count
