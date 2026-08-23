"""add installing node status

Revision ID: a1c2d3e4f5a6
Revises: f05edd038082
Create Date: 2026-08-21 19:40:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a1c2d3e4f5a6'
down_revision = 'f05edd038082'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Distinguishes "the SSH bootstrap job owns this node right now" from
    # the existing `provisioning` (connected, panel reachable, just no
    # inbound yet) -- an admin manually tapping "Создать инбаунд" on a node
    # whose bootstrap was still installing 3x-ui hit a raw connection error
    # racing the background job. PostgreSQL 12+ allows adding an enum value
    # inside a transaction as long as it isn't used by the same one, which
    # this migration doesn't do.
    op.execute("ALTER TYPE node_status ADD VALUE IF NOT EXISTS 'installing' BEFORE 'provisioning'")


def downgrade() -> None:
    # PostgreSQL has no ALTER TYPE ... DROP VALUE -- removing an enum value
    # requires rebuilding the type (create new, migrate column, drop old).
    # Not worth it for a downgrade path that would only run in dev, and
    # only if no row is left with status='installing' by then; skipped.
    pass
