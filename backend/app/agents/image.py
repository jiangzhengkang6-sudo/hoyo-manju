"""ImageAgent：候选图生成与质量检查（FR-031/032/034）。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.character import consistency_params, project_style
from app.agents.shot import build_image_prompt, characters_for_shot
from app.agents.story import run_coro
from app.core.errors import ProviderError
from app.db.models import AssetStatus, CharacterAsset, Episode, MediaAsset, Project, Shot
from app.providers.base import ImageRequest, ProviderKind
from app.providers.registry import resolve
from app.services import media as media_service
from app.services.capabilities import validate_image_request

#: 漫剧默认竖屏尺寸（FR-091 会拿它与供应商能力声明比对，因此集中定义）
DEFAULT_IMAGE_WIDTH = 1080
DEFAULT_IMAGE_HEIGHT = 1920
DEFAULT_ASPECT_RATIO = "9:16"


def reference_count_for_shot(session: Session, shot: Shot) -> int:
    """该镜实际会带上的参考图数量（有主图的角色才计入）。"""
    from app.agents import character as character_agent

    episode = session.get(Episode, shot.episode_id)
    if episode is None:
        return 0
    characters = character_agent.episode_characters(session, episode.project_id, episode.id)
    shot_chars = characters_for_shot(session, shot, characters)
    return sum(1 for c in shot_chars if c.primary_media_id)


def character_refs(
    session: Session, characters: list[CharacterAsset]
) -> tuple[list[str], list[str]]:
    """把角色的主图拆成「公网 URL」和「本地路径」两种引用。

    本地存储的 `media_url()` 是**相对签名路径**（`/api/v1/storage/signed/...`），
    外部网关访问不到，直接塞给供应商等于没给参考图——角色一致性会完全退化成
    提示词里的文字描述，同一个角色被画成好几个人。所以本地文件单独走
    `reference_paths`，由供应商上传换公网地址。
    """
    uris: list[str] = []
    paths: list[str] = []
    for character in characters:
        if not character.primary_media_id:
            continue
        asset = session.get(MediaAsset, character.primary_media_id)
        if asset is None:
            continue
        url = media_service.media_url(asset)
        if url.startswith(("http://", "https://")):
            uris.append(url)
            continue
        try:
            path = media_service.local_path(asset)
        except Exception:  # noqa: BLE001 - 远端存储时没有本地路径
            path = None
        if path and Path(path).exists():
            paths.append(str(path))
    return uris, paths


def max_reference_count(session: Session, shots: list[Shot]) -> int:
    """一批镜头里最大的参考图数量（FR-091 提交前校验用）。"""
    return max((reference_count_for_shot(session, shot) for shot in shots), default=0)


def estimate(
    session: Session,
    *,
    shots: list[Shot],
    candidates_per_shot: int,
) -> dict[str, Any]:
    """图像生成的成本预估（FR-010 / §12-6 配额拦截前置条件）。"""
    provider = resolve(ProviderKind.IMAGE, session)
    caps = provider.capabilities()
    unit_price = int(getattr(caps, "unit_price_cents", 0) or 0)
    quantity = max(0, len(shots) * candidates_per_shot)
    total = max(0, int(math.ceil(quantity * unit_price))) if unit_price else 0
    from app.services import cost as cost_service

    snap = cost_service.quota_snapshot(session)
    return {
        "provider": provider.id,
        "model": getattr(provider, "model", provider.id),
        "estimate_cents": total,
        "quota_after_cents": snap["remaining_cents"] - total,
        "remaining_cents": snap["remaining_cents"],
        "shot_count": len(shots),
        "candidates_per_shot": candidates_per_shot,
        "image_count": quantity,
        "billing_unit": caps.billing_unit,
        "unit_price_cents": unit_price,
        "estimated_latency_seconds": caps.estimated_latency_seconds,
    }


def generate_for_shots(
    session: Session,
    *,
    episode: Episode,
    shots: list[Shot],
    candidates_per_shot: int,
    progress_cb=None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    project = session.get(Project, episode.project_id)
    style = project_style(session, project)
    from app.agents import character as character_agent

    characters = character_agent.episode_characters(session, project.id, episode.id)
    provider = resolve(ProviderKind.IMAGE, session)
    caps = provider.capabilities()
    # FR-040 / FR-022：画面尺寸与构图提示都跟随项目画幅比例，不再写死竖屏
    from app.agents import video as video_agent

    aspect_ratio = getattr(project, "aspect_ratio", None)
    width, height = video_agent.resolve_resolution(aspect_ratio)
    # FR-091：提交前按供应商能力声明校验；这里再校验一次是兜底，
    # 主要入口（/shots/generate-images 路由）已经提前拦过，用户能看到可读的错误。
    validate_image_request(
        caps,
        width=width,
        height=height,
        count=candidates_per_shot,
        reference_count=max_reference_count(session, shots),
    )

    created: list[str] = []
    failures: list[dict[str, Any]] = []
    # FR-042 / §13.1：实际消耗必须回传，否则 executor 结算时拿不到金额（会按 0 记账）
    cost_cents = 0
    total = max(1, len(shots) * candidates_per_shot)
    done = 0
    cancelled = False

    for shot in shots:
        # 逐镜检查取消信号：用户点「停止」后，已开始的那一张会跑完，
        # 但后面的镜头不再提交（每一步都是真金白银）。
        if should_cancel and should_cancel():
            cancelled = True
            break
        shot_chars = characters_for_shot(session, shot, characters)
        prompt, negative = build_image_prompt(
            shot, style=style, characters=shot_chars, aspect_ratio=aspect_ratio
        )
        # 角色主图必须真的随请求发出去：本地文件交给供应商上传换公网地址，
        # 远端存储才直接给 URL。缺了这一步，模型只能靠文字猜长相。
        ref_uris, ref_paths = character_refs(session, shot_chars)
        request = ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            count=candidates_per_shot,
            reference_media_ids=[c.primary_media_id for c in shot_chars if c.primary_media_id],
            reference_uris=ref_uris,
            reference_paths=ref_paths,
            metadata={
                "shot_no": shot.shot_no,
                "characters": [c.name for c in shot_chars],
                "composition": (shot.shot_json or {}).get("composition", "中景"),
                "camera_movement": (shot.shot_json or {}).get("camera_movement", "固定"),
                "label": f"shot_{shot.shot_no:03d}",
                "consistency": [consistency_params(c) for c in shot_chars],
            },
        )
        result = run_coro(provider.generate(request))
        if not result.ok:
            failures.append(
                {
                    "shot_no": shot.shot_no,
                    "code": result.error_code,
                    "message": result.error_message,
                    "retryable": result.retryable,
                }
            )
            shot.image_status = "FAILED"
            done += candidates_per_shot
            if progress_cb:
                progress_cb(int(done / total * 100))
            continue

        for artifact in result.artifacts:
            asset = media_service.save_artifact(
                session,
                artifact,
                kind="image",
                role="shot_image",
                owner_type="shot",
                owner_id=shot.id,
                episode_id=episode.id,
                project_id=project.id,
                status=AssetStatus.CANDIDATE.value,
                version=shot.current_version,
                metadata={
                    "shot_id": shot.id,
                    "shot_no": shot.shot_no,
                    "provider": provider.id,
                    "model": request.model or provider.id,
                    "prompt": prompt,
                    "negative_prompt": negative,
                    "consistency": request.metadata["consistency"],
                },
            )
            created.append(asset.id)
            done += 1
            if progress_cb:
                progress_cb(int(done / total * 100))
        cost_cents += int(result.cost_cents or 0)
        if result.artifacts:
            shot.image_status = "CANDIDATES_READY"

    session.flush()
    return {
        "media_ids": created,
        "failures": failures,
        "count": len(created),
        "cost_cents": cost_cents,
        "cancelled": cancelled,
    }


def candidates(session: Session, shot: Shot) -> list[dict[str, Any]]:
    assets = media_service.list_media(session, owner_id=shot.id, kind="image", limit=200)
    out = []
    for asset in sorted(assets, key=lambda a: a.created_at):
        data = media_service.to_dict(asset)
        data["rejected_reason"] = asset.metadata_json.get("rejected_reason", "")
        # stale_reason = 文件与记录对不上（早前同名 key 被覆盖）。这类素材不能选、
        # 不能确认、也不能「恢复」——恢复只会把一张别人的图放回候选区。
        data["stale_reason"] = str(asset.metadata_json.get("stale_reason") or "")
        data["broken"] = bool(data["stale_reason"])
        data["selected"] = asset.id == shot.selected_media_id
        data["confirmed"] = asset.id == shot.confirmed_media_id
        data["usable"] = asset.status != AssetStatus.REJECTED.value and not data["broken"]
        out.append(data)
    return out


def select_candidate(session: Session, shot: Shot, media_id: str) -> Shot:
    asset = _require_owned(session, shot, media_id)
    if asset.status == AssetStatus.REJECTED.value:
        from app.core.errors import StateConflict

        raise StateConflict("已驳回的候选不能选中，请先恢复或重新生成")
    shot.selected_media_id = media_id
    session.flush()
    return shot


def reject_candidate(session: Session, shot: Shot, media_id: str, reason: str) -> MediaAsset:
    asset = _require_owned(session, shot, media_id)
    asset.status = AssetStatus.REJECTED.value
    asset.metadata_json = {**asset.metadata_json, "rejected_reason": reason}
    if shot.selected_media_id == media_id:
        shot.selected_media_id = None
    if shot.confirmed_media_id == media_id:
        shot.confirmed_media_id = None
        shot.image_status = "CANDIDATES_READY"
    session.flush()
    return asset


def restore_candidate(session: Session, shot: Shot, media_id: str) -> MediaAsset:
    asset = _require_owned(session, shot, media_id)
    if asset.metadata_json.get("stale_reason"):
        from app.core.errors import StateConflict

        raise StateConflict(
            "这张图片的文件已失效（早前被同名文件覆盖），不能恢复；请重新生成候选图"
        )
    if asset.status == AssetStatus.REJECTED.value:
        asset.status = AssetStatus.CANDIDATE.value
        asset.metadata_json = {k: v for k, v in asset.metadata_json.items() if k != "rejected_reason"}
    session.flush()
    return asset


def _require_owned(session: Session, shot: Shot, media_id: str) -> MediaAsset:
    from app.core.errors import StateConflict

    asset = media_service.get_media(session, media_id)
    if asset.owner_id != shot.id:
        raise StateConflict("该候选图不属于此分镜")
    return asset


def usable_media_ids(session: Session, shot: Shot) -> list[str]:
    return [
        a.id
        for a in media_service.list_media(session, owner_id=shot.id, kind="image", limit=200)
        if a.status != AssetStatus.REJECTED.value
        and not (a.metadata_json or {}).get("stale_reason")
    ]
