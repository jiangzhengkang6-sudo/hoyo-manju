"""CharacterAgent：角色设定、参考图任务、一致性参数（FR-MVP-020/021/023/024）。"""

from __future__ import annotations

import re
from typing import Any, Callable

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
    StoryDocument,
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
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[MediaAsset], int, bool]:
    """生成角色参考图；返回 `(资产列表, 实际消耗分, 是否被取消)`。"""
    provider = resolve(ProviderKind.IMAGE, session)
    created: list[MediaAsset] = []
    # FR-042 / §13.1：实际消耗要回传，否则 executor 结算时按 0 记账
    cost_cents = 0
    total = max(1, len(asset_kinds) * count_per_kind)
    done = 0
    cancelled = False
    for kind in asset_kinds:
        if should_cancel and should_cancel():
            cancelled = True
            break
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
    if created and not character.primary_media_id:
        # 首次生成自动把第一张设为**主参考图**。
        # 不设的话角色卡永远是「暂无素材」：用户看不到刚生成的图，
        # 后续出分镜图还会被 FR-023「缺已确认参考图」拦住，等于白花钱。
        character.primary_media_id = created[0].id
        session.flush()
    return created, cost_cents, cancelled


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


#: 角色卡上可编辑、且能从剧本人物表抓取的字段。
#: 与 `CharacterCreate` / `CharacterUpdate` / 前端角色表单保持同一套键名。
SPEC_FIELDS: tuple[str, ...] = (
    "identity",
    "age_range",
    "appearance",
    "outfit",
    "personality",
    "speech_style",
    "forbidden_changes",
)

#: `spec_json` 里的内部标记（下划线开头）：不返回给前端、不参与图像提示词。
MARK_USER_EDITED = "_user_edited"
MARK_SOURCE_DOC = "_source_document_id"
MARK_SOURCE_VERSION = "_source_version"


def public_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    """去掉内部标记后的角色设定（供接口返回与版本记录使用）。"""
    return {k: v for k, v in (spec or {}).items() if not str(k).startswith("_")}


def spec_from_bible_item(item: dict[str, Any]) -> dict[str, Any]:
    """人物表条目 → 角色卡 spec，字段给齐（缺项用中性默认值）。

    LLM 有时只写 name/identity，直接照抄会让角色卡「只有名字」；
    这里把 7 个字段全部铺平，空值由用户在角色卡里补。
    """
    spec: dict[str, Any] = {}
    for field in SPEC_FIELDS:
        value = item.get(field)
        if field == "forbidden_changes":
            if isinstance(value, str):
                value = [v.strip() for v in re.split(r"[、,，;；/]", value) if v.strip()]
            elif isinstance(value, list):
                value = [str(v).strip() for v in value if str(v).strip()]
            else:
                value = []
        else:
            value = str(value).strip() if value is not None else ""
        spec[field] = value
    return spec


def episode_characters(
    session: Session, project_id: str, episode_id: str | None = None
) -> dict[str, CharacterAsset]:
    """按名字取角色卡：**本集专属优先**，其次项目级公共角色。

    同一项目下可能有多集互不相干的故事，各有同名主角（EP02 魔王「陈默」、
    EP03 刑警「陈默」）。之前各处直接 `{c.name: c for c in 项目全部角色}`，
    字典会把同名的两张卡折叠成一条（后写的赢），画面/配音/一致性参数就可能
    用到另一集的角色。统一走这个函数，顺序确定：本集 > 项目级 > 其他集。
    """
    rows = list(
        session.scalars(
            select(CharacterAsset).where(CharacterAsset.project_id == project_id)
        )
    )

    def rank(c: CharacterAsset) -> int:
        if episode_id and c.episode_id == episode_id:
            return 0
        if c.episode_id is None:
            return 1
        return 2

    picked: dict[str, CharacterAsset] = {}
    for c in rows:
        current = picked.get(c.name)
        if current is None or rank(c) < rank(current):
            picked[c.name] = c
    return picked


def sync_characters_from_script(
    session: Session,
    project: Project,
    script: dict[str, Any],
    *,
    document: StoryDocument | None = None,
    episode_id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """把剧本人物表同步为角色卡（幂等）。

    - **剧本一生成就调用**：以前只在工作流跑到「角色与画风」节点时同步，
      用户停在「故事与剧本」页时角色页是空的，得先生成一遍工作流才看得到角色；
    - **抓取完整设定**：人物表的 7 个字段全部落到 `spec_json`，缺项补默认值；
    - **按剧集隔离**：卡片记录 `episode_id`，同名角色在不同剧集各存一张，
      互不覆盖（EP02 的魔王陈默 vs EP03 的刑警陈默）；
    - **保护手改**：用户在角色页改过的卡（`_user_edited`）默认不覆盖，只补空字段；
      确需「用剧本覆盖」时显式传 `force=True`；
    - 已存在的角色保留参考图与音色，只在设定真的变化时递增版本。

    返回 `{"created": [...], "updated": [...], "kept": [...], "characters": [...]}`。
    """
    bible = (script or {}).get("story_bible") or {}
    items = [i for i in (bible.get("characters") or []) if isinstance(i, dict)]
    rows = list(
        session.scalars(
            select(CharacterAsset).where(CharacterAsset.project_id == project.id)
        )
    )
    # 本集专属优先；没有本集卡时才复用项目级公共角色（手工新增的那张）
    scoped = {c.name: c for c in rows if episode_id and c.episode_id == episode_id}
    shared = {c.name: c for c in rows if c.episode_id is None}
    existing = {**shared, **scoped}
    created: list[CharacterAsset] = []
    updated: list[CharacterAsset] = []
    kept: list[CharacterAsset] = []
    ordered: list[CharacterAsset] = []
    seen: set[str] = set()

    for item in items:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        incoming = spec_from_bible_item(item)
        is_main = bool(item.get("is_main", True))
        character = existing.get(name)
        if character is None:
            spec = dict(incoming)
            if document is not None:
                spec[MARK_SOURCE_DOC] = document.id
                spec[MARK_SOURCE_VERSION] = document.version
            character = CharacterAsset(
                project_id=project.id,
                episode_id=episode_id,
                name=name,
                spec_json=spec,
                is_main=is_main,
                status=AssetStatus.DRAFT.value,
                version=1,
                # 跟随项目「统一语速」，新角色不用逐个手调
                voice_speed=float(getattr(project, "default_voice_speed", 1.0) or 1.0),
            )
            session.add(character)
            existing[name] = character
            created.append(character)
        else:
            current = dict(character.spec_json or {})
            changed = False
            if character.status == AssetStatus.ARCHIVED.value:
                # 剧本里又出现了这个角色：把归档卡恢复出来，否则分镜引用它时
                # 会被 FR-023 以「引用了未建档的角色」拦住，而用户明明建过卡。
                character.status = AssetStatus.DRAFT.value
                changed = True
            user_edited = bool(current.get(MARK_USER_EDITED))
            for field in SPEC_FIELDS:
                new_value = incoming[field]
                old_value = current.get(field)
                if user_edited and not force:
                    # 手改过的字段以用户为准，只把空白项补齐
                    if not old_value and new_value:
                        current[field] = new_value
                        changed = True
                elif old_value != new_value:
                    current[field] = new_value
                    changed = True
            if document is not None:
                if current.get(MARK_SOURCE_DOC) != document.id:
                    current[MARK_SOURCE_DOC] = document.id
                if current.get(MARK_SOURCE_VERSION) != document.version:
                    current[MARK_SOURCE_VERSION] = document.version
            if changed:
                character.spec_json = current
                character.version = int(character.version or 1) + 1
                character.status = AssetStatus.DRAFT.value
                updated.append(character)
            else:
                character.spec_json = current
                kept.append(character)
            if character.is_main != is_main:
                character.is_main = is_main
        ordered.append(character)

    session.flush()
    return {
        "created": created,
        "updated": updated,
        "kept": kept,
        "characters": ordered,
    }


def ensure_character_refs(
    session: Session, episode: Episode, shots: list[Shot]
) -> list[dict[str, Any]]:
    """FR-023 引用完整性检查：缺失已确认角色资产时阻断并给出补齐提示。

    镜头上打了 `no_character=true` 标记（用户在界面上确认「本镜无角色」）时
    直接跳过检查——这也是 PRD 要求的合法绕过路径。
    """
    project = session.get(Project, episode.project_id)
    # 本集专属优先：同名角色在别的剧集另有一张卡，不能拿错人
    characters = episode_characters(session, project.id, episode.id)
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
