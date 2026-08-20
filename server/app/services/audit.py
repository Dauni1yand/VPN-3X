"""Records admin actions (README: node/setting changes from the bot must be
logged). Just appends to the session -- the caller's existing transaction
commits it, same pattern as everything else in a request handler."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog


def log_admin_action(
    db: AsyncSession, *, admin_telegram_id: int, action: str, target: str | None = None, details: str | None = None
) -> None:
    db.add(
        AdminAuditLog(
            admin_telegram_id=admin_telegram_id,
            action=action,
            target=target,
            details=details,
        )
    )
