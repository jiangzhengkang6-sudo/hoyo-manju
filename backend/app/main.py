"""FastAPI 应用入口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import (
    admin,
    exports,
    generation,
    health,
    jobs,
    media,
    projects,
    story,
    workflow,
    workspace,
)
from app.core.config import LOCAL_USER_ID, LOCAL_WORKSPACE_ID, settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.db.session import finalize_session, init_db
from app.services import executor  # noqa: F401 - 导入以注册 job handler
from app.worker.runner import EmbeddedWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("hoyomanju")

# 供应商出网代理策略：`PROVIDER_PROXY_URL` 为空时保持跟随环境变量（默认），
# 设为 direct 可摆脱 IDE/沙箱注入的本地代理（它们常常把请求变成 502）。
from app.providers.net import apply_provider_proxy  # noqa: E402

_proxy_mode = apply_provider_proxy()
if _proxy_mode != "env":
    logger.info("供应商出网代理策略：%s", _proxy_mode)

API_PREFIX = "/api/v1"

embedded_worker: EmbeddedWorker | None = None
backup_task: "asyncio.Task[None] | None" = None


class RequestSessionMiddleware:
    """在响应真正发出之前提交/回滚请求级数据库会话。

    为什么需要它：FastAPI 的 ``yield`` 依赖清理发生在响应发出**之后**，
    于是客户端可能在事务提交前就发出下一个请求。实测「POST /projects 返回 201
    后立刻 POST /projects/{id}/episodes」有约 6 成概率读到旧数据而 404。
    这里改为拦截 ``http.response.start``：此刻路由处理函数已经执行完毕，
    但字节还没发给客户端，提交时机是正确的。
    """

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        # 每个请求一个追踪号：错误体里的 `trace_id` 与服务端日志可用它对上
        from app.core.errors import new_trace_id, set_trace_id

        trace_id = new_trace_id()
        set_trace_id(trace_id)
        scope.setdefault("state", {})["trace_id"] = trace_id

        finished = False

        async def send_wrapper(message: dict) -> None:
            nonlocal finished
            if message["type"] == "http.response.start" and not finished:
                finished = True
                finalize_session(scope, ok=int(message.get("status", 500)) < 400)
                # 让客户端也能从响应头拿到追踪号，便于报障时直接提供
                headers = list(message.get("headers") or [])
                headers.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = headers
            await send(message)  # type: ignore[misc]

        try:
            await self.app(scope, receive, send_wrapper)  # type: ignore[misc]
        except Exception:
            if not finished:
                finished = True
                finalize_session(scope, ok=False)
            raise
        finally:
            # 兜底：响应从未发出（连接中断等）时也要释放会话
            finalize_session(scope, ok=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedded_worker, backup_task
    init_db()
    _seed()
    if settings.env != "test" and settings.enable_embedded_worker:
        embedded_worker = EmbeddedWorker()
        embedded_worker.start()
        logger.info("内嵌 worker 已启动（并发 %s）", settings.worker_concurrency)
    elif not settings.enable_embedded_worker:
        logger.info("内嵌 worker 已关闭，请确保有独立 worker 进程在运行")
    # §11.2-2：每日备份 + 7 点保留
    backup_task = asyncio.create_task(_backup_loop())
    yield
    if backup_task is not None:
        backup_task.cancel()
        try:
            await backup_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        backup_task = None
    if embedded_worker is not None:
        await embedded_worker.stop()
        embedded_worker = None


async def _backup_loop() -> None:
    """§11.2-2：启动时先备一次，之后按 `BACKUP_INTERVAL_HOURS` 周期执行。"""
    if not settings.backup_enabled:
        logger.info("备份已关闭（BACKUP_ENABLED=0）")
        return
    from app.services import backup as backup_service

    while True:
        try:
            result = await asyncio.to_thread(backup_service.run_backup, reason="scheduled")
            if result.get("errors"):
                logger.warning("备份完成但有错误：%s", result["errors"])
            else:
                logger.info(
                    "备份完成：%s 份，保留 %s 份，清理 %s 份",
                    len(result["created"]),
                    result["keep"],
                    len(result["pruned"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 备份失败不能拖垮服务
            logger.exception("备份失败，将在下个周期重试")
        hours = max(1, int(settings.backup_interval_hours or 24))
        await asyncio.sleep(hours * 3600)


def _seed() -> None:
    from app.db.models import PriceBook, User
    from app.db.session import session_scope

    with session_scope() as session:
        if session.get(User, LOCAL_USER_ID) is None:
            session.add(
                User(
                    id=LOCAL_USER_ID,
                    name="本地创作者",
                    workspace_id=LOCAL_WORKSPACE_ID,
                    quota_limit_cents=settings.quota_limit_cents,
                )
            )
        from app.providers.http_providers import PRICE_TABLE

        if not session.query(PriceBook).first():
            for key, table in PRICE_TABLE.items():
                provider, _, model = key.partition(":")
                for unit, price in table.items():
                    session.add(
                        PriceBook(
                            provider=provider,
                            kind="builtin",
                            model=model,
                            billing_unit=unit,
                            unit_price_cents=price,
                            note="内置价格表（仅估算用）",
                        )
                    )


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="面向个人创作者的 AI 漫剧端到端制作工作台（MVP）",
    lifespan=lifespan,
)

# 会话提交中间件（最先添加 → 最靠近路由，保证在响应字节发出前完成提交）
app.add_middleware(RequestSessionMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Last-Event-ID", "x-trace-id"],
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError) -> JSONResponse:  # noqa: ANN001
    from app.core.errors import ErrorCode, ensure_trace_id

    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": ErrorCode.VALIDATION_ERROR,
                "message": str(exc),
                "details": {},
                "retryable": False,
                "trace_id": ensure_trace_id(),
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(  # noqa: ANN001
    request, exc: RequestValidationError
) -> JSONResponse:
    """请求体/查询参数校验失败也要用统一错误体（API.md §0.1），而不是 FastAPI 的 422 默认结构。"""
    from app.core.errors import ErrorCode, ensure_trace_id

    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [str(part) for part in first.get("loc", ())]
    field = ".".join(loc) or "请求参数"
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": ErrorCode.VALIDATION_ERROR,
                "message": f"{field}：{first.get('msg', '参数不合法')}",
                "details": {"loc": loc, "errors": errors},
                "retryable": False,
                "trace_id": ensure_trace_id(),
            }
        },
    )


for module in (health, workspace, projects, story, generation, workflow, jobs, media, exports, admin):
    app.include_router(module.router, prefix=API_PREFIX)


@app.get("/api/v1", include_in_schema=False)
def api_root() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.version,
        "docs": "/docs",
        "prefix": API_PREFIX,
    }


# --------------------------------------------------------------------------- #
# 前端静态资源（构建产物存在时直接托管，方便单机部署）
# --------------------------------------------------------------------------- #

FRONTEND_DIST = (
    Path(settings.frontend_dist)
    if settings.frontend_dist
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)

if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
    if (FRONTEND_DIST / "assets").exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
