"""Minimal Redis-backed fixed-window rate limiter -- defense in depth on top
of Cloudflare's own edge rate limiting (README: the main server sits behind
Cloudflare), for the handful of endpoints that mint VPN access or are
reachable without our shared internal API key (the CryptoBot webhook, gated
by its own HMAC signature instead)."""

from __future__ import annotations

from fastapi import HTTPException, status
from redis.asyncio import Redis

from app.core.config import settings

_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url)
    return _redis


async def enforce_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    redis_key = f"ratelimit:{key}"
    current = await _client().incr(redis_key)
    # `nx=True` only sets the TTL if the key doesn't already have one --
    # calling this every time (not just when current == 1) makes the window
    # self-healing: if the process died between INCR and EXPIRE on some
    # earlier request, the key would otherwise be left counting forever with
    # no TTL, permanently rate-limiting whatever this key identifies.
    await _client().expire(redis_key, window_seconds, nx=True)
    if current > limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
