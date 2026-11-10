# hoyo漫剧 · AI 漫剧智能体工作台（MVP）

面向个人创作者的 AI 漫剧端到端制作工作台。输入一段故事，经由 **8 个智能体 + 6 个人工确认门控**，
产出竖屏漫剧的**预览视频**与**可交付素材包**（成片 MP4 + 剧本 + 权利声明 + 字幕 + 图片 / 视频 / 音频素材）。

> 需求来源：`漫剧mvp.md`（MVP 需求文档 V0.1）
> 接口契约：`docs/API.md`（前后端唯一事实来源）
> 实现说明：`docs/MVP-实现说明.md`

---

## 1. 一句话闭环

```
故事输入 → 剧本确认 → 角色/画风确认 → 分镜图确认 → 视频生成确认 → TTS/BGM → 自动合成 → 预览 → 导出
   ①         ②             ③              ④              ⑤
```

失败必须**可查看 / 可重试 / 可恢复**，且**不重复计费**（成本台账 RESERVE → SETTLE/RELEASE 双向对账）。

---

## 2. 快速开始

### 2.1 本地开发（推荐，无需 Docker）

前置：Python 3.12+、Node 20+、FFmpeg（也可由 `imageio-ffmpeg` 自带）。

```powershell
# 1) 后端依赖
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 配置（不配也能跑，默认全 Mock 供应商）
cd ..
Copy-Item .env.example .env

# 3) 建表
cd backend
alembic upgrade head

# 4) 起后端（默认内嵌 worker，单进程即可跑完整闭环）
python -m uvicorn app.main:app --reload --port 18437
#   Swagger: http://127.0.0.1:18437/docs
#   健康检查: http://127.0.0.1:18437/api/v1/health
```

```powershell
# 5) 前端
cd frontend
npm install
npm run dev
#   工作台: http://127.0.0.1:15731
```

### 2.2 Docker Compose 一键起

```powershell
Copy-Item .env.example .env
docker compose up -d --build          # 前端 http://127.0.0.1:8080
```

按需启用附加组件：

```powershell
docker compose --profile workers up -d   # 独立 worker 进程（横向扩容用）
docker compose --profile infra up -d     # Redis + MinIO
```

### 2.3 跑测试

```powershell
cd backend
python -m pytest            # 9 项端到端验收测试
```

起好后端后，还可以跑真实 HTTP 联调（不是 TestClient，打的是真正的 uvicorn 进程）：

```powershell
cd backend
python scripts\run_isolated.py scripts\smoke_http.py   # 完整闭环：6 门控 → 预览 → 成片 → 素材包（约 2 分钟）
python scripts\run_isolated.py scripts\smoke_sse.py    # SSE 实时推送 + Last-Event-ID 断线续传
python scripts\run_isolated.py scripts\check_p0_fixes.py
```

> **务必用 `run_isolated.py` 跑核验脚本。** 它会起一个临时后端实例（独立 SQLite / 独立存储 /
> 强制 Mock 供应商），跑完销毁。直接 `python scripts\smoke_http.py` 会打开发库里的服务，
> 每跑一次就往库里塞一个新项目（`联调项目·夜行者`、`降级核验`…），几十次之后项目列表和
> 资产库里全是重复条目，还会把 `cost_ledgers` 的配额计数刷高。
> 若确实想对已启动的开发服务做联调，再显式传 URL：`python scripts\smoke_http.py http://127.0.0.1:18437`。

`scripts/ui_smoke.py` 是真实浏览器冒烟（新建项目 → 新建剧集 → 保存故事 → 启动工作流 →
门控确认），需要一个已开启 CDP 端口的 Chromium：`python scripts\ui_smoke.py <cdp_port>`。

---

## 3. 目录结构

```
hoyomanju/
├─ backend/
│  ├─ app/
│  │  ├─ api/routes/        # REST 路由（health/projects/story/generation/workflow/jobs/media/exports/admin）
│  │  ├─ agents/            # 8 个智能体：story/character/shot/image/video/audio/edit/safety
│  │  ├─ workflow/          # LangGraph 图、状态、节点、降级状态机
│  │  ├─ services/          # 事件流、存储、成本台账、任务、渲染、媒体、执行器、供应商日志
│  │  ├─ providers/         # 供应商适配：Mock / 火山方舟 / OpenAI / 可灵 / MiniMax
│  │  ├─ worker/runner.py   # 数据库租约队列 worker（含内嵌模式）
│  │  ├─ db/                # SQLAlchemy 模型与会话
│  │  └─ main.py            # FastAPI 入口
│  ├─ alembic/              # 数据库迁移
│  ├─ tests/                # 端到端验收测试
│  ├─ scripts/              # HTTP / SSE 联调与回归脚本
│  └─ requirements.txt
├─ frontend/                # Vue 3 + TS + Pinia + Vue Router 工作台
├─ docs/
│  ├─ API.md                # 后端 API 契约（v1）
│  └─ MVP-实现说明.md        # 架构与实现说明
├─ docker-compose.yml
└─ .env.example
```

---

## 4. 智能体与工作流

| # | 智能体 | 职责 |
|---|--------|------|
| 1 | StoryAgent | 故事输入 → StoryBible + BeatSheet + ShotScript |
| 2 | CharacterAgent | 角色卡、外观特征、主参考图、画风 |
| 3 | ShotAgent | 分镜表、镜头语言、时长与节奏 |
| 4 | ImageAgent | 分镜首帧候选图生成（多候选） |
| 5 | VideoAgent | 图生视频、成本预估与确认 |
| 6 | AudioAgent | 台词 TTS、BGM、字幕切分 |
| 7 | EditAgent | 时间线合成、预览渲染、成片与素材包导出 |
| 8 | SafetyAgent | 输入 / 输出内容安全检查、AIGC 标识、风险记录 |

工作流节点顺序（6 个人工门控）：

```
initialize → safety_check_input → story_agent → validate_script
  → [gate 剧本确认] → character_agent → [gate 角色/画风确认]
  → shot_agent → image_generation → [gate 分镜图确认]
  → cost_estimate → [gate 视频成本确认] → video_generation → [gate 视频确认]
  → audio_agent → audio_generation → edit_agent → render_preview
  → [gate 导出确认] → export → completed
```

门控通过 `interrupt()` 暂停并把状态写入 checkpoint，前端确认后从 checkpoint 续跑（不重跑已完成节点）。

---

## 5. 供应商

各模态一个主供应商，**未配置密钥时自动回退 Mock**，因此空手也能跑通完整闭环。

| 模态 | 可选 | Mock 行为 |
|------|------|-----------|
| text | `mock` / `ark` / `openai` / `deepseek` | 结构化剧本 JSON |
| image | `mock` / `ark` / `openai` / `gemini`（toapis 异步，需国际出口）/ `qwen`（通义同步，国内可达） | Pillow 真实生成 PNG |
| video | `mock` / `ark` / `kling` / `grok`（toapis） | FFmpeg zoompan 真实生成 MP4 |
| speech | `mock` / `minimax` / `openai` / `qwen` | FFmpeg 生成真实 MP3 |

在「设置」页或 `PUT /api/v1/settings/providers` 配置密钥（服务端加密存储，接口只回 `configured` 与掩码前缀）。

---

## 6. 关键约束

- **配额不写死**：前端所有数值限制从 `GET /api/v1/config/limits` 读取。
- **统一错误体**：`{"error": {code, message, details, retryable, trace_id}}`。
- **幂等**：`jobs.idempotency_key` 唯一；重试使用 `{base}#retry{n}` 并记录 `parent_job_id`。
- **成本对账**：`RESERVE(+) / SETTLE(+) / RELEASE(−)`，`used = ΣSETTLE`，`reserved = ΣRESERVE + ΣRELEASE`，整数分。
- **实时进度**：`GET /api/v1/episodes/{id}/events`（SSE），客户端带 `last_event_id` 断线续传。
- **降级不阻断交付**：TTS 失败只记 warning；渲染失败不影响素材包 ZIP 下载。

---

## 7. 许可与合规

导出产物一律附带 AIGC 标识与权利声明（`rights.json`），可在 `GET /api/v1/episodes/{id}/safety` 查看风险汇总。
