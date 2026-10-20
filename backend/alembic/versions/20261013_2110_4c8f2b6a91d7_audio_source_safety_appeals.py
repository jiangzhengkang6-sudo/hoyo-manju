"""audio track source and safety appeals

§12-4：TTS 失败降级 —— `audio_tracks.source` 区分 `tts` / `manual`（用户上传音频顶替）
/ `skipped`（跳过配音），否则无法判断某条台词是「没生成」还是「被有意跳过」。
§12-8：内容拦截申诉 —— 新增 `safety_appeals` 表。

Revision ID: 4c8f2b6a91d7
Revises: 7d2e4a91c5b3
Create Date: 2026-10-13 21:10:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8f2b6a91d7"
down_revision: str | None = "7d2e4a91c5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 既有音轨都是 TTS 产出的，server_default 让它们直接归到 tts
    op.add_column(
        "audio_tracks",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="tts"),
    )

    op.create_table(
        "safety_appeals",
        sa.Column("id", sa.String(length=48), primary_key=True),
        sa.Column("episode_id", sa.String(length=48), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("project_id", sa.String(length=48), nullable=True),
        sa.Column("target_type", sa.String(length=24), nullable=False, server_default="asset"),
        sa.Column("target_id", sa.String(length=48), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("resolution_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_safety_appeals_episode_id", "safety_appeals", ["episode_id"])
    op.create_index("ix_safety_appeals_project_id", "safety_appeals", ["project_id"])
    op.create_index("ix_safety_appeals_target_id", "safety_appeals", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_safety_appeals_target_id", table_name="safety_appeals")
    op.drop_index("ix_safety_appeals_project_id", table_name="safety_appeals")
    op.drop_index("ix_safety_appeals_episode_id", table_name="safety_appeals")
    op.drop_table("safety_appeals")
    op.drop_column("audio_tracks", "source")
