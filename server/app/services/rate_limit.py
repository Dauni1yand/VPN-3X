"""Minimal Redis-backed fixed-window rate limiter -- defense in depth on top
of Cloudflare's own edge rate limiting (README: the main server sits behind
Cloudflare), for the handful of endpoints that mint VPN access or are
reachable without our shared internal API key (the CryptoBot webhook, gated
by its own HMAC signature instead)."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url)
    return _redis


async def enforce_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    """Fails OPEN when Redis is unreachable.

    This limiter is defense in depth on top of Cloudflare's edge limits;
    the guarantees that actually protect revenue -- a payment credited
    once per invoice id, VPN time granted once per ad impression id --
    are unique constraints in Postgres and keep holding without Redis.
    Failing closed instead would mean a Redis blip stops paying users from
    getting a config at all, which is a far worse outcome than briefly
    unmetered requests.
    """
    redis_key = f"ratelimit:{key}"
    try:
        current = await _client().incr(redis_key)
    except Exception as exc:  # noqa: BLE001 -- availability beats metering here
        logger.warning("rate limiter unavailable, allowing request for %s: %s", key, exc)
        return
    # `nx=True` only sets the TTL if the key doesn't already have one --
    # calling this every time (not just when current == 1) makes the window
    # self-healing: if the process died between INCR and EXPIRE on some
    # earlier request, the key would otherwise be left counting forever with
    # no TTL, permanently rate-limiting whatever this key identifies.
    try:
        await _client().expire(redis_key, window_seconds, nx=True)
    except Exception as exc:  # noqa: BLE001 -- counter already incremented; TTL is best-effort
        logger.warning("could not set rate-limit TTL for %s: %s", key, exc)
    if current > limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
