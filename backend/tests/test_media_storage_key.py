"""同名产物不能互相覆盖。

线上事故：同一镜连续生成三次，供应商每次都返回 `gemini_1.png / gemini_2.png`，
存储 key 只按「项目/剧集/日期/文件名」拼 → 后一批把前一批覆盖掉。
表现是「以前生成的图不见了」，而库里还留着 6 条记录指向同样两个文件，
用户既看到一堆重复图，又白花了积分。
"""

from __future__ import annotations

from app.db.session import session_scope
from app.services import media as media_service


def test_same_filename_gets_unique_storage_key(client, project: dict, episode: dict) -> None:
    with session_scope() as session:
        first = media_service.save_media(
            session,
            content=b"first-batch",
            kind="image",
            role="shot_image",
            filename="gemini_1.png",
            owner_type="shot",
            owner_id="sht_demo",
            episode_id=episode["id"],
            project_id=project["id"],
        )
        second = media_service.save_media(
            session,
            content=b"second-batch",
            kind="image",
            role="shot_image",
            filename="gemini_1.png",
            owner_type="shot",
            owner_id="sht_demo",
            episode_id=episode["id"],
            project_id=project["id"],
        )
        assert first.storage_key != second.storage_key, "同名产物覆盖了上一批文件"
        assert first.id != second.id
        # 展示名保留原始文件名，便于用户识别
        assert first.filename == "gemini_1.png"
        # 两份内容都还在
        assert media_service.read_media(first) == b"first-batch"
        assert media_service.read_media(second) == b"second-batch"
