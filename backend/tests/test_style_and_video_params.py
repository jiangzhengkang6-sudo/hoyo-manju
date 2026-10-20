"""FR-022 画风全量注入 + FR-040 画幅比例与视频参数持久化。

这两条都属于「后端有能力但没接线」：`palette` 存在库里却从未进提示词，
`aspect_ratio` 存在项目上却从未影响出图出片尺寸。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.shot import build_image_prompt
from app.agents.video import ASPECT_PRESETS, resolve_resolution
from app.db.models import Project, Shot
from app.db.session import session_scope


class _FakeShot:
    shot_json = {
        "composition": "中景",
        "camera_movement": "推",
        "action": "抬头",
        "image_prompt": "雨夜街道",
        "emotion": "怅然",
    }
    duration_ms = 4000


class _FakeStyle:
    description = "日系赛璐璐"
    camera_style = "固定机位"
    lighting_style = "柔和顶光"
    negative_prompt = "低饱和"
    palette = {"主色": "#2B4C7E", "辅色": "#E8B04B"}


def test_palette_is_injected_into_prompt() -> None:
    """FR-022：色板必须进入提示词（此前 5 项画风只注入 4 项）。"""
    prompt, _ = build_image_prompt(_FakeShot(), style=_FakeStyle(), characters=[])
    assert "#2B4C7E" in prompt
    assert "#E8B04B" in prompt
    assert "配色" in prompt


def test_palette_accepts_list_and_string_forms() -> None:
    """palette 允许 dict / list / str 三种存法，都要能注入。"""

    class ListStyle(_FakeStyle):
        palette = ["#111111", "#222222"]

    class StrStyle(_FakeStyle):
        palette = "暖色调，低对比"

    prompt_list, _ = build_image_prompt(_FakeShot(), style=ListStyle(), characters=[])
    assert "#111111" in prompt_list and "#222222" in prompt_list

    prompt_str, _ = build_image_prompt(_FakeShot(), style=StrStyle(), characters=[])
    assert "暖色调，低对比" in prompt_str


def test_empty_palette_does_not_pollute_prompt() -> None:
    class EmptyStyle(_FakeStyle):
        palette = {}

    prompt, _ = build_image_prompt(_FakeShot(), style=EmptyStyle(), characters=[])
    assert "配色" not in prompt


def test_aspect_ratio_changes_orientation_hint() -> None:
    """FR-040：构图提示随画幅比例走，不再写死竖屏。"""
    vertical, _ = build_image_prompt(_FakeShot(), style=None, characters=[], aspect_ratio="9:16")
    assert "竖屏 9:16" in vertical

    wide, _ = build_image_prompt(_FakeShot(), style=None, characters=[], aspect_ratio="16:9")
    assert "横屏 16:9" in wide
    assert "竖屏" not in wide

    square, _ = build_image_prompt(_FakeShot(), style=None, characters=[], aspect_ratio="1:1")
    assert "方形 1:1" in square


def test_default_aspect_ratio_is_vertical() -> None:
    prompt, _ = build_image_prompt(_FakeShot(), style=None, characters=[])
    assert "竖屏 9:16" in prompt


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        ("9:16", (1080, 1920)),
        ("16:9", (1920, 1080)),
        ("1:1", (1080, 1080)),
        ("21:9", (2560, 1080)),
        ("不存在的比例", (1080, 1920)),
        (None, (1080, 1920)),
    ],
)
def test_resolve_resolution(ratio, expected) -> None:
    assert resolve_resolution(ratio) == expected


def test_video_params_roundtrip(client: TestClient, project: dict, episode: dict) -> None:
    """FR-040：视频参数要能写入并读回，且真的落到 shot_json.video_params。"""
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"], shot_no=1, duration_ms=4000,
            shot_json={"action": "走动"}, stage="CONFIRMED",
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    read = client.get(f"/api/v1/episodes/{episode['id']}/video/params").json()
    assert read["aspect_ratio"] == project.get("aspect_ratio", "9:16")
    assert read["width"] and read["height"]
    assert read["aspect_presets"]
    assert read["shots"][0]["motion_strength"] is not None

    resp = client.put(
        f"/api/v1/episodes/{episode['id']}/video/params",
        json={"motion_strength": 0.75, "duration_ms": 6000, "camera_movement": "摇"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated_shots"] == 1

    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        vp = shot.shot_json["video_params"]
        assert vp["motion_strength"] == 0.75
        assert vp["camera_movement"] == "摇"
        # duration_ms 同时要落到列上，否则时间线与生成会不一致
        assert shot.duration_ms == 6000


def test_aspect_ratio_persists_on_project(client: TestClient, project: dict,
                                          episode: dict) -> None:
    resp = client.put(
        f"/api/v1/episodes/{episode['id']}/video/params",
        json={"aspect_ratio": "16:9"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["aspect_ratio"] == "16:9"
    assert resp.json()["width"] == 1920

    with session_scope() as session:
        row = session.get(Project, project["id"])
        assert row.aspect_ratio == "16:9"


def test_unknown_aspect_ratio_is_rejected(client: TestClient, episode: dict) -> None:
    resp = client.put(
        f"/api/v1/episodes/{episode['id']}/video/params",
        json={"aspect_ratio": "3:7"},
    )
    assert resp.status_code == 409, resp.text
    assert "不支持的画幅比例" in resp.json()["error"]["message"]


def test_motion_strength_is_clamped(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        session.add(Shot(episode_id=episode["id"], shot_no=1, duration_ms=4000,
                         shot_json={"action": "走"}, stage="CONFIRMED"))
    resp = client.put(
        f"/api/v1/episodes/{episode['id']}/video/params",
        json={"motion_strength": 9.9},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"]["motion_strength"] == 1.0


def test_style_is_default_flag(client: TestClient, project: dict) -> None:
    """FR-022：默认画风要能在列表里被识别。"""
    created = client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={"name": "测试画风", "description": "柔和"},
    )
    assert created.status_code == 201, created.text
    style_id = created.json()["id"]
    assert created.json()["is_default"] is False

    with session_scope() as session:
        row = session.get(Project, project["id"])
        row.default_style_id = style_id
        session.flush()

    items = client.get(f"/api/v1/projects/{project['id']}/styles").json()["items"]
    target = next(s for s in items if s["id"] == style_id)
    assert target["is_default"] is True
    # 其它画风必须为 False，否则界面会同时标记多个默认
    assert all(not s["is_default"] for s in items if s["id"] != style_id)
