"""项目级「角色与画风资产库」（PRD §5.1-6）后端回归。

这一页此前完全没有：角色与画风只能从剧集内的阶段页操作，
项目层没有「这个项目有哪些角色 / 哪些画风」的入口。
后端接口本身早就存在，本文件把它们钉住，重点是两处**局部更新不能清空字段**。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_project(client: TestClient, name: str = "资产库项目") -> dict:
    resp = client.post("/api/v1/projects", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

def test_project_characters_start_empty_and_roundtrip(client: TestClient) -> None:
    project = _create_project(client)
    resp = client.get(f"/api/v1/projects/{project['id']}/characters")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []

    created = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林晚", "identity": "夜班护士", "appearance": "及肩黑发"},
    )
    assert created.status_code == 201, created.text
    character = created.json()
    # 前端 `voiceLabel` / `speedLabel` 依赖这三个字段
    assert character["voice_id"] is None
    assert character["voice_speed"] == 1.0
    assert character["status"] == "DRAFT"
    assert character["is_main"] is True

    listed = client.get(f"/api/v1/projects/{project['id']}/characters").json()["items"]
    assert [c["name"] for c in listed] == ["林晚"]


def test_duplicate_character_name_in_same_project_conflicts(client: TestClient) -> None:
    project = _create_project(client)
    body = {"name": "陈默"}
    assert client.post(f"/api/v1/projects/{project['id']}/characters", json=body).status_code == 201
    dup = client.post(f"/api/v1/projects/{project['id']}/characters", json=body)
    assert dup.status_code == 409, dup.text


def test_patch_character_keeps_other_fields(client: TestClient) -> None:
    """只改身份不能把名字/外貌清空（`character_dict` 拍平字段的经典坑）。"""
    project = _create_project(client)
    character = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林晚", "identity": "夜班护士", "appearance": "及肩黑发", "outfit": "白大褂"},
    ).json()

    resp = client.patch(f"/api/v1/characters/{character['id']}", json={"identity": "急诊科护士"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["identity"] == "急诊科护士"
    assert updated["name"] == "林晚"
    assert updated["appearance"] == "及肩黑发"
    assert updated["outfit"] == "白大褂"


# --------------------------------------------------------------------------- #
# 画风
# --------------------------------------------------------------------------- #

def test_project_has_a_default_style_out_of_the_box(client: TestClient) -> None:
    """项目自带一个默认画风 —— 前端不能假设「列表第一条就是自己刚建的」。"""
    project = _create_project(client)
    styles = client.get(f"/api/v1/projects/{project['id']}/styles").json()["items"]
    assert len(styles) >= 1
    assert any(s["is_default"] for s in styles), styles
    assert client.get(f"/api/v1/projects/{project['id']}").json()["default_style_id"]


def test_create_and_patch_style(client: TestClient) -> None:
    project = _create_project(client)
    created = client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={
            "name": "霓虹夜雨",
            "description": "高对比冷调",
            "palette": ["#1b2a41", "#c9a227", "冷色调"],
            "camera_style": "低角度",
            "lighting_style": "霓虹反射",
        },
    )
    assert created.status_code == 201, created.text
    style = created.json()
    assert style["version"] == 1
    assert style["status"] == "CONFIRMED"
    assert style["is_default"] is False
    assert style["palette"] == ["#1b2a41", "#c9a227", "冷色调"]

    resp = client.patch(f"/api/v1/styles/{style['id']}", json={"description": "高对比冷调，雨天"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["version"] == 2
    assert updated["description"] == "高对比冷调，雨天"
    # 未提供的字段必须原样保留
    assert updated["name"] == "霓虹夜雨"
    assert updated["palette"] == ["#1b2a41", "#c9a227", "冷色调"]
    assert updated["camera_style"] == "低角度"
    assert updated["lighting_style"] == "霓虹反射"


def test_patch_style_does_not_reset_to_create_defaults(client: TestClient) -> None:
    """回归：`StyleIn` 带非 None 默认值，曾被直接当作 PATCH body 使用。

    结果是「只改一个字段」会把 name 重置成「默认画风」、palette 清空、camera/lighting 归零，
    属于静默数据丢失。改用 `StyleUpdate`（全 None 默认）后未提供的字段一律不动。
    """
    project = _create_project(client)
    style = client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={
            "name": "水墨",
            "description": "留白与淡墨",
            "palette": ["#f5f5f0"],
            "camera_style": "固定机位",
            "lighting_style": "柔光",
        },
    ).json()

    updated = client.patch(f"/api/v1/styles/{style['id']}", json={"name": "水墨 v2"}).json()
    assert updated["name"] == "水墨 v2"
    assert updated["description"] == "留白与淡墨"
    assert updated["palette"] == ["#f5f5f0"]
    assert updated["camera_style"] == "固定机位"
    assert updated["lighting_style"] == "柔光"

    # 空 body 也不该改动任何内容，更不该抬 version
    version_before = updated["version"]
    again = client.patch(f"/api/v1/styles/{style['id']}", json={}).json()
    assert again["name"] == "水墨 v2"
    assert again["version"] == version_before


def test_apply_style_returns_affected_shots_not_style_asset(client: TestClient) -> None:
    """前端类型必须按这个形状来：`{project_id, style_id, affected_shots}`，不是 StyleAsset。"""
    project = _create_project(client)
    style = client.post(
        f"/api/v1/projects/{project['id']}/styles", json={"name": "硬光"}
    ).json()

    resp = client.post(f"/api/v1/styles/{style['id']}/apply")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) >= {"project_id", "style_id", "affected_shots"}
    assert body["style_id"] == style["id"]
    assert "id" not in body

    assert client.get(f"/api/v1/projects/{project['id']}").json()["default_style_id"] == style["id"]
    styles = client.get(f"/api/v1/projects/{project['id']}/styles").json()["items"]
    mine = [s for s in styles if s["id"] == style["id"]][0]
    assert mine["is_default"] is True
    # 旧画风不会被删除，只是不再是默认
    assert len(styles) >= 2


def test_archive_character_removes_it_from_the_library(client: TestClient) -> None:
    project = _create_project(client)
    character = client.post(
        f"/api/v1/projects/{project['id']}/characters", json={"name": "待归档"}
    ).json()

    resp = client.delete(f"/api/v1/characters/{character['id']}")
    assert resp.status_code in (200, 204), resp.text
    listed = client.get(f"/api/v1/projects/{project['id']}/characters").json()["items"]
    assert "待归档" not in [c["name"] for c in listed]
