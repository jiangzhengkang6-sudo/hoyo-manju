"""FR-060 时间线校验补全 + FR-063 画幅感知输出分辨率。

FR-060 要求合成前检查：镜头顺序、视频时长、台词音频、字幕时间、BGM、
分辨率和空缺镜头。此前缺「镜头顺序」「BGM 文件存在性」「画幅一致性」三类。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.edit import (
    ASPECT_VALUES,
    preset_dimensions,
    resolution_for,
    validate_timeline,
)
from app.db.models import AudioTrack, Episode, MediaAsset, Shot
from app.db.session import session_scope


def _codes(result: dict) -> set[str]:
    return {e["code"] for e in result["errors"]} | {w["code"] for w in result["warnings"]}


def _add_shot(session, episode_id: str, shot_no: int, *, duration_ms: int = 4000,
              media_id: str | None = None) -> Shot:
    shot = Shot(
        episode_id=episode_id,
        shot_no=shot_no,
        duration_ms=duration_ms,
        required=True,
        confirmed_media_id=media_id,
        shot_json={"action": "走动", "image_prompt": "街道"},
    )
    session.add(shot)
    return shot


def _png_asset(session, episode_id: str, *, width: int, height: int) -> MediaAsset:
    asset = MediaAsset(
        episode_id=episode_id, kind="image", role="shot_image",
        storage_key="k.png", filename="k.png", mime_type="image/png",
        width=width, height=height, status="CONFIRMED",
    )
    session.add(asset)
    session.flush()
    return asset


# --------------------------------------------------------------------------- #
# 分辨率
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        ("9:16", (720, 1280)),
        ("16:9", (1280, 720)),
        ("1:1", (720, 720)),
        ("4:3", (960, 720)),
        ("3:4", (720, 960)),
        ("21:9", (1680, 720)),
        (None, (720, 1280)),
        ("乱填", (720, 1280)),
    ],
)
def test_resolution_follows_aspect_ratio(ratio, expected) -> None:
    assert resolution_for(ratio, "720p") == expected


def test_resolution_is_even() -> None:
    """H.264 要求宽高为偶数，否则 ffmpeg 直接报错。"""
    for ratio in ASPECT_VALUES:
        for preset in ("preview", "720p", "1080p"):
            w, h = resolution_for(ratio, preset)
            assert w % 2 == 0 and h % 2 == 0, (ratio, preset, w, h)


def test_legacy_preset_ids_still_resolve() -> None:
    assert preset_dimensions("720p_9x16", "9:16") == (720, 1280)
    assert preset_dimensions("1080p_9x16", "9:16") == (1080, 1920)
    # 历史 id 也不再强行出竖屏，而是跟随项目画幅
    assert preset_dimensions("720p_9x16", "16:9") == (1280, 720)
    assert preset_dimensions("preview", "1:1") == (540, 540)


# --------------------------------------------------------------------------- #
# 镜头顺序
# --------------------------------------------------------------------------- #

def test_duplicate_shot_no_is_prevented_at_db_level(
    client: TestClient, episode: dict
) -> None:
    """镜号重复由数据库唯一约束兜底（比应用层校验更强）。

    `validate_timeline` 里仍保留 `DUPLICATE_SHOT_NO` 检查，作为绕过 ORM
    约束时的纵深防御；这里断言约束本身确实存在。
    """
    from sqlalchemy.exc import IntegrityError

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        session.flush()
        _add_shot(session, ep.id, 1, media_id=asset.id)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_non_positive_shot_no_is_an_error(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 0, media_id=asset.id)
        session.flush()
        result = validate_timeline(session, ep)
    assert "INVALID_SHOT_NO" in _codes(result)


def test_shot_no_gap_is_a_warning_not_error(client: TestClient, episode: dict) -> None:
    """镜号不连续只是提醒（用户可能故意删了中间一镜），不应阻断合成。"""
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        _add_shot(session, ep.id, 3, media_id=asset.id)
        session.flush()
        result = validate_timeline(session, ep)
    assert "SHOT_NO_GAP" in _codes(result)
    assert all(e["code"] != "SHOT_NO_GAP" for e in result["errors"])


# --------------------------------------------------------------------------- #
# BGM
# --------------------------------------------------------------------------- #

def test_bgm_without_media_is_an_error(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        session.add(AudioTrack(
            episode_id=ep.id, kind="bgm", media_id=None,
            start_ms=0, end_ms=4000, rights_declared=True,
        ))
        session.flush()
        result = validate_timeline(session, ep)
    assert "BGM_MEDIA_MISSING" in _codes(result)


def test_bgm_with_missing_file_is_an_error(client: TestClient, episode: dict) -> None:
    """记录在但文件不在（存储被清理）—— 合成时 ffmpeg 会失败，必须提前拦。"""
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        bgm = MediaAsset(
            episode_id=ep.id, kind="audio", role="bgm",
            storage_key="definitely-not-there.mp3", filename="b.mp3",
            mime_type="audio/mpeg", status="CONFIRMED",
        )
        session.add(bgm)
        session.flush()
        session.add(AudioTrack(
            episode_id=ep.id, kind="bgm", media_id=bgm.id,
            start_ms=0, end_ms=4000, rights_declared=True,
        ))
        session.flush()
        result = validate_timeline(session, ep)
    assert "BGM_FILE_MISSING" in _codes(result)


def test_bgm_without_rights_is_still_an_error(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        bgm = MediaAsset(
            episode_id=ep.id, kind="audio", role="bgm",
            storage_key="x.mp3", filename="x.mp3", mime_type="audio/mpeg",
            status="CONFIRMED",
        )
        session.add(bgm)
        session.flush()
        session.add(AudioTrack(
            episode_id=ep.id, kind="bgm", media_id=bgm.id,
            start_ms=0, end_ms=4000, rights_declared=False,
        ))
        session.flush()
        result = validate_timeline(session, ep)
    assert "RIGHTS_MISSING" in _codes(result)


# --------------------------------------------------------------------------- #
# 画幅一致性
# --------------------------------------------------------------------------- #

def test_aspect_mismatch_warns_when_source_ratio_differs(
    client: TestClient, episode: dict
) -> None:
    """项目改过画幅比例后，旧素材比例会不一致 —— 要提示会被拉伸。"""
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        # 项目默认 9:16，但素材是横屏 1280×720
        asset = _png_asset(session, ep.id, width=1280, height=720)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        session.flush()
        result = validate_timeline(session, ep)
    assert "ASPECT_MISMATCH" in _codes(result)
    # 只是警告，不阻断合成
    assert all(e["code"] != "ASPECT_MISMATCH" for e in result["errors"])
    assert any(w["code"] == "ASPECT_MISMATCH" for w in result["warnings"])


def test_aspect_match_produces_no_warning(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=720, height=1280)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        session.flush()
        result = validate_timeline(session, ep)
    assert "ASPECT_MISMATCH" not in _codes(result)


def test_timeline_dimensions_follow_project_aspect_ratio(
    client: TestClient, project: dict, episode: dict
) -> None:
    """时间线尺寸必须与项目画幅一致，否则合成长宽比错误。"""
    client.put(
        f"/api/v1/episodes/{episode['id']}/video/params",
        json={"aspect_ratio": "16:9"},
    )
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        asset = _png_asset(session, ep.id, width=1280, height=720)
        _add_shot(session, ep.id, 1, media_id=asset.id)
        session.flush()
        result = validate_timeline(session, ep)
    tl = result["timeline"]
    assert tl["aspect_ratio"] == "16:9"
    assert (tl["width"], tl["height"]) == (1280, 720)


def test_export_preset_accepts_aspect_neutral_ids(
    client: TestClient, project: dict, episode: dict
) -> None:
    """`720p` / `1080p` 是画幅无关档位，schema 必须接受。"""
    from app.schemas import RenderExportIn

    assert RenderExportIn(preset="720p").preset == "720p"
    assert RenderExportIn(preset="1080p").preset == "1080p"
    assert RenderExportIn(preset="720p_9x16").preset == "720p_9x16"
    with pytest.raises(Exception):
        RenderExportIn(preset="4k")


def test_limits_exposes_resolutions_by_aspect(client: TestClient) -> None:
    body = client.get("/api/v1/config/limits").json()
    assert set(body["aspect_ratios"]) == set(ASPECT_VALUES)
    table = body["resolutions_by_aspect"]
    assert table["9:16"]["720p"] == [720, 1280]
    assert table["16:9"]["720p"] == [1280, 720]
    assert table["1:1"]["1080p"] == [1080, 1080]
    ids = {p["id"] for p in body["export_presets"]}
    assert {"720p", "1080p", "720p_9x16", "1080p_9x16"} <= ids
