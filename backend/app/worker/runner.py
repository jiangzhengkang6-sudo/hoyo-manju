"""Worker：轮询任务表、抢占租约、执行 handler。

可作为独立进程运行：``python -m app.worker.runner``；也可由 API 进程内嵌启动。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import os
import signal
import threading
import uuid
from typing import Any

from app.core.config import settings
from app.db.models import Job, JobStatus
from app.db.session import init_db, session_scope
from app.services import executor as executor_service
from app.services import jobs as jobs_service

WORKER_ID = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self, concurrency: int | None = None, poll_interval: float | None = None) -> None:
        self.concurrency = concurrency or settings.worker_concurrency
        self.poll_interval = poll_interval or settings.worker_poll_interval
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="job"
        )
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.last_reap = dt.datetime.now(dt.timezone.utc)

    # ------------------------------------------------------------------ #
    def tick(self) -> int:
        """扫描一轮可执行任务，返回本次提交的数量。

        注意：必须先让抢占事务提交，再把任务丢给线程池；否则执行线程可能
        在新连接里读到尚未提交的 QUEUED 状态而直接放弃，任务会一直挂住。
        """
        with self._lock:
            free = self.concurrency - len(self._running)
        if free <= 0:
            return 0
        claimed: list[str] = []
        with session_scope() as session:
            if (dt.datetime.now(dt.timezone.utc) - self.last_reap).total_seconds() > 30:
                reaped = jobs_service.reap_expired_leases(session)
                # 非终态堆积过久（排队/重试退避后长期无人接管）→ EXPIRED（FR-070）
                expired = jobs_service.expire_stale_jobs(session)
                if reaped or expired:
                    session.flush()
                self.last_reap = dt.datetime.now(dt.timezone.utc)
            for job_id in jobs_service.next_queued_job_ids(session, limit=free):
                if jobs_service.claim_job(session, job_id, WORKER_ID):
                    claimed.append(job_id)
        for job_id in claimed:
            with self._lock:
                self._running.add(job_id)
            self.pool.submit(self._execute, job_id)
        return len(claimed)

    def _execute(self, job_id: str) -> None:
        try:
            with session_scope() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                if job.status != JobStatus.RUNNING.value or job.lease_owner != WORKER_ID:
                    return
                executor_service.run_job(session, job, worker_id=WORKER_ID)
        except Exception:  # noqa: BLE001 - worker 不能因单个任务崩溃
            pass
        finally:
            with self._lock:
                self._running.discard(job_id)

    def run_forever(self) -> None:
        init_db()
        while not self._stop.is_set():
            try:
                submitted = self.tick()
            except Exception:  # noqa: BLE001
                submitted = 0
            if not submitted:
                self._stop.wait(self.poll_interval)

    def stop(self) -> None:
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=False)


class EmbeddedWorker:
    """在 API 进程内以事件循环任务形式运行 worker（MVP 单机部署默认）。"""

    def __init__(self) -> None:
        self.worker = Worker()
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.worker.tick)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.worker.poll_interval)

    def start(self) -> None:
        init_db()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.worker.stop()


def main() -> None:  # pragma: no cover - 独立进程入口
    worker = Worker()

    def _handle(signum, frame):  # noqa: ANN001
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()


def status() -> dict[str, Any]:
    return {
        "worker_id": WORKER_ID,
        "concurrency": settings.worker_concurrency,
        "poll_interval": settings.worker_poll_interval,
    }
