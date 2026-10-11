# Silent block for banned / blacklisted users.
#
# These handlers run in group -1, before every other plugin. If the sender is
# on the blacklist the update is dropped here, so the bot never answers them:
# no /start reply, no files, no "you are banned" message, nothing.
# Owners (config.ADMINS) are never blocked.

import logging

from pyrogram import Client, filters
from config import ADMINS
from plugins.dbusers import db

logger = logging.getLogger(__name__)


async def _is_blocked(user) -> bool:
    if not user or user.id in ADMINS:
        return False
    try:
        return await db.is_user_banned(user.id)
    except Exception as e:
        # If the database can't be read, don't lock everybody out.
        logger.warning(f"[BAN] ban check failed, letting the update through: {type(e).__name__}: {e}")
        return False


@Client.on_message(filters.incoming, group=-1)
async def block_banned_messages(client, message):
    if await _is_blocked(message.from_user):
        message.stop_propagation()


@Client.on_callback_query(group=-1)
async def block_banned_callbacks(client, query):
    if await _is_blocked(query.from_user):
        query.stop_propagation()
