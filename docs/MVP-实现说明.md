# hoyo漫剧 MVP · 实现说明

> 对应需求：`漫剧mvp.md`（MVP 需求文档 V0.1）
> 对应接口：`docs/API.md`（后端 API 契约 v1）
> 对应代码：`backend/`（FastAPI）、`frontend/`（Vue 3）

本文说明 MVP 的**技术选型、系统结构、关键设计决策与已知边界**，供后续迭代与验收对照。

---

## 1. 技术选型与理由

| 层 | 选型 | 理由 |
|----|------|------|
| Web 框架 | FastAPI | 原生 async、Pydantic v2 校验、自动 OpenAPI（`/docs` 可直接联调） |
| 数据校验 | Pydantic v2 | 需求文档里的 JSON Schema 直接映射为模型，`SCHEMA_VALIDATION_FAILED` 由它统一抛出 |
| ORM | SQLAlchemy 2.0（`Mapped[...]` 风格） | 类型友好；`UTCDateTime` 统一时区，避免 SQLite naive datetime 比较报错 |
| 数据库 | SQLite（MVP 默认） | 零运维、单文件，个人创作者场景足够；`DATABASE_URL` 可平滑切 PostgreSQL |
| 工作流 | LangGraph + SQLite checkpointer | 需求要求"人工确认门控 + 断点续跑"，`interrupt()` + checkpoint 是原生能力 |
| 队列 | 数据库租约队列（默认）/ Redis（可选） | MVP 不引入额外中间件；租约 + 心跳保证进程崩溃后任务可被重新领取 |
| 对象存储 | 本地磁盘（默认）/ S3 兼容（可选） | 本地零依赖；`storage.py` 统一 key 生成与签名 URL，切换后端不改业务代码 |
| 媒体处理 | FFmpeg（`imageio-ffmpeg` 自带二进制） | 转码、拼接、字幕烧录、混音、抽帧一站式；无需用户单独安装 |
| 前端 | Vue 3 + TS + Pinia + Vue Router + Vite | 组合式 API 心智负担低；Pinia 管工作流状态；Vite 构建快 |

---

## 2. 系统结构

```
┌──────────────┐   REST / SSE   ┌───────────────────────────────┐
│  Vue 3 前端   │ ─────────────► │  FastAPI（app/main.py）        │
│  工作台       │ ◄───────────── │  /api/v1/**                   │
└──────────────┘   event_log    └───────────┬───────────────────┘
                                            │
                    ┌───────────────────────┼────────────────────────┐
                    ▼                       ▼                        ▼
            ┌───────────────┐      ┌────────────────┐      ┌─────────────────┐
            │ 工作流引擎     │      │ 任务队列/Worker │      │ 服务层           │
            │ LangGraph      │      │ 租约 + 线程池   │      │ 事件/存储/成本/  │
            │ 6 个人工门控    │      │ 并发 3         │      │ 渲染/媒体/安全   │
            └───────┬───────┘      └───────┬────────┘      └────────┬────────┘
                    │                      │                        │
                    └──────────┬───────────┴────────────┬───────────┘
                               ▼                        ▼
                      ┌─────────────────┐      ┌──────────────────┐
                      │ 8 个智能体       │      │ 供应商注册表      │
                      │ agents/*        │─────►│ Mock/Ark/OpenAI/ │
                      └─────────────────┘      │ Kling/MiniMax    │
                                               └──────────────────┘
                               ▼
                      ┌─────────────────────────────────┐
                      │ SQLite + 本地存储 + FFmpeg       │
                      └─────────────────────────────────┘
```

### 2.1 两条执行路径，同一套处理器

| 路径 | 触发 | 机制 |
|------|------|------|
| **内联执行** | 工作流节点内部 | 节点创建 `Job` 后直接调用 `executor.run_job_inline()`，同步拿结果写回 state |
| **队列执行** | API 直接触发（如重试、单独重新生成） | 写入 `jobs` 表 → worker 用租约领取 → 线程池执行 |

两条路径最终都走 `executor.run_job()`，因此**成本结算、状态流转、事件发布、供应商调用记录的行为完全一致**。

---

## 3. 工作流与门控

### 3.1 节点序列

```
initialize
 → safety_check_input
 → story_agent → validate_script
 → [门控① 剧本确认]
 → character_agent
 → [门控② 角色/画风确认]
 → shot_agent → image_generation
 → [门控③ 分镜图确认]
 → cost_estimate
 → [门控④ 视频成本确认]
 → video_generation
 → [门控⑤ 视频确认]
 → audio_agent → audio_generation
 → edit_agent → render_preview
 → [门控⑥ 导出确认]
 → export → completed
```

### 3.2 门控实现要点

- 门控节点调用 `interrupt(payload)` 暂停图执行，LangGraph 把 state 写入 SQLite checkpoint。
- **门控节点在 `interrupt()` 之前必须无副作用**——因为恢复时节点体会被重新执行一遍。校验逻辑一律放在 `interrupt()` 之后。
- 恢复时由 `POST /api/v1/episodes/{id}/workflow/resume` 携带 `checkpoint_id` + `decision`（`confirm` / `reject` / `skip` / `regenerate`）与可选修改内容，从断点继续，**不重跑已完成节点**。
- `checkpoint_id` 过期或与当前 run 不匹配 → `CHECKPOINT_STALE`。

### 3.3 降级

`ENABLE_LANGGRAPH=false` 时使用 `app/workflow/fallback.py` 中的等价状态机，节点顺序、门控语义与事件发布保持一致，便于排查 LangGraph 相关问题。

---

## 4. 8 个智能体

| 智能体 | 文件 | 输入 → 输出 |
|--------|------|-------------|
| StoryAgent | `agents/story.py` | 故事文本 → `story_json`（StoryBible + BeatSheet + ShotScript） |
| CharacterAgent | `agents/character.py` | 剧本角色 → `CharacterAsset`（外观、主参考图、版本） |
| ShotAgent | `agents/shot.py` | 剧本分镜 → `Shot` 行 + 角色引用关系 |
| ImageAgent | `agents/image.py` | 分镜 + 角色参考图 → 多候选 `MediaAsset`（图） |
| VideoAgent | `agents/video.py` | 确认图 + 运动参数 → 视频片段 `MediaAsset` |
| AudioAgent | `agents/audio.py` | 台词 → TTS 音频 + BGM + `SubtitleCue` |
| EditAgent | `agents/edit.py` | 时间线 → 预览/成片 MP4 + 素材包 ZIP |
| SafetyAgent | `agents/safety.py` | 文本/媒体 → 安全检查结果 + AIGC 标识 |

**角色一致性**：`character_agent` 为每个主要角色生成主参考图（`primary_media_id`），`image_agent` 生成分镜图时把参考图作为 `reference_media_ids` 传入供应商；缺失参考图时 `ensure_character_refs()` 返回 `MISSING_REFERENCE` 并阻断（除非显式 `allow_missing_reference`）。

---

## 5. 任务队列与"不重复计费"

### 5.1 租约模型

```sql
-- 领取：条件更新，天然互斥
UPDATE jobs SET status='RUNNING', worker_id=?, lease_expires_at=?
WHERE id=? AND status IN ('CREATED','QUEUED','RETRYING');
```

- worker 定期 `reap_expired_leases()`，把 `lease_expires_at` 过期且仍 `RUNNING` 的任务重置为 `RETRYING`，实现**进程崩溃后自动恢复**。
- `Worker.tick()` 先在**同一个事务内**领取全部可执行任务并提交，之后才提交线程池执行——避免 `_execute` 读到未提交的 `QUEUED` 状态。

### 5.2 幂等与重试

| 场景 | 行为 |
|------|------|
| 相同 `idempotency_key` 重复提交 | 返回原任务，附 `IDEMPOTENCY_REPLAY` 提示，**不新建、不重复计费** |
| 用户连点生成按钮 | 生成类接口的幂等键带一个时间桶（`IDEMPOTENCY_WINDOW_SECONDS`，默认 5 秒）：同一窗口内的重复请求命中同一个任务；窗口之外视为一次新的生成，可正常重新生成 |
| 自动重试（FR-072 / §10.2） | 可重试错误（`PROVIDER_TIMEOUT`/`PROVIDER_ERROR`/`STORAGE_ERROR`/`RENDER_ERROR`/`INTERNAL_ERROR`）且 `attempt < max_attempts` 时，**同一条任务记录**置 `RETRYING`、`attempt + 1`、写入 `next_attempt_at`（退避 `attempt × RETRY_BACKOFF_SECONDS`），到点后重新入队执行；本轮错误信息保留在 `error_code/error_message/error_detail` 供排查。次数用尽或不可重试才置 `FAILED`。配额预留跨尝试保持，仅在最终终结时释放 |
| 手动重试 | `POST /jobs/{id}/retry`；**仅允许终态任务**，执行中返回 `STATE_CONFLICT`；新建 `{base}#retry{n}`，`parent_job_id` 指向原任务，不覆盖原始错误 |
| 任务过期（FR-070） | 非终态（`CREATED`/`QUEUED`/`RETRYING`）超过 `JOB_EXPIRE_SECONDS` 仍未完成 → `EXPIRED`，worker 每 30 秒扫描一次 |
| 默认重试上限 | `create_job` 未显式传 `max_attempts` 时取 `1 + AUTO_RETRY_LIMIT`（默认 2，即 1 次自动重试） |
| 取消 | 排队中 → 释放全部预留并置 `CANCELLED`；执行中 → 置 `CANCELLING`，在下一个检查点停止 |

时间桶由 `jobs_service.build_windowed_key(...)` 统一生成，所有生成类路由共用同一实现。

### 5.3 成本台账

三条流水，全部为整数分：

```
RESERVE(+cents)   任务开始时预留
SETTLE(+actual)   成功后按实际结算
RELEASE(-cents)   失败/取消时释放预留
```

- `used_cents = Σ SETTLE`
- `reserved_cents = Σ RESERVE + Σ RELEASE`（RELEASE 为负）
- 结算时**释放整笔预留**并把 `job.cost_reserved_cents` 归零，保证 `reserved` 不会残留（这正是 `test_worker_restart_no_double_billing` 校验的点）。
- 超出 `QUOTA_LIMIT_CENTS` → `QUOTA_EXCEEDED`，任务不进入执行。

---

## 6. 事件流（SSE）

- 事件统一落库 `event_log`（自增 `id`），API 与 worker 即使分属不同进程也能对齐游标。
- `GET /api/v1/episodes/{id}/events` 支持 `Last-Event-ID` 请求头或 `?last_event_id=`，服务端从该游标之后回放并继续推送。
- 实现基于**轮询 event_log 表**而非进程内队列，因此多进程/多实例部署天然可用。
- 事件类型：`job.created`、`job.status_changed`、`job.progress`、`job.succeeded`、`job.failed`、`job.cancelled`、`workflow.gate_waiting`、`workflow.resumed`、`workflow.node_changed`、`episode.stage_changed`、`shot.confirmed`、`media.created`、`render.progress`、`heartbeat`。
- 前端 SSE 断开时回退到轮询 `GET /jobs/{id}`，保证进度不丢。

---

## 7. 媒体与渲染

### 7.1 供应商适配

统一协议 `app/providers/base.py`：`TextRequest / ImageRequest / VideoRequest / SpeechRequest` → `ProviderResult`。

- **Mock 供应商生成真实文件**（这是让闭环可离线验证的关键）：
  - 图像：Pillow 绘制 PNG（含分镜号、构图示意）
  - 视频：FFmpeg `zoompan` 生成真实 MP4
  - 语音：FFmpeg `aevalsrc` 生成真实 MP3
- **回退策略**：`registry.resolve()` 发现主供应商未配置密钥时自动回退 Mock，并在日志中标注；因此 `.env` 全空也能跑完整闭环。
- **调用记录**：`services/provider_log.py` 用线程局部收集器 + 透明代理记录每次供应商调用的延迟、用量、成本、request_id，任务结束后写入 `provider_calls`，供任务详情与审计查看。

### 7.2 渲染管线

`services/render.py` 用**单条 FFmpeg 命令**完成拼接 + 字幕 + 混音：

1. 每个分镜视频片段作为 `-i` 输入；
2. 台词音频逐个 `-i` 输入（**必须真的加进 `inputs` 列表**，否则报 `Invalid file index N`）；
3. `anullsrc` 作为静音底座，保证输出时长与视频一致；
4. `drawtext` 烧录字幕与 AIGC 标识（正式成片不可关闭）；
5. 预览用 `-preset veryfast -crf 30`，成片用 `-preset medium -crf 21`。

**FFmpeg 转义是这里最容易踩的坑**：filtergraph 有"两级解析"（filtergraph 分隔符 → filter 参数分隔符），普通引号保护不了 `:` 和 `,`。最终方案：

- 中文字幕文本先做**全角化替换**（`:`→`：`、`,`→`，`、`'`→`’` 等），彻底规避解析歧义；
- `text=` / `fontfile=` **不加引号**；
- Windows 字体路径 `C:\Windows\Fonts\msyh.ttc` 写成 `C\\:/Windows/Fonts/msyh.ttc`（冒号前**两个**反斜杠）；
- `enable=between(t\,start\,end)` 中的逗号用**一个**反斜杠。

分辨率预设：`preview` 540×960、`720p_9x16` 720×1280、`1080p_9x16` 1080×1920。

### 7.3 降级不阻断交付

- TTS 失败：`audio_agent` 记录 warning，流程继续，字幕仍可用。
- 渲染失败：不影响素材包 ZIP 的生成与下载（需求 §10.2），成片可在修复后重试导出。

---

## 8. 存储与导出包

### 8.1 存储

- key 由 `build_key(*parts, filename=...)` 生成，**所有路径片段与文件名都做 ASCII 清洗**（中文文件名会污染 key 并引发 HTTP 头编码问题）。
- 中文原始文件名通过 `Content-Disposition` 的 RFC 5987 形式返回：`inline; filename="<ascii>"; filename*=UTF-8''<percent-encoded>`。
- 支持签名 URL（`SIGNED_URL_TTL_SECONDS`，默认 30 分钟）。

### 8.2 素材包结构

```
manifest.json          素材清单（每个素材的 sha256 / 尺寸 / 生成参数 / 安全检查结果）
script.json            剧本 JSON（含版本）
rights.json            权利与生成信息声明（含 AIGC 标识）
subtitles/episode.srt
subtitles/episode.vtt
images/shot_{no}_v{ver}_{id}.png
videos/shot_{no}_{id}.mp4
audio/shot_{no}_dialogue_{id}.mp3
audio/{bgm|sfx}_{id}.mp3
exports/{kind}_{preset}_{id}.mp4
README.txt             包内说明
```

`ExportRecord.kind` 取值：`preview`（预览）/ `final`（正式成片）/ `package`（素材包）。

---

## 9. 前端工作台

- 路由：`/`、`/projects/:projectId`、`/episodes/:episodeId` 及子路由 `story|characters|shots|video|audio|render`、`/jobs`、`/settings`。
- 状态：Pinia 管理 `project` / `episode(workflow)` / `jobs` / `config` / `toast`。
- **限制不写死**：候选数上下限、分镜时长上下限、剧集时长上限、码率、分辨率预设全部来自 `GET /api/v1/config/limits`。
- 错误处理：统一解析 `{"error": {...}}`，展示 `message` 与 `trace_id`；`retryable` 为真时提供"重试"按钮。
- 视觉：深色创作工具风格，左侧导航 + 剧集信息条 + 主内容区；分镜/视频用 9:16 竖屏候选网格；工作流用横向节点进度条标出当前门控。
- 启动入口：剧集工作台右上角在**尚未创建 WorkflowRun**时显示「启动工作流」，点击后后端自动跑到第一个门控；门控等待时同一位置变为「去确认」并跳到对应阶段页。

---

## 10. 测试与验收

`backend/tests/test_acceptance.py` 覆盖 MVP 闭环的 9 个关键路径：

| 测试 | 覆盖点 |
|------|--------|
| `test_full_mvp_loop` | 故事 → 6 个门控 → 预览渲染 → 成片导出 → 素材包下载 |
| `test_worker_restart_no_double_billing` | 幂等窗口内连点命中同一任务、不重复计费/不重复出图 |
| `test_idempotency_window_buckets` | 幂等键时间桶：同桶复用、跨桶新建 |
| `test_checkpoint_stale_rejected` | 伪造/过期 `checkpoint_id` 被拒（`CHECKPOINT_STALE`） |
| `test_failure_retry_and_recovery` | 供应商超时 → 失败可查 → 重试生成 `#retry1` |
| `test_cancel_job` | 取消排队任务释放配额 |
| `test_script_validation_blocks_bad_input` | 剧本校验拦截非法分镜 |
| `test_safety_check` | 风险内容拦截 |
| `test_events_sse_history` | 事件流历史连续且有序 |

运行：

```powershell
cd backend
python -m pytest            # 9 passed
```

测试隔离：临时数据根目录放在 `<repo>/.tmp/pytest-<hex>`（`%TEMP%` 下的目录权限为 0o700，FFmpeg 会 `Permission denied`）。

除单元/集成测试外，`backend/scripts/` 还提供三个**打真实 uvicorn 进程**的冒烟脚本（需先启动后端）：

| 脚本 | 验证内容 |
|------|----------|
| `smoke_http.py` | 完整闭环：6 门控 → 预览 → 成片 → 素材包下载与 manifest 校验 |
| `smoke_sse.py` | SSE 实时推送 + `Last-Event-ID` 断线续传 |
| `check_session_commit.py` | 事务提交时序回归（响应可见性） |
| `ui_smoke.py` | 真实浏览器 UI 全流程（需外部提供 CDP 端口） |

---

## 11. 已知边界与后续迭代

| 项 | 现状 | 下一步 |
|----|------|--------|
| 鉴权 | 单用户 `usr_local`（MVP 定位） | 接入登录与多租户工作区 |
| 队列 | 数据库租约队列 | 量大时切 Redis / Celery，接口不变 |
| 字幕 | `drawtext` 逐条烧录 | 换 `ass` 复杂排版、支持字体样式与多语言 |
| 图像一致性 | 单张主参考图 | 引入 IP-Adapter / LoRA 强化角色一致性 |
| 成本 | 内置价格表估算 | 接供应商真实计费回执，做对账报表 |
| 前端 | 单机工作台 | 协作、评论、版本对比 |
| 可观测 | `provider_calls` + `audit_logs` + `event_log` | 接 OpenTelemetry / 指标看板 |

---

## 12. 联调中修掉的两个隐患（务必保留）

### 12.1 请求级会话必须在响应字节发出前提交

`get_session` 原本是 `yield` 依赖，提交发生在响应发出**之后**；客户端拿到 `201 Created`
就立刻发下一个请求时，可能读到尚未提交的数据。实测「`POST /projects` → 立刻
`POST /projects/{id}/episodes`」12 次里有 7 次返回 `404 NOT_FOUND`。

现在 `get_session` 只负责创建并把 Session 挂到 `request.state.db_session`，
由 `app.main.RequestSessionMiddleware` 拦截 `http.response.start`（此刻路由已执行完、
字节还没发给客户端）统一 `commit`；状态码 ≥400 或异常路径则 `rollback`。
回归脚本：连续 12 次「创建项目 → 立刻建剧集」必须 0 失败。

### 12.2 工作流响应体统一为扁平 WorkflowRun

`GET /episodes/{id}/workflow` 曾返回 `{"workflow": ..., "graph": [...]}`，
与 `docs/API.md §9`（返回 run 本身）不一致，前端把整个包装对象当成 run，
导致 `workflow` 恒为真值、「启动工作流」入口永不出现、`current_node` 取不到。
现在 `GET /workflow`、`POST /workflow/{start,resume,retry,rollback}` 一律返回扁平
run（未启动时为 `null`）；节点清单只走 `GET /workflow/graph`。

### 12.3 前端 SSE 自引用会触发 TDZ

`const sse = useSse({ url: () => xxx(sse.lastEventId.value) })` 在 `immediate` 为真时
会同步调用 `url()`，抛 `ReferenceError: Cannot access 'sse' before initialization`，
整个视图白屏。`useSse` 的 `url` 现改为 `(lastEventId) => string | null`，游标由内部注入。
