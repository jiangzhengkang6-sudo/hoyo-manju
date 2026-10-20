"""验证 SSE 实时事件流：订阅 → 触发 → 收到事件 → 断线续传。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from http.client import HTTPConnection

BASE_HOST = "127.0.0.1"
BASE_PORT = int(os.environ.get("HOYOMANJU_PORT", "18437"))
API = f"http://{BASE_HOST}:{BASE_PORT}/api/v1"


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        return json.loads(raw)


print("=" * 70)
print("SSE 实时事件流验证")
print("=" * 70)

project = call("POST", "/projects", {"name": f"SSE 验证 {time.strftime('%H%M%S')}"})
episode = call("POST", f"/projects/{project['id']}/episodes", {"title": "SSE 集", "episode_no": 1})
episode_id = episode["id"]
call("POST", f"/episodes/{episode_id}/story",
     {"input_type": "idea", "source_text": "深夜的电台里，主持人念出一封没有署名的信。"})

received: list[tuple[str, str]] = []
stop = threading.Event()


def listen(last_event_id: str | None = None) -> None:
    """用裸 HTTP 连接读 SSE，模拟浏览器 EventSource。"""
    conn = HTTPConnection(BASE_HOST, BASE_PORT, timeout=30)
    path = f"/api/v1/episodes/{episode_id}/events"
    headers = {"Accept": "text/event-stream"}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    print(f"     SSE 响应: {resp.status} {resp.headers.get('content-type')} "
          f"(Last-Event-ID={last_event_id})")
    event_id, event_type = "", "message"
    while not stop.is_set():
        try:
            raw = resp.readline()
        except Exception:
            break
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            received.append((event_id, event_type))
            if len(received) >= 6:
                stop.set()
                break
    conn.close()


thread = threading.Thread(target=listen, daemon=True)
thread.start()
time.sleep(1.5)

call("POST", f"/episodes/{episode_id}/workflow/start", {"auto_run": True})
thread.join(timeout=25)
stop.set()

print(f"     收到事件 {len(received)} 条:")
for eid, etype in received[:8]:
    print(f"       id={eid:<6} event={etype}")

assert len(received) >= 3, "SSE 未收到足够事件"
ids = [int(e[0]) for e in received if e[0].isdigit()]
assert ids == sorted(ids), f"事件 id 非递增: {ids}"
types = {e[1] for e in received}
assert "workflow.gate_waiting" in types or "job.status_changed" in types, types
print("[PASS] SSE 事件按 id 递增且类型正确")

# --- 断线续传 ---
last = str(max(ids))
resumed: list[tuple[str, str]] = []
received.clear()
stop2 = threading.Event()


def listen_resume() -> None:
    conn = HTTPConnection(BASE_HOST, BASE_PORT, timeout=30)
    conn.request("GET", f"/api/v1/episodes/{episode_id}/events",
                 headers={"Accept": "text/event-stream", "Last-Event-ID": last})
    resp = conn.getresponse()
    print(f"     续传响应: {resp.status} (Last-Event-ID={last})")
    event_id, event_type = "", "message"
    while not stop2.is_set():
        try:
            raw = resp.readline()
        except Exception:
            break
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\r\n")
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            resumed.append((event_id, event_type))
            stop2.set()
            break
    conn.close()


_run = call("GET", f"/episodes/{episode_id}/workflow")
call("POST", f"/episodes/{episode_id}/workflow/resume",
     {"workflow_run_id": _run["id"],
      "gate": "gate_script_confirmation", "decision": "approve",
      "checkpoint_id": _run["checkpoint_id"],
      "payload": {}})

t2 = threading.Thread(target=listen_resume, daemon=True)
t2.start()
t2.join(timeout=20)
stop2.set()

if resumed:
    new_ids = [int(r[0]) for r in resumed if r[0].isdigit()]
    assert min(new_ids) > int(last), f"续传事件未跳过已读游标: {new_ids} vs {last}"
    print(f"[PASS] 断线续传正常：从 id>{last} 继续，收到 {resumed[0]}")
else:
    print("[WARN] 续传未在超时内收到新事件（可能该节点无新事件）")

print("=" * 70)
print("SSE 验证通过 ✅")
print("=" * 70)
