"""Pushes a message straight to the admin(s) via the raw Telegram Bot HTTP
API, bypassing the bot service entirely (README: node-alerting must reach
the admin in Telegram). Deliberately independent of whether the bot
process is up -- an alert about infrastructure trouble shouldn't also
depend on a second service being healthy."""

from __future__ import annotations

import httpx

from app.core.config import settings


# Telegram rejects a sendMessage over 4096 characters outright. Since
# delivery failures here are swallowed (one admin being unreachable must not
# skip the rest), an oversized alert would silently reach nobody -- so a long
# message is trimmed rather than lost. Node-bootstrap failures carry remote
# diagnostics and are the realistic way to exceed this.
MAX_TELEGRAM_MESSAGE = 4096
_TRUNCATION_NOTE = "\n\n[...сообщение обрезано]"


async def notify_admins(text: str) -> None:
    if not settings.telegram_bot_token or not settings.admin_ids:
        return  # not configured yet -- don't fail the caller over it

    if len(text) > MAX_TELEGRAM_MESSAGE:
        text = text[: MAX_TELEGRAM_MESSAGE - len(_TRUNCATION_NOTE)] + _TRUNCATION_NOTE

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for admin_id in settings.admin_ids:
            try:
                resp = await client.post(url, json={"chat_id": admin_id, "text": text})
                resp.raise_for_status()
            except Exception:  # noqa: BLE001 -- one admin's delivery failure shouldn't skip the rest
                pass
