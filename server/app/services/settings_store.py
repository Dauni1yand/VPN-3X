"""Admin-tunable key/value settings (README: подстраиваемые из тг-бота цена
подписки, длительности за рекламу, порог алертов и т.д.) -- backed by the
`settings` table so a value survives a deploy and every service reads the
same one, instead of each service hardcoding its own constant."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

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
}


async def get_setting(db: AsyncSession, key: str) -> str:
    setting = await db.get(Setting, key)
    if setting is not None:
        return setting.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(f"unknown setting {key!r}")


async def get_all_settings(db: AsyncSession) -> dict[str, str]:
    values = dict(DEFAULTS)
    rows = (await db.execute(Setting.__table__.select())).all()
    for key, value, _updated_at in rows:
        values[key] = value
    return values


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}")
    setting = await db.get(Setting, key)
    if setting is None:
        db.add(Setting(key=key, value=value))
    else:
        setting.value = value
