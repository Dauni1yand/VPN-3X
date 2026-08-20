"""Pushes a message straight to the admin(s) via the raw Telegram Bot HTTP
API, bypassing the bot service entirely (README: node-alerting must reach
the admin in Telegram). Deliberately independent of whether the bot
process is up -- an alert about infrastructure trouble shouldn't also
depend on a second service being healthy."""

from __future__ import annotations

import httpx

from app.core.config import settings


async def notify_admins(text: str) -> None:
    if not settings.telegram_bot_token or not settings.admin_ids:
        return  # not configured yet -- don't fail the caller over it

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for admin_id in settings.admin_ids:
            try:
                resp = await client.post(url, json={"chat_id": admin_id, "text": text})
                resp.raise_for_status()
            except Exception:  # noqa: BLE001 -- one admin's delivery failure shouldn't skip the rest
                pass
