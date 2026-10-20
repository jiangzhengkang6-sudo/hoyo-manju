"""pytest 公共夹具：隔离的数据库、存储与工作流 checkpoint。"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

# 注意：必须放在工作区内并用 0o777 创建。tempfile.mkdtemp 使用 0o700，
# 会生成受限的 Windows DACL，导致 ffmpeg 子进程无法写入渲染产物。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TMP = _REPO_ROOT / ".tmp" / f"pytest-{uuid.uuid4().hex[:8]}"
_TMP.mkdir(parents=True, exist_ok=True)
(_TMP / "storage").mkdir(parents=True, exist_ok=True)
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["CHECKPOINT_DB"] = str(_TMP / "checkpoints.db")
os.environ["STORAGE_BACKEND"] = "local"
os.environ["ENABLE_LANGGRAPH"] = os.environ.get("ENABLE_LANGGRAPH", "1")
os.environ["QUOTA_LIMIT_CENTS"] = "20000"
os.environ["CANDIDATES_PER_SHOT_DEFAULT"] = "2"
os.environ["MIN_SHOTS"] = "6"
# 自动重试在测试里立即执行，避免 drain() 因退避期被判定为空闲而提前返回
os.environ["RETRY_BACKOFF_SECONDS"] = "0"

# 测试必须封闭自洽：强制所有模态走 Mock。
# backend/.env 里可能配了真实供应商（deepseek/gemini/grok/qwen），
# 若不覆盖，pytest 会真的去调付费外部接口 —— 既慢、又不稳定、还花钱。
for _kind in ("TEXT", "IMAGE", "VIDEO", "SPEECH"):
    os.environ[f"{_kind}_PROVIDER"] = "mock"
os.environ["DEFAULT_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.worker.runner import Worker  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    try:
        engine.dispose()
    except Exception:
        pass
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture()
def client():
    from app.db.models import Base

    Base.metadata.drop_all(engine)
    init_db()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def worker():
    return Worker(concurrency=2, poll_interval=0.05)


def drain(worker: Worker, timeout: float = 180.0) -> None:
    """把队列中的任务全部跑完（同时等待已提交的任务真正结束）。"""
    deadline = time.time() + timeout
    idle_rounds = 0
    while time.time() < deadline:
        submitted = worker.tick()
        with worker._lock:
            busy = len(worker._running)
        if submitted == 0 and busy == 0:
            idle_rounds += 1
            if idle_rounds >= 3:
                return
        else:
            idle_rounds = 0
        time.sleep(0.05)


def wait_job(client: TestClient, worker: Worker, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        worker.tick()
        data = client.get(f"/api/v1/jobs/{job_id}").json()
        if data["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}:
            return data
        time.sleep(0.05)
    raise TimeoutError(f"任务未在 {timeout}s 内结束：{job_id}")


@pytest.fixture()
def project(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={
            "name": "测试项目",
            "genre": "悬疑",
            "aspect_ratio": "9:16",
            "default_language": "zh-CN",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def episode(client: TestClient, project: dict) -> dict:
    response = client.post(
        f"/api/v1/projects/{project['id']}/episodes",
        json={"title": "第一集 雨夜", "target_duration_ms": 60000},
    )
    assert response.status_code == 201, response.text
    return response.json()


STORY_TEXT = (
    "林晚在雨夜收到一封没有署名的信，信上只有一个地址。"
    "她循着地址找到旧城区的咖啡馆，遇见了三年前失踪的沈砚。"
    "沈砚说，她记忆里那场火灾从来不是意外。"
    "两人在天台上对峙，真相被摊开在夜色里，而林晚必须决定要不要相信他。"
)
