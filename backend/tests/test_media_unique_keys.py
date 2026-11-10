"""媒体素材「一次写入、永不覆盖」：防止角色立绘被别的角色顶替。

线上事故：供应商产物文件名是「每批重新计数」的（gemini_1.png），
如果直接拿它当存储键，同一天同一目录下的第二次生成会**覆盖**第一次的文件，
而数据库里仍留着旧记录指向该路径——用户看到的正是「主角立绘变成了勇者」。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.errors import StorageConflict
from app.services import media as media_service
from app.services.storage import LocalStorage, build_key


def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 96)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_storage_refuses_to_overwrite(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    storage.put("a/b.txt", b"one")
    with pytest.raises(StorageConflict):
        storage.put("a/b.txt", b"two")
    assert storage.get("a/b.txt") == b"one"


def test_same_filename_gets_distinct_keys(client: TestClient, project: dict) -> None:
    from app.db.session import session_scope

    first = _png((200, 30, 40))
    second = _png((20, 120, 200))
    with session_scope() as session:
        a = media_service.save_media(
            session, content=first, kind="image", role="character_front",
            filename="gemini_1.png", owner_type="character", owner_id="chr_a",
            project_id=project["id"],
        )
        b = media_service.save_media(
            session, content=second, kind="image", role="character_front",
            filename="gemini_1.png", owner_type="character", owner_id="chr_b",
            project_id=project["id"],
        )
        session.flush()
        assert a.storage_key != b.storage_key
        # 两份内容都还在，谁也没顶替谁
        assert media_service.read_media(a) == first
        assert media_service.read_media(b) == second


def test_save_media_retries_when_key_already_taken(
    client: TestClient, project: dict, monkeypatch
) -> None:
    """极端情况：随机后缀撞上已有文件 → 换名字重试，而不是覆盖。"""
    from app.db.session import session_scope
    from app.services import storage as storage_module

    storage = storage_module.get_storage()
    taken = build_key(project["id"], "misc", "image", filename="gemini_1_deadbeef.png")
    storage.put(taken, b"occupied")

    tokens = iter(["deadbeef", "cafebabe"])
    monkeypatch.setattr(media_service.secrets, "token_hex", lambda n: next(tokens))

    content = _png((10, 200, 10))
    with session_scope() as session:
        asset = media_service.save_media(
            session, content=content, kind="image",
            filename="gemini_1.png", owner_type="character", owner_id="chr_c",
            project_id=project["id"],
        )
        session.flush()
        assert asset.storage_key.endswith("gemini_1_cafebabe.png")
        assert media_service.read_media(asset) == content
        assert storage.get(taken) == b"occupied"      # 原文件没被动过
