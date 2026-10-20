"""character voice speed

FR-051：TTS 必须保留语速。`character_assets` 增加 `voice_speed`（倍率，默认 1.0），
随角色一起持久化，并在生成台词音频时传给供应商、留档到音轨与素材元数据。

Revision ID: 7d2e4a91c5b3
Revises: 3f9b1c6d24ae
Create Date: 2026-10-13 19:30:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2e4a91c5b3"
down_revision: str | None = "3f9b1c6d24ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default 让既有行立刻拿到 1.0，避免旧数据读出 NULL 导致语速变成 0
    op.add_column(
        "character_assets",
        sa.Column("voice_speed", sa.Float(), nullable=False, server_default="1.0"),
    )


def downgrade() -> None:
    op.drop_column("character_assets", "voice_speed")
