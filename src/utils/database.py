import json
import asyncio
import aiomysql
from pymysql.converters import conversions
from pymysql.constants import FIELD_TYPE
from utils.config import cfg

class Database:
    def __init__(self):
        self._pool = None
        self.converter = conversions.copy()
        self.converter[FIELD_TYPE.JSON] = json.loads

    async def get_pool(self):
        if self._pool is None:
            try:
                self._pool = await aiomysql.create_pool(
                    host=cfg['database']['DB_HOST'],
                    port=cfg['database']['DB_PORT'],
                    user=cfg['database']['DB_USER'],
                    password=cfg['database']['DB_PASSWORD'],
                    db=cfg['database']['DB_NAME'],
                    charset=cfg['database']['DB_CHARSET'],
                    autocommit=cfg['database']['DB_AUTOCOMMIT'].lower() == 'true',
                    cursorclass=aiomysql.DictCursor,
                    minsize=cfg['database']['DB_MINSIZE'],
                    maxsize=cfg['database']['DB_MAXSIZE'],
                    pool_recycle=cfg['database']['DB_POOL_RECYCLE'],
                    connect_timeout=cfg['database']['DB_CONNECT_TIMEOUT'],
                    echo=cfg['database']['DB_ECHO'].lower() == 'true',
                    conv=self.converter
                )
            except Exception as e:
                print(f'Ошибка подключения к базе данных: {e}')
                raise
        return self._pool

    async def close(self):
        if self._pool is not None:
            try:
                if hasattr(self._pool, '_close_waiter'):
                    self._pool._close_waiter.cancel()

                self._pool.close()

                try:
                    await asyncio.wait_for(self._pool.wait_closed(), timeout=10.0)
                except Exception:
                    pass

                self._pool = None

                await asyncio.sleep(1)
                print('Соединение с базой данных закрыто')
            except Exception as e:
                print(f'Ошибка при закрытии соединения с БД: {e}')
                self._pool = None

    async def execute(self, query: str, params: tuple = ()):
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                await conn.commit()
                return cur.lastrowid

    async def fetchall(self, query: str, params: tuple = ()):
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return await cur.fetchall()

    async def add(self, id: int, info: json = {}, base: str = 'activities'):
        pool = await self.get_pool()

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                columns = ['id'] + list(info.keys())
                values = [id] + list(info.values())

                columns_str = ', '.join(columns)
                values_str = ', '.join(['%s'] * len(values))

                await cur.execute(f'INSERT INTO {base} ({columns_str}) VALUES ({values_str})', values)

    async def get(self, id: int, base: str = 'activities'):
        pool = await self.get_pool()

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f'SELECT * FROM {base} WHERE id = %s', (id,))
                result = await cur.fetchone()
                return result

    async def set(self, id: int, values, base: str = 'activities'):
        pool = await self.get_pool()

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                resp = ', '.join([f'{key} = %s' for key in values.keys()])
                vals = list(values.values()) + [id]

                await cur.execute(f'UPDATE {base} SET {resp} WHERE id = %s', (tuple(vals)))

    async def delete(self, id: int, base: str = 'activities'):
        pool = await self.get_pool()

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f'DELETE FROM {base} WHERE id = %s', (id,))
                await conn.commit()