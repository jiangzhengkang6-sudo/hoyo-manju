"""验证：POST 返回 201 之后，数据库里是否已经能看到这一行（即 commit 是否早于响应）。

回归用例：修复 RequestSessionMiddleware 之前，10 次里有 10 次「响应后不可见」。
用法：先启动后端，再 `python scripts/check_session_commit.py`。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437") + "/api/v1"
# 默认取后端的数据目录；可用 HOYOMANJU_DB 覆盖
DB = Path(
    os.environ.get("HOYOMANJU_DB")
    or Path(__file__).resolve().parents[1] / "data" / "hoyomanju.db"
)


def post(path: str, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(API + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


late = 0
for i in range(10):
    project = post("/projects", {"name": f"提交时序 {i} {time.strftime('%H%M%S')}"})
    pid = project["id"]

    # 立刻用独立连接直读
    con = sqlite3.connect(DB)
    found = con.execute("select count(1) from projects where id = ?", (pid,)).fetchone()[0]
    con.close()
    if not found:
        late += 1
        # 稍等再看
        time.sleep(0.4)
        con = sqlite3.connect(DB)
        after = con.execute("select count(1) from projects where id = ?", (pid,)).fetchone()[0]
        con.close()
        print(f"{i}: 响应后不可见，0.4s 后可见={bool(after)}  ({pid})")
    else:
        print(f"{i}: 响应后立即可见  ({pid})")

print("late commits =", late, "/ 10")
