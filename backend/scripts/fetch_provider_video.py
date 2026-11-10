"""按供应商任务编号取回视频（这里用于 ToAPIs / seedance 系列）。

用途：任务在网关侧已经生成成功、但本地因为网络问题没拉回来。钱已经花了，
把这个文件下载回来，再用「视频页 → 上传片段」挂到对应镜头，不必重新生成。

用法：
    python scripts/fetch_provider_video.py tsk_vid_xxxxxxxx
    python scripts/fetch_provider_video.py tsk_vid_xxxxxxxx --out E:/tmp/clip.mp4

只读：只查任务与下载产物，不提交任何新的生成任务，不产生费用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings
from app.providers.net import apply_provider_proxy

#: 视频任务查询地址 = 提交地址 + /{task_id}（与 providers/custom.py 保持一致）
VIDEO_TASK_BASE = settings.grok_video_url.rstrip("/")


def _task_url(task_id: str) -> str:
    return f"{VIDEO_TASK_BASE}/{task_id}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.grok_video_api_key or settings.gemini_image_api_key}"}


def _result_urls(body: dict) -> list[str]:
    result = body.get("result") or {}
    urls: list[str] = []
    for item in result.get("data") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    if not urls and isinstance(result.get("url"), str):
        urls.append(result["url"])
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="按任务编号下载供应商已生成的视频")
    parser.add_argument("task_id", help="网关任务编号，形如 tsk_vid_01M6QZ...")
    parser.add_argument("--out", default="", help="保存路径（默认 .tmp/<任务编号>.mp4）")
    args = parser.parse_args()

    apply_provider_proxy()
    task_id = args.task_id.strip()

    with httpx.Client(timeout=60) as client:
        resp = client.get(_task_url(task_id), headers=_headers())
        print(f"查询：{_task_url(task_id)}")
        print(f"状态码：{resp.status_code}")
        if resp.status_code >= 400:
            print(resp.text[:400])
            return 1
        body = resp.json() or {}
        print(f"任务状态：{body.get('status')}  进度：{body.get('progress')}")
        print(f"模型：{body.get('model')}")
        billing = body.get("billing") or {}
        if billing:
            print(f"计费：{billing.get('status')} · {billing.get('credits')} 积分 · ${billing.get('cost_usd')}")
        urls = _result_urls(body)
        if not urls:
            print("没有产物地址（任务可能还没完成或已失败）：")
            print(json.dumps(body, ensure_ascii=False)[:600])
            return 1
        print(f"产物：{len(urls)} 个")

        out_path = Path(args.out) if args.out else Path(".tmp") / f"{task_id}.mp4"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        target = urls[0]
        with client.stream("GET", target) as stream:
            stream.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in stream.iter_bytes():
                    fh.write(chunk)

    size = out_path.stat().st_size
    head = out_path.read_bytes()[:12]
    print(f"已保存：{out_path.resolve()}")
    print(f"大小：{size / 1024:.0f} KB  文件头：{head.hex()}")
    if head[4:8] != b"ftyp":
        print("警告：文件头不是 mp4（可能下到了错误页），请检查产物地址")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
