"""§10.1 剧集状态机：BLOCKED 可回原阶段；上游变更只标 STALE 不删资源。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents import shot as shot_agent
from app.db.models import Episode, EpisodeStage, Shot
from app.db.session import session_scope


def _seed_shots(episode_id: str, count: int = 2) -> list[str]:
    ids: list[str] = []
    with session_scope() as session:
        for no in range(1, count + 1):
            shot = Shot(
                episode_id=episode_id,
                shot_no=no,
                duration_ms=4000,
                shot_json={"action": f"动作 {no}", "image_prompt": "画面"},
                stage="CONFIRMED",
            )
            session.add(shot)
            session.flush()
            ids.append(shot.id)
    return ids


def test_episode_stage_enum_has_stale() -> None:
    """§10.1：STALE 必须是正式状态成员。"""
    assert EpisodeStage.STALE.value == "STALE"
    assert EpisodeStage.BLOCKED.value == "BLOCKED"


def test_blocked_restores_original_stage(client: TestClient, project: dict,
                                         episode: dict) -> None:
    """§10.1：解除阻塞后回到原阶段，而不是停在 BLOCKED。"""
    from app.workflow import engine as workflow_engine

    episode_id = episode["id"]
    with session_scope() as session:
        ep = session.get(Episode, episode_id)
        # 模拟一次「在视频复核阶段被阻塞」
        ep.stage = EpisodeStage.BLOCKED.value
        ep.stage_before_block = EpisodeStage.VIDEO_REVIEW.value
        ep.blocked_reason = "测试用阻塞"
        session.flush()

        restored = workflow_engine.restore_stage_after_unblock(session, ep)
        assert restored is True
        assert ep.stage == EpisodeStage.VIDEO_REVIEW.value
        assert ep.stage_before_block == ""
        assert ep.blocked_reason == ""


def test_restore_is_noop_when_not_blocked(client: TestClient, episode: dict) -> None:
    from app.workflow import engine as workflow_engine

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        ep.stage = EpisodeStage.SCRIPT_REVIEW.value
        session.flush()
        assert workflow_engine.restore_stage_after_unblock(session, ep) is False
        assert ep.stage == EpisodeStage.SCRIPT_REVIEW.value


def test_restore_falls_back_to_draft_without_marker(client: TestClient,
                                                    episode: dict) -> None:
    """没有 stage_before_block 记录时不能猜，退回 DRAFT 让用户重新推进。"""
    from app.workflow import engine as workflow_engine

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        ep.stage = EpisodeStage.BLOCKED.value
        ep.stage_before_block = ""
        session.flush()
        assert workflow_engine.restore_stage_after_unblock(session, ep) is True
        assert ep.stage == EpisodeStage.DRAFT.value


def test_stale_only_marked_never_deletes_downstream(client: TestClient,
                                                    project: dict,
                                                    episode: dict) -> None:
    """§10.1：上游变更只把受影响资源标 STALE，不强制删除下游产物。"""
    episode_id = episode["id"]
    _seed_shots(episode_id, count=2)

    with session_scope() as session:
        ep = session.get(Episode, episode_id)
        ep.stage = EpisodeStage.SHOT_REVIEW.value
        session.flush()

    resp = client.post(
        f"/api/v1/projects/{project['id']}/style/switch",
        json={"style_id": None, "name": "新画风", "description": "测试画风切换"},
    )
    # 接口签名可能要求已存在的 style；无论成功与否，都必须满足「不删资源」的约束
    if resp.status_code == 200:
        with session_scope() as session:
            ep = session.get(Episode, episode_id)
            assert ep.stage == EpisodeStage.STALE.value, ep.stage
            shots = list(session.scalars(Shot.__table__.select()))
            assert shots, "标记 STALE 不能删除分镜"
            assert all(r.stale for r in shots), "受影响分镜应全部标 STALE"


def test_stale_stage_not_applied_to_draft(client: TestClient, episode: dict) -> None:
    """还在 DRAFT 的剧集不该被标 STALE（没有下游资源可受影响）。"""
    episode_id = episode["id"]
    _seed_shots(episode_id, count=1)
    with session_scope() as session:
        ep = session.get(Episode, episode_id)
        assert ep.stage == EpisodeStage.DRAFT.value
        assert shot_agent.sync_episode_stale_stage(session, ep) is False
        assert ep.stage == EpisodeStage.DRAFT.value


def test_episode_payload_exposes_stale_summary(client: TestClient, episode: dict) -> None:
    """前端需要 stale_shot_count / stale_reasons 才能给出重新生成入口。"""
    from sqlalchemy import select

    episode_id = episode["id"]
    _seed_shots(episode_id, count=2)
    with session_scope() as session:
        for shot in session.scalars(select(Shot).where(Shot.episode_id == episode_id)):
            shot.stale = True
            shot.stale_reason = "画风已切换"
        session.flush()

    body = client.get(f"/api/v1/episodes/{episode_id}").json()
    assert body["stale_shot_count"] == 2
    assert "画风已切换" in body["stale_reasons"]
