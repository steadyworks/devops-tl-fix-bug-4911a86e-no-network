import asyncio
import logging
from typing import Optional

from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from backend.env_loader import EnvLoader

FieldT = str | int | float | bytes


class RedisClient:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._create_client()

    def _create_client(self) -> None:
        retry_strategy = Retry(
            backoff=ExponentialBackoff(),
            retries=3,
            supported_errors=(ConnectionError, RedisTimeoutError),
        )

        self.__connection_pool = ConnectionPool(
            host=EnvLoader.get("REDIS_HOST"),
            port=int(EnvLoader.get("REDIS_PORT")),
            username=EnvLoader.get("REDIS_USERNAME"),
            password=EnvLoader.get("REDIS_PASSWORD"),
            decode_responses=True,
            socket_keepalive=True,
            socket_timeout=20,
            socket_connect_timeout=10,
            health_check_interval=20,
            retry=retry_strategy,
        )

        self.client = Redis(
            connection_pool=self.__connection_pool,
            decode_responses=True,
        )

    async def _recreate_client(self) -> None:
        async with self._lock:
            try:
                await self.client.close()
            except Exception as e:
                logging.warning(f"Failed to close Redis client cleanly: {e}")
            self._create_client()

    async def safe_blpop(
        self, key: str, timeout: Optional[int | float] = 0
    ) -> Optional[tuple[str, str]]:
        try:
            return await self.client.blpop(key, timeout=timeout)
        except (ConnectionError, RedisTimeoutError) as e:
            logging.warning(f"Redis error on BLPOP: {e}. Recreating Redis client.")
            await self._recreate_client()
            try:
                return await self.client.blpop(key, timeout=timeout)
            except Exception as e2:
                logging.error(f"Retry after reconnect failed on BLPOP: {e2}")
                raise

    async def safe_rpush(self, name: str, *values: FieldT) -> int:
        try:
            return await self.client.rpush(name, *values)
        except (ConnectionError, RedisTimeoutError) as e:
            logging.warning(f"Redis error on RPUSH: {e}. Recreating Redis client.")
            await self._recreate_client()
            try:
                return await self.client.rpush(name, *values)
            except Exception as e2:
                logging.error(f"Retry after reconnect failed on RPUSH: {e2}")
                raise
