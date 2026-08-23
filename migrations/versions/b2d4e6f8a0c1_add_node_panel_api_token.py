"""add node panel api token

3x-ui mints an API token during install (Settings -> API Tokens, "install"
scope = admin). Presenting it as `Authorization: Bearer <token>` sets
`api_authed` in the panel, which short-circuits its CSRF middleware -- so it
avoids the login/CSRF dance entirely. Nullable: manually connected nodes may
only ever have login/password.

Revision ID: b2d4e6f8a0c1
Revises: a1c2d3e4f5a6
"""

from alembic import op
import sqlalchemy as sa

revision = "b2d4e6f8a0c1"
down_revision = "a1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("panel_api_token_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("nodes", "panel_api_token_encrypted")
