"""台词语音轨补 `line_index`：按台词序号对位，而不是按时间位置硬猜。

背景：`audio_tracks` 只有 `start_ms`，没有「这是该镜第几句台词」的信息。
`dialogue_lines()` / `_replace_dialogue_track()` 因此只能按 start_ms 排序后
用**下标**去对位。只要中间有一句 TTS 失败或被跳过，后面的音轨就整体错一位：
状态表把上一句的音频标成这一句的「已生成」，用户给第 3 句上传音频时
会覆盖第 2 句。这个错位不会报错，只会让成片台词对不上脸。

改法：增加可空列 `line_index`（0 起）。历史数据留空，读取时回退到按位置匹配。

Revision ID: 3f7c1a9d2b6e
Revises: 9a2b7c1e4d33
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "3f7c1a9d2b6e"
down_revision: str | None = "9a2b7c1e4d33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audio_tracks") as batch:
        batch.add_column(sa.Column("line_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audio_tracks") as batch:
        batch.drop_column("line_index")
