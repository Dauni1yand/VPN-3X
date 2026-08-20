"""Verifies CryptoBot (Crypto Pay API) webhook signatures.

Per CryptoBot's docs: the secret key is SHA256(your API token), and the
signature is HMAC-SHA256 of the raw request body using that secret,
compared against the `crypto-pay-api-signature` header."""

from __future__ import annotations

import hashlib
import hmac

from app.core.config import settings


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    if not settings.cryptobot_api_token or not signature_header:
        return False
    secret = hashlib.sha256(settings.cryptobot_api_token.encode()).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
