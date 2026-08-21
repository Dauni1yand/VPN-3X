from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import async_session_maker
from app.services.queue import get_queue

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Plain liveness -- install.sh and any process supervisor poll this,
    so it must stay a cheap 200 that doesn't depend on Postgres or Redis."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Per-dependency status. Separate from /health because a stopped redis
    should be *visible* without failing liveness -- an unreachable queue is
    exactly what turned a node install into an opaque 500 once."""
    components: dict[str, str] = {}

    try:
        async with async_session_maker() as db:
            await db.execute(text("SELECT 1"))
        components["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- report, don't raise
        components["database"] = f"error: {type(exc).__name__}: {exc}"

    try:
        queue = await get_queue()
        await queue.ping()
        components["queue"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- report, don't raise
        components["queue"] = f"error: {type(exc).__name__}: {exc}"

    return {
        "status": "ok" if all(v == "ok" for v in components.values()) else "degraded",
        "components": components,
    }
