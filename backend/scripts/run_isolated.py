"""在隔离环境里运行核验脚本，绝不污染开发库。

问题背景：`smoke_http.py` / `check_*.py` 都打「正在运行的服务」，而那个服务用的是
`backend/data/hoyomanju.db`。每跑一次就往库里塞一个新项目（联调项目·夜行者、
降级核验、资产库核验…），跑几十次后资产库里全是重复条目，还混着真实数据。

本脚本的做法：起一个**临时后端实例**（独立数据库 / 独立存储 / 强制 Mock 供应商），
把目标脚本指向它，跑完销毁。开发库与真实供应商完全不受影响，也不花钱。

用法：
    python scripts/run_isolated.py scripts/smoke_http.py
    python scripts/run_isolated.py scripts/check_p0_fixes.py
    python scripts/run_isolated.py scripts/check_project_assets.py --port 18100

脚本本身的 `HOYOMANJU_API` / `HOYOMANJU_PORT` 会被自动注入，无需手动传 URL。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROXY_VARS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)


def _free_port(preferred: int) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def _wait_health(base: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/v1/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    return False


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python scripts/run_isolated.py <核验脚本> [--port N]")
        return 2
    target = argv[0]
    port = 18000
    if "--port" in argv:
        idx = argv.index("--port")
        if idx + 1 < len(argv):
            port = int(argv[idx + 1])
    port = _free_port(port)

    tmp = Path(tempfile.mkdtemp(prefix="hoyomanju-iso-"))
    (tmp / "storage").mkdir(parents=True, exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env.update(
        {
            "PYTHONPATH": str(BACKEND.parent / ".deps"),
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite:///{(tmp / 'iso.db').as_posix()}",
            # 有些核验脚本直接读库文件，这里是它的路径（不是 URL）
            "HOYOMANJU_DB": str(tmp / "iso.db"),
            "STORAGE_DIR": str(tmp / "storage"),
            "CHECKPOINT_DB": str(tmp / "checkpoints.db"),
            "STORAGE_BACKEND": "local",
            # 强制 Mock：不调外部付费接口，也不受 .env 里真实供应商影响
            "DEFAULT_PROVIDER": "mock",
            "TEXT_PROVIDER": "mock",
            "IMAGE_PROVIDER": "mock",
            "VIDEO_PROVIDER": "mock",
            "SPEECH_PROVIDER": "mock",
            "RETRY_BACKOFF_SECONDS": "0",
        }
    )

    base = f"http://127.0.0.1:{port}"
    print(f"[隔离] 临时目录 {tmp}")
    print(f"[隔离] 后端 {base}（Mock 供应商 / 独立数据库）")

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    code = 1
    try:
        if not _wait_health(base):
            print("[隔离] ✗ 后端启动超时")
            return 1
        print("[隔离] 后端就绪，开始跑核验\n" + "-" * 60)
        child_env = dict(env)
        child_env["HOYOMANJU_API"] = base
        child_env["HOYOMANJU_PORT"] = str(port)
        code = subprocess.call(
            [sys.executable, target], cwd=str(BACKEND), env=child_env
        )
        print("-" * 60)
        print(f"[隔离] 核验退出码 {code}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        print("[隔离] 临时数据已销毁，开发库未被改动")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
