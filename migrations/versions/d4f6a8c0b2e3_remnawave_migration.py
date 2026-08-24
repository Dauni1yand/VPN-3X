"""move nodes, inbounds and clients onto Remnawave

Remnawave is one central panel owning every node, where 3x-ui was one panel
per VPS. So the per-node panel credentials stop being required (they are
kept nullable rather than dropped, so rows created under 3x-ui stay
readable and the migration stays reversible), and three new handles appear:

  * nodes  -> the node's UUID in the panel, its config profile, and the
              per-node internal squad that keeps "the server picks the node"
              true,
  * inbounds -> the inbound's UUID inside that config profile,
  * clients  -> the Remnawave user backing the client, plus the short UUID
              and subscription URL that come with it.

Revision ID: d4f6a8c0b2e3
Revises: c3e5f7a9b1d2
"""

import sqlalchemy as sa
from alembic import op

revision = "d4f6a8c0b2e3"
down_revision = "c3e5f7a9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("remnawave_node_uuid", sa.String(36), nullable=True))
    op.add_column("nodes", sa.Column("config_profile_uuid", sa.String(36), nullable=True))
    op.add_column("nodes", sa.Column("internal_squad_uuid", sa.String(36), nullable=True))

    # A Remnawave node has no panel of its own to hold credentials for.
    op.alter_column("nodes", "panel_base_url", existing_type=sa.String(255), nullable=True)
    op.alter_column("nodes", "panel_login", existing_type=sa.String(255), nullable=True)
    op.alter_column("nodes", "panel_password_encrypted", existing_type=sa.Text(), nullable=True)

    op.add_column("inbounds", sa.Column("remnawave_inbound_uuid", sa.String(36), nullable=True))
    op.alter_column("inbounds", "remote_inbound_id", existing_type=sa.Integer(), nullable=True)

    op.add_column("clients", sa.Column("remnawave_user_uuid", sa.String(36), nullable=True))
    op.add_column("clients", sa.Column("remnawave_short_uuid", sa.String(64), nullable=True))
    op.add_column("clients", sa.Column("subscription_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "subscription_url")
    op.drop_column("clients", "remnawave_short_uuid")
    op.drop_column("clients", "remnawave_user_uuid")

    op.alter_column("inbounds", "remote_inbound_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("inbounds", "remnawave_inbound_uuid")

    # Only safe while every surviving row still carries its 3x-ui
    # credentials; rows created under Remnawave have none, and the NOT NULL
    # would reject them.
    op.alter_column("nodes", "panel_password_encrypted", existing_type=sa.Text(), nullable=False)
    op.alter_column("nodes", "panel_login", existing_type=sa.String(255), nullable=False)
    op.alter_column("nodes", "panel_base_url", existing_type=sa.String(255), nullable=False)

    op.drop_column("nodes", "internal_squad_uuid")
    op.drop_column("nodes", "config_profile_uuid")
    op.drop_column("nodes", "remnawave_node_uuid")
