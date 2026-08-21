"""Minimal client for CryptoBot's Crypto Pay API (https://pay.crypt.bot/api).

Docs: https://help.crypt.bot/crypto-pay-api -- an API token for your own
Crypto Pay app is created inside @CryptoBot via "/pay" -> "Create App", then
set on the running bot via /setcryptobottoken (bot/handlers/admin.py) --
not an env var, so it can be added/rotated without a redeploy. The token
lives in the main server's `settings` table (encrypted at rest), fetched
fresh on every call rather than cached, since it changes rarely and this
avoids the bot serving stale-token errors after an admin rotates it."""

from __future__ import annotations

import httpx

from bot.services.api_client import server_api

_BASE_URL = "https://pay.crypt.bot/api"


class CryptoBotNotConfiguredError(RuntimeError):
    pass


class CryptoBotClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(base_url=_BASE_URL, timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _token(self) -> str:
        token = await server_api.get_setting("cryptobot_api_token")
        if not token:
            raise CryptoBotNotConfiguredError(
                "CryptoBot API token isn't set yet -- an admin needs to run /setcryptobottoken"
            )
        return token

    async def create_invoice(self, amount: str, asset: str, description: str, payload: str) -> dict:
        resp = await self._http.post(
            "/createInvoice",
            headers={"Crypto-Pay-API-Token": await self._token()},
            json={"amount": amount, "asset": asset, "description": description, "payload": payload},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"CryptoBot createInvoice failed: {body}")
        return body["result"]

    async def get_invoice_status(self, invoice_id: int) -> str:
        resp = await self._http.get(
            "/getInvoices",
            headers={"Crypto-Pay-API-Token": await self._token()},
            params={"invoice_ids": invoice_id},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"CryptoBot getInvoices failed: {body}")
        items = body["result"]["items"]
        if not items:
            raise ValueError(f"unknown invoice {invoice_id}")
        return items[0]["status"]  # "active" | "paid" | "expired"


cryptobot = CryptoBotClient()
