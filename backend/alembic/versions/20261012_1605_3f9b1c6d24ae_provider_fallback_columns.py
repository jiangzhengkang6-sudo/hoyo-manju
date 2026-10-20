"""provider fallback columns

FR-090：为文本/图像/视频/TTS 配置备用供应商——`provider_settings` 增加一组
`fallback_*` 列（密钥同样加密存储，只回传掩码）。

Revision ID: 3f9b1c6d24ae
Revises: 8c41d0a7f2b9
Create Date: 2026-10-12 16:05:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f9b1c6d24ae"
down_revision: str | None = "8c41d0a7f2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("fallback_provider", sa.String(length=48), ""),
    ("fallback_model", sa.String(length=96), ""),
    ("fallback_base_url", sa.String(length=255), ""),
    ("fallback_api_key_encrypted", sa.Text(), ""),
    ("fallback_api_key_masked", sa.String(length=64), ""),
)


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column(
            "provider_settings",
            sa.Column(name, type_, nullable=False, server_default=default),
        )


def downgrade() -> None:
    for name, _type, _default in _COLUMNS:
        op.drop_column("provider_settings", name)
