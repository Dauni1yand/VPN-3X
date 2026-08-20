"""Minimal client for CryptoBot's Crypto Pay API (https://pay.crypt.bot/api).

Docs: https://help.crypt.bot/crypto-pay-api -- an API token for your own
Crypto Pay app is created inside @CryptoBot via "/pay" -> "Create App".
"""

from __future__ import annotations

import httpx

from bot.config import settings

_BASE_URL = "https://pay.crypt.bot/api"


class CryptoBotClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"Crypto-Pay-API-Token": settings.cryptobot_api_token},
            timeout=15.0,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def create_invoice(self, amount: str, asset: str, description: str, payload: str) -> dict:
        resp = await self._http.post(
            "/createInvoice",
            json={"amount": amount, "asset": asset, "description": description, "payload": payload},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"CryptoBot createInvoice failed: {body}")
        return body["result"]

    async def get_invoice_status(self, invoice_id: int) -> str:
        resp = await self._http.get("/getInvoices", params={"invoice_ids": invoice_id})
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"CryptoBot getInvoices failed: {body}")
        items = body["result"]["items"]
        if not items:
            raise ValueError(f"unknown invoice {invoice_id}")
        return items[0]["status"]  # "active" | "paid" | "expired"


cryptobot = CryptoBotClient()
