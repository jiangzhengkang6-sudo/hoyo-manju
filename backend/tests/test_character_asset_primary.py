"""首次生成角色参考图后要能立刻看到结果。

线上现象：任务 100% 成功、媒体也落库了，但角色卡 `primary_media_id` 一直是空，
界面永远显示「暂无素材」——用户以为图丢了，后面出分镜图还会被
FR-023「缺已确认参考图」拦住，等于白花钱。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import wait_job


def _create_character(client: TestClient, project: dict, episode: dict) -> str:
    resp = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林晚", "identity": "女主", "episode_id": episode["id"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _generate(client: TestClient, worker, character_id: str) -> dict:
    created = client.post(
        f"/api/v1/characters/{character_id}/generate",
        json={"asset_kinds": ["front"], "count_per_kind": 1},
    )
    assert created.status_code == 200, created.text
    finished = wait_job(client, worker, created.json()["id"])
    assert finished["status"] == "SUCCEEDED", finished
    return finished


def test_first_generation_sets_primary_media(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    character_id = _create_character(client, project, episode)
    assert client.get(f"/api/v1/characters/{character_id}").json()["primary_media_id"] is None

    finished = _generate(client, worker, character_id)
    media_ids = finished["result_ref"]["media_ids"]
    assert media_ids

    character = client.get(f"/api/v1/characters/{character_id}").json()
    assert character["primary_media_id"] == media_ids[0], character
    assert character["primary_media_url"], "主图应带可用 URL"

    # 素材列表必须给出 `media_id`（前端「设为主图」直接把它回传，缺了会 422）
    assets = client.get(f"/api/v1/characters/{character_id}/assets").json()
    assert assets["items"], assets
    assert assets["items"][0]["media_id"] == media_ids[0], assets["items"][0].keys()

    # 用这份 media_id 能真的确认主图（回归 422 body.media_id）
    confirmed = client.post(
        f"/api/v1/characters/{character_id}/confirm",
        json={"media_id": assets["items"][0]["media_id"]},
    )
    assert confirmed.status_code == 200, confirmed.text


def test_second_generation_keeps_existing_primary(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """再生成一批不能把用户已经选好的主图换掉。"""
    character_id = _create_character(client, project, episode)
    _generate(client, worker, character_id)
    first_primary = client.get(f"/api/v1/characters/{character_id}").json()["primary_media_id"]

    _generate(client, worker, character_id)
    assert client.get(f"/api/v1/characters/{character_id}").json()["primary_media_id"] == first_primary
