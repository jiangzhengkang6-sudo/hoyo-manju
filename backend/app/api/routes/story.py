"""故事输入、剧本编辑与校验、分镜表与候选图确认。"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import select

from app.agents import image as image_agent
from app.agents import safety as safety_agent
from app.agents import shot as shot_agent
from app.agents import story as story_agent
from app.api.deps import (
    SessionDep,
    get_episode,
    get_shot,
    record_approval,
    shots_of,
)
from app.core.config import settings
from app.core.errors import AppError, NotFound, SchemaValidationFailed, StateConflict
from app.db.models import (
    AssetStatus,
    EpisodeStage,
    EntityVersion,
    JobStatus,
    MediaAsset,
    Shot,
    StoryDocument,
    WorkflowStatus,
)
from app.schemas import (
    BeatRegenerate,
    ConfirmBatch,
    GenerateImages,
    RejectCandidate,
    ScriptSave,
    ScriptValidate,
    SelectCandidate,
    ShotRegenerate,
    ShotSuggestIn,
    ShotUpdate,
    StoryInput,
)
from app.services import events
from app.services import degraded as degraded_service
from app.services import jobs as jobs_service
from app.services import versions as versions_service
from app.workflow import engine as workflow_engine

router = APIRouter(tags=["story"])


def _latest_document(session: SessionDep, episode_id: str) -> StoryDocument | None:
    return session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode_id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )


def _document_dict(document: StoryDocument | None) -> dict | None:
    if document is None:
        return None
    return {
        "id": document.id,
        "episode_id": document.episode_id,
        "version": document.version,
        "input_type": document.input_type,
        "source_text": document.source_text,
        "story_json": document.story_json,
        "analysis_json": document.analysis_json,
        #: 别名：前端按 `story.analysis` 读取故事分析（FR-011），
        #: 与 `analysis_json` 同源，避免两套字段名对不上导致分析面板空白。
        "analysis": document.analysis_json,
        "status": document.status,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


# --------------------------------------------------------------------------- #
# 故事
# --------------------------------------------------------------------------- #

@router.post("/episodes/{episode_id}/story")
def save_story(episode_id: str, payload: StoryInput, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    if payload.source_text:
        safety_agent.assert_input_allowed(session, payload.source_text, episode_id=episode.id)
    version = (_latest_document(session, episode.id).version if _latest_document(session, episode.id) else 0) + 1
    document = StoryDocument(
        episode_id=episode.id,
        version=version,
        input_type=payload.input_type,
        source_text=payload.source_text,
        story_json={},
        status="DRAFT",
    )
    session.add(document)
    session.flush()
    return _document_dict(document)


@router.get("/episodes/{episode_id}/story")
def read_story(episode_id: str, session: SessionDep) -> dict | None:
    """最新故事文档（docs/API.md §4：直接返回文档，未保存时为 null）。

    注意不要包成 `{"story": ...}`——前端（以及 GET /script）都按扁平文档读取，
    包一层会让 `source_text`/`analysis` 全部取不到，刷新后正文变空。
    """
    episode = get_episode(session, episode_id=episode_id)
    return _document_dict(_latest_document(session, episode.id))


@router.post("/episodes/{episode_id}/story/estimate")
def estimate_story(episode_id: str, payload: StoryInput, session: SessionDep) -> dict:
    """FR-010：长文本输入必须显示字符数与预计生成成本。

    与 `/story/generate` 使用同一份提示词，因此这里的预估与实扣金额口径一致。
    """
    from app.agents import story as story_agent

    episode = get_episode(session, episode_id=episode_id)
    estimate = story_agent.estimate_script(
        session,
        source_text=payload.source_text,
        shot_count=payload.shot_count or settings.min_shots,
    )
    return {**estimate, "episode_id": episode.id}


@router.get("/episodes/{episode_id}/story/versions")
def story_versions(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    rows = session.scalars(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
    )
    return {
        "items": [
            {
                "id": d.id,
                "version": d.version,
                "status": d.status,
                "input_type": d.input_type,
                "source_chars": len(d.source_text or ""),
                "shot_count": len((d.story_json or {}).get("shots", [])),
                "created_at": d.created_at,
            }
            for d in rows
        ]
    }


@router.get("/episodes/{episode_id}/versions")
def episode_versions(
    episode_id: str,
    session: SessionDep,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> dict:
    """FR-004：列出该剧集的剧本 / 角色描述 / 分镜提示词 / 时间线版本。

    此前 `EntityVersion` 只写不读，前端无从查看历史版本；
    `entity_type` 可取 script / character / shot / shot_prompt / timeline / style。
    """
    episode = get_episode(session, episode_id=episode_id)
    rows = versions_service.history(
        session,
        episode_id=episode.id,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=max(1, min(limit, 500)),
    )
    return {
        "items": [versions_service.to_dict(r) for r in rows],
        "types": versions_service.TARGET_LABELS,
    }


@router.get("/episodes/{episode_id}/versions/{version_id}")
def episode_version_detail(episode_id: str, version_id: str, session: SessionDep) -> dict:
    """读取某个版本的完整快照，并给出与上一版的差异。"""
    episode = get_episode(session, episode_id=episode_id)
    row = session.get(EntityVersion, version_id)
    if row is None or row.episode_id != episode.id:
        raise NotFound(f"版本不存在：{version_id}")
    previous = session.scalars(
        select(EntityVersion)
        .where(
            EntityVersion.entity_type == row.entity_type,
            EntityVersion.entity_id == row.entity_id,
            EntityVersion.version < row.version,
        )
        .order_by(EntityVersion.version.desc())
        .limit(1)
    ).first()
    return {
        **versions_service.to_dict(row, with_payload=True),
        "diff": versions_service.diff_summary(
            previous.payload if previous else None, row.payload
        ),
    }


@router.post("/episodes/{episode_id}/story/generate")
def generate_story(episode_id: str, session: SessionDep, shot_count: int | None = None) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    source_text = document.source_text if document else ""
    # 没有输入就不能生成：模型拿到空文本会自由发挥，产出一部与用户创意
    # 毫无关系的剧本（线上实测：用户输入未落库时，剧本凭空变成便利店故事）。
    if not (source_text or "").strip():
        raise AppError(
            "故事内容为空，无法生成剧本：请先填写一句话创意或故事文本并保存"
        )
    safety_agent.assert_input_allowed(session, source_text, episode_id=episode.id)
    version = (document.version if document else 0) + 1
    job = jobs_service.create_job(
        session,
        type="STORY_GENERATION",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "STORY_GENERATION", version
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="story_agent",
        input_json={
            "source_text": source_text,
            "shot_count": shot_count or settings.min_shots,
            "input_type": document.input_type if document else "idea",
            # 带着已有分析结果重生剧本：executor 会把它写进新版本，
            # 否则每重新生成一次剧本，故事分析就被清空一次。
            "analysis": (document.analysis_json if document else None) or {},
        },
        input_summary={"source_chars": len(source_text), "shot_count": shot_count or settings.min_shots},
        max_attempts=2,
    )
    episode.stage = EpisodeStage.SCRIPT_REVIEW.value
    session.flush()
    return jobs_service.to_dict(job)


@router.post("/episodes/{episode_id}/story/analyze")
def analyze_story(episode_id: str, session: SessionDep) -> dict:
    """FR-011 故事分析（同步，模型不可用时用已有剧本兜底）。

    返回 `{"analysis": {...}, "story": {...}}`：analysis 即最新分析结果，
    story 是更新后的故事文档，前端可直接替换本地状态而不必再发一次 GET。
    """
    from app.agents import story as story_agent

    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    if document is None:
        raise NotFound("请先保存故事输入")
    analysis = story_agent.analyze_story(session, episode, document.source_text)
    document.analysis_json = analysis
    session.flush()
    return {"analysis": analysis, "story": _document_dict(document)}


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #

@router.get("/episodes/{episode_id}/script")
def read_script(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    if document is None:
        raise NotFound("该剧集尚未生成剧本")
    return _document_dict(document)


@router.put("/episodes/{episode_id}/script")
def save_script(episode_id: str, payload: ScriptSave, session: SessionDep) -> dict:
    from app.schemas import ScriptModel

    episode = get_episode(session, episode_id=episode_id)
    current = _latest_document(session, episode.id)
    if payload.base_version is not None and current and payload.base_version != current.version:
        raise StateConflict(
            f"剧本已被修改：当前版本 {current.version}，提交版本 {payload.base_version}",
            details={"current_version": current.version},
        )
    try:
        model = ScriptModel.model_validate(payload.story_json)
    except ValidationError as exc:
        raise SchemaValidationFailed(
            "剧本 Schema 校验失败，请修正后再保存",
            details={"errors": [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg")}
                for e in exc.errors(include_url=False)[:20]
            ]},
        ) from exc

    script = model.model_dump(mode="json")
    version = (current.version if current else 0) + 1
    document = StoryDocument(
        episode_id=episode.id,
        version=version,
        input_type=current.input_type if current else "manual",
        source_text=current.source_text if current else "",
        story_json=script,
        analysis_json=current.analysis_json if current else {},
        status="DRAFT",
    )
    session.add(document)
    session.flush()
    shots = shot_agent.sync_shots_from_script(session, episode, script)
    # 统一走 versions 服务，保证 input_digest / entity_label 与其它实体一致
    # （此前这里手写 EntityVersion，漏了 input_digest，导致版本无法判断是否真的变了）
    versions_service.record(
        session,
        episode_id=episode.id,
        entity_type=versions_service.TARGET_SCRIPT,
        entity_id=document.id,
        payload=script,
        note=payload.note or "人工编辑",
        version=version,
        force=True,
    )
    episode.stage = EpisodeStage.SCRIPT_REVIEW.value
    session.flush()
    # 注意：这里**不**自动抓角色。`PUT /script` 是人工保存动作，顺手建卡会让
    # 「保存剧本」这个本该无副作用的接口产生新数据（也会跟用户随后手动新增
    # 同名角色撞 409）。需要按改后的剧本重建角色卡时，走
    # `POST /episodes/{id}/characters/sync`（角色页的「从剧本同步」按钮）。
    return {**_document_dict(document), "shot_count": len(shots)}


@router.post("/episodes/{episode_id}/script/validate")
def validate_script(episode_id: str, payload: ScriptValidate, session: SessionDep) -> dict:
    from app.schemas import ScriptModel

    get_episode(session, episode_id=episode_id)
    try:
        model = ScriptModel.model_validate(payload.story_json)
    except ValidationError as exc:
        return {
            "valid": False,
            "errors": [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
                for e in exc.errors(include_url=False)
            ],
            "warnings": [],
        }
    return {"valid": True, "errors": [], "warnings": [],
            "total_duration_ms": model.total_duration_ms, "shot_count": len(model.shots)}


@router.post("/episodes/{episode_id}/beats/regenerate")
def regenerate_beats(episode_id: str, payload: BeatRegenerate, session: SessionDep) -> dict:
    """FR-014：只重生成某个剧情节拍（含其下辖分镜）。

    节拍→分镜的映射取自剧本 `beat_sheet[].shots`，换算成镜号后复用分镜
    局部重生成链路；未选中节拍的分镜不会被改动（除非勾选「重新规划后续镜头」）。
    """
    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    if document is None:
        raise NotFound("该剧集尚未生成剧本")

    beats = (document.story_json or {}).get("beat_sheet") or []
    if not beats:
        raise StateConflict(
            "当前剧本没有剧情节拍表，无法按节拍重生成；请改用按分镜重生成",
            details={"hint": "POST /episodes/{id}/shots/regenerate"},
        )

    wanted = set(payload.beat_nos)
    known = {int(b.get("beat_no", 0)) for b in beats}
    missing = sorted(wanted - known)
    if missing:
        raise NotFound(
            f"剧情节拍不存在：{missing}",
            details={"available": sorted(known)},
        )

    shot_nos: list[int] = []
    for beat in beats:
        if int(beat.get("beat_no", 0)) not in wanted:
            continue
        for shot_no in beat.get("shots") or []:
            value = int(shot_no)
            if value not in shot_nos:
                shot_nos.append(value)
    if not shot_nos:
        raise StateConflict(
            "选中的剧情节拍没有关联任何分镜，无法重生成",
            details={"beat_nos": sorted(wanted)},
        )

    version = document.version + 1
    job = jobs_service.create_job(
        session,
        type="SCRIPT_REGENERATE",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "SCRIPT_REGENERATE", version, "beats",
            ",".join(map(str, sorted(wanted))),
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="story_agent",
        input_json={
            "shot_nos": shot_nos,
            "beat_nos": sorted(wanted),
            "replan_downstream": payload.replan_downstream,
            "instruction": payload.instruction,
        },
        input_summary={"beat_nos": sorted(wanted), "shot_nos": shot_nos},
        max_attempts=2,
    )
    session.flush()
    return {**jobs_service.to_dict(job), "shot_nos": shot_nos}


@router.post("/episodes/{episode_id}/shots/{shot_no}/suggest")
def suggest_shot(
    episode_id: str,
    shot_no: int,
    payload: ShotSuggestIn,
    session: SessionDep,
) -> dict:
    """按大纲为单个分镜生成一份**可编辑草稿**（不写库）。

    前端把它填进「镜头描述」弹窗，用户改完再保存剧本；
    与 `POST /shots/regenerate`（直接产出新版本）互补：
    这里适合「这一镜不够贴剧情，让 AI 先给一版，我来定稿」。
    """
    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    if document is None:
        raise NotFound("该剧集尚未生成剧本")
    suggestion, cost_cents = story_agent.suggest_shot(
        session,
        script=document.story_json or {},
        shot_no=shot_no,
        instruction=payload.instruction,
    )
    return {
        "shot_no": shot_no,
        "suggestion": suggestion,
        "cost_cents": cost_cents,
        "script_version": document.version,
    }


@router.post("/episodes/{episode_id}/shots/regenerate")
def regenerate_shots(episode_id: str, payload: ShotRegenerate, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    document = _latest_document(session, episode.id)
    if document is None:
        raise NotFound("该剧集尚未生成剧本")
    version = document.version + 1
    job = jobs_service.create_job(
        session,
        type="SCRIPT_REGENERATE",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "SCRIPT_REGENERATE", version, ",".join(map(str, sorted(payload.shot_nos)))
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="story_agent",
        input_json={
            "shot_nos": payload.shot_nos,
            "replan_downstream": payload.replan_downstream,
            "instruction": payload.instruction,
        },
        input_summary={"shot_nos": payload.shot_nos},
        max_attempts=2,
    )
    session.flush()
    return jobs_service.to_dict(job)


# --------------------------------------------------------------------------- #
# 分镜
# --------------------------------------------------------------------------- #

@router.get("/episodes/{episode_id}/shots")
def list_shots(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    items = []
    for shot in shots_of(session, episode.id):
        items.append(shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot)))
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "confirmed": len([s for s in items if s["confirmed_media_id"]]),
            "stale": len([s for s in items if s["stale"]]),
        },
    }


@router.get("/shots/{shot_id}")
def read_shot(shot_id: str, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/shots/{shot_id}/suggest")
def suggest_shot_row(
    shot_id: str,
    payload: ShotSuggestIn,
    session: SessionDep,
) -> dict:
    """按大纲给**这一镜**生成可编辑草稿（不写库）。

    分镜画面页用它把动作/台词/构图/提示词填进编辑区，用户改完再点「保存镜头」。
    与剧本页的 `/episodes/{id}/shots/{shot_no}/suggest` 共用同一套 prompt 与归一逻辑，
    区别是这里以用户正在编辑的 `Shot` 行为准（可能已偏离剧本 JSON）。
    """
    shot = get_shot(session, shot_id=shot_id)
    episode = get_episode(session, episode_id=shot.episode_id)
    suggestion, cost_cents = story_agent.suggest_shot_for_row(
        session, episode=episode, shot=shot, instruction=payload.instruction
    )
    return {"shot_no": shot.shot_no, "suggestion": suggestion, "cost_cents": cost_cents}


@router.patch("/shots/{shot_id}")
def update_shot(shot_id: str, payload: ShotUpdate, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    patch = payload.model_dump(exclude_unset=True)
    old_dialogue = (shot.shot_json or {}).get("dialogue") or []
    shot = shot_agent.update_shot(session, shot, patch)
    new_dialogue = (shot.shot_json or {}).get("dialogue") or []
    if patch.get("dialogue") is not None and old_dialogue != new_dialogue:
        from app.agents import audio as audio_agent

        audio_agent.mark_stale_for_shot(session, shot, "台词已修改，音频需要重新生成")
        shot.audio_status = "STALE"
    versions_service.record(
        session,
        episode_id=shot.episode_id,
        entity_type=versions_service.TARGET_SHOT,
        entity_id=shot.id,
        payload=shot.shot_json or {},
        note="人工编辑分镜",
        version=shot.current_version,
        force=True,
    )
    session.flush()
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/episodes/{episode_id}/shots/estimate-images")
def estimate_images(episode_id: str, payload: GenerateImages, session: SessionDep) -> dict:
    """FR-010 / FR-023 / FR-042：提交图像任务前的成本预估、配额影响与引用完整性。"""
    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    if payload.shot_nos:
        shots = [s for s in shots if s.shot_no in set(payload.shot_nos)]
    if not shots:
        raise NotFound("没有匹配的分镜")
    from app.agents import character as character_agent
    from app.agents import image as image_agent

    quote = image_agent.estimate(
        session, shots=shots, candidates_per_shot=payload.candidates_per_shot
    )
    # FR-023：让前端在点「生成」之前就能看到会阻断的镜头，而不是提交后吃 409
    problems = character_agent.ensure_character_refs(session, episode, shots)
    blocking = [
        p for p in problems
        if p["reason"] in {"MISSING_CHARACTER", "MISSING_REFERENCE"}
    ]
    return {
        **quote,
        "problems": problems,
        "blocking_problems": blocking,
        "can_generate": not blocking,
    }


@router.post("/episodes/{episode_id}/shots/generate-images")
def generate_images(episode_id: str, payload: GenerateImages, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    # §12：图像供应商不可用时暂停画面生成；已确认画面仍可预览/合成/导出
    degraded_service.ensure_available(degraded_service.KIND_IMAGE, action="画面生成")
    shots = shots_of(session, episode.id)
    if payload.shot_nos:
        shots = [s for s in shots if s.shot_no in set(payload.shot_nos)]
    if not shots:
        raise NotFound("没有匹配的分镜")
    from app.agents import character as character_agent
    from app.agents import image as image_agent
    from app.providers import registry
    from app.providers.base import ProviderKind
    from app.services import capabilities as caps_service

    # FR-091：提交任务前按供应商能力声明校验尺寸/候选数/参考图数量，不满足就提前阻断
    caps_service.validate_image_request(
        registry.resolve(ProviderKind.IMAGE, session).capabilities(),
        width=image_agent.DEFAULT_IMAGE_WIDTH,
        height=image_agent.DEFAULT_IMAGE_HEIGHT,
        count=payload.candidates_per_shot,
        reference_count=image_agent.max_reference_count(session, shots),
    )
    # FR-042 / §12-6：不足则在建任务前就阻断（已成功资源仍可预览与导出）
    quote = image_agent.estimate(
        session, shots=shots, candidates_per_shot=payload.candidates_per_shot
    )

    # FR-023：引用完整性必须在**提交接口**就阻断，而不是丢给异步任务——
    # 否则用户会先被扣一次预留，再在任务中心看到失败。
    # `MISSING_CHARACTER`（角色未建档）与 `MISSING_REFERENCE`（没有已确认参考图）
    # 都属于「引用不完整」，两者都要阻断。
    problems = character_agent.ensure_character_refs(session, episode, shots)
    blocking = [
        p for p in problems
        if p["reason"] in {"MISSING_CHARACTER", "MISSING_REFERENCE"}
    ]
    if blocking:
        raise StateConflict(
            "角色资产缺失，已阻断图像生成（FR-023）；请先补齐角色与参考图，"
            "或把该镜头标记为无角色镜头",
            details={"problems": blocking, "all_problems": problems},
        )

    version = max((s.current_version for s in shots), default=1)
    job = jobs_service.create_job(
        session,
        type="IMAGE_GENERATION",
        idempotency_key=jobs_service.build_windowed_key(
            episode.id, "IMAGE_GENERATION",
            ",".join(str(s.shot_no) for s in shots), version,
            payload.candidates_per_shot,
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="image_generation",
        input_json={
            "shot_nos": [s.shot_no for s in shots],
            "candidates_per_shot": payload.candidates_per_shot,
        },
        input_summary={
            "shot_count": len(shots),
            "candidates_per_shot": payload.candidates_per_shot,
            "estimate_cents": quote["estimate_cents"],
            "provider": quote["provider"],
        },
        provider=quote["provider"],
        model=str(quote.get("model", "")),
        reserve_cents=quote["estimate_cents"],
        max_attempts=2,
    )
    episode.stage = EpisodeStage.SHOT_REVIEW.value
    session.flush()
    return {"job": jobs_service.to_dict(job), "problems": problems, "estimate": quote}


@router.get("/shots/{shot_id}/candidates")
def list_candidates(shot_id: str, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    return {"items": image_agent.candidates(session, shot)}


@router.post("/shots/{shot_id}/select")
def select_candidate(shot_id: str, payload: SelectCandidate, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    image_agent.select_candidate(session, shot, payload.media_id)
    session.flush()
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/shots/{shot_id}/confirm")
def confirm_shot(shot_id: str, payload: SelectCandidate, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    asset = image_agent._require_owned(session, shot, payload.media_id)
    if asset.status == AssetStatus.REJECTED.value:
        raise StateConflict("质量检查未通过的图片不能确认用于视频生成（FR-034）")
    if asset.safety_status == "BLOCKED":
        raise StateConflict("内容安全未通过的图片不能确认")
    shot.selected_media_id = payload.media_id
    shot.confirmed_media_id = payload.media_id
    shot.confirmed_at = dt.datetime.now(dt.timezone.utc)
    shot.image_status = "CONFIRMED"
    shot.stale = False
    shot.stale_reason = None
    asset.status = AssetStatus.CONFIRMED.value
    record_approval(
        session,
        episode_id=shot.episode_id,
        stage="gate_shot_confirmation",
        target_type="shot",
        target_id=shot.id,
        asset_version=shot.current_version,
        candidate_media_id=payload.media_id,
        payload={"shot_no": shot.shot_no},
    )
    session.flush()
    events.publish(
        session,
        events.SHOT_CONFIRMED,
        {"shot_id": shot.id, "shot_no": shot.shot_no, "media_id": payload.media_id,
         "episode_id": shot.episode_id},
        episode_id=shot.episode_id,
    )
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/shots/{shot_id}/reject")
def reject_candidate(shot_id: str, payload: RejectCandidate, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    image_agent.reject_candidate(session, shot, payload.media_id, payload.reason)
    session.flush()
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/shots/{shot_id}/restore-candidate")
def restore_candidate(shot_id: str, payload: SelectCandidate, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    image_agent.restore_candidate(session, shot, payload.media_id)
    session.flush()
    return shot_agent.to_dict(session, shot, candidates=image_agent.candidates(session, shot))


@router.post("/episodes/{episode_id}/shots/confirm-batch")
def confirm_batch(episode_id: str, payload: ConfirmBatch, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    results = []
    for shot in shots_of(session, episode.id):
        if shot.shot_no not in set(payload.shot_nos):
            continue
        media_id = shot.selected_media_id or _first_usable(session, shot)
        if not media_id:
            results.append({"shot_no": shot.shot_no, "ok": False, "message": "没有可用候选图"})
            continue
        # 与单张确认保持同一道闸：质量检查未通过或内容安全未通过的图片不得进视频生成
        # （FR-031/034/081；批量入口曾漏掉这两个校验）。
        asset = session.get(MediaAsset, media_id)
        if asset is not None and asset.status == AssetStatus.REJECTED.value:
            results.append(
                {"shot_no": shot.shot_no, "ok": False, "message": "质量检查未通过的图片不能确认"}
            )
            continue
        if asset is not None and asset.safety_status == "BLOCKED":
            results.append(
                {"shot_no": shot.shot_no, "ok": False, "message": "内容安全未通过的图片不能确认"}
            )
            continue
        shot.confirmed_media_id = media_id
        shot.selected_media_id = media_id
        shot.confirmed_at = dt.datetime.now(dt.timezone.utc)
        shot.image_status = "CONFIRMED"
        shot.stale = False
        if asset:
            asset.status = AssetStatus.CONFIRMED.value
        record_approval(
            session,
            episode_id=episode.id,
            stage="gate_shot_confirmation",
            target_type="shot",
            target_id=shot.id,
            asset_version=shot.current_version,
            candidate_media_id=media_id,
            payload={"shot_no": shot.shot_no, "batch": True},
        )
        results.append({"shot_no": shot.shot_no, "ok": True, "media_id": media_id})
    session.flush()
    return {"items": results}


def _first_usable(session: SessionDep, shot: Shot) -> str | None:
    for item in image_agent.candidates(session, shot):
        if item["usable"]:
            return item["id"]
    return None
