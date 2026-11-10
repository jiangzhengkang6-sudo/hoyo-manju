"""剧集「彻底删除」：内容真的清空、审计留痕、有任务时拒绝。

产品上同时存在两个动作，语义必须分清：
- 归档（`POST /episodes/{id}/archive`）：只改状态，可恢复；
- 删除（`DELETE /episodes/{id}`）：剧本/分镜/角色/音视频/任务全部清掉，不可恢复。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import STORY_TEXT, wait_job


def _generate_script(client: TestClient, worker, episode: dict) -> None:
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    created = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert created.status_code == 200, created.text
    finished = wait_job(client, worker, created.json()["id"])
    assert finished["status"] == "SUCCEEDED", finished


def _audit_actions(client: TestClient, project_id: str) -> list[str]:
    data = client.get("/api/v1/audit-logs", params={"project_id": project_id}).json()
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    return [str(item.get("action")) for item in items]


def test_delete_episode_removes_all_content(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    _generate_script(client, worker, episode)
    chars = client.get(
        f"/api/v1/projects/{project['id']}/characters", params={"episode_id": episode["id"]}
    ).json()
    assert chars["items"], "前置条件：剧本生成后应有角色卡"

    resp = client.delete(f"/api/v1/episodes/{episode['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True

    # 剧集没了、角色卡也没了
    assert client.get(f"/api/v1/episodes/{episode['id']}").status_code == 404
    assert client.get(f"/api/v1/projects/{project['id']}/characters").json()["items"] == []
    # 列表里也不应再出现（含已归档视图）
    listed = client.get(
        f"/api/v1/projects/{project['id']}/episodes", params={"include_archived": "true"}
    ).json()["items"]
    assert episode["id"] not in [e["id"] for e in listed]
    # 剧本接口同样 404
    assert client.get(f"/api/v1/episodes/{episode['id']}/script").status_code == 404


def test_delete_episode_is_audited(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    _generate_script(client, worker, episode)
    client.delete(f"/api/v1/episodes/{episode['id']}")
    # 审计日志不随剧集删除而丢失（合规要求）
    assert "DELETE" in _audit_actions(client, project["id"])


def test_delete_episode_rejected_when_job_active(
    client: TestClient, project: dict, episode: dict
) -> None:
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    # 只创建任务、不跑 worker：任务停在排队态
    created = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert created.status_code == 200, created.text

    resp = client.delete(f"/api/v1/episodes/{episode['id']}")
    assert resp.status_code == 409, resp.text
    assert "任务" in resp.json()["error"]["message"]
    # 剧集还在
    assert client.get(f"/api/v1/episodes/{episode['id']}").status_code == 200
