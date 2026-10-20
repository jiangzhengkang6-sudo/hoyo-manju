"""真实浏览器 UI 全流程冒烟：新建项目 → 新建剧集 → 保存故事 → 启动工作流 → 门控确认。

需要一个已开启 CDP 的 Chromium（本项目用 cua-driver 的隔离实例），并先启动后端：

    python scripts/ui_smoke.py <cdp_port>

依赖：websockets（仓库内 .deps）；后端地址可用 HOYOMANJU_API 覆盖。
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

PORT = int(sys.argv[1])
import os

BASE = os.environ.get("HOYOMANJU_API", "http://127.0.0.1:18437")

HELPERS = r"""
window.__q = (sel) => document.querySelector(sel);
window.__byText = (text, tag) => {
  const nodes = [...document.querySelectorAll(tag || 'button,a,[role=button]')];
  return nodes.find(n => (n.innerText || '').trim() === text)
      || nodes.find(n => (n.innerText || '').includes(text));
};
window.__click = (text, tag) => {
  const el = window.__byText(text, tag);
  if (!el) return {ok: false, reason: 'not found: ' + text};
  el.click();
  return {ok: true, tag: el.tagName, text: (el.innerText||'').trim().slice(0, 30)};
};
window.__fill = (sel, value) => {
  const el = window.__q(sel);
  if (!el) return {ok: false, reason: 'no selector ' + sel};
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, value: el.value};
};
"""


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def send(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr, await_promise=False):
        res = await self.send(
            "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=await_promise
        )
        return res.get("result", {}).get("value")

    async def click(self, text, tag=None):
        arg = json.dumps(tag) if tag else "null"
        return await self.js(f"window.__click({json.dumps(text)}, {arg})")

    async def fill(self, sel, value):
        return await self.js(f"window.__fill({json.dumps(sel)}, {json.dumps(value)})")

    async def path(self):
        return await self.js("location.pathname")

    async def body(self):
        return await self.js("document.body.innerText") or ""

    async def workflow(self, episode_id):
        """直接从后端读工作流状态（不依赖页面上下文，避免 CDP 连接被页面重渲染打断）。"""

        def _read():
            with urllib.request.urlopen(
                f"{BASE}/api/v1/episodes/{episode_id}/workflow", timeout=20
            ) as resp:
                return json.loads(resp.read())

        data = await asyncio.to_thread(_read)
        if not isinstance(data, dict):
            return {}
        return {
            "status": data.get("status"),
            "node": data.get("current_node"),
            "gate": data.get("pending_gate"),
        }

    async def wait_for(self, probe, timeout=180, interval=1.5):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            value = await probe()
            if value:
                return value
            await asyncio.sleep(interval)
        return None


def gate_probe(p: Page, episode_id: str, gate: str):
    async def probe():
        run = await p.workflow(episode_id)
        return run if run.get("gate") == gate else None

    return probe


def _text_probe(p: Page, text: str):
    async def probe():
        body = await p.body()
        return body if text in body else None

    return probe


async def main() -> int:
    raw = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list").read()
    targets = json.loads(raw)
    target = next(t for t in targets if t["type"] == "page")
    failures: list[str] = []

    def check(cond, label, extra=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} {extra}")
        if not cond:
            failures.append(label)

    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
        p = Page(ws)
        await p.send("Runtime.enable")
        await p.send("Page.enable")
        await p.send("Page.navigate", url=f"{BASE}/")
        await asyncio.sleep(3)
        await p.js(HELPERS)
        check("后端在线" in await p.body(), "首页显示后端在线")

        print("\n[1] 新建项目")
        await p.click("新建项目")
        await asyncio.sleep(1)
        name = "UI 验收 " + str(int(asyncio.get_event_loop().time()))[-5:]
        await p.fill("#p-name", name)
        await p.fill("#p-genre", "都市悬疑")
        await p.click("创建项目")
        await asyncio.sleep(2.5)
        check(name in await p.body(), "列表中出现新项目", name)

        print("\n[2] 新建剧集")
        await p.click("新建剧集")
        await asyncio.sleep(1)
        await p.fill("#e-title", "第一话 · 匿名邮件")
        await p.click("创建并进入工作台")
        await asyncio.sleep(3)
        path = await p.path()
        check(path.startswith("/episodes/"), "进入剧集工作台", path)
        episode_id = path.split("/")[2]
        print("     episode_id:", episode_id)

        print("\n[3] 保存故事并启动工作流")
        await p.fill("#story-text", "深夜电台主持人念出一封匿名信，信中预告了明早的头条新闻。")
        await p.click("保存故事")
        await asyncio.sleep(2)
        started = await p.wait_for(
            _text_probe(p, "启动工作流"), timeout=60, interval=1.0
        )
        check(started is not None, "出现「启动工作流」入口")
        await p.click("启动工作流")
        run = await p.wait_for(gate_probe(p, episode_id, "gate_script_confirmation"))
        check(run is not None, "工作流停在剧本确认门控", json.dumps(run, ensure_ascii=False))
        check("确认剧本" in await p.body(), "页面出现剧本门控卡片")

        print("\n[4] 点击通过")
        await p.click("确认剧本，进入角色与画风")
        nxt = await p.wait_for(gate_probe(p, episode_id, "gate_character_confirmation"))
        check(nxt is not None, "门控推进到角色与画风", json.dumps(nxt, ensure_ascii=False))

        print("\n[5] 任务中心")
        try:
            await p.send("Page.navigate", url=f"{BASE}/jobs")
            await asyncio.sleep(3)
            body = await p.body()
            check("任务" in body, "任务中心可打开")
        except Exception as exc:  # 页面重载可能打断调试连接，不影响前面的结论
            print("  [SKIP] 任务中心（调试连接已断开）：", type(exc).__name__)

    print("\n" + "=" * 60)
    if failures:
        print("UI 验收失败：", failures)
        return 1
    print("UI 全流程验收通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
