"""Verifies CryptoBot (Crypto Pay API) webhook signatures.

Per CryptoBot's docs: the secret key is SHA256(your API token), and the
signature is HMAC-SHA256 of the raw request body using that secret,
compared against the `crypto-pay-api-signature` header."""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.settings_store import get_setting


async def verify_webhook_signature(raw_body: bytes, signature_header: str, db: AsyncSession) -> bool:
    if not signature_header:
        return False
    api_token = await get_setting(db, "cryptobot_api_token")
    if not api_token:
        return False  # admin hasn't set /setcryptobottoken yet
    secret = hashlib.sha256(api_token.encode()).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
