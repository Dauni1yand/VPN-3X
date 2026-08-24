"""add clients.vless_uri

Stores the share link as the node's own 3x-ui generated it, so the config we
hand back later is byte-for-byte the one we issued rather than a second
composition of the same parameters.

Revision ID: c3e5f7a9b1d2
Revises: b2d4e6f8a0c1
"""

import sqlalchemy as sa
from alembic import op

revision = "c3e5f7a9b1d2"
down_revision = "b2d4e6f8a0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("vless_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "vless_uri")
