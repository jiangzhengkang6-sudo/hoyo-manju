"""FR-080 输入检查：台词与提示词的入库路径必须过安全检查。

此前故事的正文与上传素材有检查，但**分镜台词与提示词完全没有**——
用户可以绕过所有内容审核，把任意文本直接送进图像/视频/TTS 供应商。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode, SafetyBlocked
from app.db.models import Shot
from app.db.session import session_scope
from app.services import safety


def _seed_shot(episode_id: str, *, shot_json: dict | None = None) -> str:
    with session_scope() as session:
        shot = Shot(
            episode_id=episode_id,
            shot_no=1,
            duration_ms=4000,
            shot_json=shot_json or {"action": "走动", "image_prompt": "窗边的猫"},
            stage="CONFIRMED",
        )
        session.add(shot)
        session.flush()
        return shot.id


def test_clean_fields_pass() -> None:
    result = safety.check_fields(
        {
            "image_prompt": "雨夜的旧城区街道，霓虹倒影",
            "dialogue": [{"character": "林晚", "text": "这封信是谁寄的？"}],
            "narration": "她推开门，风灌了进来。",
        }
    )
    assert result["allowed"] is True
    assert result["problems"] == []
    assert "image_prompt" in result["checked"]
    assert "dialogue" in result["checked"]


@pytest.mark.parametrize(
    ("field", "value", "expected_label"),
    [
        ("image_prompt", "画面里要有血腥的肢解特写", "画面提示词"),
        ("dialogue", [{"character": "反派", "text": "这是炸弹配方，照做就行"}], "台词"),
        ("narration", "他靠诈骗教程发了财", "旁白"),
        ("negative_prompt", "不要裸体", "负向提示词"),
    ],
)
def test_dirty_fields_are_blocked(field: str, value: object, expected_label: str) -> None:
    result = safety.check_fields({field: value})
    assert result["allowed"] is False, result
    problem = result["problems"][0]
    assert problem["field"] == field
    assert problem["label"] == expected_label
    assert problem["suggestions"], "必须给出可执行的修改建议"


def test_character_name_is_also_checked() -> None:
    """角色名同样进提示词/字幕，不能成为绕过口子。"""
    result = safety.check_fields(
        {"dialogue": [{"character": "屠杀者", "text": "你好"}]}
    )
    assert result["allowed"] is False
    assert result["problems"][0]["field"] == "dialogue"


def test_ensure_raises_safety_blocked() -> None:
    with pytest.raises(SafetyBlocked) as excinfo:
        safety.ensure_fields_allowed({"image_prompt": "制毒流程示意"})
    err = excinfo.value
    assert err.code == ErrorCode.SAFETY_BLOCKED
    assert err.status_code == 422
    assert err.details["problems"][0]["field"] == "image_prompt"


def test_shot_patch_blocks_dirty_prompt(client: TestClient, episode: dict) -> None:
    """FR-080 端到端：脏提示词走 PATCH 必须 422，且**不落库**。"""
    shot_id = _seed_shot(episode["id"])

    resp = client.patch(
        f"/api/v1/shots/{shot_id}",
        json={"image_prompt": "血腥的肢解现场，特写"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()["error"]
    assert body["code"] == ErrorCode.SAFETY_BLOCKED
    assert body["details"]["problems"][0]["field"] == "image_prompt"
    assert body["details"]["suggestions"]

    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        # 关键：被拒的内容不能已经写进 shot_json
        assert shot.shot_json["image_prompt"] == "窗边的猫", shot.shot_json


def test_shot_patch_blocks_dirty_dialogue(client: TestClient, episode: dict) -> None:
    shot_id = _seed_shot(episode["id"])
    resp = client.patch(
        f"/api/v1/shots/{shot_id}",
        json={"dialogue": [{"character": "甲", "text": "我们来谈洗钱的细节"}]},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == ErrorCode.SAFETY_BLOCKED

    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert not shot.shot_json.get("dialogue"), "被拒台词不应落库"


def test_shot_patch_allows_clean_text(client: TestClient, episode: dict) -> None:
    shot_id = _seed_shot(episode["id"])
    resp = client.patch(
        f"/api/v1/shots/{shot_id}",
        json={
            "image_prompt": "雨夜，女孩站在路灯下抬头",
            "dialogue": [{"character": "林晚", "text": "你终于来了。"}],
        },
    )
    assert resp.status_code == 200, resp.text

    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert shot.shot_json["image_prompt"] == "雨夜，女孩站在路灯下抬头"


def test_clean_patch_does_not_break_other_fields(client: TestClient, episode: dict) -> None:
    """安全检查不能误伤未涉及的字段。"""
    shot_id = _seed_shot(episode["id"])
    resp = client.patch(f"/api/v1/shots/{shot_id}", json={"duration_ms": 6000})
    assert resp.status_code == 200, resp.text
    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert shot.duration_ms == 6000
        assert shot.shot_json["image_prompt"] == "窗边的猫"
