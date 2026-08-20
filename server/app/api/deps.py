from fastapi import Header, HTTPException, status

from app.core.config import settings


async def require_internal_api_key(x_api_key: str = Header(default="")) -> None:
    """Guards the internal API surface the Telegram bot calls.

    The Android app talks to a separate, narrower public surface (not
    implemented yet) that will use per-user auth instead of this shared
    secret -- this dependency is only for server<->bot traffic.
    """
    if x_api_key != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
