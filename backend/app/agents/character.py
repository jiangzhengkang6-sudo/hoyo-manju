"""CharacterAgent：角色设定、参考图任务、一致性参数（FR-MVP-020/021/023/024）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.story import run_coro
from app.core.errors import StateConflict
from app.db.models import (
    AssetStatus,
    CharacterAsset,
    Episode,
    MediaAsset,
    Project,
    Shot,
    ShotCharacterRef,
    StyleAsset,
)
from app.providers.base import ImageRequest, ProviderKind
from app.providers.registry import resolve
from app.services import media as media_service

KIND_LABEL = {
    "setting": "角色设定图",
    "front": "正面全身图",
    "half_body": "半身图",
    "expression": "表情图",
}

KIND_FRAMING = {
    "setting": "全身站姿，展示完整服饰与配色，纯色背景，设定稿风格",
    "front": "正面全身，正视角，自然站姿，纯色背景",
    "half_body": "半身近景，胸像以上，正视角",
    "expression": "面部特写，同一人物，展示情绪表情",
}


def consistency_params(character: CharacterAsset) -> dict[str, Any]:
    """一致性参数：MVP 通过固定提示词片段 + 参考图引用保证角色一致（不做 LoRA）。"""
    spec = character.spec_json or {}
    traits = [
        spec.get("appearance", ""),
        spec.get("outfit", ""),
        spec.get("age_range", ""),
    ]
    anchor = "，".join(t for t in traits if t)
    return {
        "character_id": character.id,
        "name": character.name,
        "anchor_prompt": anchor,
        "forbidden_changes": spec.get("forbidden_changes", []),
        "reference_media_id": character.primary_media_id,
        "version": character.version,
    }


def build_prompt(
    character: CharacterAsset,
    kind: str,
    style: StyleAsset | None = None,
) -> str:
    spec = character.spec_json or {}
    parts = [
        f"{character.name}，{spec.get('identity', '')}",
        f"年龄 {spec.get('age_range', '')}" if spec.get("age_range") else "",
        spec.get("appearance", ""),
        spec.get("outfit", ""),
        KIND_FRAMING.get(kind, ""),
        f"性格气质：{spec.get('personality', '')}" if spec.get("personality") else "",
    ]
    if style:
        parts += [style.description, style.lighting_style, style.camera_style]
    parts += ["竖屏 9:16 构图", "高质量，线条干净，色彩统一"]
    return "，".join(p for p in parts if p)


def negative_prompt(character: CharacterAsset, style: StyleAsset | None = None) -> str:
    spec = character.spec_json or {}
    parts = ["低清", "畸形手指", "多余肢体", "文字水印", "五官崩坏"]
    parts += list(spec.get("forbidden_changes", []))
    if style and style.negative_prompt:
        parts.append(style.negative_prompt)
    return "，".join(parts)


def generate_assets(
    session: Session,
    *,
    character: CharacterAsset,
    asset_kinds: list[str],
    count_per_kind: int,
    project: Project,
    style: StyleAsset | None,
    progress_cb=None,
) -> tuple[list[MediaAsset], int]:
    """生成角色参考图；返回 `(资产列表, 实际消耗分)`。"""
    provider = resolve(ProviderKind.IMAGE, session)
    created: list[MediaAsset] = []
    # FR-042 / §13.1：实际消耗要回传，否则 executor 结算时按 0 记账
    cost_cents = 0
    total = max(1, len(asset_kinds) * count_per_kind)
    done = 0
    for kind in asset_kinds:
        request = ImageRequest(
            prompt=build_prompt(character, kind, style),
            negative_prompt=negative_prompt(character, style),
            width=1080,
            height=1920,
            count=count_per_kind,
            reference_uris=[media_service.media_url(m) for m in _reference_media(session, character)],
            metadata={
                "shot_no": None,
                "characters": [character.name],
                "composition": "全景",
                "camera_movement": "固定",
                "label": f"{character.name}_{kind}",
            },
        )
        result = run_coro(provider.generate(request))
        if not result.ok:
            from app.core.errors import ProviderError

            raise ProviderError(
                result.error_message or "角色资产生成失败",
                code=result.error_code or "PROVIDER_ERROR",
                retryable=result.retryable,
            )
        for artifact in result.artifacts:
            asset = media_service.save_artifact(
                session,
                artifact,
                kind="image",
                role=f"character_{kind}",
                owner_type="character",
                owner_id=character.id,
                episode_id=None,
                project_id=project.id,
                status=AssetStatus.CANDIDATE.value,
                version=character.version,
                metadata={
                    "character_id": character.id,
                    "asset_kind": kind,
                    "model": provider.id,
                    "prompt": request.prompt,
                    "negative_prompt": request.negative_prompt,
                    "params": {"width": request.width, "height": request.height},
                },
            )
            created.append(asset)
            done += 1
            if progress_cb:
                progress_cb(int(done / total * 100))
        cost_cents += int(result.cost_cents or 0)
    return created, cost_cents


def _reference_media(session: Session, character: CharacterAsset) -> list[MediaAsset]:
    if not character.primary_media_id:
        return []
    asset = session.get(MediaAsset, character.primary_media_id)
    return [asset] if asset else []


def list_assets(session: Session, character_id: str) -> list[MediaAsset]:
    return media_service.list_media(session, owner_id=character_id, limit=100)


def confirm_primary(session: Session, character: CharacterAsset, media_id: str) -> CharacterAsset:
    asset = media_service.get_media(session, media_id)
    if asset.owner_id != character.id:
        raise StateConflict("该图片不属于此角色，无法作为主参考图")
    if asset.status == AssetStatus.REJECTED.value:
        raise StateConflict("质量检查未通过的图片不能作为主参考图")
    if character.primary_media_id and character.primary_media_id != media_id:
        old = session.get(MediaAsset, character.primary_media_id)
        if old:
            media_service.supersede(session, old)
    character.primary_media_id = media_id
    character.status = AssetStatus.CONFIRMED.value
    asset.status = AssetStatus.CONFIRMED.value
    session.flush()
    return character


def affected_shots(session: Session, character: CharacterAsset) -> list[dict[str, Any]]:
    """替换主参考图后受影响的分镜（FR-MVP-024）。"""
    rows = session.execute(
        select(Shot, ShotCharacterRef)
        .join(ShotCharacterRef, ShotCharacterRef.shot_id == Shot.id)
        .where(ShotCharacterRef.character_id == character.id)
        .order_by(Shot.shot_no)
    ).all()
    return [
        {
            "shot_id": shot.id,
            "shot_no": shot.shot_no,
            "image_status": shot.image_status,
            "video_status": shot.video_status,
            "confirmed": bool(shot.confirmed_media_id),
        }
        for shot, _ref in rows
    ]


def bump_version(session: Session, character: CharacterAsset, note: str = "") -> None:
    character.version = int(character.version or 1) + 1
    session.flush()


def project_style(session: Session, project: Project) -> StyleAsset | None:
    if not project.default_style_id:
        return None
    return session.get(StyleAsset, project.default_style_id)


def sync_characters_from_script(
    session: Session, project: Project, script: dict[str, Any]
) -> list[CharacterAsset]:
    """把剧本人物表同步为角色卡；已存在角色保留其参考图与音色。"""
    bible = (script or {}).get("story_bible") or {}
    items = bible.get("characters") or []
    existing = {
        c.name: c
        for c in session.scalars(
            select(CharacterAsset).where(CharacterAsset.project_id == project.id)
        )
    }
    result: list[CharacterAsset] = []
    seen: set[str] = set()
    for item in items:
        name = (item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        spec = {
            "identity": item.get("identity", ""),
            "age_range": item.get("age_range", ""),
            "appearance": item.get("appearance", ""),
            "outfit": item.get("outfit", ""),
            "personality": item.get("personality", ""),
            "speech_style": item.get("speech_style", ""),
            "forbidden_changes": item.get("forbidden_changes", []),
        }
        character = existing.get(name)
        if character is None:
            character = CharacterAsset(
                project_id=project.id,
                name=name,
                spec_json=spec,
                is_main=bool(item.get("is_main", True)),
                status=AssetStatus.DRAFT.value,
                version=1,
            )
            session.add(character)
            existing[name] = character
        else:
            if character.spec_json != spec:
                character.spec_json = spec
                character.version = int(character.version or 1) + 1
                character.status = AssetStatus.DRAFT.value
            character.is_main = bool(item.get("is_main", character.is_main))
        result.append(character)
    session.flush()
    return result


def ensure_character_refs(
    session: Session, episode: Episode, shots: list[Shot]
) -> list[dict[str, Any]]:
    """FR-023 引用完整性检查：缺失已确认角色资产时阻断并给出补齐提示。

    镜头上打了 `no_character=true` 标记（用户在界面上确认「本镜无角色」）时
    直接跳过检查——这也是 PRD 要求的合法绕过路径。
    """
    project = session.get(Project, episode.project_id)
    characters = {
        c.name: c
        for c in session.scalars(
            select(CharacterAsset).where(CharacterAsset.project_id == project.id)
        )
    }
    problems: list[dict[str, Any]] = []
    pending: dict[tuple[str, str], ShotCharacterRef] = {}
    for shot in shots:
        data = shot.shot_json or {}
        # FR-023：用户显式确认「本镜无角色」后不再要求角色资产
        if data.get("no_character"):
            continue
        names = list(dict.fromkeys(data.get("characters", [])))
        for name in names:
            character = characters.get(name)
            if character is None:
                problems.append(
                    {
                        "shot_no": shot.shot_no,
                        "character": name,
                        "reason": "MISSING_CHARACTER",
                        "message": f"镜 {shot.shot_no} 引用了未建档的角色「{name}」",
                    }
                )
                continue
            if not character.primary_media_id:
                problems.append(
                    {
                        "shot_no": shot.shot_no,
                        "character": name,
                        "reason": "MISSING_REFERENCE",
                        "message": f"角色「{name}」尚未确认参考图，请先补齐或标记为无角色镜头",
                    }
                )
                continue
            ref = pending.get((shot.id, character.id)) or session.scalar(
                select(ShotCharacterRef).where(
                    ShotCharacterRef.shot_id == shot.id,
                    ShotCharacterRef.character_id == character.id,
                )
            )
            if ref is None:
                ref = ShotCharacterRef(
                    shot_id=shot.id,
                    character_id=character.id,
                    character_version=character.version,
                    media_id=character.primary_media_id,
                )
                session.add(ref)
                pending[(shot.id, character.id)] = ref
            else:
                ref.character_version = character.version
                ref.media_id = character.primary_media_id
                pending[(shot.id, character.id)] = ref
    session.flush()
    return problems
