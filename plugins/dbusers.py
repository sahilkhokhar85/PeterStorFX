import motor.motor_asyncio
from config import DB_NAME, DB_URI

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

    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
        )
    
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
        return bool(user)

    async def get_user(self, id):
        """Single query combining what is_user_exist + is_user_banned used
        to do as two separate round trips."""
        return await self.col.find_one({'id': int(id)})

    async def total_users_count(self):
        count = await self.col.count_documents({})
        return count
    
    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})

    async def delete_user_unless_banned(self, user_id):
        # Banned users keep their record, otherwise the ban is lost when a
        # broadcast finds they blocked the bot and they could just /start again.
        await self.col.delete_one({'id': int(user_id), 'banned': {'$ne': True}})

    async def ban_user(self, user_id):
        await self.col.update_one({'id': int(user_id)}, {'$set': {'banned': True}})

    async def unban_user(self, user_id):
        await self.col.update_one({'id': int(user_id)}, {'$set': {'banned': False}})

    async def is_user_banned(self, user_id):
        user = await self.col.find_one({'id': int(user_id)})
        if user:
            return user.get('banned', False)
        return False

    async def total_banned_count(self):
        count = await self.col.count_documents({'banned': True})
        return count


db = Database(DB_URI, DB_NAME)
