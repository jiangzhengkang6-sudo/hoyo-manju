"""角色主图必须真的作为参考图发给生图模型。

之前的实现只在 `metadata` 里记了「本镜出场角色」，`reference_uris` 传的是
本地签名相对路径（`/api/v1/storage/signed/...`），外部网关访问不到；
`ToapisImageProvider.generate()` 更是完全没拼这个字段。结果是：角色一致性
只剩提示词里的文字描述，模型自由发挥——同一个角色在不同镜头里被画成
不同的人（发色、身高、服装都对不上）。

修复分两段：
1. agent 侧把主图拆成「公网 URL」与「本地文件路径」两种引用；
2. provider 侧提交前把本地文件上传换公网 URL，写进 `reference_images`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents import image as image_agent
from app.db.models import AssetStatus, CharacterAsset, MediaAsset
from app.db.session import session_scope
from app.providers.base import ImageRequest, ProviderResult
from app.providers.custom import _REFERENCE_URL_CACHE, ToapisImageProvider
from app.services.storage import get_storage


def _character_with_primary(session, project: dict, episode: dict, media_id: str | None) -> CharacterAsset:
    character = CharacterAsset(
        project_id=project["id"],
        episode_id=episode["id"],
        name="米娅",
        primary_media_id=media_id,
    )
    session.add(character)
    session.flush()
    return character


def _write_png(name: str = "miya.png") -> Path:
    """把图片写进真实的存储根目录（`local_path()` 是 root/key，不看 uri 字段）。"""
    root = get_storage().root  # type: ignore[attr-defined]
    path = Path(root) / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return path


def _local_asset(session, project: dict, episode: dict, path: Path) -> MediaAsset:
    """造一条指向真实本地文件的素材（存储层只认文件在不在）。"""
    asset = MediaAsset(
        project_id=project["id"],
        episode_id=episode["id"],
        owner_type="character",
        owner_id="chr_test",
        kind="image",
        role="character_front",
        uri=str(path),
        storage_key=path.name,
        filename=path.name,
        mime_type="image/png",
        size_bytes=path.stat().st_size,
        width=512,
        height=1024,
        status=AssetStatus.CONFIRMED.value,
        metadata_json={},
    )
    session.add(asset)
    session.flush()
    return asset


def test_character_refs_returns_local_path_for_local_storage(
    client, project: dict, episode: dict, tmp_path: Path
) -> None:
    """本地存储：签名路径外部访问不到，必须回落到本地文件路径。"""
    png = _write_png()

    with session_scope() as session:
        asset = _local_asset(session, project, episode, png)
        character = _character_with_primary(session, project, episode, asset.id)
        uris, paths = image_agent.character_refs(session, [character])

    assert uris == [], "相对签名路径不能当公网 URL 发出去"
    assert paths == [str(png)]


def test_character_refs_skips_characters_without_primary(
    client, project: dict, episode: dict
) -> None:
    with session_scope() as session:
        character = _character_with_primary(session, project, episode, None)
        uris, paths = image_agent.character_refs(session, [character])

    assert (uris, paths) == ([], [])


def test_provider_uploads_local_reference_and_sets_field(
    client, project: dict, episode: dict, tmp_path: Path, monkeypatch
) -> None:
    """提交前把本地参考图换成公网 URL，并写进 `reference_images`。"""
    png = _write_png("miya_fail.png")
    _REFERENCE_URL_CACHE.clear()  # 同一路径会被前一个用例缓存，否则这里走不到上传

    provider = ToapisImageProvider(model="gemini-nano-banana-2.1", api_key="k")
    captured: dict[str, object] = {}

    async def fake_upload(client_, path, label="首帧"):  # noqa: ANN001
        captured["uploaded"] = (str(path), label)
        return "https://cdn.example.com/miya.png", None

    async def fake_run_task(payload, *, want=1, prepare=None):  # noqa: ANN001
        if prepare is not None:
            failure = await prepare(None, payload)  # type: ignore[arg-type]
            assert failure is None
        captured["payload"] = payload
        return ProviderResult(
            ok=True,
            kind=provider.kind,
            artifacts=[{"content": b"", "mime_type": "image/png"}],
            cost_cents=25,
        )

    monkeypatch.setattr(provider, "_upload_image", fake_upload)
    monkeypatch.setattr(provider, "_run_task", fake_run_task)

    request = ImageRequest(
        prompt="米娅跪在王座前",
        width=2752,
        height=1536,
        count=1,
        reference_paths=[str(png)],
    )
    result = __import__("asyncio").run(provider.generate(request))

    assert result.ok
    assert captured["uploaded"] == (str(png), "角色参考图")
    payload = captured["payload"]
    assert payload["reference_images"] == ["https://cdn.example.com/miya.png"]
    assert payload["size"] == "16:9"


def test_provider_reports_upload_failure_instead_of_dropping_reference(
    client, project: dict, episode: dict, tmp_path: Path, monkeypatch
) -> None:
    """上传失败必须整体失败——静默丢掉参考图等于让角色换脸。"""
    png = _write_png("miya_fail.png")
    _REFERENCE_URL_CACHE.clear()  # 同一路径会被前一个用例缓存，否则这里走不到上传

    provider = ToapisImageProvider(model="gemini-nano-banana-2.1", api_key="k")

    async def fake_upload(client_, path, label="首帧"):  # noqa: ANN001
        return "", ProviderResult.failure(
            provider.kind, "UNAVAILABLE", "上传网关 502", retryable=True
        )

    async def fake_run_task(payload, *, want=1, prepare=None):  # noqa: ANN001
        if prepare is not None:
            failure = await prepare(None, payload)  # type: ignore[arg-type]
            assert failure is not None
            return failure
        raise AssertionError("没有参考图时不该走到这里")

    monkeypatch.setattr(provider, "_upload_image", fake_upload)
    monkeypatch.setattr(provider, "_run_task", fake_run_task)

    result = __import__("asyncio").run(
        provider.generate(ImageRequest(prompt="x", reference_paths=[str(png)]))
    )

    assert result.ok is False
    assert "502" in (result.error_message or "") or "上传" in (result.error_message or "")
