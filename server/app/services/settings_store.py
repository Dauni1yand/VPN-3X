"""Admin-tunable key/value settings (README: подстраиваемые из тг-бота цена
подписки, длительности за рекламу, порог алертов и т.д.) -- backed by the
`settings` table so a value survives a deploy and every service reads the
same one, instead of each service hardcoding its own constant."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret
from app.db.models import Setting

# Known keys and their defaults -- used when nothing's been set yet, so the
# service works out of the box before an admin ever touches /setsetting.
DEFAULTS: dict[str, str] = {
    "subscription_price_amount": "5",
    "subscription_price_asset": "USDT",
    "subscription_duration_seconds": str(30 * 24 * 60 * 60),
    "ad_short_duration_seconds": str(15 * 60),
    "ad_long_duration_seconds": str(60 * 60),
    "node_alert_consecutive_failure_threshold": "3",
    # Set via the bot's /setcryptobottoken, not an env var -- see
    # bot/handlers/admin.py. Empty means "not configured yet".
    "cryptobot_api_token": "",
    # Cloudflare account-level config, set via /setcloudflaretoken and
    # /setcloudflarezone -- see app/services/cloudflare.py and
    # bot/handlers/admin.py's /connectcloudflare.
    "cloudflare_api_token": "",
    "cloudflare_zone_id": "",
}

# Encrypted at rest (same Fernet key as node panel passwords), unlike the
# plain-text settings above. Still readable in full via get_setting/
# get_all_settings by anyone who already holds the internal API key (the
# bot needs the real value to call CryptoBot/Cloudflare) -- the encryption
# is about what sits in the database, not a second access-control layer on
# top of the internal API key. cloudflare_zone_id is NOT in here: it's an
# identifier visible on the Cloudflare dashboard to anyone with access to
# the zone, not a credential on its own.
_SECRET_KEYS = {"cryptobot_api_token", "cloudflare_api_token"}


async def get_setting(db: AsyncSession, key: str) -> str:
    setting = await db.get(Setting, key)
    if setting is not None:
        value = setting.value
    elif key in DEFAULTS:
        value = DEFAULTS[key]
    else:
        raise KeyError(f"unknown setting {key!r}")

    if key in _SECRET_KEYS and value:
        value = decrypt_secret(value)
    return value


async def get_all_settings(db: AsyncSession, *, mask_secrets: bool = False) -> dict[str, str]:
    """`mask_secrets=True` is for a human-facing overview (the bot's
    /settings command) -- individual secret values are still readable via
    get_setting, which is how the bot actually uses the CryptoBot token."""
    values = dict(DEFAULTS)
    rows = (await db.execute(Setting.__table__.select())).all()
    for key, value, _updated_at in rows:
        values[key] = value

    for key in _SECRET_KEYS:
        if mask_secrets:
            values[key] = "(set)" if values.get(key) else "(not set)"
        elif values.get(key):
            values[key] = decrypt_secret(values[key])
    return values


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    if key in _SECRET_KEYS and value:
        value = encrypt_secret(value)
    setting = await db.get(Setting, key)
    if setting is None:
        db.add(Setting(key=key, value=value))
    else:
        setting.value = value
