"""媒体素材完整性体检：数据库记录 vs 存储里的真实文件。

用法（backend 目录）：
    python scripts/check_media_integrity.py                # 只读，列出异常
    python scripts/check_media_integrity.py --mark-stale    # 把不一致的记录标记为 REJECTED

为什么需要它：素材是「一次写入、永不覆盖」的，但只要历史上出现过同名 key
（例如供应商产物文件名 gemini_1.png 被复用），后写的文件就会顶替先写的，
数据库里的 sha256/size 与实际文件对不上——表现就是「角色立绘变成了别人」。
这条检查不花钱，能第一时间发现这类静默损坏。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.models import AssetStatus, MediaAsset  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.services.storage import get_storage  # noqa: E402


def inspect(session) -> list[dict]:
    storage = get_storage()
    rows: list[dict] = []
    for asset in session.scalars(select(MediaAsset).order_by(MediaAsset.created_at)):
        item = {
            "id": asset.id,
            "kind": asset.kind,
            "role": asset.role,
            "owner": f"{asset.owner_type}:{asset.owner_id}",
            "status": asset.status,
            "key": asset.storage_key,
            "reason": "",
        }
        # 已标记失效的记录不再报警：它们是被覆盖事件的历史证据，文件本来就不属于它们
        if asset.status == AssetStatus.REJECTED.value:
            item["reason"] = "skipped"
            rows.append(item)
            continue
        try:
            content = storage.get(asset.storage_key)
        except Exception as exc:  # noqa: BLE001
            item["reason"] = f"文件缺失/不可读（{type(exc).__name__}）"
            rows.append(item)
            continue
        if asset.size_bytes and len(content) != asset.size_bytes:
            item["reason"] = f"体积不符：库里 {asset.size_bytes}B，实际 {len(content)}B"
        elif asset.sha256 and hashlib.sha256(content).hexdigest() != asset.sha256:
            item["reason"] = "内容哈希不一致（文件被覆盖过）"
        rows.append(item)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mark-stale", action="store_true",
                        help="把不一致的记录标记为 REJECTED（不删除文件）")
    args = parser.parse_args()

    with session_scope() as session:
        rows = inspect(session)
        bad = [row for row in rows if row["reason"] and row["reason"] != "skipped"]
        skipped = len([row for row in rows if row["reason"] == "skipped"])
        print(f"素材总数 {len(rows)}，异常 {len(bad)}，已失效 {skipped}")
        for row in bad:
            print(f"  ! {row['id']}  {row['kind']}/{row['role']}  {row['owner']}  {row['status']}")
            print(f"      {row['key']}")
            print(f"      原因：{row['reason']}")
        if bad and args.mark_stale:
            for row in bad:
                asset = session.get(MediaAsset, row["id"])
                if asset is None:
                    continue
                asset.status = AssetStatus.REJECTED.value
                meta = dict(asset.metadata_json or {})
                meta["stale_reason"] = row["reason"]
                # 前端候选图卡片读的是 rejected_reason，两个都写，用户才知道为什么不能用
                meta["rejected_reason"] = f"素材文件异常：{row['reason']}"
                asset.metadata_json = meta
            print(f"已把 {len(bad)} 条标记为 REJECTED（文件保留，便于取证）")
    return 1 if bad and not args.mark_stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
