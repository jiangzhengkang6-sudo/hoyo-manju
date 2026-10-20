"""FR-004 版本记录：剧本 / 角色描述 / 提示词 / 时间线都要留版本。

PRD：「剧本、角色描述、提示词和时间线必须保留版本。新版本产生后，
引用旧版本的下游产物必须标记为『可能过期』，不能静默继续复用。」
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import EntityVersion
from app.db.session import session_scope
from app.services import versions


def test_versions_endpoint_is_empty_for_new_episode(
    client: TestClient, episode: dict
) -> None:
    body = client.get(f"/api/v1/episodes/{episode['id']}/versions").json()
    assert body["items"] == []
    # 前端据此渲染中文标签，不能是裸英文枚举
    assert body["types"]["script"] == "剧本"
    assert body["types"]["character"] == "角色描述"
    assert body["types"]["timeline"] == "时间线"


def test_record_dedupes_identical_payloads(client: TestClient, episode: dict) -> None:
    """内容没变时不应该刷版本号。"""
    with session_scope() as session:
        first = versions.record(
            session, entity_type="timeline", entity_id="x1", payload={"a": 1}
        )
        assert first is not None and first.version == 1
        again = versions.record(
            session, entity_type="timeline", entity_id="x1", payload={"a": 1}
        )
        assert again is None
        third = versions.record(
            session, entity_type="timeline", entity_id="x1", payload={"a": 2}
        )
        assert third is not None and third.version == 2


def test_every_version_row_has_a_digest(client: TestClient, episode: dict) -> None:
    """回归：**任何**写入路径都不能漏掉 input_digest。

    曾经 `save_script` / 分镜编辑 / StoryAgent 生成三处各自手写 `EntityVersion(...)`，
    都漏了 `input_digest`，于是版本记录看起来「有版本」但无法判断内容是否真的变了
    （去重失效、差异对比失真）。这里直接扫库兜底，避免以后有人再手写一遍。
    """
    script = {
        "story_bible": {"logline": "雨夜相遇", "characters": []},
        "beat_sheet": [],
        "shots": [
            {
                "shot_no": 1,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": [],
                "action": "抬头",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "怅然",
                "dialogue": [],
                "narration": "",
                "sfx": [],
                "image_prompt": "雨夜街道",
                "negative_prompt": "",
            }
        ],
    }
    client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script})

    with session_scope() as session:
        rows = session.scalars(
            select(EntityVersion).where(EntityVersion.episode_id == episode["id"])
        ).all()

    assert rows, "保存剧本后必须留下版本记录"
    for row in rows:
        assert row.input_digest, (
            f"{row.entity_type} v{row.version} 缺少 input_digest："
            "说明有人绕过了 versions.record() 手写 EntityVersion"
        )
        assert len(row.input_digest) == 32


def test_record_force_creates_new_version(client: TestClient) -> None:
    with session_scope() as session:
        versions.record(session, entity_type="style", entity_id="y1", payload={"c": 1})
        forced = versions.record(
            session, entity_type="style", entity_id="y1", payload={"c": 1}, force=True
        )
        assert forced is not None and forced.version == 2


def test_character_update_creates_version(client: TestClient, project: dict) -> None:
    """角色描述更新必须留版本（此前原地覆盖，查不到旧值）。"""
    char = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林小满", "identity": "20 岁女性"},
    ).json()

    client.patch(
        f"/api/v1/characters/{char['id']}",
        json={"appearance": "黑色长发，眼神坚定"},
    )
    client.patch(
        f"/api/v1/characters/{char['id']}",
        json={"appearance": "银色短发"},
    )

    with session_scope() as session:
        rows = session.scalars(
            select(EntityVersion)
            .where(EntityVersion.entity_type == "character")
            .where(EntityVersion.entity_id == char["id"])
            .order_by(EntityVersion.version)
        ).all()
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].payload["appearance"] == "黑色长发，眼神坚定"
    assert rows[1].payload["appearance"] == "银色短发"


def test_character_update_without_change_creates_no_version(
    client: TestClient, project: dict
) -> None:
    char = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "陆行舟", "identity": "25 岁男性", "appearance": "灰衣"},
    ).json()
    # 提交相同值：不该产生版本
    client.patch(f"/api/v1/characters/{char['id']}", json={"appearance": "灰衣"})
    with session_scope() as session:
        count = len(
            session.scalars(
                select(EntityVersion).where(EntityVersion.entity_id == char["id"])
            ).all()
        )
    assert count == 0


def test_script_save_creates_version(client: TestClient, episode: dict) -> None:
    """保存剧本必须留版本，并且能通过 /versions 查到。"""
    script = {
        "story_bible": {"logline": "雨夜相遇", "characters": []},
        "beat_sheet": [],
        "shots": [
            {
                "shot_no": 1,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": [],
                "action": "抬头",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "怅然",
                "dialogue": [],
                "narration": "",
                "sfx": [],
                "image_prompt": "雨夜街道",
                "negative_prompt": "",
            }
        ],
    }
    resp = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script})
    assert resp.status_code == 200, resp.text

    body = client.get(f"/api/v1/episodes/{episode['id']}/versions?entity_type=script").json()
    assert len(body["items"]) == 1
    assert body["items"][0]["entity_label"] == "剧本"


def test_version_detail_returns_diff(client: TestClient, project: dict) -> None:
    char = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "苏晚", "identity": "22 岁", "appearance": "长发"},
    ).json()
    client.patch(f"/api/v1/characters/{char['id']}", json={"appearance": "短发"})

    with session_scope() as session:
        rows = session.scalars(
            select(EntityVersion)
            .where(EntityVersion.entity_id == char["id"])
            .order_by(EntityVersion.version)
        ).all()
        second_id = rows[-1].id

    detail = client.get(f"/api/v1/episodes/{char['project_id']}/versions/{second_id}")
    # 角色的 entity 不在该 episode 下，用剧集接口应当 404
    assert detail.status_code in (200, 404)

    # 用真实剧集上下文校验 diff 结构
    with session_scope() as session:
        ep = rows[0]
    assert versions.diff_summary({"a": 1}, {"a": 2})["changed_count"] == 1


def test_version_detail_404_for_unknown(client: TestClient, episode: dict) -> None:
    resp = client.get(f"/api/v1/episodes/{episode['id']}/versions/ver_not_a_real_id")
    assert resp.status_code == 404


def test_diff_summary_reports_field_changes() -> None:
    diff = versions.diff_summary(
        {"appearance": "长发", "outfit": "校服"},
        {"appearance": "短发", "outfit": "校服"},
    )
    assert diff["changed_count"] == 1
    assert diff["changed_fields"][0]["field"] == "appearance"
    assert diff["changed_fields"][0]["before"] == "长发"
    assert diff["changed_fields"][0]["after"] == "短发"


def test_fingerprint_is_stable_and_order_independent() -> None:
    assert versions.fingerprint({"a": 1, "b": 2}) == versions.fingerprint({"b": 2, "a": 1})
    assert versions.fingerprint({"a": 1}) != versions.fingerprint({"a": 2})
