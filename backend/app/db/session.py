"""数据库引擎与会话管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.core.config import settings
from app.db.models import Base

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False, "timeout": 30}

engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 平台相关
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


def init_db() -> None:
    """创建全部表（MVP 使用 create_all；生产迁移走 Alembic）。"""
    Base.metadata.create_all(engine)


def get_session(request: Request) -> Session:
    """FastAPI 依赖：每个请求一个 Session，挂在 ``request.state`` 上。

    提交/回滚由 :class:`app.main.RequestSessionMiddleware` 在响应真正发出之前统一完成，
    而不是用 ``yield`` 依赖的清理钩子——后者在响应发出之后才执行，
    客户端可能在事务提交前就发出下一个请求（例如「创建项目后立刻建剧集」会读到旧数据而 404）。
    """
    state = request.scope.setdefault("state", {})
    session = state.get("db_session")
    if session is None:
        session = SessionLocal()
        state["db_session"] = session
    return session


def finalize_session(scope: dict, *, ok: bool) -> None:
    """提交（或回滚）并关闭该请求的 Session；重复调用是安全的。"""
    state = scope.get("state") or {}
    session = state.pop("db_session", None)
    if session is None:
        return
    try:
        if ok:
            session.commit()
        else:
            session.rollback()
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / worker 中使用。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
