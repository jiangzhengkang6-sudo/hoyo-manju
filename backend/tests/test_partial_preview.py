"""部分预览：镜头没齐也能先看已就绪的成片效果（FR-060 的宽松出口）。

完整时间线校验要求每个 required 镜头都有画面/视频，一个没齐就整片出不来；
但用户常常想先看「第一个镜头」长什么样，不必等全部齐。于是 preview 支持
`partial`：把未就绪镜头从时间线剔除、字幕/BGM 按新起点重映射，正式导出仍走完整校验。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.agents import edit as edit_agent
from app.db.models import Episode, Shot
from app.db.session import session_scope
from app.services import media as media_service


def _png(size: tuple[int, int] = (64, 96)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 90, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed(project: dict, episode: dict) -> str:
    """三个镜头，只有第 1 镜有确认画面，返回该画面素材 id。"""
    with session_scope() as session:
        media = media_service.save_media(
            session,
            content=_png(),
            kind="image",
            role="shot",
            filename="ready_1.png",
            owner_type="episode",
            owner_id=episode["id"],
            project_id=project["id"],
        )
        session.flush()
        mid = media.id
        for no, duration in ((1, 4000), (2, 4000), (3, 6000)):
            session.add(
                Shot(
                    episode_id=episode["id"],
                    shot_no=no,
                    duration_ms=duration,
                    stage="CONFIRMED",
                    confirmed_media_id=(mid if no == 1 else None),
                    shot_json={"action": f"动作{no}", "image_prompt": "画面"},
                )
            )
        session.flush()
        return mid


def test_full_validation_still_blocks_unready_shots(
    client: TestClient, project: dict, episode: dict
) -> None:
    _seed(project, episode)
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        result = edit_agent.validate_timeline(session, ep)
        assert result["valid"] is False
        codes = {e["code"] for e in result["errors"]}
        assert "MISSING_FRAME" in codes
        assert len(result["timeline"]["tracks"]["video"]) == 3


def test_partial_preview_keeps_only_ready_shots(
    client: TestClient, project: dict, episode: dict
) -> None:
    mid = _seed(project, episode)
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        result = edit_agent.validate_timeline(session, ep, only_ready=True)
        assert result["valid"] is True, result["errors"]
        video = result["timeline"]["tracks"]["video"]
        assert len(video) == 1
        assert video[0]["shot_no"] == 1
        assert video[0]["media_id"] == mid
        assert result["timeline"]["total_duration_ms"] == 4000


def test_partial_preview_route_accepts_partial_flag(
    client: TestClient, project: dict, episode: dict
) -> None:
    _seed(project, episode)
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/render/preview",
        json={"watermark": True, "partial": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input_summary"]["estimate_cents"] >= 0
    # 完整预览仍被拒绝（第 2、3 镜缺画面）
    full = client.post(
        f"/api/v1/episodes/{episode['id']}/render/preview",
        json={"watermark": True, "partial": False},
    )
    assert full.status_code == 500, full.text
    assert full.json()["error"]["code"] == "RENDER_ERROR"
