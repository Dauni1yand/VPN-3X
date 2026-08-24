"""Generates X25519 keypairs and short IDs for REALITY inbounds.

We generate these ourselves rather than asking the panel for a pair: it is
one fewer round trip, and one fewer response shape to depend on. The
encoding below -- raw 32-byte key, base64url, padding stripped -- matches
what `xray x25519` itself outputs and what xray-core expects in
`realitySettings.privateKey`.
"""

from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_reality_keypair() -> tuple[str, str]:
    """Returns (private_key, public_key), both base64url-nopad encoded."""
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64url_nopad(private_bytes), _b64url_nopad(public_bytes)


def generate_short_id() -> str:
    """A REALITY shortId: 1-16 hex chars. We always use the full 16."""
    return secrets.token_hex(8)
