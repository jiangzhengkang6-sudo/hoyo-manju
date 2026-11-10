"""项目级「统一语速」：新建角色/剧本同步角色时继承它。

Revision ID: 5b8e2c7d1a44
Revises: 3f7c1a9d2b6e
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2c7d1a44"
down_revision: str | None = "3f7c1a9d2b6e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(
            sa.Column("default_voice_speed", sa.Float(), nullable=False, server_default="1.0")
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("default_voice_speed")
