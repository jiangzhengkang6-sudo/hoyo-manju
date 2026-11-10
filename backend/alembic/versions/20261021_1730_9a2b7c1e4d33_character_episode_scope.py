"""角色卡按剧集隔离：同一项目下多集同名角色不能互相覆盖。

背景：`character_assets` 原先以 `(project_id, name)` 唯一。一个项目里放了
两集完全不相干的故事（EP02 异世界「陈默」、EP03 刑侦「陈默」）时，两集同步
角色会命中同一条记录，后同步的把先同步的设定覆盖掉，角色页于是出现
「与原文无关的角色」。

改法：加 `episode_id`（可空）。从剧本抓的角色带上所属剧集，手工新增的
公共角色保持为空（所有剧集可见）；唯一约束从 `(project_id, name)` 改成
`(episode_id, name)`，并按 `spec_json._source_document_id` 回填历史数据。

Revision ID: 9a2b7c1e4d33
Revises: 4c8f2b6a91d7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "9a2b7c1e4d33"
down_revision: str | None = "4c8f2b6a91d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("character_assets") as batch:
        batch.add_column(sa.Column("episode_id", sa.String(length=48), nullable=True))
        batch.drop_constraint("uq_character_name_per_project", type_="unique")
        batch.create_unique_constraint(
            "uq_character_name_per_episode", ["episode_id", "name"]
        )
        batch.create_index("ix_character_assets_episode_id", ["episode_id"])
        batch.create_foreign_key(
            "fk_character_assets_episode_id", "episodes", ["episode_id"], ["id"]
        )

    # 历史数据回填：spec_json 里记着来源剧本文档 id，用它反查剧集
    op.execute(
        """
        UPDATE character_assets
           SET episode_id = (
               SELECT sd.episode_id FROM story_documents sd
                WHERE sd.id = json_extract(character_assets.spec_json, '$._source_document_id')
           )
         WHERE json_valid(character_assets.spec_json)
           AND json_extract(character_assets.spec_json, '$._source_document_id') IS NOT NULL
        """
    )


def downgrade() -> None:
    # 回退前先把同项目内重名的剧集角色收敛（保留项目级那条，其余置空剧集）
    op.execute(
        """
        UPDATE character_assets
           SET episode_id = NULL
         WHERE episode_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM character_assets other
                WHERE other.project_id = character_assets.project_id
                  AND other.name = character_assets.name
                  AND other.id <> character_assets.id
                  AND other.episode_id IS NULL
           )
        """
    )
    with op.batch_alter_table("character_assets") as batch:
        batch.drop_constraint("fk_character_assets_episode_id", type_="foreignkey")
        batch.drop_index("ix_character_assets_episode_id")
        batch.drop_constraint("uq_character_name_per_episode", type_="unique")
        batch.create_unique_constraint(
            "uq_character_name_per_project", ["project_id", "name"]
        )
        batch.drop_column("episode_id")
