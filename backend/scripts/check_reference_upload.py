"""零费用验证：角色主图能否上传换公网 URL（不提交生成任务）。

只调 `POST /v1/uploads/images`，不创建生图任务，因此不产生生成费用。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.providers.net import apply_provider_proxy
from app.services import media as media_service
from app.services.storage import get_storage
from app.core.config import settings


async def main() -> int:
    apply_provider_proxy()
    key = "gemini_1.png"
    # 米娅主图
    asset_key = None
    from app.db.session import session_scope
    from app.db.models import MediaAsset

    with session_scope() as session:
        asset = session.get(MediaAsset, "med_714cd8df8c434a66")
        asset_key = asset.storage_key
        print("素材:", asset.id, asset.filename, asset.storage_key, f"{asset.width}x{asset.height}")

    path = get_storage().local_path(asset_key)
    print("本地路径:", path, "存在:", path.exists() if path else None)
    if not path or not path.exists():
        return 1
    size = path.stat().st_size
    print(f"文件大小: {size / 1024:.0f} KB")

    url = settings.gemini_image_url.rstrip("/")
    base = url.split("/v1/")[0]
    upload = settings.toapis_upload_url or f"{base}/v1/uploads/images"
    print("上传地址:", upload)

    async with httpx.AsyncClient(timeout=120) as client:
        with open(path, "rb") as fh:
            resp = await client.post(
                upload,
                headers={"Authorization": f"Bearer {settings.gemini_image_api_key}"},
                files={"file": (path.name, fh, "image/png")},
                data={"purpose": "generation"},
            )
        print("上传状态:", resp.status_code)
        print("响应:", resp.text[:300])
        if resp.status_code >= 400:
            return 1
        body = resp.json() or {}
        public = str(((body.get("data") or {}) if isinstance(body, dict) else {}).get("url") or "")
        print("公网 URL:", public)
        if not public:
            return 1
        # 验证网关侧能取到（这是生图模型要拉的地址）
        head = await client.get(public)
        print("回读状态:", head.status_code, "字节:", len(head.content), "类型:", head.headers.get("content-type"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
