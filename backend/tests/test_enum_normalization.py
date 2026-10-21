"""枚举归一化回归（真实 LLM 输出的变体不该让整条剧本生成失败）。

背景：DeepSeek 会把 camera_movement 写成「推镜头」「pan」「固定机位」，
严格 Literal 校验会连报两次错、把「生成剧本」这一步彻底打挂
（线上报错：shots.4.camera_movement: Input should be '固定','推',...）。
"""

from __future__ import annotations

import pytest

from app.schemas import (
    CAMERA_ALIASES,
    CAMERA_MOVEMENTS,
    COMPOSITION_ALIASES,
    COMPOSITIONS,
    ShotScript,
    VideoParams,
    normalize_enum,
)


def _shot(**over) -> ShotScript:
    data = {
        "shot_no": 1,
        "duration_ms": 4000,
        "action": "林晚推门进入咖啡馆",
        "image_prompt": "雨夜咖啡馆，林晚推门，冷色调，9:16",
        "camera_movement": "固定",
        "composition": "中景",
    }
    data.update(over)
    return ShotScript.model_validate(data)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("推", "推"),
        ("推镜头", "推"),
        ("推近", "推"),
        ("缓慢推近", "推"),
        ("pan", "摇"),
        ("PAN", "摇"),
        ("固定机位", "固定"),
        ("升降镜头", "升降"),
        ("拉远", "拉"),
        ("跟随", "跟"),
        (" 摇 ", "摇"),
        ("Zoom In", "推"),
    ],
)
def test_camera_movement_variants_are_normalized(raw: str, expected: str) -> None:
    assert _shot(camera_movement=raw).camera_movement == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("特写镜头", "特写"),
        ("close-up", "特写"),
        ("中近景", "近景"),
        ("广角", "全景"),
        ("过肩镜头", "过肩"),
        ("俯视", "俯拍"),
        ("low angle", "仰拍"),
    ],
)
def test_composition_variants_are_normalized(raw: str, expected: str) -> None:
    assert _shot(composition=raw).composition == expected


def test_unknown_value_falls_back_to_default_instead_of_failing() -> None:
    """识别不出的值退回默认，而不是让整条剧本生成失败。"""
    shot = _shot(camera_movement="环绕", composition="说不清")
    assert shot.camera_movement == "固定"
    assert shot.composition == "中景"


def test_video_params_camera_movement_is_normalized() -> None:
    shot = _shot(camera_movement="推镜头")
    # 模型里的 _sync_video_params 会把 video_params.camera_movement 对齐到分镜值
    assert shot.video_params.camera_movement == "推"
    assert VideoParams.model_validate({"camera_movement": "pan"}).camera_movement == "摇"


def test_motion_strength_is_clamped() -> None:
    assert VideoParams.model_validate({"motion_strength": 1.8}).motion_strength == 1.0
    assert VideoParams.model_validate({"motion_strength": -3}).motion_strength == 0.0


def test_normalize_enum_outputs_only_canonical_values() -> None:
    """任何输入都不该产出规范集以外的值。"""
    for raw in ("推镜头", "pan", "乱写", "", None, "crane up", "拉远"):
        assert normalize_enum(raw, CAMERA_MOVEMENTS, CAMERA_ALIASES, "固定") in CAMERA_MOVEMENTS
        assert normalize_enum(raw, COMPOSITIONS, COMPOSITION_ALIASES, "中景") in COMPOSITIONS
