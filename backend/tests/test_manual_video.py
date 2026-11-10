"""上传自己的视频顶替某个镜头。

场景（真实发生过）：供应商那边任务其实成功了、也扣了钱，本地因为轮询网络
抖动判了失败。用户从供应商控制台把片子下载回来，需要一条「直接用这个文件」
的路，而不是再花一次生成费。

接口：`POST /shots/{shot_id}/video/manual {media_id}`。
它把上传素材归到该镜头名下并置为 CONFIRMED，之后合成/门控与生成出来的片段一致。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents import edit as edit_agent
from app.db.models import Episode, MediaAsset, Shot
from app.db.session import session_scope
from app.services import media as media_service

#: 视频只校验 mime 白名单，不解析内容；这里给一段占位字节即可
_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128


def _seed_shot(project: dict, episode: dict, shot_no: int = 1) -> str:
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"],
            shot_no=shot_no,
            duration_ms=4000,
            stage="CONFIRMED",
            shot_json={"action": "推门而入", "image_prompt": "雨夜巷口"},
        )
        session.add(shot)
        session.flush()
        return shot.id


def _upload(client: TestClient, project: dict, episode: dict, kind: str = "video") -> str:
    """走真实上传接口（含使用权声明），返回素材 id。"""
    mime = "video/mp4" if kind == "video" else "image/png"
    name = "clip.mp4" if kind == "video" else "still.png"
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": (name, _FAKE_MP4, mime)},
        data={
            "kind": kind,
            "episode_id": episode["id"],
            "project_id": project["id"],
            "rights_declared": "true",
            "source_note": "供应商控制台下载",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_manual_video_becomes_confirmed_clip(
    client: TestClient, project: dict, episode: dict
) -> None:
    shot_id = _seed_shot(project, episode)
    media_id = _upload(client, project, episode)

    resp = client.post(f"/api/v1/shots/{shot_id}/video/manual", json={"media_id": media_id})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["video_status"] == "CONFIRMED"
    assert body["video_media_id"] == media_id

    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert shot.video_status == "CONFIRMED"
        assert shot.video_media_id == media_id
        asset = session.get(MediaAsset, media_id)
        # 归到镜头名下：confirm() 与时间线都按 owner 判定归属
        assert asset.owner_type == "shot"
        assert asset.owner_id == shot_id
        assert asset.role == "shot_video"
        assert asset.status == "CONFIRMED"


def test_manual_video_enters_timeline_as_video_track(
    client: TestClient, project: dict, episode: dict
) -> None:
    """挂上去之后，时间线这一镜必须是 video 轨（不是静态图兜底）。"""
    shot_id = _seed_shot(project, episode)
    media_id = _upload(client, project, episode)
    client.post(f"/api/v1/shots/{shot_id}/video/manual", json={"media_id": media_id})

    with session_scope() as session:
        timeline = edit_agent.build_timeline(session, session.get(Episode, episode["id"]))
    entry = timeline["tracks"]["video"][0]
    assert entry["kind"] == "video"
    assert entry["media_id"] == media_id


def test_manual_video_rejects_non_video_asset(
    client: TestClient, project: dict, episode: dict
) -> None:
    """图片不能当片段挂——否则合成时才炸，错误离现场太远。"""
    shot_id = _seed_shot(project, episode)
    media_id = _upload(client, project, episode, kind="image")

    resp = client.post(f"/api/v1/shots/{shot_id}/video/manual", json={"media_id": media_id})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "STATE_CONFLICT"
    with session_scope() as session:
        assert session.get(Shot, shot_id).video_status == "PENDING"


def test_manual_video_reports_duration_mismatch(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    """上传片段比剧本长/短时给一句提醒（合成会按剧本时长裁剪）。"""
    shot_id = _seed_shot(project, episode)
    media_id = _upload(client, project, episode)

    monkeypatch.setattr(
        media_service, "probe_duration_ms", lambda asset: 9000
    )
    resp = client.post(f"/api/v1/shots/{shot_id}/video/manual", json={"media_id": media_id})

    body = resp.json()
    assert body["duration_ms"] == 9000
    assert body["warnings"], "时长对不上不该静默"
    assert "9000" in body["warnings"][0]
