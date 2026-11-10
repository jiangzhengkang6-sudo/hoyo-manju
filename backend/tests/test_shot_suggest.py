"""单镜「按大纲生成」：只给草稿，不写库。

产品意图：分镜表里每一镜都可以先让 AI 按大纲写一版（动作/台词/构图/提示词），
用户在弹窗里改完再应用 —— AI 给初稿，人来定稿。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import STORY_TEXT, wait_job
from app.schemas import CAMERA_MOVEMENTS, COMPOSITIONS


def _generate_script(client: TestClient, worker, episode: dict) -> None:
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    created = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert created.status_code == 200, created.text
    finished = wait_job(client, worker, created.json()["id"])
    assert finished["status"] == "SUCCEEDED", finished


def test_suggest_shot_returns_editable_draft(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    _generate_script(client, worker, episode)
    before = client.get(f"/api/v1/episodes/{episode['id']}/script").json()

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/1/suggest", json={"instruction": "更有压迫感"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    suggestion = body["suggestion"]
    assert body["shot_no"] == 1
    assert suggestion["action"], suggestion
    assert suggestion["composition"] in list(COMPOSITIONS), suggestion
    assert suggestion["camera_movement"] in list(CAMERA_MOVEMENTS), suggestion
    assert 1000 <= int(suggestion["duration_ms"]) <= 15000, suggestion
    assert suggestion["image_prompt"], suggestion
    assert isinstance(suggestion["dialogue"], list)

    # 关键：只给建议，不落库（版本号与镜头内容都不变）
    after = client.get(f"/api/v1/episodes/{episode['id']}/script").json()
    assert after["version"] == before["version"]
    assert after["story_json"]["shots"][0]["action"] == before["story_json"]["shots"][0]["action"]


def test_suggest_shot_requires_script(client: TestClient, project: dict, episode: dict) -> None:
    resp = client.post(f"/api/v1/episodes/{episode['id']}/shots/1/suggest", json={})
    assert resp.status_code == 404, resp.text


def test_suggest_unknown_shot_is_rejected(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    _generate_script(client, worker, episode)
    resp = client.post(f"/api/v1/episodes/{episode['id']}/shots/999/suggest", json={})
    assert resp.status_code >= 400 and resp.status_code < 500, resp.text
    assert "999" in resp.json()["error"]["message"]


def test_suggest_by_shot_id_uses_editable_row(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """分镜画面页走 `/shots/{shot_id}/suggest`：以正在编辑的那一行为准。"""
    _generate_script(client, worker, episode)
    shots = client.get(f"/api/v1/episodes/{episode['id']}/shots").json()["items"]
    shot = shots[0]
    before = shot["shot_json"].get("action")

    resp = client.post(f"/api/v1/shots/{shot['id']}/suggest", json={"instruction": "更紧张"})
    assert resp.status_code == 200, resp.text
    suggestion = resp.json()["suggestion"]
    assert suggestion["action"], suggestion
    assert suggestion["image_prompt"], suggestion

    # 依然不写库
    after = client.get(f"/api/v1/shots/{shot['id']}").json()
    assert after["shot_json"].get("action") == before


def test_suggest_by_shot_id_unknown_shot(client: TestClient, project: dict) -> None:
    resp = client.post("/api/v1/shots/sht_not_exist/suggest", json={})
    assert resp.status_code == 404, resp.text
