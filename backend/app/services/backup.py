"""§11.2-2 数据备份：每日备份 + 7 点保留。

PRD 原文：「数据库每日备份，保留最近 7 份」。这里用 SQLite 官方的
`VACUUM INTO` 做**在线一致性快照**——它比直接复制文件安全得多：
`hoyomanju.db-wal` 里可能还有没 checkpoint 的事务，直接 `copy` 会拿到
一个缺数据的库（甚至损坏）。`VACUUM INTO` 由 SQLite 自己在读事务里
把当前一致视图写成一个新的完整库文件。

同时备份 LangGraph 的 checkpoint 库，否则「剧集状态能恢复但工作流不能」，
备份就只成功了一半。
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.db.session import session_scope

#: 备份文件命名：`<名字>.<YYYYmmdd-HHMMSS>.db`
_STAMP = "%Y%m%d-%H%M%S"


def backup_dir() -> Path:
    path = Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sqlite_path_from_url(url: str) -> Path | None:
    """从 SQLAlchemy URL 里取出 sqlite 文件路径；非 sqlite 返回 None。

    兼容两种形态：`sqlite:///C:/path/x.db`（Windows 绝对路径）与
    `sqlite:////abs/x.db`（POSIX 绝对路径）。
    """
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """用 `VACUUM INTO` 做在线一致快照。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    # VACUUM INTO 的目标路径不能带单引号（SQLite 语法），正常备份目录不会有
    conn = sqlite3.connect(str(source))
    try:
        conn.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        conn.close()


def _snapshot_via_sqlalchemy(target: Path) -> bool:
    """兜底：数据库不是本地 sqlite 文件时，用 ORM 连接执行 VACUUM INTO。"""
    try:
        with session_scope() as session:
            session.execute(text(f"VACUUM INTO '{target.as_posix()}'"))
        return True
    except Exception:
        return False


def _unique_stamp(root: Path) -> str:
    """生成不与已有备份冲突的时间戳（同一秒内连做多次备份也不会互相覆盖）。"""
    stamp = dt.datetime.now().strftime(_STAMP)
    if not any(root.glob(f"*.{stamp}.db")):
        return stamp
    for seq in range(1, 100):
        candidate = f"{stamp}-{seq}"
        if not any(root.glob(f"*.{candidate}.db")):
            return candidate
    # 极端情况：退回到微秒
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def run_backup(*, reason: str = "manual", keep: int | None = None) -> dict[str, Any]:
    """执行一次备份并做保留裁剪，返回本次备份的元信息。

    `reason` 只写进返回体与日志，不参与命名——命名保持时间戳，
    便于按时间排序与按份数裁剪。
    """
    keep = int(keep if keep is not None else settings.backup_keep)
    keep = max(1, keep)
    root = backup_dir()
    stamp = _unique_stamp(root)
    created: list[dict[str, Any]] = []
    errors: list[str] = []

    targets: list[tuple[str, Path]] = []
    main = _sqlite_path_from_url(settings.database_url)
    if main is None:
        main = Path(settings.data_dir / "hoyomanju.db")
    targets.append(("main", main))
    checkpoint = Path(settings.checkpoint_db)
    if checkpoint.exists():
        targets.append(("checkpoints", checkpoint))

    for name, source in targets:
        if not source or not Path(source).exists():
            continue
        dest = root / f"{name}.{stamp}.db"
        try:
            _snapshot_sqlite(Path(source), dest)
            if not dest.exists() or dest.stat().st_size == 0:
                # VACUUM INTO 没成功（例如不是本地 sqlite），退回连接层
                if not _snapshot_via_sqlalchemy(dest):
                    errors.append(f"{name}: 备份失败")
                    continue
            created.append(
                {
                    "name": name,
                    "path": str(dest),
                    "size_bytes": dest.stat().st_size,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 备份失败不能拖垮主流程
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    pruned = prune(keep=keep)
    return {
        "reason": reason,
        "stamp": stamp,
        "dir": str(root),
        "created": created,
        "errors": errors,
        "keep": keep,
        "pruned": pruned,
    }


def prune(*, keep: int | None = None) -> list[str]:
    """每个库只保留最近 `keep` 份，返回被删除的文件名。"""
    keep = max(1, int(keep if keep is not None else settings.backup_keep))
    root = backup_dir()
    removed: list[str] = []
    # 按 `<name>.<stamp>.db` 分组，时间戳可直接字符串排序
    groups: dict[str, list[Path]] = {}
    for path in root.glob("*.db"):
        parts = path.name.split(".")
        if len(parts) < 3:
            continue
        groups.setdefault(parts[0], []).append(path)
    for _name, files in groups.items():
        files.sort(key=lambda p: p.name, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
                removed.append(stale.name)
            except OSError:
                pass
    return removed


def list_backups() -> list[dict[str, Any]]:
    """列出全部备份，新的在前。"""
    root = backup_dir()
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.db"), key=lambda p: p.name, reverse=True):
        parts = path.name.split(".")
        stat = path.stat()
        items.append(
            {
                "name": parts[0] if parts else path.stem,
                "filename": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "created_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return items


def restore(source: str, *, force: bool = False) -> dict[str, Any]:
    """从备份恢复主库。

    恢复前会先把**当前**库另存一份 `pre-restore.*.db`，避免「恢复错了就没救」。

    重要：SQLite 文件级恢复要求**没有其他连接在写**。如果服务还在跑，
    直接覆盖会得到索引不一致的库（`PRAGMA integrity_check` 报
    `wrong # of entries in index ...`）。因此这里会先做一次占用探测，
    检测到活跃连接且未显式 `force=True` 时直接拒绝。
    """
    src = Path(source)
    if not src.exists():
        from app.core.errors import NotFound

        raise NotFound(f"备份文件不存在：{source}")
    main = _sqlite_path_from_url(settings.database_url)
    if main is None:
        from app.core.errors import StateConflict

        raise StateConflict("当前数据库不是本地 SQLite 文件，无法直接恢复")

    warnings: list[str] = []
    if not force and _has_active_connections(main):
        from app.core.errors import StateConflict

        raise StateConflict(
            "检测到数据库仍有活跃连接，拒绝在线恢复（会破坏索引一致性）。"
            "请先停止服务，或使用 force=True 明确承担风险",
            details={"target": str(main)},
        )

    safety = backup_dir() / f"pre-restore.{dt.datetime.now().strftime(_STAMP)}.db"
    if main.exists():
        try:
            # 用 VACUUM INTO 做安全副本，而不是 copy2：当前库可能正被持有
            _snapshot_sqlite(main, safety)
        except Exception:
            shutil.copy2(main, safety)

    # 恢复时清掉 WAL/SHM，否则旧 WAL 会把恢复后的库再覆盖回去。
    # Windows 上文件可能仍被占用，删不掉不是致命错误——
    # 但要**明确告诉调用方必须先停服务**，否则恢复结果不可信。
    locked: list[str] = []
    for suffix in ("-wal", "-shm"):
        side = Path(str(main) + suffix)
        if not side.exists():
            continue
        try:
            side.unlink()
        except OSError:
            locked.append(side.name)
    if locked:
        warnings.append(
            "以下文件被占用，未能清理："
            + "、".join(locked)
            + "；请先停止服务再执行恢复，否则恢复结果可能被旧 WAL 覆盖"
        )
    shutil.copy2(src, main)

    verify = "unknown"
    try:
        conn = sqlite3.connect(str(main))
        try:
            verify = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        verify = f"error: {exc}"
    if verify != "ok":
        warnings.append(
            f"恢复后的库完整性检查未通过（{verify}）；"
            "通常是恢复时有活跃连接，请停服务后重试"
        )

    return {
        "restored_from": str(src),
        "safety_copy": str(safety),
        "target": str(main),
        "integrity_check": verify,
        "warnings": warnings,
        "note": "已清理 WAL/SHM；请重启服务以重新建立连接池",
    }


def _has_active_connections(main: Path) -> bool:
    """探测主库是否还被别的连接持有。

    仅靠 `BEGIN EXCLUSIVE` 不够：WAL 模式下空闲连接池不会挡住排他锁，
    但 `-wal` 文件**存在**就说明有连接以 WAL 模式打开过这个库，
    此时直接覆盖主库文件必然造成索引与数据不一致。所以两个信号都要看。
    """
    if not main.exists():
        return False
    # 信号一：WAL/SHM 还在，说明有活跃（或未干净关闭）的 WAL 连接
    if Path(str(main) + "-wal").exists() or Path(str(main) + "-shm").exists():
        return True
    # 信号二：拿不到排他锁，说明确实有人在读写
    try:
        conn = sqlite3.connect(str(main), timeout=0.5, isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
            return False
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True
    except Exception:  # noqa: BLE001
        # 探测本身出错时不武断拒绝，交给调用方判断
        return False
