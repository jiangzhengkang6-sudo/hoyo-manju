"""§12-4 TTS 降级 + §12-8 内容拦截申诉 的真实 HTTP 端到端核验。

前置：后端已在 127.0.0.1:18437 运行。
用法：python scripts/check_degrade_appeal.py

核验点：
- 缺音频的台词被标成 missing（而不是悄悄消失），跳过/上传后状态随之变化；
- 被拦截的素材**真的挡住**预览、导出与素材包（此前 can_export 只算不用）；
- 申诉是人工通道：提交后仍被挡，人工 APPROVED 才放行，REJECTED 继续挡。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437") + "/api/v1"
DB = pathlib.Path(__file__).resolve().parents[1] / "data" / "hoyomanju.db"
TAG = uuid.uuid4().hex[:8]
FAILS: list[str] = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else None)


def upload(path: pathlib.Path, kind: str, episode_id: str, mime: str) -> tuple[int, dict | None]:
    """最小 multipart 上传（避免依赖第三方库）。

    注意：文件分片的 `Content-Type` 必须写成服务端白名单里的真实类型 ——
    写 `application/octet-stream` 会被上传校验直接拒掉。
    """
    boundary = f"----hoyomanju{uuid.uuid4().hex}"
    payload = path.read_bytes()
    parts: list[bytes] = []
    for name, value in (("kind", kind), ("rights_declared", "true"), ("episode_id", episode_id)):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        + payload
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + "/media/upload",
        data=b"".join(parts),
        method="POST",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def must(cond: bool, label: str, extra=None) -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAILS.append(label)


def block_asset(media_id: str) -> None:
    """直接把素材标成 BLOCKED（模拟供应商输出审核拦截）。"""
    conn = sqlite3.connect(DB)
    conn.execute(
        "update media_assets set safety_status='BLOCKED' where id=?", (media_id,)
    )
    conn.commit()
    conn.close()


print("=== 准备项目 / 剧集 / 角色 ===")
st, prj = call("POST", "/projects", {"name": f"降级核验 {TAG}", "aspect_ratio": "9:16"})
must(st == 201, "创建项目", (st, prj))
pid = prj["id"]
st, epi = call("POST", f"/projects/{pid}/episodes", {"title": "第一集"})
must(st == 201, "创建剧集", (st, epi))
eid = epi["id"]
st, ch = call("POST", f"/projects/{pid}/characters", {"name": "林晚"})
must(st == 201, "创建角色", (st, ch))
st, voices = call("GET", f"/projects/{pid}/voices")
voices = voices["items"] if isinstance(voices, dict) else voices
st, _ = call("POST", f"/characters/{ch['id']}/voice", {"voice_id": voices[0]["id"]})
must(st == 200, "绑定音色", st)

shots = [
    {
        "shot_no": i, "duration_ms": 3000, "scene_no": 1, "characters": ["林晚"],
        "action": f"第 {i} 个动作", "camera_movement": "固定", "composition": "中景",
        "emotion": "平静",
        "dialogue": [{"character": "林晚", "text": f"第 {i} 句台词", "emotion": "平静"}],
        "narration": "", "sfx": [], "image_prompt": f"雨夜第 {i} 帧", "negative_prompt": "",
    }
    for i in range(1, 4)
]
st, _ = call("PUT", f"/episodes/{eid}/script", {
    "story_json": {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": "林晚"}]},
        "beat_sheet": [], "shots": shots,
    }
})
must(st == 200, "写入分镜脚本", st)

print("\n=== §12-4 逐句状态 ===")
st, lines = call("GET", f"/episodes/{eid}/tts/lines")
must(st == 200, "取逐句状态", st)
must(lines["counts"]["total"] == 3 and lines["counts"]["missing"] == 3, "3 句全部缺音频", lines["counts"])

print("\n=== §12-4 跳过配音 ===")
st, res = call("POST", f"/episodes/{eid}/tts/skip", {"shot_nos": [1], "reason": "先出片"})
must(st == 200 and res["counts"]["skipped"] == 1, "跳过第 1 镜", (st, res))
st, lines = call("GET", f"/episodes/{eid}/tts/lines")
statuses = {i["shot_no"]: i["status"] for i in lines["items"]}
must(statuses == {1: "skipped", 2: "missing", 3: "missing"}, "skipped 与 missing 区分开", statuses)

print("\n=== §12-4 上传音频顶替 ===")
audio = pathlib.Path(__file__).parent / "check_degrade_appeal.py"
tmp = pathlib.Path(__file__).resolve().parents[1] / ".tmp" / f"manual-{TAG}.mp3"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.write_bytes(b"ID3\x03\x00\x00\x00" + b"fake-audio" * 40)
st, asset = upload(tmp, "audio", eid, "audio/mpeg")
must(st == 201, "上传音频素材", (st, asset))
st, track = call("POST", f"/episodes/{eid}/tts/manual", {
    "shot_no": 2, "line_index": 0, "media_id": asset["id"],
})
must(st == 201 and track["source"] == "manual", "顶替第 2 镜并标为 manual", (st, track))
st, lines = call("GET", f"/episodes/{eid}/tts/lines")
statuses = {i["shot_no"]: i["status"] for i in lines["items"]}
must(statuses == {1: "skipped", 2: "ready", 3: "missing"}, "状态随兜底动作更新", statuses)
must(lines["counts"]["manual"] == 1, "manual 计数", lines["counts"])

print("\n=== §12-8 闸门（干净时放行） ===")
st, gate = call("GET", f"/episodes/{eid}/export-gate")
must(st == 200 and gate["can_export"] is True, "无拦截时 can_export=true", (st, gate))

print("\n=== §12-8 拦截必须真的挡住导出 ===")
img = pathlib.Path(__file__).resolve().parents[1] / ".tmp" / f"blocked-{TAG}.png"
img.write_bytes(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
))
st, img_asset = upload(img, "image", eid, "image/png")
must(st == 201, "上传图片素材", (st, img_asset))
block_asset(img_asset["id"])

st, gate = call("GET", f"/episodes/{eid}/export-gate")
must(gate["can_export"] is False, "被拦截后 can_export=false", gate)
must(img_asset["id"] in gate["unresolved"], "unresolved 含该素材", gate)

for path, body in (
    (f"/episodes/{eid}/render/preview", {"watermark": True}),
    (f"/episodes/{eid}/render/export", {"preset": "720p"}),
):
    st, err = call("POST", path, body)
    must(st == 422 and err["error"]["code"] == "SAFETY_BLOCKED", f"{path} 被 422 拦下", (st, err))
    must(bool(err["error"].get("trace_id")), f"{path} 错误体带 trace_id", err["error"])

st, err = call("POST", f"/episodes/{eid}/export/package")
must(st == 422 and err["error"]["code"] == "SAFETY_BLOCKED", "素材包同样被拦", (st, err))

print("\n=== §12-8 申诉：提交不解禁，人工批准才放行 ===")
st, appeal = call("POST", f"/episodes/{eid}/appeals", {
    "target_type": "asset", "target_id": img_asset["id"],
    "reason": "剧情需要的冲突戏，不含真实暴力", "detail": "第 3 镜",
})
must(st == 201 and appeal["status"] == "PENDING", "提交申诉（PENDING）", (st, appeal))

st, gate = call("GET", f"/episodes/{eid}/export-gate")
must(gate["can_export"] is False, "提交后仍然被挡", gate)
must(img_asset["id"] in gate["pending"], "进入待复核", gate)

st, dup = call("POST", f"/episodes/{eid}/appeals", {
    "target_type": "asset", "target_id": img_asset["id"], "reason": "重复提交",
})
must(st == 409, "重复申诉被拒", (st, dup))

st, resolved = call("POST", f"/appeals/{appeal['id']}/resolve", {
    "status": "APPROVED", "resolution_note": "复核通过",
})
must(st == 200 and resolved["status"] == "APPROVED", "人工批准", (st, resolved))
st, gate = call("GET", f"/episodes/{eid}/export-gate")
must(gate["can_export"] is True and gate["unresolved"] == [], "批准后放行", gate)

st, err = call("POST", f"/episodes/{eid}/export/package")
must(not (st == 422 and err and err["error"]["code"] == "SAFETY_BLOCKED"), "导出不再被安全闸门拦", (st, err))

print("\n=== §12-8 驳回继续挡 ===")
st, img2 = upload(img, "image", eid, "image/png")
block_asset(img2["id"])
st, appeal2 = call("POST", f"/episodes/{eid}/appeals", {
    "target_type": "asset", "target_id": img2["id"], "reason": "请求放行",
})
st, resolved2 = call("POST", f"/appeals/{appeal2['id']}/resolve", {
    "status": "REJECTED", "resolution_note": "仍不符合规范",
})
must(st == 200 and resolved2["status"] == "REJECTED", "人工驳回", (st, resolved2))
st, gate = call("GET", f"/episodes/{eid}/export-gate")
must(gate["can_export"] is False, "驳回后继续挡", gate)

tmp.unlink(missing_ok=True)
img.unlink(missing_ok=True)

print("\n" + "=" * 46)
if FAILS:
    print(f"结果：{len(FAILS)} 项未通过 -> {FAILS}")
else:
    print("结果：全部通过 ✅")
