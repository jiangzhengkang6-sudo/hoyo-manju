"""候选图的三道闸：已驳回不能选、文件失效不能恢复、失效项不进可用集合。

背景：素材文件曾被同名 key 覆盖，数据库里留下「记录指向别人的图」的行。
这类行如果还能被选中/恢复，用户就会把一张张冠李戴的图确认进视频。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.models import MediaAsset, Shot
from app.db.session import session_scope


def _seed(episode_id: str, project_id: str) -> tuple[str, str, str]:
    """返回 (shot_id, 已驳回素材 id, 文件失效素材 id)。"""
    with session_scope() as session:
        shot = Shot(
            episode_id=episode_id, shot_no=1, duration_ms=4000,
            shot_json={"action": "推门", "image_prompt": "雨夜街道"},
            stage="CANDIDATES_READY",
        )
        session.add(shot)
        session.flush()
        rejected = MediaAsset(
            project_id=project_id, episode_id=episode_id, owner_type="shot",
            owner_id=shot.id, kind="image", role="shot_image", status="REJECTED",
            storage_key="test/shot/a.png", filename="a.png", size_bytes=10,
            metadata_json={"rejected_reason": "人物脸崩"},
        )
        broken = MediaAsset(
            project_id=project_id, episode_id=episode_id, owner_type="shot",
            owner_id=shot.id, kind="image", role="shot_image", status="REJECTED",
            storage_key="test/shot/b.png", filename="b.png", size_bytes=10,
            metadata_json={
                "stale_reason": "体积不符：库里 10B，实际 20B",
                "rejected_reason": "素材文件异常",
            },
        )
        session.add_all([rejected, broken])
        session.flush()
        return shot.id, rejected.id, broken.id


def test_rejected_candidate_cannot_be_selected(client: TestClient, project: dict,
                                               episode: dict) -> None:
    shot_id, rejected_id, _ = _seed(episode["id"], project["id"])
    resp = client.post(f"/api/v1/shots/{shot_id}/select", json={"media_id": rejected_id})
    assert resp.status_code == 409, resp.text


def test_broken_candidate_cannot_be_restored(client: TestClient, project: dict,
                                             episode: dict) -> None:
    shot_id, _, broken_id = _seed(episode["id"], project["id"])
    resp = client.post(
        f"/api/v1/shots/{shot_id}/restore-candidate", json={"media_id": broken_id}
    )
    assert resp.status_code == 409, resp.text
    assert "失效" in resp.json()["error"]["message"]


def test_candidates_payload_flags_broken(client: TestClient, project: dict,
                                         episode: dict) -> None:
    shot_id, rejected_id, broken_id = _seed(episode["id"], project["id"])
    items = client.get(f"/api/v1/shots/{shot_id}/candidates").json()["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id[broken_id]["broken"] is True
    assert by_id[broken_id]["usable"] is False
    assert by_id[rejected_id]["broken"] is False       # 只是被驳回，文件是好的
    assert by_id[rejected_id]["usable"] is False
