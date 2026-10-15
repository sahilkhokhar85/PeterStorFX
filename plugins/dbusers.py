import asyncio
import logging
import time

import motor.motor_asyncio
from pymongo import UpdateOne
from config import DB_NAME, DB_URI, BOT_ID

logger = logging.getLogger(__name__)

# The blacklist is shared by every bot that uses this database. Each bot keeps a
# copy in memory and re-reads it from MongoDB at most this often, so a /ban or
# /unban made on one bot reaches the others within this many seconds.
BAN_REFRESH_SECONDS = 15


class Database:

    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        self.db = self._client[database_name]
        self.col = self.db.users
        # Permanent blacklist. One document per banned Telegram id (_id = user id).
        # Works for ids that never started the bot, and survives broadcast cleanups.
        self.banned_col = self.db.banned_users
        self._banned_ids = None          # in-memory copy of the blacklist
        self._banned_loaded_at = 0.0
        self._load_lock = asyncio.Lock()
        # Users are stored per bot (field bot_id), so each bot only knows - and
        # broadcasts to - the people who started THAT bot. Bans are global.
        self.bot_id = BOT_ID

    def _mine(self, id):
        return {'id': int(id), 'bot_id': self.bot_id}

    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            bot_id = self.bot_id,
        )

    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)

    async def is_user_exist(self, id):
        user = await self.col.find_one(self._mine(id))
        return bool(user)

    async def get_user(self, id):
        """Single query combining what is_user_exist + is_user_banned used
        to do as two separate round trips."""
        return await self.col.find_one(self._mine(id))

    async def total_users_count(self):
        count = await self.col.count_documents({'bot_id': self.bot_id})
        return count

    async def get_all_users(self):
        return self.col.find({'bot_id': self.bot_id})

    async def delete_user(self, user_id):
        await self.col.delete_many(self._mine(user_id))

    async def delete_user_unless_banned(self, user_id):
        # Banned users keep their record, otherwise the ban is lost when a
        # broadcast finds they blocked the bot and they could just /start again.
        uid = int(user_id)
        if await self.is_user_banned(uid):
            return
        await self.col.delete_one({'id': uid, 'bot_id': self.bot_id, 'banned': {'$ne': True}})

    # ------------------------------------------------------------------
    # Ban / blacklist
    # ------------------------------------------------------------------

    async def _ensure_loaded(self):
        """Load the blacklist into memory once (and import any old bans that
        were stored only as users.banned = True)."""
        if self._banned_ids is not None:
            return
        async with self._load_lock:
            if self._banned_ids is not None:
                return
            ids = set()
            async for doc in self.banned_col.find({}, {'_id': 1}):
                ids.add(int(doc['_id']))
            legacy = set()
            async for doc in self.col.find({'banned': True}, {'id': 1}):
                if 'id' in doc and int(doc['id']) not in ids:
                    legacy.add(int(doc['id']))
            if legacy:
                await self.banned_col.bulk_write(
                    [UpdateOne({'_id': u}, {'$setOnInsert': {'at': time.time(), 'src': 'legacy'}}, upsert=True)
                     for u in legacy],
                    ordered=False,
                )
                ids |= legacy
                logger.info(f"[BAN] imported {len(legacy)} old bans into banned_users")
            try:
                # /start and every ban check look users up by 'id' - keep that fast.
                await self.col.create_index([('id', 1), ('bot_id', 1)])
            except Exception as e:
                logger.warning(f"[BAN] couldn't create users.id index: {type(e).__name__}: {e}")
            self._banned_ids = ids
            self._banned_loaded_at = time.monotonic()
            logger.info(f"[BAN] blacklist loaded: {len(ids)} ids")

    async def _refresh_banned(self):
        """Pick up bans/unbans made by the OTHER bots that share this database."""
        if time.monotonic() - self._banned_loaded_at < BAN_REFRESH_SECONDS:
            return
        async with self._load_lock:
            if time.monotonic() - self._banned_loaded_at < BAN_REFRESH_SECONDS:
                return
            try:
                ids = set()
                async for doc in self.banned_col.find({}, {'_id': 1}):
                    ids.add(int(doc['_id']))
                self._banned_ids = ids
            except Exception as e:
                # keep the copy we already have; try again after the next interval
                logger.warning(f"[BAN] refresh failed, keeping the old list: {type(e).__name__}: {e}")
            self._banned_loaded_at = time.monotonic()

    async def ban_users(self, user_ids):
        """Blacklist many ids at once. Returns how many were NEW (not banned before)."""
        await self._ensure_loaded()
        ids = {int(u) for u in user_ids}
        new_ids = ids - self._banned_ids
        if new_ids:
            await self.banned_col.bulk_write(
                [UpdateOne({'_id': u}, {'$setOnInsert': {'at': time.time()}}, upsert=True) for u in new_ids],
                ordered=False,
            )
            self._banned_ids |= new_ids
        if ids:
            # keep the old flag in sync for users that already exist
            await self.col.update_many({'id': {'$in': list(ids)}}, {'$set': {'banned': True}})
        return len(new_ids)

    async def unban_users(self, user_ids):
        """Remove many ids from the blacklist. Returns how many were actually banned before."""
        await self._ensure_loaded()
        ids = {int(u) for u in user_ids}
        was_banned = ids & self._banned_ids
        if ids:
            await self.banned_col.delete_many({'_id': {'$in': list(ids)}})
            await self.col.update_many({'id': {'$in': list(ids)}}, {'$set': {'banned': False}})
            self._banned_ids -= ids
        return len(was_banned)

    async def ban_user(self, user_id):
        await self.ban_users([user_id])

    async def unban_user(self, user_id):
        await self.unban_users([user_id])

    async def is_user_banned(self, user_id):
        await self._ensure_loaded()
        await self._refresh_banned()
        return int(user_id) in self._banned_ids

    async def get_banned_ids(self):
        await self._ensure_loaded()
        await self._refresh_banned()
        return sorted(self._banned_ids)

    async def total_banned_count(self):
        await self._ensure_loaded()
        await self._refresh_banned()
        return len(self._banned_ids)


db = Database(DB_URI, DB_NAME)
