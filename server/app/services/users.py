"""Race-free get-or-create for a Telegram user.

A plain "SELECT, then INSERT if missing" has a TOCTOU race: two concurrent
requests for the same telegram_id (e.g. two payment-confirmation calls
racing each other) can both see "no user yet" before either commits, so
both try to INSERT -- one succeeds, the other hits the unique constraint on
users.telegram_id mid-transaction, which aborts that whole transaction (not
just that one statement) with an uncaught IntegrityError. `ON CONFLICT DO
NOTHING` makes the insert itself a no-op instead of an error when it loses
the race, so the request never crashes over something as ordinary as two
requests arriving for the same user at once."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


async def get_or_create_user(db: AsyncSession, telegram_id: int) -> User:
    stmt = (
        pg_insert(User)
        .values(telegram_id=telegram_id)
        .on_conflict_do_nothing(index_elements=[User.telegram_id])
    )
    await db.execute(stmt)
    return (await db.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()
