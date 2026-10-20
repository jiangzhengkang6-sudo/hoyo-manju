"""回归验证：本轮修掉的 4 个 P0 缺陷。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437") + "/api/v1"
FAILED = 0


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            ctype = resp.headers.get("content-type", "")
            return resp.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def must(cond: bool, label: str, extra=None) -> None:
    global FAILED
    if cond:
        print(f"[PASS] {label}")
    else:
        FAILED += 1
        print(f"[FAIL] {label} :: {extra}")


# ---- ① 带画风备注创建项目不再 422 ----
print("\n① FR-001 带画风创建项目")
st, project = call("POST", "/projects", {
    "name": f"回归-画风 {time.strftime('%H%M%S')}",
    "genre": "都市悬疑",
    "style": "日系赛璐璐、冷色调",
})
must(st == 201, "POST /projects 带字符串画风返回 201", (st, project))
project_id = project["id"] if st == 201 else None
st2, styles = call("GET", f"/projects/{project_id}/styles")
items = styles.get("items", styles) if isinstance(styles, dict) else []
must(
    bool(items) and any((s.get("name") or "").startswith("日系赛璐璐") for s in items),
    "画风备注落成了画风资产",
    items,
)

# ---- ② 项目归属用户与种子用户一致 ----
print("\n② §8.2 项目归属用户")
import sqlite3

db = os.environ.get(
    "HOYOMANJU_DB",
    str(Path(__file__).resolve().parents[1] / "data" / "hoyomanju.db"),
)
if not Path(db).exists():
    raise SystemExit(f"找不到数据库：{db}\n请先启动后端，或用 HOYOMANJU_DB 指定路径。")
con = sqlite3.connect(db)
owner = con.execute("select user_id from projects where id = ?", (project_id,)).fetchone()[0]
users = [r[0] for r in con.execute("select id from users").fetchall()]
con.close()
must(owner in users, f"project.user_id 指向真实用户（{owner}）", users)

# ---- ③ 角色卡字段拍平 + 编辑回写不清空 ----
print("\n③ FR-020 角色卡读写往返")
st, character = call("POST", f"/projects/{project_id}/characters", {
    "name": "林晚",
    "identity": "调查记者",
    "age_range": "28-32",
    "appearance": "齐肩黑发，眼神锐利",
    "outfit": "米色风衣",
    "personality": "执拗，共情力强",
    "speech_style": "语速快，爱追问",
    "forbidden_changes": ["不要改变发色"],
})
must(st in (200, 201), "创建角色卡", (st, character))
char_id = character["id"]
must(character.get("identity") == "调查记者", "创建响应已拍平 identity", character.get("identity"))
must(
    character.get("appearance") == "齐肩黑发，眼神锐利",
    "创建响应已拍平 appearance",
    character.get("appearance"),
)

st, listed = call("GET", f"/projects/{project_id}/characters")
rows = listed.get("items", [])
row = next((c for c in rows if c["id"] == char_id), {})
must(row.get("identity") == "调查记者", "列表接口已拍平 identity", row.get("identity"))
must(row.get("speech_style") == "语速快，爱追问", "列表接口已拍平 speech_style", row.get("speech_style"))

# 模拟前端：读到的值原样回写（旧实现会把读到 None→空串，从而清空字段）
patch = {
    "identity": row.get("identity"),
    "age_range": row.get("age_range"),
    "appearance": row.get("appearance"),
    "outfit": row.get("outfit"),
    "personality": row.get("personality"),
    "speech_style": row.get("speech_style"),
    "forbidden_changes": row.get("forbidden_changes"),
}
st, after = call("PATCH", f"/characters/{char_id}", patch)
must(st == 200, "PATCH 角色卡", (st, after))
must(after.get("appearance") == "齐肩黑发，眼神锐利", "回写后 appearance 未被清空", after.get("appearance"))
must(after.get("outfit") == "米色风衣", "回写后 outfit 未被清空", after.get("outfit"))
must(
    after.get("forbidden_changes") == ["不要改变发色"],
    "回写后 forbidden_changes 未被清空",
    after.get("forbidden_changes"),
)

# ---- ④ 故事分析可见且能兜底 ----
print("\n④ FR-011 故事分析")
st, episode = call("POST", f"/projects/{project_id}/episodes", {"title": "第一话"})
must(st in (200, 201), "创建剧集", (st, episode))
episode_id = episode["id"]
st, _ = call("POST", f"/episodes/{episode_id}/story", {
    "input_type": "idea",
    "source_text": "深夜电台主持人念出一封匿名信，信中预告了明早的头条新闻。",
})
must(st == 200, "保存故事", st)

st, analyzed = call("POST", f"/episodes/{episode_id}/story/analyze")
must(st == 200, "调用故事分析", (st, analyzed))
analysis = analyzed.get("analysis") if isinstance(analyzed, dict) else None
must(isinstance(analysis, dict), "分析响应含 analysis 对象", analyzed)
must(bool(analysis.get("theme")) or bool(analysis.get("characters")), "分析结果有内容", analysis)

st, doc = call("GET", f"/episodes/{episode_id}/story")
must(st == 200, "GET /story", st)
must(isinstance(doc.get("analysis"), dict), "GET /story 带 analysis 别名", list(doc.keys()))
must(doc.get("source_text", "").startswith("深夜电台"), "GET /story 含完整原文", doc.get("source_text"))

print("\n" + "=" * 60)
print("P0 回归全部通过 ✅" if not FAILED else f"失败 {FAILED} 项 ❌")
print("=" * 60)
raise SystemExit(1 if FAILED else 0)
