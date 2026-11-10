# hoyo漫剧 · AI 漫剧智能体工作台

面向个人创作者的漫剧生产平台：输入一段故事，经剧本、分镜、画面、视频、配音、字幕到成片，
全程由可中断、可恢复的智能体工作流驱动，每个高成本环节都设人工确认门控。

**核心取舍**：AI 负责批量产出，人负责关键取舍。生成链路不追求「一键到底」，
而是把每一步的产物、成本与失败都摊开给人看——确认了才进入下一步，失败了可查看、可重试、可恢复，且不重复计费。

| 指标 | 规模 |
|---|---|
| REST 接口 | 153 个 |
| 异步任务类型 | 11 类 |
| 编排节点 / 人工门控 | 21 个 / 6 道 |
| 接入供应商 | 9 家（含 Mock 兜底） |
| 数据表 / 数据库迁移 | 25 张 / 8 个 |
| 自动化测试 | 后端 408 项 + 前端 150 余项 |

---

## 目录

- [1. 能力范围](#1-能力范围)
- [2. 系统架构](#2-系统架构)
- [3. 技术栈](#3-技术栈)
- [4. 快速开始](#4-快速开始)
- [5. 配置](#5-配置)
- [6. 目录结构](#6-目录结构)
- [7. 工作流与智能体](#7-工作流与智能体)
- [8. 关键设计](#8-关键设计)
- [9. 测试与质量](#9-测试与质量)
- [10. 部署](#10-部署)
- [11. 文档](#11-文档)
- [12. 合规与许可](#12-合规与许可)

---

## 1. 能力范围

一段故事进来，一条成片出去，中间七道工序都可以单独重跑：

| 阶段 | 产出 | 人工介入点 |
|---|---|---|
| 剧本 | StoryBible、人物表、场景表、节拍表、分镜初稿 | 确认剧本，可直接编辑 |
| 角色与画风 | 角色卡（外观 / 服饰 / 性格 / 禁用变化）、主参考图、项目画风 | 确认角色与画风 |
| 画面 | 每个分镜多张候选图 | 挑选并确认首帧 |
| 视频 | 图生视频片段（可跳过，回落静态图） | 确认视频成本、确认片段 |
| 配音 | 逐句 TTS 音轨 + 氛围音 / BGM 轨 | 绑定音色、试听、逐条重跑 |
| 字幕 | 按音频时间轴生成，可逐条校正，导出 SRT / VTT | 手工校时与改词 |
| 成片 | 低码率预览、正式成片、素材包 ZIP | 确认导出 |

失败路径同样完整：缺素材、音频过期、比例不符、权利未声明都能定位到具体镜号，
不满足条件时**在提交前**就拒绝，而不是跑到一半才发现。

---

## 2. 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│  Vue 3 工作台（15 个页面）：项目 / 剧本 / 分镜 / 画面 / 视频     │
│                           / 音频 / 字幕 / 合成导出 / 任务中心   │
└───────────────────────────┬──────────────────────────────────┘
                            │ REST + SSE
┌───────────────────────────▼──────────────────────────────────┐
│  FastAPI（153 个接口）                                         │
│  ├─ 请求级事务中间件（统一提交 / 统一错误体 / trace_id）         │
│  ├─ 工作流引擎（LangGraph 21 节点，6 道 interrupt 门控）         │
│  └─ SSE 事件流（断线续传，last_event_id）                       │
└──────┬───────────────────────────────┬───────────────────────┘
       │                               │
┌──────▼───────────┐          ┌────────▼───────────────────────┐
│ 任务队列（DB 租约）│          │ 领域能力                        │
│ 11 类异步任务      │          │ 8 个智能体 + 渲染 + 存储 + 安全  │
│ 重试 / 取消 / 恢复 │          │                                 │
└──────┬───────────┘          └────────┬───────────────────────┘
       │                               │
┌──────▼───────────────────────────────▼───────────────────────┐
│  供应商抽象层（文本 / 图像 / 视频 / 语音）                       │
│  能力声明 → 参数钳制 → 错误码归一 → 熔断 → 主备切换 → Mock 兜底  │
└──────────────────────────────────────────────────────────────┘
```

**四条支撑线**贯穿所有阶段，缺一条整条链路就不可信：

| 支撑 | 解决的问题 |
|---|---|
| 成本台账 | 每次调用前预留、成功按实结算、失败全额释放，幂等键防重复扣费 |
| 任务系统 | 租约抢占、指数退避重试、协作式取消、失败后按供应商任务号补拉 |
| 实时事件 | 进度、候选图、门控状态通过 SSE 推到前端，不需要手动刷新 |
| 内容安全 | 输入 / 输出双向检查、未解决拦截禁止导出、AIGC 标识写入成片与素材包 |

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12、TypeScript 5.6 |
| Web | FastAPI、Starlette、Uvicorn、Pydantic 2 |
| 编排 | LangGraph（StateGraph + SqliteSaver checkpoint） |
| 数据 | SQLAlchemy 2.0（`Mapped` 风格）、Alembic、SQLite（WAL） |
| 媒体 | FFmpeg（拼接 / 转场 / 混音 / 字幕烧录 / 变速 / 探测）、Pillow |
| 出网 | httpx（异步客户端，长轮询与产物分片下载） |
| 前端 | Vue 3（`<script setup>`）、Vite、Pinia、Vue Router、原生 CSS（设计令牌 + 容器查询） |
| 测试 | pytest + pytest-asyncio（408 项）、Vitest + @vue/test-utils（150+ 项）、vue-tsc |
| 部署 | Docker Compose（可选 Redis / MinIO profile）、Nginx |

---

## 4. 快速开始

### 4.1 本地开发

前置：Python 3.12、Node 20+、FFmpeg（未安装时由 `imageio-ffmpeg` 提供）。

```powershell
# 后端
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd ..
Copy-Item .env.example .env      # 不配密钥也能跑：默认全部走 Mock 供应商

cd backend
alembic upgrade head
python -m uvicorn app.main:app --reload --port 18437
#   Swagger:  http://127.0.0.1:18437/docs
#   Health:   http://127.0.0.1:18437/api/v1/health
```

```powershell
# 前端（另开一个终端）
cd frontend
npm install
npm run dev
#   工作台: http://127.0.0.1:15731
```

后端默认在 API 进程内嵌 worker，单进程即可跑完整闭环；任务量大时可切换独立 worker（见 [部署](#10-部署)）。

### 4.2 Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up -d --build          # 前端 http://127.0.0.1:8080

docker compose --profile workers up -d   # 独立 worker（横向扩容）
docker compose --profile infra up -d     # Redis + MinIO
```

### 4.3 验证安装

```powershell
cd backend
python -m pytest                  # 408 项，全部使用 Mock 供应商，不产生调用费用
```

真实 HTTP 联调（起临时实例，跑完销毁，不污染开发库）：

```powershell
python scripts\run_isolated.py scripts\smoke_http.py   # 六道门控 → 预览 → 成片 → 素材包
python scripts\run_isolated.py scripts\smoke_sse.py    # SSE 推送 + Last-Event-ID 续传
```

> 请用 `run_isolated.py` 执行核验脚本：它会起独立 SQLite 与独立存储并强制 Mock 供应商。
> 直接运行脚本会打开发库并不断写入新项目，把配额计数刷高。

---

## 5. 配置

所有配置集中在 `.env`（模板见 `.env.example`），关键项：

| 变量 | 说明 |
|---|---|
| `PROVIDER_PROXY_URL` | 出网代理策略；`direct` 表示直连，忽略系统 `HTTPS_PROXY` |
| `TEXT_PROVIDER` / `IMAGE_PROVIDER` / `VIDEO_PROVIDER` / `SPEECH_PROVIDER` | 各模态供应商；未配置密钥时自动回退 Mock |
| `QUOTA_LIMIT_CREDITS` | 配额上限，按供应商积分口径（1 积分 = `CREDIT_USD_VALUE` 美元） |
| `CREDIT_USD_VALUE` / `USD_CNY_RATE` | 积分与人民币换算，用于成本展示 |
| `DATABASE_URL` | 默认 SQLite；改一行即可切 PostgreSQL |
| `STORAGE_BACKEND` | `local` 或 `s3`（MinIO / 兼容对象存储） |
| `SIGNED_URL_TTL_SECONDS` | 媒体签名地址有效期，默认 30 分钟 |

供应商密钥也可在「设置」页写入，服务端加密存储，接口只返回是否已配置与掩码前缀。

---

## 6. 目录结构

```
hoyomanju/
├─ backend/
│  ├─ app/
│  │  ├─ api/routes/        # REST 路由（11 个模块，153 个接口）
│  │  ├─ agents/            # 领域能力：story / character / shot / image / video / audio / edit / safety
│  │  ├─ workflow/          # LangGraph 图、状态、21 个节点、降级状态机
│  │  ├─ services/          # 任务、成本、事件、存储、渲染、媒体、执行器、恢复、审计
│  │  ├─ providers/         # 供应商适配：统一协议 + 9 家实现 + Mock
│  │  ├─ worker/runner.py   # 租约队列 worker（支持内嵌与独立进程）
│  │  ├─ db/                # 25 张表模型与会话
│  │  └─ main.py            # 应用入口（中间件、路由挂载、静态托管）
│  ├─ alembic/versions/     # 8 个数据库迁移
│  ├─ tests/                # 408 项测试
│  ├─ scripts/              # 联调、体检与运维脚本
│  └─ requirements.txt
├─ frontend/
│  ├─ src/views/            # 15 个页面，一页对应一个制作环节
│  ├─ src/components/       # 通用组件（候选图、门控卡、任务条、媒体缩略…）
│  ├─ src/stores/           # Pinia：剧集 / 项目 / 配置 / 任务
│  ├─ src/api/              # 接口封装（类型、归一化、错误处理）
│  ├─ src/styles/           # 设计令牌与基础样式
│  └─ vitest.config.ts
├─ docs/                    # 设计、实现、接口与优化文档
├─ docker-compose.yml
└─ .env.example
```

---

## 7. 工作流与智能体

### 7.1 智能体职责

| 智能体 | 职责 |
|---|---|
| StoryAgent | 故事 → StoryBible + 节拍表 + 分镜初稿；意图分析与局部重生成 |
| CharacterAgent | 角色卡、一致性参数、主参考图；剧本生成后自动建卡 |
| ShotAgent | 分镜拆解、镜头语言、时长与节奏；画面提示词组装 |
| ImageAgent | 分镜首帧多候选生成；角色主图作为参考图注入 |
| VideoAgent | 图生视频、成本预演、片段确认与补拉 |
| AudioAgent | 逐句 TTS、音色绑定、氛围音 / BGM、音色配方指纹 |
| EditAgent | 时间线构建、校验、字幕生成、合成与导出、素材包 |
| SafetyAgent | 输入输出内容检查、拦截与申诉、AIGC 标识 |

### 7.2 编排顺序

```
initialize → safety_check_input → story_agent → validate_script
  → [剧本确认] → character_agent → [角色/画风确认]
  → shot_agent → image_generation → [分镜图确认]
  → cost_estimate → [视频成本确认] → video_generation → [视频确认]
  → audio_agent → audio_generation → edit_agent → render_preview
  → [导出确认] → export → completed
```

门控通过 `interrupt()` 暂停并写入 checkpoint，前端确认后从断点续跑，**不重跑已完成节点**。

### 7.3 供应商

| 模态 | 可选实现 |
|---|---|
| 文本 | `deepseek` / `ark` / `openai` / `mock` |
| 图像 | `gemini`（异步网关）/ `qwen`（同步接口）/ `ark` / `openai` / `mock` |
| 视频 | `grok`（异步网关，seedance 系列）/ `ark` / `kling` / `mock` |
| 语音 | `qwen` / `minimax` / `openai` / `mock` |

每家供应商声明自己的能力（时长区间、画幅、参考图数量、单价），提交前统一钳制，
不支持的参数不会发给对方；错误码归一到 7 类，连续失败触发熔断并给出手动恢复入口。

---

## 8. 关键设计

### 8.1 成本：三段式台账

```
RESERVE(+) ── 创建任务时冻结额度
   ├─ 成功 → SETTLE(实际) + RELEASE(-预留)   → 净扣实际成本
   └─ 失败 → RELEASE(-预留)                  → 净扣 0
```

- 可用额度 = 上限 − ΣSETTLE − (ΣRESERVE + ΣRELEASE)
- 所有读额度的操作走台账求和，不读 ORM 内存字段（历史事故：重复释放导致冻结额度为负）
- 幂等键 = 业务维度 + 参数摘要 + 5 秒时间桶；同一请求重复提交只会返回既有任务
- 供应商的产物链接通常只有 24 小时有效期，因此提供「按任务号补拉」与后台自动重试，
  补回后按网关回执补记成本

### 8.2 任务：租约队列

- 抢占用条件 UPDATE，抢到才执行，避免重复提交（视频重复提交就是重复扣费）
- 失败按指数退避重试；**已经提交到供应商的失败不再自动重试**，避免重复下单
- 取消是协作式的：批处理循环逐个检查取消信号，已产出的素材保留
- worker 定期扫描异常租约，避免进程崩溃后任务永久 RUNNING

### 8.3 实时：SSE 而非轮询

事件落库后按自增 id 推送，客户端带 `last_event_id` 断线续传。
前端收到事件后重载「分镜表 + 任务列表 + 总览」，而不只是刷新一个进度条——
只刷进度不刷数据，就会出现「任务成功但页面还是旧内容」。

### 8.4 媒体：一次写入，永不覆盖

供应商产物文件名常从 `xxx_1.png` 重新计数，直接当存储键会覆盖上一批文件。
存储层使用独占创建 + 随机后缀 + 撞名重试，并保留 sha256 完整性校验脚本。

### 8.5 一致性：角色与音色

- **画面**：角色主图作为参考图随请求发送；本地素材先上传换公网地址（网关不认相对路径）
- **音色**：部分 TTS 供应商不认 `voice` / `speed` 参数，改由指令前缀控制音色、本地 FFmpeg 变速；
  音色配方（供应商 + 音色 + 语速 + 指令）算成指纹写入素材，参数一变旧音频立即被识别为过期

---

## 9. 测试与质量

```powershell
cd backend && python -m pytest                    # 408 项
cd frontend && npm test                           # 150+ 项
cd frontend && npx vue-tsc --noEmit               # 模板级类型检查
```

覆盖重点：

- 成本：预留 / 结算 / 释放的幂等与对账，重复执行不重复扣费
- 任务：取消、重试、租约回收、结果序列化失败后的状态一致性
- 媒体：存储键唯一性、Range 请求、签名地址过期
- 音频：音色配方漂移识别、逐句重跑不串位、字幕时间轴对齐
- 渲染：部分预览（只合成已就绪镜头）、时间线校验可定位到镜号
- 供应商：轮询容错、错误码归一、上传失败不静默降级

---

## 10. 部署

- 单机：`docker compose up -d --build`（前端 Nginx 静态资源 + 反代，后端单进程含内嵌 worker）
- 扩容：`--profile workers` 起独立 worker；`MAX_CONCURRENT_JOBS` 控制并发
- 存储：默认本地磁盘；切对象存储改 `STORAGE_BACKEND=s3`
- 数据库：默认 SQLite（WAL）；并发写入增多时切 PostgreSQL，`DATABASE_URL` 一行生效
- 备份：`backend/scripts/backup_db.py` 与启动时快照，保留最近 7 份

---

## 11. 文档

| 文档 | 内容 |
|---|---|
| [`docs/项目说明.md`](docs/项目说明.md) | 项目定位与整体说明 |
| [`docs/实现说明.md`](docs/实现说明.md) | 技术选型、系统结构、关键决策与边界 |
| [`docs/API.md`](docs/API.md) | 后端接口契约（前后端唯一事实来源） |
| [`docs/功能模块说明.md`](docs/功能模块说明.md) | 12 个功能模块的职责、代码位置与成熟度 |
| [`docs/核心模块实现流程.md`](docs/核心模块实现流程.md) | 四个核心模块的业务流程与技术栈作用 |
| [`docs/技术栈说明.md`](docs/技术栈说明.md) | 每项技术在项目中的实际职责 |
| [`docs/优化路线图.md`](docs/优化路线图.md) | 按优先级排列的演进方向 |
| [`PRD-智能体漫剧平台-全新方案.md`](PRD-智能体漫剧平台-全新方案.md) | 产品需求 |

---

## 12. 合规与许可

- 导出产物一律附带 AIGC 标识（画面水印 + 文件元数据）与权利声明 `rights.json`
- 用户上传的任何素材必须声明使用权，未声明的素材不会进入导出包
- 内容安全检查命中时禁止确认与导出，申诉经人工批准后放行
- 项目代码仅供学习与个人使用；生成内容的版权与合规责任由使用者承担
