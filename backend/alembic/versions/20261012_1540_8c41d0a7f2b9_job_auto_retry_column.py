"""job auto retry column

FR-072：任务失败后可有限自动重试——为 `jobs` 增加退避时间列 `next_attempt_at`。
队列扫描与抢占都会跳过退避未到期的任务。

Revision ID: 8c41d0a7f2b9
Revises: 6311fb01d129
Create Date: 2026-10-12 15:40:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models

revision: str = "8c41d0a7f2b9"
down_revision: str | None = "6311fb01d129"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("next_attempt_at", app.db.models.UTCDateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "next_attempt_at")
