"""§11.2-2 备份运维入口。

用法：
    python scripts/backup_db.py                # 立刻备份一次（保留 7 份）
    python scripts/backup_db.py --list         # 列出已有备份
    python scripts/backup_db.py --keep 14      # 自定义保留份数
    python scripts/backup_db.py --restore <文件>  # 从备份恢复（需先停服务）

建议配成每日计划任务：
    python scripts/backup_db.py >> data/backups/backup.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="数据库备份 / 恢复（PRD §11.2-2）")
    parser.add_argument("--keep", type=int, default=None, help="保留份数，默认取 BACKUP_KEEP（7）")
    parser.add_argument("--list", action="store_true", help="只列出已有备份")
    parser.add_argument("--restore", metavar="FILE", help="从指定备份恢复（需先停止服务）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="恢复时跳过活跃连接检查（危险，仅在确认已停服时使用）",
    )
    args = parser.parse_args()

    if args.list:
        items = backup.list_backups()
        if not items:
            print("暂无备份")
            return 0
        for item in items:
            size_mb = item["size_bytes"] / 1024 / 1024
            print(f"{item['filename']:<44} {size_mb:>8.2f} MB  {item['created_at']}")
        return 0

    if args.restore:
        try:
            result = backup.restore(args.restore, force=args.force)
        except Exception as exc:  # noqa: BLE001
            print(f"恢复失败：{exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = backup.run_backup(reason="cli", keep=args.keep)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
