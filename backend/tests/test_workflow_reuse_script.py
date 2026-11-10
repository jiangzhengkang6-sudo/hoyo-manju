"""启动工作流不能把已有剧本重写一遍。

线上场景：用户在「故事与剧本」页生成并手改过剧本，然后去「角色与画风」想继续，
点「启动工作流」——旧实现会从 `story_agent` 节点重新跑一遍模型：
既多扣一次钱，又把手改内容覆盖成一个新版本，用户看到「工作流一启动剧本就变了」。
现在只要已有可用剧本（`story_json.shots` 非空）就默认复用，显式传
`reuse_script=false` 才会重新生成。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import STORY_TEXT, wait_job


def _generate_script(client: TestClient, worker, episode: dict) -> dict:
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    created = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert created.status_code == 200, created.text
    finished = wait_job(client, worker, created.json()["id"])
    assert finished["status"] == "SUCCEEDED", finished
    return finished


def _story_jobs(client: TestClient, episode: dict) -> list[dict]:
    data = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    return [j for j in items if j["type"] == "STORY_GENERATION"]


def test_workflow_start_reuses_existing_script(
    client: TestClient, worker, episode: dict
) -> None:
    _generate_script(client, worker, episode)
    version_before = client.get(f"/api/v1/episodes/{episode['id']}/script").json()["version"]
    jobs_before = len(_story_jobs(client, episode))

    started = client.post(f"/api/v1/episodes/{episode['id']}/workflow/start", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["pending_gate"] == "gate_script_confirmation", body

    # 剧本没被改写、也没多跑一次生成任务
    assert client.get(f"/api/v1/episodes/{episode['id']}/script").json()["version"] == version_before
    assert len(_story_jobs(client, episode)) == jobs_before

    # 工作流日志要如实写明「复用」，否则用户会以为系统偷懒跳过了生成
    logs = client.get(f"/api/v1/episodes/{episode['id']}/workflow/logs").json()
    entries = logs["items"] if isinstance(logs, dict) and "items" in logs else logs
    assert any("复用已有剧本" in str(e.get("message", "")) for e in entries), entries


def test_explicit_regenerate_still_possible(
    client: TestClient, worker, episode: dict
) -> None:
    """显式要求重新生成时，仍然照旧走模型（不能把「复用」做成不可回退）。"""
    _generate_script(client, worker, episode)
    version_before = client.get(f"/api/v1/episodes/{episode['id']}/script").json()["version"]
    jobs_before = len(_story_jobs(client, episode))

    started = client.post(
        f"/api/v1/episodes/{episode['id']}/workflow/start",
        json={"reuse_script": False},
    )
    assert started.status_code == 200, started.text
    assert started.json()["pending_gate"] == "gate_script_confirmation"
    assert client.get(f"/api/v1/episodes/{episode['id']}/script").json()["version"] > version_before
    assert len(_story_jobs(client, episode)) == jobs_before + 1


def test_workflow_start_without_script_still_generates(
    client: TestClient, worker, episode: dict
) -> None:
    """没有剧本时照常生成 —— 「复用」只对已有剧本生效。"""
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    resp = client.post(f"/api/v1/episodes/{episode['id']}/workflow/start", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending_gate"] == "gate_script_confirmation"
    assert client.get(f"/api/v1/episodes/{episode['id']}/script").status_code == 200
