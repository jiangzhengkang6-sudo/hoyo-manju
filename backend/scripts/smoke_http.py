"""端到端联调：通过真实 HTTP 接口跑通完整 MVP 闭环。

与 pytest 的 TestClient 不同，这里打的是真正的 uvicorn 进程，
用于验证 ASGI 层、SSE、静态资源与文件下载的真实行为。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "HOYOMANJU_API", "http://127.0.0.1:18437"
)
API = f"{BASE}/api/v1"

TMP = Path(__file__).resolve().parents[1] / ".tmp"

STORY = (
    "都市悬疑短剧《夜行者》。深夜十一点，记者林晚收到一封匿名邮件，"
    "附件是三年前一桩失踪案的现场照片。她约见刑警沈砚，两人在临街咖啡馆第一次正面交锋。"
    "沈砚提醒她别插手，林晚却从照片角落里认出自己父亲的车牌。"
)

FAILED = 0


def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    url = f"{API}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            return resp.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw[:400]


def ok(cond: bool, label: str, extra: object = "") -> None:
    global FAILED
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILED += 1
        print(f"[FAIL] {label}  {extra}")


def must(cond: bool, label: str, extra: object = "") -> None:
    ok(cond, label, extra)
    if not cond:
        raise SystemExit(1)


def upload(path: Path, kind: str, episode_id: str, mime: str) -> tuple[int, object]:
    """最小 multipart 上传（不引第三方库）。

    文件分片的 `Content-Type` 必须是服务端白名单里的真实类型，
    写 `application/octet-stream` 会被上传校验直接拒掉。
    """
    boundary = f"----hoyomanju{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in (("kind", kind), ("rights_declared", "true"), ("episode_id", episode_id)):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API}/media/upload",
        data=b"".join(parts),
        method="POST",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def make_bgm_mp3(seconds: float = 5.0) -> Path:
    """用 ffmpeg 造一段**真实可解码**的 BGM（正弦叠加 + 淡入淡出）。

    §15.1 要求端到端闭环里必须有一条 BGM，而合成阶段真的会用 ffmpeg 去解这个文件 ——
    所以这里不能像 §12-4 的核验那样塞一段假字节，否则渲染会失败。
    """
    from app.providers.mock import ffmpeg_bin

    exe = ffmpeg_bin()
    if not exe:
        raise SystemExit("未找到 ffmpeg，无法生成 BGM 素材")
    TMP.mkdir(parents=True, exist_ok=True)
    target = TMP / "smoke-bgm.mp3"
    # 和弦感的三音叠加，避免单频听起来像测试音
    expr = (
        "sin(2*PI*220*t)*0.18 + sin(2*PI*277.18*t)*0.14 + sin(2*PI*329.63*t)*0.12"
    )
    proc = subprocess.run(
        [
            exe, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"aevalsrc='{expr}*min(1,t*2)*min(1,(1-t/{seconds:.3f})*2)':s=44100:d={seconds:.3f}",
            "-c:a", "libmp3lame", "-b:a", "96k", str(target),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        raise SystemExit(f"BGM 生成失败：{proc.stderr[:400]}")
    return target


print("=" * 78)
print("端到端联调 ·", BASE)
print("=" * 78)

status, health = call("GET", "/health")
must(status == 200, "健康检查", health)
print("     版本:", health["version"], "| 存储:", health["storage"]["backend"],
      "| ffmpeg:", health["ffmpeg"]["available"], "| 引擎:", health["graph_engine"])

status, limits = call("GET", "/config/limits")
must(status == 200, "读取 /config/limits（前端限制的唯一来源）")
print("     候选数:", limits["candidates_per_shot_default"], "~", limits["candidates_per_shot_max"],
      "| 分镜时长:", limits["shot_duration_ms_min"], "~", limits["shot_duration_ms_max"],
      "| 剧集上限:", limits["episode_duration_ms_max"], "| 预设:", [p["id"] for p in limits["export_presets"]])

status, quota0 = call("GET", "/quota")
must(status == 200, "配额查询")
used0 = quota0["used_cents"]
print(f"     起始配额: used={used0} reserved={quota0['reserved_cents']} limit={quota0['limit_cents']}")

status, project = call("POST", "/projects",
                       {"name": f"联调项目·夜行者 {time.strftime('%H%M%S')}",
                        "description": "端到端验证"})
must(status in (200, 201), "创建项目", project)
project_id = project["id"]

status, episode = call("POST", f"/projects/{project_id}/episodes",
                       {"title": "第 1 集 · 匿名邮件", "episode_no": 1})
must(status in (200, 201), "创建剧集", episode)
episode_id = episode["id"]

status, saved = call("POST", f"/episodes/{episode_id}/story",
                     {"input_type": "idea", "source_text": STORY})
must(status == 200, "保存故事（含输入安全检查）", saved)
print(f"     故事字数: {len(STORY)}")

status, started = call("POST", f"/episodes/{episode_id}/workflow/start", {"auto_run": True})
must(status == 200, "启动工作流", started)
must(started["pending_gate"] == "gate_script_confirmation", "停在门控①", started)


def wait_gate(gate: str, label: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, run = call("GET", f"/episodes/{episode_id}/workflow")
        if run["pending_gate"] == gate:
            must(run["status"] == "WAITING_CONFIRMATION", f"{label} 处于等待确认", run["status"])
            return run
        if run["status"] in {"FAILED", "CANCELLED"}:
            must(False, f"等待 {label}", run)
        time.sleep(0.6)
    must(False, f"等待 {label}（超时）")
    return {}


def resume(run: dict, gate: str, payload: dict | None = None, decision: str = "approve") -> dict:
    body = {
        "workflow_run_id": run["id"],
        "gate": gate,
        "decision": decision,
        "checkpoint_id": run["checkpoint_id"],
        "payload": payload or {},
    }
    st, resp = call("POST", f"/episodes/{episode_id}/workflow/resume", body)
    must(st == 200, f"确认并续跑 {gate}", resp)
    return resp


# ---- ① 剧本确认 ----
run = wait_gate("gate_script_confirmation", "① 剧本确认")
st, script = call("GET", f"/episodes/{episode_id}/script")
must(st == 200, "读取剧本")
shots = script["story_json"]["shots"]
must(len(shots) >= limits["min_shots"], f"分镜数 ≥ {limits['min_shots']}", len(shots))
print(f"     剧本 v{script.get('version')} | 分镜 {len(shots)} 个 | 场景 "
      f"{len(script['story_json'].get('scenes', []))} 个")
run = resume(run, "gate_script_confirmation")
must(run["pending_gate"] == "gate_character_confirmation", "进入门控②")

# ---- ② 角色/画风确认 ----
st, characters = call("GET", f"/projects/{project_id}/characters")
must(st == 200 and characters["items"], "已生成角色资产")
print("     角色:", "、".join(c["name"] for c in characters["items"]))
for character in characters["items"]:
    st, assets = call("GET", f"/characters/{character['id']}/assets")
    usable = [a for a in assets["items"] if a["status"] != "REJECTED"]
    must(bool(usable), f"角色「{character['name']}」有可用参考图")
    st, confirmed = call("POST", f"/characters/{character['id']}/confirm",
                         {"media_id": usable[0]["id"]})
    must(st == 200, f"确认角色「{character['name']}」主参考图", confirmed)
run = wait_gate("gate_character_confirmation", "② 角色/画风确认")
run = resume(run, "gate_character_confirmation")
must(run["pending_gate"] == "gate_shot_confirmation", "进入门控③")

# ---- ③ 分镜图确认 ----
st, shot_list = call("GET", f"/episodes/{episode_id}/shots")
must(st == 200 and shot_list["items"], "已生成分镜与候选图")
for shot in shot_list["items"]:
    usable = [c for c in shot["candidates"] if c["usable"]]
    must(bool(usable), f"分镜 {shot['shot_no']} 有可用候选图")
    st, confirmed = call("POST", f"/shots/{shot['id']}/confirm", {"media_id": usable[0]["id"]})
    must(st == 200, f"确认分镜 {shot['shot_no']} 画面", confirmed)
print(f"     已确认 {len(shot_list['items'])} 个分镜画面")
run = wait_gate("gate_shot_confirmation", "③ 分镜图确认")
run = resume(run, "gate_shot_confirmation")
must(run["pending_gate"] == "gate_video_cost_confirmation", "进入门控④")

# ---- ④ 视频成本确认 ----
quote = run["state"].get("video_quote") or {}
must(quote.get("estimate_cents", 0) > 0, "视频成本预估已生成", quote)
print("     预估成本明细:", json.dumps(quote, ensure_ascii=False)[:260])
run = resume(run, "gate_video_cost_confirmation")
must(run["pending_gate"] == "gate_video_confirmation", "进入门控⑤")

# ---- ⑤ 视频确认 ----
st, shot_list = call("GET", f"/episodes/{episode_id}/shots")
for shot in shot_list["items"]:
    st, detail = call("GET", f"/shots/{shot['id']}/video")
    must(detail["video_status"] in {"CANDIDATES_READY", "READY", "SUCCEEDED"},
         f"分镜 {shot['shot_no']} 视频状态", detail["video_status"])
    must(bool(detail["history"]), f"分镜 {shot['shot_no']} 有视频产物")
    st, confirmed = call("POST", f"/shots/{shot['id']}/video/confirm",
                         {"media_id": detail["history"][-1]["media_id"]})
    must(st == 200, f"确认分镜 {shot['shot_no']} 视频", confirmed)
run = wait_gate("gate_video_confirmation", "⑤ 视频确认")

# ---- ⑤.5 BGM 挂载（PRD §15.1：端到端必须含一条 BGM）----
# 必须放在门控⑤恢复**之前** —— 恢复后会依次跑 audio_agent → audio_generation
# → edit_agent → render_preview，预览才会把 BGM 混进去。
print("     · 上传并挂载 BGM")
bgm_file = make_bgm_mp3()
st, bgm_asset = upload(bgm_file, "audio", episode_id, "audio/mpeg")
must(st == 201, "上传 BGM 素材", (st, bgm_asset))
st, bgm_track = call("POST", f"/episodes/{episode_id}/bgm", {
    "media_id": bgm_asset["id"],
    "kind": "bgm",
    "start_ms": 0,
    "end_ms": 4000,
    "volume_db": -14,
    "fade_in_ms": 300,
    "fade_out_ms": 700,
    "rights_declared": True,
})
must(st == 201, "挂载 BGM 音轨", (st, bgm_track))
must(bgm_track.get("kind") == "bgm" and bgm_track.get("rights_declared") is True,
     "BGM 音轨类型与权利声明正确", bgm_track)
st, bgm_list = call("GET", f"/episodes/{episode_id}/bgm")
must(st == 200 and any(t["id"] == bgm_track["id"] for t in bgm_list["items"]),
     "BGM 出现在音轨列表", (st, bgm_list))
print(f"     BGM: media={bgm_asset['id']} track={bgm_track['id']} "
      f"0→4000ms vol={bgm_track['volume_db']}dB")

run = resume(run, "gate_video_confirmation")
must(run["pending_gate"] == "gate_export_confirmation", "进入门控⑥")

render = run["state"].get("render") or {}
must(render.get("ok") is True, "自动合成预览成功", render)
# bgm_count 在 render.manifest 里（不是 render 顶层）
manifest = render.get("manifest") or {}
bgm_count = int(manifest.get("bgm_count") or 0)
must(bgm_count >= 1, "预览合成已混入 BGM", render)
must(int(manifest.get("dialogue_count") or 0) >= 1, "预览合成已混入台词音频", manifest)
print(f"     预览渲染: media={render.get('media_id')} export={render.get('export_id')} "
      f"bgm={bgm_count} dialogue={manifest.get('dialogue_count')}")

st, subtitles = call("GET", f"/episodes/{episode_id}/subtitles")
must(st == 200 and subtitles["items"], "字幕已生成", st)
st, tts = call("GET", f"/episodes/{episode_id}/tts")
must(st == 200 and tts["items"], "台词音频已生成", st)
print(f"     字幕 {len(subtitles['items'])} 条 | 台词音频 {len(tts['items'])} 条")

# ---- ⑥ 导出确认 ----
run = resume(run, "gate_export_confirmation", payload={"preset": "720p_9x16", "package": True})
must(run["status"] == "COMPLETED", "工作流完成", run["status"])

st, episode_state = call("GET", f"/episodes/{episode_id}")
must(episode_state["stage"] == "EXPORTED", "剧集阶段 = EXPORTED", episode_state["stage"])

# ---- 产物校验 ----
st, exports = call("GET", f"/episodes/{episode_id}/exports")
items = exports["items"]
kinds = {e["kind"] for e in items}
must({"preview", "final", "package"} <= kinds, "预览 / 成片 / 素材包齐备", kinds)
for item in sorted(items, key=lambda x: x["kind"]):
    print(f"     {item['kind']:<8} {str(item.get('preset')):<14} "
          f"{item.get('size_bytes', 0) / 1024:>9.0f} KB")

out = Path(__file__).resolve().parents[1] / ".tmp" / "integration-out"
out.mkdir(parents=True, exist_ok=True)

for item in items:
    if item["kind"] not in {"preview", "final", "package"}:
        continue
    st, blob = call("GET", f"/exports/{item['id']}/download")
    must(st == 200 and isinstance(blob, bytes) and len(blob) > 1000,
         f"下载 {item['kind']}", (st, len(blob) if isinstance(blob, bytes) else blob))
    ext = ".zip" if item["kind"] == "package" else ".mp4"
    path = out / f"{item['kind']}{ext}"
    path.write_bytes(blob)
    if ext == ".mp4":
        must(blob[4:8] == b"ftyp", f"{item['kind']} 是合法 MP4", blob[:12])
    else:
        must(blob[:2] == b"PK", f"{item['kind']} 是合法 ZIP", blob[:12])
    print(f"     {path.name:<16} {len(blob) / 1024:>9.0f} KB")

with zipfile.ZipFile(out / "package.zip") as zf:
    names = zf.namelist()
    for key in ["manifest.json", "script.json", "rights.json",
                "subtitles/episode.srt", "subtitles/episode.vtt"]:
        must(key in names, f"素材包含 {key}")
    must(any(n.startswith("images/") for n in names), "素材包含 images/")
    must(any(n.startswith("videos/") for n in names), "素材包含 videos/")
    must(any(n.startswith("audio/") for n in names), "素材包含 audio/")
    manifest = json.loads(zf.read("manifest.json"))
    must(bool(manifest.get("assets")), "manifest 含素材清单")
    must(all(a.get("sha256") for a in manifest["assets"]), "manifest 素材均带 sha256")

    # §15.1：BGM 必须真的进素材包（音轨 + 时间线 + 可解压的文件）
    bgm_entries = [a for a in manifest["audio"] if a.get("kind") == "bgm"]
    must(bool(bgm_entries), "manifest.audio 含 BGM 条目", [a.get("kind") for a in manifest["audio"]])
    must(all(a.get("rights_declared") for a in bgm_entries), "BGM 条目带权利声明", bgm_entries)
    must(bool(manifest["timeline"]["bgm_track"]), "manifest 时间线含 BGM 轨",
         manifest["timeline"]["bgm_track"])
    bgm_in_zip = [n for n in names if n.startswith("audio/bgm_")]
    must(bool(bgm_in_zip), "素材包内含 BGM 音频文件", names)
    print(f"     素材包 {len(names)} 个条目 | assets {len(manifest['assets'])} 个 | "
          f"BGM {len(bgm_entries)} 条")

# ---- 成本与审计 ----
st, quota = call("GET", "/quota")
must(quota["reserved_cents"] == 0, "无残留预留（不会重复计费）", quota)
must(quota["used_cents"] > used0, "本集产生了实际消耗", (used0, quota["used_cents"]))
print(f"     配额: used={quota['used_cents']} reserved={quota['reserved_cents']} "
      f"remaining={quota['remaining_cents']}")

st, ledger = call("GET", f"/cost/ledger?episode_id={episode_id}")
kinds_in_ledger = {i["kind"] for i in ledger["items"]}
must({"RESERVE", "SETTLE"} <= kinds_in_ledger, "成本台账含 RESERVE / SETTLE", kinds_in_ledger)
print(f"     台账流水 {len(ledger['items'])} 条 | 类型 {sorted(kinds_in_ledger)}")

st, approvals = call("GET", f"/episodes/{episode_id}/approvals")
items_a = approvals["items"]
gates = {a["stage"] for a in items_a if a["target_type"] == "workflow_gate"}
expected_gates = {
    "gate_script_confirmation", "gate_character_confirmation", "gate_shot_confirmation",
    "gate_video_cost_confirmation", "gate_video_confirmation", "gate_export_confirmation",
}
must(expected_gates <= gates, "6 个人工门控均已留痕", sorted(gates))
asset_approvals = {a["stage"] for a in items_a if a["target_type"] != "workflow_gate"}
must(bool(asset_approvals), "资产确认记录已写入 approvals", sorted(asset_approvals))
print(f"     审批记录 {len(items_a)} 条 | 门控 {len(gates)} 个 | 资产确认 {len(asset_approvals)} 类")

st, jobs = call("GET", f"/jobs?episode_id={episode_id}")
must(st == 200 and jobs["items"], "任务中心可查", st)
statuses: dict[str, int] = {}
for job in jobs["items"]:
    statuses[job["status"]] = statuses.get(job["status"], 0) + 1
print(f"     任务 {len(jobs['items'])} 个 | 状态分布 {statuses}")

st, overview = call("GET", f"/episodes/{episode_id}/overview")
must(st == 200, "剧集概览可查")

st, events = call("GET", f"/events/history?episode_id={episode_id}")
types = {e["type"] for e in events["items"]}
must("workflow.gate_waiting" in types, "事件流含门控等待事件", sorted(types))
must(events["items"] == sorted(events["items"], key=lambda i: i["id"]), "事件流按 id 有序")
print(f"     事件 {len(events['items'])} 条 | 类型 {len(types)} 种")

print("=" * 78)
print("端到端联调全部通过 ✅" if FAILED == 0 else f"存在 {FAILED} 项失败 ❌")
print("=" * 78)
raise SystemExit(1 if FAILED else 0)
