"""Shared arq queue handle for enqueuing background jobs from the API.

PLAN.md section 6: slow work (a node bootstrap is minutes of apt + an
installer over SSH) must not run inside an HTTP request. The API enqueues
and answers immediately; the worker does the waiting."""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import settings

_pool: ArqRedis | None = None


async def get_queue() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
