"""§11.2-2 备份：每日备份 + 7 点保留 + 可恢复。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import backup


@pytest.fixture(autouse=True)
def _isolated_backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把备份目录指向临时目录，避免污染真实 data/backups。"""
    target = tmp_path / "backups"
    monkeypatch.setattr(settings, "backup_dir", target)
    target.mkdir(parents=True, exist_ok=True)
    yield target


@pytest.fixture(autouse=True)
def _ensure_db_file(client) -> None:
    """备份测的是「磁盘上的库文件」，所以先要有一个真实落盘的库。

    复用 `client` 夹具：它会 `init_db()` 建表并真正写盘；
    否则 `DATABASE_URL` 指向的 `test.db` 还不存在，备份自然一份都做不出来。
    """
    return None


def test_backup_creates_consistent_sqlite_snapshot() -> None:
    """关键：备份必须是**可打开、完整**的库，而不是 WAL 尚未落盘的半成品。"""
    result = backup.run_backup(reason="test", keep=7)
    assert result["errors"] == []

    mains = [c for c in result["created"] if c["name"] == "main"]
    assert mains, result
    path = Path(mains[0]["path"])
    assert path.exists() and path.stat().st_size > 0

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        assert tables > 5, f"备份里只有 {tables} 张表，疑似空库"
    finally:
        conn.close()


def test_backup_includes_checkpoint_db() -> None:
    """只备份主库不够：工作流 checkpoint 丢了，剧集会卡在中间态。"""
    result = backup.run_backup(reason="test", keep=7)
    names = {c["name"] for c in result["created"]}
    assert "main" in names
    if Path(settings.checkpoint_db).exists():
        assert "checkpoints" in names, result


def test_retention_keeps_only_n_per_database() -> None:
    """§11.2-2：保留最近 N 份（默认 7），旧的按时间淘汰。"""
    for i in range(9):
        backup.run_backup(reason=f"loop{i}", keep=7)

    mains = [b for b in backup.list_backups() if b["name"] == "main"]
    assert len(mains) == 7, [b["filename"] for b in mains]

    # 留下的必须是**最新**的 7 份（文件名时间戳降序，列表已新的在前）
    stamps = [b["filename"] for b in mains]
    assert stamps == sorted(stamps, reverse=True)


def test_prune_is_per_database_not_global() -> None:
    """裁剪要按库分组：不能因为主库备了 7 次就把 checkpoint 全删了。"""
    for _ in range(9):
        backup.run_backup(reason="loop", keep=7)

    names = {b["name"] for b in backup.list_backups()}
    assert "main" in names
    if Path(settings.checkpoint_db).exists():
        assert "checkpoints" in names


def test_restore_keeps_a_safety_copy(tmp_path: Path) -> None:
    """恢复前必须先给当前库留一份，否则「恢复错了」就再也回不去。"""
    from app.db import session as db_session

    taken = backup.run_backup(reason="before-restore", keep=7)
    source = Path([c for c in taken["created"] if c["name"] == "main"][0]["path"])
    assert source.exists()

    main = backup._sqlite_path_from_url(settings.database_url)
    assert main is not None

    # 关键：文件级恢复要求没有任何连接持有该库，否则覆盖后会得到
    # 「database disk image is malformed」的坏库，并且**污染同一进程里后续所有用例**。
    # 先把连接池完全释放，再执行 force 恢复（正常运维路径应先停服务，
    # 下面的用例覆盖了「有活跃连接时拒绝」的行为）。
    db_session.engine.dispose()

    result = backup.restore(str(source), force=True)
    assert Path(result["safety_copy"]).exists()
    assert Path(result["target"]).exists()
    assert Path(result["safety_copy"]).stat().st_size > 0

    # 恢复后的库必须是完好的，不能把坏库留给后面的用例
    conn = sqlite3.connect(str(main))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_restore_refuses_while_service_is_running(client) -> None:
    """在线恢复会破坏索引一致性，因此必须默认拒绝。"""
    from app.core.errors import StateConflict

    taken = backup.run_backup(reason="t", keep=7)
    source = Path([c for c in taken["created"] if c["name"] == "main"][0]["path"])

    with pytest.raises(StateConflict):
        backup.restore(str(source))

    # 备份本身仍然是可以正常打开的一致快照
    conn = sqlite3.connect(str(source))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_restore_rejects_missing_file() -> None:
    from app.core.errors import NotFound

    with pytest.raises(NotFound):
        backup.restore(str(Path(settings.backup_dir) / "nope.db"))


def test_list_backups_newest_first() -> None:
    backup.run_backup(reason="a", keep=7)
    backup.run_backup(reason="b", keep=7)
    items = backup.list_backups()
    assert items, "至少要列出刚做的备份"
    filenames = [b["filename"] for b in items]
    assert filenames == sorted(filenames, reverse=True)
    for item in items:
        assert item["size_bytes"] > 0
        assert item["created_at"]
