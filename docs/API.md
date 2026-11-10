# hoyo漫剧 MVP — 后端 API 契约（v1）

所有接口前缀 `/api/v1`。除 `/health`、`/media/{id}/content` 外，请求与响应均为 JSON（`application/json; charset=utf-8`）。
时间字段统一 ISO-8601 UTC 字符串（如 `2026-01-01T08:00:00Z`）。金额统一为**最小货币单位整数**（分，字段名以 `_cents` 结尾）。

## 0. 通用约定

### 0.1 统一错误体

```json
{
  "error": {
    "code": "SCHEMA_VALIDATION_FAILED",
    "message": "剧本校验失败：shots[2].duration_ms 超出上限",
    "details": { "loc": ["shots", 2, "duration_ms"] },
    "retryable": false,
    "trace_id": "..."
  }
}
```

错误码：`VALIDATION_ERROR`、`NOT_FOUND`、`CONFLICT`、`STATE_CONFLICT`、`IDEMPOTENCY_REPLAY`、
`PROVIDER_ERROR`、`PROVIDER_TIMEOUT`、`SCHEMA_VALIDATION_FAILED`、`SAFETY_BLOCKED`、
`QUOTA_EXCEEDED`、`STORAGE_ERROR`、`RENDER_ERROR`、`CHECKPOINT_STALE`、`INTERNAL_ERROR`。

### 0.2 任务状态枚举（Job）

`CREATED` `QUEUED` `RUNNING` `WAITING_CONFIRMATION` `SUCCEEDED` `FAILED` `RETRYING` `CANCELLING` `CANCELLED` `EXPIRED`

### 0.3 剧集阶段枚举（Episode.stage）

`DRAFT` `SCRIPT_REVIEW` `ASSET_REVIEW` `SHOT_REVIEW` `VIDEO_REVIEW` `AUDIO_REVIEW` `RENDERING` `PREVIEW_READY` `EXPORTED` `BLOCKED`

### 0.4 任务对象

```json
{
  "id": "job_...",
  "workflow_run_id": "wfr_...",
  "episode_id": "epi_...",
  "type": "IMAGE_GENERATION",
  "status": "RUNNING",
  "stage": "image_generation",
  "progress": 40,
  "attempt": 1,
  "max_attempts": 2,
  "idempotency_key": "...",
  "input_summary": { "shot_no": 3, "candidates": 2 },
  "provider": "mock",
  "model": "mock-image-v1",
  "cost_reserved_cents": 20,
  "cost_settled_cents": 0,
  "error_code": null,
  "error_message": null,
  "parent_job_id": null,
  "result_ref": { "media_ids": ["med_..."] },
  "created_at": "...",
  "started_at": "...",
  "finished_at": null,
  "duration_ms": null,
  "cancelable": true
}
```

## 1. 健康与配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 返回 `{status, version, db, storage, ffmpeg, queue, providers, graph_engine}` |
| GET | `/config/limits` | 配额与限制（前端不得写死）：`{max_concurrent_jobs, candidates_per_shot_default, candidates_per_shot_max, shot_duration_ms_min, shot_duration_ms_max, episode_duration_ms_max, preview_bitrate_kbps, export_presets, aspect_ratios, default_language}` |
| GET | `/config/providers` | 供应商清单与能力：`[{kind:"text", id:"mock", display_name, models:[...], capabilities:{}}]` |

## 2. 项目

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects` | body `{name, genre, aspect_ratio, default_language, style?}`；名称工作区内唯一，冲突返回 `CONFLICT` |
| GET | `/projects` | 列表，支持 `?include_archived=` |
| GET | `/projects/{project_id}` | 详情 |
| PATCH | `/projects/{project_id}` | 局部更新 |
| POST | `/projects/{project_id}/archive` | 归档 |
| POST | `/projects/{project_id}/restore` | 恢复 |
| GET | `/projects/{project_id}/cost` | 成本汇总 `{total_cents, by_stage:[{stage, cents}], by_kind:[{kind, cents}], remaining_quota_cents}` |
| GET | `/projects/{project_id}/audit` | 审计日志（删除/导出/配额/供应商变更） |

## 3. 剧集

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects/{project_id}/episodes` | body `{title, episode_no?, target_duration_ms}` |
| GET | `/projects/{project_id}/episodes` | 列表 |
| GET | `/episodes/{episode_id}` | 详情，含 `stage`、`stale_flags` |
| PATCH | `/episodes/{episode_id}` | 更新标题/目标时长 |
| POST | `/episodes/{episode_id}/copy` | 复制剧集（复制剧本与资产引用，不复制进行中任务），body `{title?}` |
| POST | `/episodes/{episode_id}/archive` / `/restore` | 归档 / 恢复（只改状态，数据全留，可恢复） |
| DELETE | `/episodes/{episode_id}` | **彻底删除**（不可恢复）：剧本、分镜、角色卡、音轨、字幕、媒体文件、任务、工作流运行、审批与申诉一并删除；成本流水与审计日志保留。有进行中的任务时返回 409 |
| GET | `/episodes/{episode_id}/cost` | 阶段成本 |
| GET | `/episodes/{episode_id}/overview` | 工作台总览：阶段、门控、任务统计、资产计数、下游过期标记 |

## 4. 故事与剧本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/episodes/{episode_id}/story` | 保存故事输入 `{input_type: "idea"|"text"|"manual", source_text}`；长度 ≤ 2000 字 |
| GET | `/episodes/{episode_id}/story` | 最新故事文档 |
| GET | `/episodes/{episode_id}/story/versions` | 版本列表 |
| POST | `/episodes/{episode_id}/story/generate` | 触发 StoryAgent（异步 job，type `STORY_GENERATION`）→ 返回 Job |
| POST | `/episodes/{episode_id}/story/analyze` | FR-011 故事分析（同步）→ `{analysis, story}`；模型不可用时按现有剧本兜底派生 |
| GET | `/episodes/{episode_id}/script` | 当前剧本（结构化 JSON + version） |
| PUT | `/episodes/{episode_id}/script` | 保存人工编辑，body `{story_json, base_version}`；Pydantic 校验失败返回 `SCHEMA_VALIDATION_FAILED` |
| POST | `/episodes/{episode_id}/script/validate` | 仅校验，body `{story_json}` → `{valid, errors:[{loc, msg}], warnings:[]}` |
| POST | `/episodes/{episode_id}/shots/regenerate` | 局部重生成 `{shot_nos:[1,2], replan_downstream:false, instruction?}` |
| POST | `/episodes/{episode_id}/shots/{shot_no}/suggest` | 按大纲为**单个分镜**生成可编辑草稿（body `{instruction?}`，**同步**返回 `{shot_no, suggestion:{action,dialogue,composition,camera_movement,duration_ms,characters,image_prompt,...}, cost_cents}`）。不写库；前端弹窗里改完再保存剧本，适合「这一镜不够贴剧情，让 AI 先给一版」 |
| POST | `/shots/{shot_id}/suggest` | 同上，但以**分镜画面页正在编辑的 Shot 行**为准（剧情上下文仍取自最新剧本）。分镜画面页的「AI 按大纲生成」用它，结果只填编辑区，需手动「保存镜头」 |

### 4.1 剧本 JSON（StoryBible + BeatSheet + ShotScript）

```json
{
  "story_bible": {
    "logline": "…", "theme": "…", "tone": "…", "setting": "…",
    "characters": [{"name":"…","identity":"…","age_range":"…","appearance":"…","outfit":"…","personality":"…","speech_style":"…","forbidden_changes":["…"]}],
    "scenes": [{"scene_no":1,"location":"…","time_of_day":"黄昏","summary":"…"}],
    "conflict": {"goal":"…","obstacle":"…","stakes":"…"},
    "relationships": [{"from":"…","to":"…","relation":"…"}]
  },
  "beat_sheet": [{"beat_no":1,"name":"开场","summary":"…","scene_no":1,"shots":[1,2]}],
  "shots": [
    {
      "shot_no": 1, "duration_ms": 4000, "scene_no": 1, "characters": ["林晚"],
      "action": "…", "camera_movement": "推", "composition": "中景", "emotion": "紧张",
      "dialogue": [{"character":"林晚","text":"…","emotion":"压低声音"}],
      "narration": "…", "sfx": ["雨声"],
      "image_prompt": "…", "negative_prompt": "…",
      "video_params": {"duration_ms": 4000, "motion_strength": 0.4, "camera_movement": "推"},
      "required": true
    }
  ]
}
```

枚举：`camera_movement ∈ {固定, 推, 拉, 摇, 移, 跟, 升降}`，`composition ∈ {特写, 近景, 中景, 全景, 远景, 过肩, 俯拍, 仰拍}`。

## 5. 角色与画风

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/projects/{project_id}/characters` | 角色卡 CRUD。GET 默认**不含**已归档角色（`DELETE` 是软归档）；`?include_archived=true` 看历史角色。**按剧集收敛**：`?episode_id=epi_x` 只返回「本集专属卡 + 项目级公共卡（`episode_id` 为空，且未被本集同名卡遮蔽）」，响应 `{items, total, hidden}`，`hidden` 是被挡掉的其他剧集角色数；`include_all=true` 看全项目。POST body 可带 `episode_id`（不带即项目级公共角色），唯一性按 `(episode_id, name)` 判定 |
| GET/PATCH/DELETE | `/characters/{character_id}` | 详情 / 更新 / 归档。PATCH 为**局部更新**：未提供的字段一律不动（`CharacterUpdate` 全 `None` 默认）；DELETE 是软归档（`status=ARCHIVED`）。更新会写入 `user_edited=true`，之后的剧本同步不再覆盖这些字段 |
| POST | `/episodes/{episode_id}/characters/sync` | 从该集**最新剧本的人物表**抓取角色，生成/补全**属于这一集**的角色卡（幂等）。同名角色在不同剧集各存一张，互不覆盖。query `force=true` 才用人物表整体覆盖手改内容；默认只补空字段。返回 `{episode_id, script_version, created, updated, kept, total, names, items}`。剧本生成任务结束时后端已自动调用过一次，此接口供「改完剧本重新抓」用 |
| POST | `/characters/{character_id}/generate` | body `{asset_kinds:["setting","front","half_body","expression"], count_per_kind}` → Job |
| POST | `/characters/{character_id}/confirm` | body `{media_id}` 确认主参考图 |
| POST | `/characters/{character_id}/replace-primary` | body `{media_id, impact:"future_only"|"mark_stale"}`，返回受影响分镜列表 |
| GET | `/characters/{character_id}/assets` | 角色资产（含版本、模型、提示词、参数） |
| GET/POST | `/projects/{project_id}/styles` | 画风资产 CRUD。GET 每条带 `is_default`（对比 `Project.default_style_id`）；项目**自带**一个默认画风，前端不能假设列表首条是自己新建的 |
| PATCH | `/styles/{style_id}` | 局部更新画风：未提供的字段一律不动（body 用 `StyleUpdate`，全 `None` 默认；用创建用的 `StyleIn` 会把 `name` 重置成「默认画风」并清空 `palette`）。有实际改动才递增 `version` |
| POST | `/styles/{style_id}/apply` | 应用到项目（设为 `default_style_id`），并把该项目已有分镜标记过期。返回 `{project_id, style_id, affected_shots}` —— **不是** StyleAsset |

## 6. 分镜与画面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/episodes/{episode_id}/shots` | 分镜表（含候选图、确认状态、过期标记、视频/音频状态） |
| GET/PATCH | `/shots/{shot_id}` | 单镜详情 / 编辑 `{duration_ms?, dialogue?, characters?, composition?, image_prompt?, negative_prompt?, ...}` |
| POST | `/episodes/{episode_id}/shots/generate-images` | body `{shot_nos:[1,2], candidates_per_shot:2}` → Job |
| GET | `/shots/{shot_id}/candidates` | 候选图历史 `[{media_id, version, url, status, safety, created_at, rejected_reason}]` |
| POST | `/shots/{shot_id}/select` | body `{media_id}` 选中候选 |
| POST | `/shots/{shot_id}/confirm` | body `{media_id}` 确认画面（写入 approvals） |
| POST | `/shots/{shot_id}/reject` | body `{media_id, reason}` 标记问题 |
| POST | `/shots/{shot_id}/restore-candidate` | body `{media_id}` 恢复历史候选 |
| POST | `/episodes/{episode_id}/shots/confirm-batch` | body `{shot_nos:[...]}` |

## 7. 视频

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/episodes/{episode_id}/video/estimate` | body `{shot_nos:[...], params?}` → `{quote_id, provider, model, estimate_cents, quota_after_cents, estimated_wait_seconds, per_shot:[...], expires_at}` |
| POST | `/episodes/{episode_id}/video/generate` | body `{shot_nos, params?, quote_id, idempotency_key, confirm:true}`；未确认成本 → `CONFLICT` |
| POST | `/shots/{shot_id}/video/confirm` | body `{media_id}` |
| POST | `/shots/{shot_id}/video/manual` | body `{media_id}`：用**已上传的视频**顶替该镜（先走 `POST /media/upload`，`kind=video` 且必须 `rights_declared=true`）。素材归到该镜头名下并直接置 `CONFIRMED`；返回 `duration_ms` 与 `warnings`（片段时长与剧本不一致时提示，合成按剧本时长裁剪）。用于「供应商已生成成功、本地拉取失败」时把片子捞回来，不必再生成一次 |
| POST | `/shots/{shot_id}/video/fetch-provider` | body `{task_id}`：按供应商任务号补拉**已生成但本地没取回**的片段，不提交新任务、不计新费用。成功返回 `{shot_no, video_status, video_media_id, duration_ms, task_id, billed_cents, warnings}`；`billed_cents` 是按网关回执补记的历史成本。上游成片只保留 **24 小时**，过期返回「链接已过期，只能重新生成」。后台 worker 每 60 秒对「失败但已提交」的任务自动试一次（最多 6 次、间隔 5 分钟） |
| POST | `/shots/{shot_id}/video/skip` | body `{reason?, fallback:"static_image"|"blank"}` |
| POST | `/shots/{shot_id}/video/retry` | 新任务，关联原任务 |
| GET | `/shots/{shot_id}/video` | 片段状态与历史 |

## 8. 音频与字幕

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/projects/{project_id}/voices` | 可用音色列表 / 新增（上传或供应商音色） |
| POST | `/characters/{character_id}/voice` | body `{voice_id, speed?}` 绑定音色并设语速（`speed` 0.5~2.0，默认 1.0）；换音色或改语速都会把该角色已有配音标记过期。`GET /voices/{voice_id}/preview` 试听（返回 `{media_id, url}`，按需合成并缓存）。供应商产物下载对 403/404/429/5xx 会做 4 次短退避重试（网关刚返回 URL 时 OSS 偶发 403）；仍失败返回 **502**（带 OSS 错误码），不再伪装成 409「当前状态不允许该操作」。**音色实现方式**：认 `voice` 参数的供应商（MiniMax 等）直接切音色；通义 `qwen-audio-…-tts-next` 实测不认该字段（传任何值都是同一个女声），改由 `style_prompt` 指令前缀（如「用低沉浑厚的成年男声朗读：」）控制，该前缀不会被念出来 |
| GET | `/episodes/{episode_id}/tts/estimate` | 配音预估 + 前置条件：query `shot_nos?`（逗号分隔）、`character_id?`；返回 `{estimate_cents, line_count, skipped_count, can_generate, blocking_problems:[{code:"MISSING_VOICE", character, shot_nos, message}], speaking_characters}` |
| POST | `/episodes/{episode_id}/tts/generate` | body `{shot_nos?, character_id?}` → Job。**FR-050**：若存在「有台词但未绑定音色」的角色，返回 `409 MISSING_VOICE`（`details.blocking_problems` 给出角色与镜号），**不创建任务、不预留配额** |
| GET | `/episodes/{episode_id}/tts` | 台词音频列表（含 `stale` 过期标记、`stale_count`、`skipped_count`、`manual_count`）；每条保留 `text`/`voice_id`/`speed`/`emotion`/`provider`/`source`（FR-051、§12-4） |
| GET | `/episodes/{episode_id}/tts/lines` | **§12-4** 逐句台词状态表：返回 `{items:[{shot_id, shot_no, character, text, emotion, status, source, media_id, track_id, stale, stale_reason}], counts:{total, ready, missing, stale, skipped, manual}}`。`status` ∈ `missing`（还没生成，需要兜底入口）/ `skipped`（有意跳过）/ `stale`（音色语速变更后过期）/ `ready`。前端只在 `missing` 行显示「上传音频 / 跳过」 |
| POST | `/episodes/{episode_id}/tts/manual` | **§12-4** TTS 失败兜底：body `{shot_no, line_index（该镜内第几句，从 0 开始）, media_id}` → 该句音轨。要求素材 `kind == "audio"`，写入 `source="manual"`、`provider="manual"`、`speed=1.0`（用户上传的音频本身就是成品，不套角色语速）。同一句重复上传是**替换**而非并存 |
| POST | `/episodes/{episode_id}/tts/skip` | **§12-4** 跳过配音继续出片：body `{shot_nos:[int], reason?}` → `{skipped_track_ids, counts}`。落成 `source="skipped"` 且 `media_id=null`，与 `missing` 明确区分 |
| GET/POST | `/episodes/{episode_id}/bgm` | BGM/音效挂载：`{media_id, kind:"bgm"|"ambience"|"sfx", start_ms, end_ms, volume_db, fade_in_ms, fade_out_ms, rights_declared}` |
| DELETE | `/episodes/{episode_id}/bgm/{track_id}` | 移除 |
| GET | `/episodes/{episode_id}/bgm/estimate?seconds=30` | 氛围音估价：按**生成秒数**计费（`billing_unit:"second"`），返回 `estimate_cents / unit_price_cents / can_generate`。传 `shot_from`/`shot_to` 时按这段镜头的实际长度估价，`window` 里给出 `{shot_from, shot_to, shot_nos, start_ms, duration_ms}` |
| GET | `/episodes/{episode_id}/bgm/suggest?shot_from=2&shot_to=3` | 按镜头内容拼一句氛围描述草稿（读分镜的 `image_prompt` 场景段 + `sfx` + `emotion`，**不调用模型、不扣费**）：`{prompt, shot_nos, seconds, sources:{scene,sfx,emotion,shots}}` |
| POST | `/episodes/{episode_id}/bgm/generate` | 生成一轨氛围音/BGM（异步任务 `BGM_GENERATION`）：`{prompt, seconds:3~120, shot_no?, shot_from?, shot_to?, volume_db:-14, fade_in_ms, fade_out_ms}`。不传镜头 = 整集垫乐（kind=bgm，从 0 铺）；传 `shot_from`/`shot_to`（`shot_no` 是单镜简写）= 覆盖这段镜头（kind=ambience，起点取首镜、长度铺到末镜结束，可跨多个镜头共用一段）。提示词只写场景/乐器/情绪，**不要写台词**；同位置重复生成是替换，不会叠轨 |
| POST | `/episodes/{episode_id}/subtitles/generate` | 生成字幕（依据确认台词与实际音频） |
| GET | `/episodes/{episode_id}/subtitles` | 字幕条目 JSON |
| GET | `/episodes/{episode_id}/subtitles/export?format=srt|vtt` | 纯文本下载 |

## 9. 工作流与门控

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/episodes/{episode_id}/workflow/start` | 创建 WorkflowRun 并跑到首个门控。body `{auto_run?:true, reuse_script?:bool}`：`reuse_script` 省略时「已有可用剧本（`story_json.shots` 非空）就复用」，不再调用模型重新生成（不额外扣费、不覆盖手改）；显式传 `false` 才重新生成 |
| GET | `/episodes/{episode_id}/workflow` | 当前 run：`{id, status, current_node, checkpoint_id, pending_gate, state, updated_at}` |
| POST | `/episodes/{episode_id}/workflow/resume` | body `{workflow_run_id, checkpoint_id, gate, decision:"approve"|"reject"|"edit", payload?, target_version?}`；版本不一致返回 `CHECKPOINT_STALE` |
| POST | `/episodes/{episode_id}/workflow/retry` | body `{node}` 重跑失败节点 |
| POST | `/episodes/{episode_id}/workflow/rollback` | body `{to_node}` 回退 |
| GET | `/episodes/{episode_id}/workflow/logs` | 关键节点日志（脱敏） |
| GET | `/episodes/{episode_id}/workflow/graph` | 节点清单与状态（供前端画流程条） |

门控标识：`gate_script_confirmation`、`gate_character_confirmation`、`gate_shot_confirmation`、
`gate_video_cost_confirmation`、`gate_video_confirmation`、`gate_export_confirmation`。

## 10. 任务中心

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/jobs` | `?episode_id=&project_id=&status=&type=&page=&page_size=` → `{items, total, page, page_size}` |
| GET | `/jobs/{job_id}` | 详情（输入摘要、耗时、供应商、模型、重试次数、成本、错误码、关联资源） |
| POST | `/jobs/{job_id}/retry` | 新任务并关联原任务 |
| POST | `/jobs/{job_id}/cancel` | 排队可取消；执行中尽力取消并返回 `note` |
| GET | `/jobs/{job_id}/provider-calls` | 供应商调用记录 |

### 10.1 SSE 事件流

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/episodes/{episode_id}/events` | 剧集级事件流，支持 `?last_event_id=` 断线补齐 |
| GET | `/events` | 工作区级事件流 |

事件格式（`text/event-stream`）：

```
id: 1712
event: job.status_changed
data: {"job_id":"job_...","status":"RUNNING","progress":40,"episode_id":"epi_...","ts":"..."}

```

事件类型：`job.created`、`job.status_changed`、`job.progress`、`job.succeeded`、`job.failed`、`job.cancelled`、
`workflow.gate_waiting`、`workflow.resumed`、`workflow.node_changed`、`episode.stage_changed`、
`shot.confirmed`、`media.created`、`render.progress`、`heartbeat`。
断线重连：客户端带 `last_event_id`，服务端从该 id 之后补发；无该参数时补发最近 50 条。

## 11. 媒体与上传

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/media/upload` | multipart `file` + `kind`（`text`/`image`/`audio`/`video`）+ `rights_declared` + `source_note` |
| GET | `/media/{media_id}` | 元数据（含 sha256、尺寸、时长、安全检查结果、AIGC 标识） |
| GET | `/media/{media_id}/url` | 签名 URL（默认 30 分钟有效） |
| GET | `/media/{media_id}/content` | 直接流式返回内容（本地开发用） |
| GET | `/episodes/{episode_id}/media` | 剧集素材列表，支持 `?kind=` |
| GET | `/media/{media_id}/rights` / PUT | 权利声明 |

## 12. 时间线、合成与导出

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/episodes/{episode_id}/timeline` | 时间线（镜头顺序、视频时长、台词音频、字幕、BGM） |
| GET | `/episodes/{episode_id}/timeline/validate` | 校验 → `{valid, errors:[{code, message, shot_no, track}], warnings:[]}` |
| GET | `/episodes/{episode_id}/export-gate` | **§12-8** 导出闸门：`{can_export, blocked:[media_id], unresolved:[media_id], pending, rejected, approved, appeals:[SafetyAppeal], aigc}`。`unresolved` 是「被拦截且未获人工批准」的素材，前端据此禁用导出按钮并展示申诉入口 |
| POST | `/episodes/{episode_id}/appeals` | **§12-8** 提交内容拦截申诉：body `{target_type:"asset"\|"input"\|"episode", target_id?, reason（必填）, detail?}` → 201 `SafetyAppeal`（`status:"PENDING"`）。`target_type="asset"` 时要求该素材确实为 `BLOCKED`（否则 `409`），同一目标只允许一条待审申诉（重复提交 `409`）。**提交本身不解禁** |
| GET | `/episodes/{episode_id}/appeals` | 本集申诉列表 → `{items, pending, approved, rejected}` |
| POST | `/episodes/{episode_id}/render/preview` | 生成低码率预览 MP4 → Job。body `{watermark?, partial?}`：`partial=true` 时只合成**已就绪**镜头（有画面/视频），未就绪镜头从时间线剔除、字幕/BGM 按新起点重映射，产物 manifest 带 `partial:true`；正式导出仍走完整校验。**§12-8**：存在未解决拦截时 `422 SAFETY_BLOCKED`（`details` 为闸门状态） |
| POST | `/episodes/{episode_id}/render/export` | 正式导出 body `{preset:"720p_9x16"\|"1080p_9x16", watermark:false}` → Job。**§12-8**：同上前置拦截 |
| GET | `/episodes/{episode_id}/exports` | 导出记录 |
| GET | `/exports/{export_id}` | 导出详情 `{status, kind, media_id, url, error, manifest}` |
| GET | `/exports/{export_id}/download` | 下载（签名 URL 重定向） |
| POST | `/episodes/{episode_id}/export/package` | 生成 ZIP 素材包 → Job（**不依赖渲染成功**；**§12-8** 同受导出闸门约束） |

> **§12-8 生效位置**：门控审批 `POST /episodes/{episode_id}/workflow/resume`（`decision=approve`）与上面三个导出类接口共用同一闸门。
> 「输出内容被拦截 → 禁止确认和导出」因此落在**确认**（门控审批）与**导出**（预览/正式导出/素材包）两侧，而不是只算一个 `can_export` 标志。

素材包结构：

```
episode_<no>_package.zip
├── manifest.json          # 素材清单 + 生成信息
├── script.json            # 剧本 JSON（含版本）
├── rights.json            # 权利/生成信息
├── subtitles/episode.srt
├── subtitles/episode.vtt
├── images/shot_001_c1.png ...
├── videos/shot_001.mp4 ...
├── audio/shot_001_dialogue.mp3, bgm.mp3 ...
└── README.txt             # AIGC 标识与使用说明
```

## 13. 成本与配额

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/cost/ledger` | `?episode_id=&project_id=&kind=&page=` 成本流水 |
| GET | `/cost/summary` | `?episode_id=` 汇总 |
| GET | `/quota` | `{limit_cents, used_cents, reserved_cents, remaining_cents, concurrent_limit, running_jobs}` + **积分口径** `{limit_credits, used_credits, reserved_credits, remaining_credits, cents_per_credit}`。内部账本仍用「分」，积分只在展示层换算：1 积分 = `CREDIT_USD_VALUE`(默认 $0.005) × `USD_CNY_RATE` ≈ 3.6 分。总配额可用 `QUOTA_LIMIT_CREDITS` 直接按供应商积分填 |

## 14. 供应商设置与安全

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/settings/providers` | 各模态主供应商与配置状态（密钥只回 `configured: true/false` 与 `masked` 前缀） |
| PUT | `/settings/providers` | body `{text:{provider, model, api_key?, base_url?}, image:{...}, video:{...}, speech:{...}}`，密钥服务端加密存储 |
| GET | `/settings/pricing` | 价格版本与单价表 |
| POST | `/safety/check` | body `{text}` → `{allowed, risk_level, categories, suggestions}` |
| GET | `/episodes/{episode_id}/approvals` | 确认记录（操作者、时间、资产版本、候选图 ID） |
| GET | `/episodes/{episode_id}/safety` | 安全检查结果汇总 |
| GET | `/appeals` | **§12-8** 人工复核队列：query `status?`（`PENDING`/`APPROVED`/`REJECTED`）、`episode_id?`、`limit?` → `{items:[SafetyAppeal]}` |
| POST | `/appeals/{appeal_id}/resolve` | **§12-8** 人工复核结论：body `{status:"PENDING"\|"APPROVED"\|"REJECTED", resolution_note?}`。**只有 `APPROVED` 会解除对应素材的导出阻断**（这是申诉入口唯一生效路径）；结论写入审计流水 |

## 15. 前端路由建议

| 路由 | 页面 |
| --- | --- |
| `/workspace` | 工作区（PRD §5.1-1：创作者资料、配额、概览计数、Provider 状态） |
| `/workspace/assets` | 跨项目资产库（只读汇总各项目角色与画风） |
| `/` | 项目列表 |
| `/projects/:projectId` | 项目概览 + 剧集列表 + 成本 + 审计 |
| `/projects/:projectId/assets` | 项目级角色与画风资产库（PRD §5.1-6：建/改/归档角色、生成参考图、画风设为项目默认） |
| `/episodes/:episodeId` | 工作台（左侧阶段导航，右侧阶段面板） |
| `/episodes/:episodeId/story` | 故事输入 + 剧本编辑确认 |
| `/episodes/:episodeId/characters` | 角色与画风（剧集内逐镜确认） |
| `/episodes/:episodeId/shots` | 分镜表 + 候选图确认 |
| `/episodes/:episodeId/video` | 视频成本确认 + 片段确认 |
| `/episodes/:episodeId/audio` | 音色绑定 + TTS + BGM + 字幕 |
| `/episodes/:episodeId/render` | 时间线校验 + 预览 + 导出 + 素材包 + 安全检查/申诉 |
| `/jobs` | 任务中心（SSE 实时） |
| `/cost` | 成本与运行记录 |
| `/settings` | 供应商与配额设置 |
