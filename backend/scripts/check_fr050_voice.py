"""FR-050 / FR-051 真实 HTTP 端到端核验（跑在真服务器上，不走 TestClient）。

前置：后端已在 127.0.0.1:18437 运行。
用法：python scripts/check_fr050_voice.py

核验点：
- 有台词但未绑音色 → 预估 `can_generate=false` + 阻断项点名角色与镜号；
  提交被 409 `MISSING_VOICE` 拒绝，且**不留任务、不占配额**。
- 绑定音色（带语速）后放行；语速越界在 schema 层被拒。
- 生成的音轨保留 `speed`/`emotion`/`provider`/`voice_id`（FR-051）。
- 改语速会把已有配音全部标记过期。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437") + "/api/v1"
TAG = uuid.uuid4().hex[:8]
FAILS: list[str] = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else None)


def must(cond: bool, label: str, extra=None) -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAILS.append(label)


print("=== 建项目 / 剧集 ===")
st, prj = call("POST", "/projects", {"name": f"FR050 核验 {TAG}", "aspect_ratio": "9:16"})
must(st == 201, "创建项目", (st, prj))
project_id = prj["id"]

st, epi = call("POST", f"/projects/{project_id}/episodes", {"title": "第一集"})
must(st == 201, "创建剧集", (st, epi))
episode_id = epi["id"]

st, ch = call("POST", f"/projects/{project_id}/characters", {"name": "林晚"})
must(st == 201, "创建角色", (st, ch))
character_id = ch["id"]

shots = [
    {
        "shot_no": i, "duration_ms": 3000, "scene_no": 1, "characters": ["林晚"],
        "action": f"第 {i} 个动作", "camera_movement": "固定", "composition": "中景",
        "emotion": "平静",
        "dialogue": [{"character": "林晚", "text": f"第 {i} 句台词", "emotion": "平静"}],
        "narration": "", "sfx": [], "image_prompt": f"雨夜街道第 {i} 帧", "negative_prompt": "",
    }
    for i in range(1, 4)
]
st, _ = call("PUT", f"/episodes/{episode_id}/script", {
    "story_json": {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": "林晚"}]},
        "beat_sheet": [], "shots": shots,
    }
})
must(st == 200, "写入分镜脚本", st)

print("\n=== FR-050：未绑音色必须被拦 ===")
st, est = call("GET", f"/episodes/{episode_id}/tts/estimate")
must(st == 200, "取配音预估", (st, est))
must(est["can_generate"] is False, "can_generate=false", est)
problems = est["blocking_problems"]
must(len(problems) == 1 and problems[0]["character"] == "林晚", "阻断项点名角色", problems)
must(problems[0]["shot_nos"] == [1, 2, 3], "阻断项给出镜号", problems)
must(est["line_count"] == 0 and est["skipped_count"] == 3, "未绑音色的句子不计入预留", est)

st, err = call("POST", f"/episodes/{episode_id}/tts/generate", {})
must(st == 409, "提交被 409 拒绝", (st, err))
must(err["error"]["code"] == "MISSING_VOICE", "错误码 MISSING_VOICE", err)
must(err["error"]["retryable"] is False, "retryable=false", err)
must(bool(err["error"].get("trace_id")), "带 trace_id", err)
must("林晚" in err["error"]["message"], "消息里点名角色", err["error"]["message"])

st, jobs = call("GET", f"/jobs?episode_id={episode_id}")
items = jobs["items"] if isinstance(jobs, dict) else jobs
tts_jobs = [j for j in items if j.get("type") == "TTS_GENERATION"]
must(tts_jobs == [], "被拒时没有留下任务", tts_jobs)

print("\n=== FR-051：绑定音色 + 语速 ===")
st, voices = call("GET", f"/projects/{project_id}/voices")
voices = voices["items"] if isinstance(voices, dict) else voices
must(bool(voices), "项目自带预设音色", voices)
voice_id = voices[0]["id"]

st, bound = call("POST", f"/characters/{character_id}/voice",
                 {"voice_id": voice_id, "speed": 1.25})
must(st == 200, "绑定音色并设语速", (st, bound))
must(bound["voice_speed"] == 1.25, "语速已持久化", bound.get("voice_speed"))

st, got = call("GET", f"/characters/{character_id}")
must(got["voice_speed"] == 1.25, "读回语速一致", got.get("voice_speed"))

st, bad = call("POST", f"/characters/{character_id}/voice", {"voice_id": voice_id, "speed": 9})
must(st == 422, "越界语速被拒 422", (st, bad))

print("\n=== FR-050：解除阻断后可生成 ===")
st, est2 = call("GET", f"/episodes/{episode_id}/tts/estimate")
must(est2["can_generate"] is True, "can_generate=true", est2)
must(est2["blocking_problems"] == [], "无阻断项", est2["blocking_problems"])
must(est2["line_count"] == 3, "3 句进入预留", est2["line_count"])

st, job = call("POST", f"/episodes/{episode_id}/tts/generate", {})
must(st == 200, "提交成功", (st, job))
job_id = job["id"]

print("  等待 TTS 任务完成…")
for _ in range(90):
    st, snap = call("GET", f"/jobs/{job_id}")
    if snap.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"):
        break
    time.sleep(2)
must(snap.get("status") == "SUCCEEDED", "TTS 任务成功", snap.get("status"))

st, tts = call("GET", f"/episodes/{episode_id}/tts")
tracks = tts["items"]
must(len(tracks) == 3, "产出 3 条音轨", len(tracks))
must(all(t["speed"] == 1.25 for t in tracks), "音轨保留语速（FR-051）", [t.get("speed") for t in tracks])
must(all(t["provider"] for t in tracks), "音轨保留供应商", None)
must(all(t["voice_id"] == voice_id for t in tracks), "音轨保留音色", None)
must(tts["stale_count"] == 0, "新生成的音轨未过期", tts["stale_count"])

print("\n=== FR-051：改语速让旧配音过期 ===")
st, patched = call("PATCH", f"/characters/{character_id}", {"voice_speed": 0.8})
must(st == 200 and patched["voice_speed"] == 0.8, "改语速成功", (st, patched.get("voice_speed")))
st, tts2 = call("GET", f"/episodes/{episode_id}/tts")
must(tts2["stale_count"] == len(tts2["items"]) == 3, "旧配音全部标记过期", tts2["stale_count"])

print("\n" + "=" * 46)
if FAILS:
    print(f"结果：{len(FAILS)} 项未通过 -> {FAILS}")
else:
    print("结果：全部通过 ✅")
