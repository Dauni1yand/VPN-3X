import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import clients, cloudflare, health, nodes, settings, subscriptions, webhooks

logger = logging.getLogger(__name__)

app = FastAPI(title="VPN-3X main server")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the traceback and hand back the actual reason.

    FastAPI's default 500 body is the plain string "Internal Server Error",
    which is what the admin ended up staring at in Telegram -- no way to
    tell a stopped redis from a bad password without shelling into the
    box. Every route here is behind the internal API key, so returning the
    exception text costs nothing and saves a round trip.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )

app.include_router(health.router)
app.include_router(nodes.router)
app.include_router(clients.router)
app.include_router(subscriptions.router)
app.include_router(settings.router)
app.include_router(webhooks.router)
app.include_router(cloudflare.router)
