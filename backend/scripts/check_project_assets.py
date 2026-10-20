"""项目级「角色与画风资产库」（PRD §5.1-6）真实 HTTP 核验。

前置：后端已在 127.0.0.1:18437 运行。
用法：python scripts/check_project_assets.py

同时核验 PRD §5.1-1「登录 / 工作区」页的数据链路 —— 该页此前被文档记为
「完全没有」，实际已存在且已挂进全局导航，这里用真实请求确认它拿得到数据。

核验点：
- 项目级角色的建 / 查 / 改 / 重名冲突；
- 项目级画风的建 / 查 / 改（version 递增）/ 应用（返回 affected_shots 而非 StyleAsset）；
- 音色列表（前端 `voiceLabel` 的数据来源）；
- 工作区 / 统计 / 跨项目资产库三个接口可返回。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437") + "/api/v1"
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


def must(cond: bool, label: str, extra=None) -> None:
    print(("  [OK]   " if cond else "  [FAIL] ") + label + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAILS.append(label)


print("=== 准备项目 ===")
st, prj = call("POST", "/projects", {"name": f"资产库核验 {TAG}", "aspect_ratio": "9:16"})
must(st == 201, "创建项目", (st, prj))
pid = prj["id"]

print("\n=== 项目级角色：建 / 查 / 改 / 重名 ===")
st, listed = call("GET", f"/projects/{pid}/characters")
must(st == 200 and listed["items"] == [], "新项目角色为空", (st, listed))

st, ch = call("POST", f"/projects/{pid}/characters", {
    "name": "林晚", "identity": "夜班护士", "appearance": "及肩黑发，白大褂",
})
must(st == 201 and ch["name"] == "林晚", "创建角色", (st, ch))
must(ch["voice_id"] is None, "新角色未绑定音色（voiceLabel 显示「未绑定音色」）", ch.get("voice_id"))
must(ch["voice_speed"] == 1.0, "新角色语速默认 1.0（speedLabel 显示 1.00×）", ch.get("voice_speed"))
must(ch["status"] == "DRAFT", "新角色状态 DRAFT（对应「草稿」标签）", ch.get("status"))
must(ch["is_main"] is True, "默认主角", ch.get("is_main"))

st, dup = call("POST", f"/projects/{pid}/characters", {"name": "林晚"})
must(st == 409, "同项目重名角色被拒", (st, dup))

st, patched = call("PATCH", f"/characters/{ch['id']}", {"identity": "急诊科护士"})
must(st == 200 and patched["identity"] == "急诊科护士", "PATCH 角色身份生效", (st, patched))
# 只改一个字段不能把其它字段清空（character_dict 拍平字段的经典坑）
must(patched["name"] == "林晚" and patched["appearance"], "PATCH 未清空其它字段", patched)

st, listed = call("GET", f"/projects/{pid}/characters")
must(st == 200 and len(listed["items"]) == 1, "角色列表可查", (st, listed))

print("\n=== 项目级画风：建 / 查 / 改 / 应用 ===")
st, styles = call("GET", f"/projects/{pid}/styles")
must(st == 200, "画风列表可查", (st, styles))

st, style = call("POST", f"/projects/{pid}/styles", {
    "name": "霓虹夜雨", "description": "高对比冷调", "palette": ["#1b2a41", "#c9a227", "冷色调"],
    "camera_style": "低角度", "lighting_style": "霓虹反射",
})
must(st == 201 and style["name"] == "霓虹夜雨", "创建画风", (st, style))
must(style["version"] == 1, "新画风 version=1", style.get("version"))
must(style["status"] == "CONFIRMED", "画风状态 CONFIRMED（对应「已确认」标签）", style.get("status"))
must(style["is_default"] is False, "新建画风默认不是项目默认", style.get("is_default"))
must(isinstance(style["palette"], list) and len(style["palette"]) == 3, "色板按数组返回", style.get("palette"))

st, patched_style = call("PATCH", f"/styles/{style['id']}", {"description": "高对比冷调，雨天"})
must(st == 200 and patched_style["version"] == 2, "PATCH 画风后 version 递增", (st, patched_style))
# 局部更新绝不能把未提供的字段重置成创建默认值（曾用 StyleIn 当 PATCH body，导致 name/palette 被清空）
must(patched_style["name"] == "霓虹夜雨", "PATCH 未把画风名重置为默认值", patched_style.get("name"))
must(patched_style["palette"] == ["#1b2a41", "#c9a227", "冷色调"], "PATCH 未清空色板", patched_style.get("palette"))
must(patched_style["camera_style"] == "低角度", "PATCH 未清空镜头风格", patched_style.get("camera_style"))

# 关键：apply 返回的是 {project_id, style_id, affected_shots}，不是 StyleAsset
st, applied = call("POST", f"/styles/{style['id']}/apply")
must(st == 200, "应用画风", (st, applied))
must(
    set(applied.keys()) >= {"project_id", "style_id", "affected_shots"},
    "apply 返回 affected_shots（前端类型已按此修正）",
    applied,
)
must("id" not in applied, "apply 不返回 StyleAsset 形态（没有 id 字段）", applied)

st, project = call("GET", f"/projects/{pid}")
must(project["default_style_id"] == style["id"], "项目 default_style_id 已指向该画风", project)

st, styles = call("GET", f"/projects/{pid}/styles")
mine = [s for s in styles["items"] if s["id"] == style["id"]]
# 项目会自动带一个默认画风，所以必须按 id 取自己创建的那条，不能假设顺序
must(len(mine) == 1, "列表里能找到刚创建的画风", styles["items"])
must(mine[0]["is_default"] is True, "该画风在列表里标记为默认", mine[0] if mine else styles)
must(
    len(styles["items"]) >= 2,
    "项目自带的默认画风仍在（应用新画风不会删旧画风）",
    len(styles["items"]),
)

print("\n=== 音色列表（voiceLabel 数据来源）===")
st, voices = call("GET", f"/projects/{pid}/voices")
items = voices["items"] if isinstance(voices, dict) else voices
must(st == 200 and len(items) >= 1, "项目音色列表非空", (st, voices))
must(all("id" in v for v in items), "每条音色都有 id", items[:1])

print("\n=== PRD §5.1-1 登录 / 工作区 页数据链路 ===")
st, ws = call("GET", "/workspace")
must(st == 200, "GET /workspace 可返回", (st, ws))
must(isinstance(ws, dict) and "user" in ws, "工作区返回 user 段", list(ws.keys()) if isinstance(ws, dict) else ws)
must(bool(ws["user"].get("name")), "创作者名非空（页面顶部展示）", ws.get("user"))
must("quota" in ws, "工作区返回 quota 段（页面配额条）", list(ws.keys()))

st, renamed = call("PATCH", "/workspace", {"name": f"核验用户 {TAG}"})
must(st == 200 and renamed.get("name") == f"核验用户 {TAG}", "PATCH /workspace 改名生效", (st, renamed))

st, stats = call("GET", "/stats/workspace")
must(st == 200, "GET /stats/workspace 可返回", (st, stats))

st, assets = call("GET", "/workspace/assets")
must(st == 200, "GET /workspace/assets 可返回", (st, assets))
must(
    isinstance(assets, dict) and all(k in assets for k in ("characters", "styles", "projects", "total")),
    "跨项目资产库返回四个键（normalizeAssets 依赖）",
    list(assets.keys()) if isinstance(assets, dict) else assets,
)
must(assets["total"] >= 2, "跨项目资产库统计到本轮创建的资产", assets.get("total"))

print("\n" + "=" * 46)
if FAILS:
    print(f"结果：{len(FAILS)} 项未通过 -> {FAILS}")
else:
    print("结果：全部通过 ✅")
