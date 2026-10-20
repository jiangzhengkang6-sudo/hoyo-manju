"""FR-023 引用完整性：必须在提交接口阻断，且提供「无角色镜头」合法绕过。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode
from app.db.models import Shot
from app.db.session import session_scope


def _seed_shot(episode_id: str, *, shot_no: int = 1, characters: list[str] | None = None,
               no_character: bool = False) -> str:
    data: dict = {"action": "走动", "image_prompt": "窗边的猫"}
    if characters is not None:
        data["characters"] = characters
    if no_character:
        data["no_character"] = True
    with session_scope() as session:
        shot = Shot(
            episode_id=episode_id,
            shot_no=shot_no,
            duration_ms=4000,
            shot_json=data,
            stage="CONFIRMED",
        )
        session.add(shot)
        session.flush()
        return shot.id


def _create_character(client: TestClient, project_id: str, name: str) -> dict:
    resp = client.post(
        f"/api/v1/projects/{project_id}/characters",
        json={"name": name, "identity": "20 岁女性", "is_main": True},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_missing_character_blocks_at_submit(client: TestClient, project: dict,
                                            episode: dict) -> None:
    """FR-023：角色未建档时，**提交接口**就要 409，而不是丢给异步任务。"""
    _seed_shot(episode["id"], characters=["并不存在的角色"])

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"candidates_per_shot": 1},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == ErrorCode.STATE_CONFLICT
    problems = body["details"]["problems"]
    assert any(p["reason"] == "MISSING_CHARACTER" for p in problems)

    # 关键：被阻断时**不能**留下任务，也不能占用配额
    jobs = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
    items = jobs["items"] if isinstance(jobs, dict) and "items" in jobs else jobs
    assert not [j for j in items if j["type"] == "IMAGE_GENERATION"], items
    quota = client.get("/api/v1/quota").json()
    assert quota["reserved_cents"] == 0, quota


def test_estimate_endpoint_warns_before_submit(client: TestClient, project: dict,
                                               episode: dict) -> None:
    """FR-023：预估接口要提前暴露会阻断的镜头，前端可先提示后提交。"""
    _seed_shot(episode["id"], characters=["也没建档"])

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/estimate-images",
        json={"candidates_per_shot": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["can_generate"] is False
    assert body["blocking_problems"], body
    assert body["blocking_problems"][0]["reason"] == "MISSING_CHARACTER"
    # 成本预估仍然要给出来，方便界面同时显示「要花多少」与「为什么还不能点」
    assert body["estimate_cents"] > 0


def test_no_character_marker_bypasses_check(client: TestClient, project: dict,
                                            episode: dict) -> None:
    """FR-023：用户显式确认「本镜无角色」是合法绕过路径。"""
    shot_id = _seed_shot(episode["id"], characters=["未建档角色"])

    resp = client.patch(f"/api/v1/shots/{shot_id}", json={"no_character": True})
    assert resp.status_code == 200, resp.text

    est = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/estimate-images",
        json={"candidates_per_shot": 1},
    ).json()
    assert est["can_generate"] is True, est
    assert est["blocking_problems"] == []


def test_character_without_reference_image_still_blocks(client: TestClient,
                                                        project: dict,
                                                        episode: dict) -> None:
    """角色已建档但没有已确认参考图 —— 仍然阻断。"""
    _create_character(client, project["id"], "林晚")
    _seed_shot(episode["id"], characters=["林晚"])

    est = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/estimate-images",
        json={"candidates_per_shot": 1},
    ).json()
    assert est["can_generate"] is False, est
    assert est["blocking_problems"][0]["character"] == "林晚"


def test_clean_shot_can_generate(client: TestClient, project: dict, episode: dict) -> None:
    """无 characters 字段的镜头不触发引用检查。"""
    _seed_shot(episode["id"])
    est = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/estimate-images",
        json={"candidates_per_shot": 1},
    ).json()
    assert est["can_generate"] is True, est
