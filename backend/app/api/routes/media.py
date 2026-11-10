"""媒体资产、上传、签名 URL 与权利声明。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import SessionDep, get_episode
from app.core.config import settings
from app.core.errors import NotFound, StateConflict, StorageError
from app.db.models import AssetStatus, MediaAsset, Project
from app.schemas import MediaRightsIn
from app.services import audit as audit_service
from app.services import media as media_service
from app.services import uploads
from app.services.storage import get_storage

router = APIRouter(tags=["media"])

KIND_BY_MIME_PREFIX = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
    "text/": "text",
    "application/pdf": "text",
}

#: 兼容旧引用；实际上限来自 settings.upload_max_bytes（§11.3 可配置）
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _disposition(filename: str, *, inline: bool = True) -> str:
    """HTTP 头只能是 latin-1，中文文件名必须走 RFC 5987 的 filename* 形式。"""
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "download"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _range_response(
    request: Request,
    content: bytes,
    media_type: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    """带 HTTP Range 的媒体响应。

    浏览器 `<video>` 依赖 206：没有 `Accept-Ranges` 时，Chrome 拖动进度条会失败，
    Safari 更是直接黑屏不播。整段返回（200）只在客户端不要 Range 时才走。
    """
    base = {"Accept-Ranges": "bytes", **(headers or {})}
    total = len(content)
    raw = request.headers.get("range") or ""
    if not raw.lower().startswith("bytes="):
        return Response(content, media_type=media_type, headers={**base, "Content-Length": str(total)})

    spec = raw[6:].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":                       # bytes=-N：最后 N 字节
            length = int(end_s)
            start, end = max(0, total - length), total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        start, end = -1, -1
    if start < 0 or start >= total or end < start:
        return Response(
            status_code=416,
            headers={**base, "Content-Range": f"bytes */{total}"},
        )
    end = min(end, total - 1)
    chunk = content[start : end + 1]
    return Response(
        chunk,
        status_code=206,
        media_type=media_type,
        headers={
            **base,
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(len(chunk)),
        },
    )


@router.post("/media/upload", status_code=201)
async def upload_media(
    session: SessionDep,
    file: UploadFile = File(...),
    kind: str = Form("image"),
    episode_id: str | None = Form(None),
    project_id: str | None = Form(None),
    rights_declared: bool = Form(False),
    source_note: str = Form(""),
) -> dict:
    content = await file.read()
    if not content:
        raise StateConflict("上传文件为空")
    mime = file.content_type or "application/octet-stream"
    if kind == "auto":
        kind = next(
            (v for k, v in KIND_BY_MIME_PREFIX.items() if mime.startswith(k)), "image"
        )
    # §11.3：单文件大小 / 格式白名单 / 工作区总容量 三道闸门（落盘前判断）
    uploads.check_upload(session, size_bytes=len(content), mime_type=mime, kind=kind)
    # FR-082：用户上传的**任何**素材都必须声明使用权。此前只强制 audio/video，
    # 图片与文本可以无声明入库，导出时会成为版权风险。
    if not rights_declared:
        raise StateConflict(
            "上传素材时必须声明使用权（FR-082）：请确认你拥有该素材的使用授权",
            details={"kind": kind, "field": "rights_declared"},
        )
    asset = media_service.save_media(
        session,
        content=content,
        kind=kind,
        role="upload",
        filename=file.filename or "upload.bin",
        mime_type=mime,
        owner_type="episode" if episode_id else "project",
        owner_id=episode_id or project_id or "",
        episode_id=episode_id,
        project_id=project_id,
        status=AssetStatus.CANDIDATE.value,
        source="upload",
        rights_declared=rights_declared,
        rights_note=source_note,
        metadata={"original_filename": file.filename, "rights_note": source_note},
    )
    session.flush()
    return media_service.to_dict(asset)


@router.get("/media/{media_id}")
def read_media(media_id: str, session: SessionDep) -> dict:
    return media_service.to_dict(media_service.get_media(session, media_id))


@router.get("/media/{media_id}/url")
def media_signed_url(media_id: str, session: SessionDep, ttl_seconds: int | None = None) -> dict:
    asset = media_service.get_media(session, media_id)
    # §11.1-2：服务端强制上限，回传的是**实际生效**的 TTL（可能已被夹取）
    ttl = media_service.clamp_ttl(ttl_seconds)
    # §11.1-3：签发下载地址即视为一次下载请求，必须留痕
    audit_service.record(
        session,
        action=audit_service.ACTION_DOWNLOAD,
        target_type=audit_service.TARGET_MEDIA,
        target_id=asset.id,
        project_id=asset.project_id,
        episode_id=asset.episode_id,
        detail={"kind": asset.kind, "via": "signed_url", "ttl_seconds": ttl},
    )
    session.flush()
    return {
        "media_id": asset.id,
        "url": media_service.media_url(asset, ttl),
        "ttl_seconds": ttl,
        "expires_in": ttl,
        "requested_ttl_seconds": ttl_seconds,
        "clamped": ttl_seconds is not None and ttl != ttl_seconds,
        "max_ttl_seconds": settings.signed_url_max_ttl_seconds,
    }


@router.get("/media/{media_id}/content")
def media_content(media_id: str, request: Request, session: SessionDep) -> Response:
    asset = media_service.get_media(session, media_id)
    try:
        content = media_service.read_media(asset)
    except StorageError as exc:
        raise NotFound(str(exc)) from exc
    # §11.1-3：媒体内容实际回传即一次下载，留痕后再返回字节
    audit_service.record(
        session,
        action=audit_service.ACTION_DOWNLOAD,
        target_type=audit_service.TARGET_MEDIA,
        target_id=asset.id,
        project_id=asset.project_id,
        episode_id=asset.episode_id,
        detail={"kind": asset.kind, "via": "content", "bytes": len(content)},
    )
    session.flush()
    return _range_response(
        request,
        content,
        asset.mime_type,
        headers={
            "Content-Disposition": _disposition(asset.filename),
            "X-AIGC-Label": "1",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/media/{media_id}/rights")
def read_rights(media_id: str, session: SessionDep) -> dict:
    asset = media_service.get_media(session, media_id)
    return {
        "media_id": asset.id,
        "source": asset.source,
        "rights_declared": asset.rights_declared,
        "rights_note": asset.rights_note,
        "aigc_labeled": asset.aigc_labeled,
        "aigc": {"label": "本内容由 AI 生成", "can_disable": False},
    }


@router.put("/media/{media_id}/rights")
def update_rights(media_id: str, payload: MediaRightsIn, session: SessionDep) -> dict:
    asset = media_service.get_media(session, media_id)
    asset.rights_declared = payload.rights_declared
    asset.rights_note = payload.source_note
    session.flush()
    return {
        "media_id": asset.id,
        "rights_declared": asset.rights_declared,
        "rights_note": asset.rights_note,
    }


@router.get("/episodes/{episode_id}/media")
def episode_media(
    episode_id: str,
    session: SessionDep,
    kind: str | None = None,
    role: str | None = None,
    limit: int = Query(200, le=500),
) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    items = media_service.list_media(
        session, episode_id=episode.id, kind=kind, role=role, limit=limit,
        include_rejected=False,
    )
    project = session.get(Project, episode.project_id)
    character_assets = media_service.list_media(
        session, project_id=project.id if project else None, kind="image", limit=limit,
        include_rejected=False,
    )
    combined = {a.id: a for a in items}
    for asset in character_assets:
        if asset.owner_type == "character":
            combined.setdefault(asset.id, asset)
    return {
        "items": [media_service.to_dict(a) for a in combined.values()],
        "counts": media_service.count_by_kind(session, episode.id),
    }


@router.get("/storage/signed/{token}")
def storage_signed(token: str, request: Request) -> Response:
    """本地存储的签名 URL 入口。"""
    storage = get_storage()
    key = storage.verify_signed(token)
    if not key:
        raise NotFound("签名已失效或非法，请重新获取素材地址")
    try:
        content = storage.get(key)
    except StorageError as exc:
        raise NotFound(str(exc)) from exc
    import mimetypes

    mime = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return _range_response(
        request, content, mime, headers={"Cache-Control": "private, max-age=600"}
    )
