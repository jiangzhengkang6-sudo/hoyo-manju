"""视频成本估算：只算「选中的镜头」，且只要这些镜头**有画面**即可。

产品口径（本轮调整）：
- 未勾选的镜头完全不参与估价，不该因为它没画面就把整次估价拦掉；
- 「有画面」= 已确认画面（confirmed）**或**已选中候选图（selected），
  与真正生成视频时的首帧取法一致（`video_agent.frame_media_id`）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.models import Shot
from app.db.session import session_scope
from app.services import media as media_service


def _make_shots(episode_id: str, project_id: str, *, select: bool = False) -> tuple[str, str]:
    """造两镜：第一镜有候选图（可选是否「选中」），第二镜没有任何画面。"""
    with session_scope() as session:
        first = Shot(
            episode_id=episode_id,
            shot_no=1,
            duration_ms=4000,
            shot_json={"shot_no": 1, "action": "测试镜头一", "required": True},
            required=True,
        )
        second = Shot(
            episode_id=episode_id,
            shot_no=2,
            duration_ms=4000,
            shot_json={"shot_no": 2, "action": "测试镜头二", "required": True},
            required=True,
        )
        session.add_all([first, second])
        session.flush()

        asset = media_service.save_artifact(
            session,
            {
                "content": b"\x89PNG\r\n\x1a\nfake",
                "filename": "frame.png",
                "mime_type": "image/png",
                "width": 1080,
                "height": 1920,
            },
            kind="image",
            role="shot_image",
            owner_type="shot",
            owner_id=first.id,
            episode_id=episode_id,
            project_id=project_id,
            status="CANDIDATE",
            version=1,
            metadata={"shot_no": 1},
        )
        # 默认只放候选图、不选中也不确认：本轮要求「有画面就算有画面」
        if select:
            first.selected_media_id = asset.id
        session.flush()
        return first.id, second.id


def test_estimate_accepts_plain_candidate_without_selection(
    client: TestClient, project: dict, episode: dict
) -> None:
    """只生成了候选图、没点「选中」也没「确认」，也应该可以估价。"""
    _make_shots(episode["id"], project["id"])
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/video/estimate", json={"shot_nos": [1]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["shot_nos"] == [1]


def test_estimate_only_checks_selected_shots(
    client: TestClient, project: dict, episode: dict
) -> None:
    first_id, _ = _make_shots(episode["id"], project["id"])

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/video/estimate", json={"shot_nos": [1]}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["shot_nos"] == [1], body
    assert body["estimate_cents"] > 0


def test_estimate_rejects_selected_shot_without_frame(
    client: TestClient, project: dict, episode: dict
) -> None:
    _make_shots(episode["id"], project["id"])

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/video/estimate", json={"shot_nos": [2]}
    )
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["details"]["pending_shot_nos"] == [2], error
    assert "没有画面" in error["message"], error


def test_estimate_without_selection_still_reports_all_pending(
    client: TestClient, project: dict, episode: dict
) -> None:
    """不传 shot_nos（老客户端）时仍按全部必需镜头校验，返回待补齐清单。"""
    _make_shots(episode["id"], project["id"])
    resp = client.post(f"/api/v1/episodes/{episode['id']}/video/estimate", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["pending_shot_nos"] == [2]


def test_generate_rejects_unconfirmed_frame(
    client: TestClient, project: dict, episode: dict
) -> None:
    """估价可以宽松，但出片必须「已确认画面」。"""
    _make_shots(episode["id"], project["id"])

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/video/generate",
        json={
            "confirm": True,
            "shot_nos": [1],
            "quote_id": "quote_test",
            "idempotency_key": "idem-gen-strict-1",
        },
    )
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["details"]["pending_shot_nos"] == [1], error
    assert "确认画面" in error["message"], error
