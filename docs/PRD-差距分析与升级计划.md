# PRD 差距分析与升级计划

**对照文档**：`PRD-智能体漫剧平台-全新方案.md`（V1.0 正式要求）
**审计对象**：hoyo漫剧 MVP（`backend/` + `frontend/`）
**审计方式**：5 组并行审计，逐条需求到代码里取证（每条结论都带 `文件:行号`），共核查 79 个需求条目
**审计日期**：2026-10-12

---

## 0. 总体结论

> 下表是 **审计时（2026-10-12 首次全量取证）** 的快照。此后 P0 的 13 项缺陷已全部修完，
> 「成本与运行记录页」也已补齐；后续每完成一个阶段会更新本节的进度。

| 状态 | 条目数 | 占比 | 含义 |
|------|-------:|-----:|------|
| ✅ done | 37 | 47% | PRD 要求已完整实现 |
| 🟡 partial | 36 | 46% | 主体已实现，存在字段/路径/交互级缺口 |
| ❌ missing | 6 | 8% | 完全没有 |

**一句话结论**：MVP 已经把 PRD 的**功能主链路**做得相当完整——没有一条生产链路是缺失的，
生成也都是真实的（Mock 供应商用 Pillow/FFmpeg 真出图、真出片、真出音，不是占位桩）。
差距集中在三处：**① 若干真实缺陷（含会清空用户数据的一个）；② 非功能要求与降级策略；
③ 前端信息架构（五区布局、缺失页面、自动化验收）**。

### 进度

| 阶段 | 状态 | 说明 |
|------|------|------|
| P0 缺陷修复 | ✅ 完成（13/13） | 见 §1；端到端 HTTP 联调通过 |
| P1 后端能力补全 | ✅ 完成（21/21） | FR-090 / FR-091 / FR-061 / FR-080 / FR-082 / FR-023 / FR-022 / FR-040 / FR-060 / FR-004 / FR-010 / FR-014 / §6.6 / §12-2/3/6/8/9 / §10.1 / §11.1-2 / §11.1-3 / §11.2-2，见 §2 |
| P2 前端信息架构 | ✅ 完成 | §5.2 五区布局、成本与运行记录页、工作区页、**项目级资产库（第六轮）** 全部完成；FR-003 / FR-010 / FR-040 / FR-004 / FR-030 / FR-032 / FR-050 / FR-051 / FR-052 / FR-053 前端入口均已完成；§12-4 上传音频兜底入口、§12-8 申诉入口已完成。**PRD §5.1 十二页已全部落地**，见 §3.1 |
| P3 非功能与验收 | ✅ **完成** | §15.1 端到端含 BGM、§15.2 恢复、§15.3 失败（含 FFmpeg 失败）、§15.4 数据可追溯、§15.5 前端自动化全部落地：`tests/test_acceptance_15.py` 18 例 + `scripts/smoke_http.py` 端到端（含 BGM 混音与素材包）+ Vitest 7 文件 135 例 |

> **本轮已完成（2026-10-13 第七轮，PRD §15 验收最后两处缺口：§15.1 BGM + §15.3 FFmpeg 失败。后端 `pytest` `247 项全绿`，`smoke_http.py` 端到端（含 BGM）全通过，前端 `vitest` `135 例全绿`、`vue-tsc` 零错误）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | §15.1 端到端补 BGM | 原文要求闭环里必须有「一条 BGM」，此前完全没验。`smoke_http.py` 在**门控⑤恢复之前**用 ffmpeg 造一段真实可解码的 MP3（正弦三音叠加 + 淡入淡出）并上传挂载 —— 位置很关键：恢复后才会依次跑 `audio_agent → audio_generation → edit_agent → render_preview`，预览才会把它混进去 | `scripts/smoke_http.py` |
> | §15.1 BGM 四处断言 | ① 出现在 `GET /bgm` 音轨列表；② 进预览混音（`render.manifest.bgm_count == 1`）；③ 进素材包 manifest（`audio` 含 `kind="bgm"` 且带权利声明）与 `timeline.bgm_track`；④ `audio/bgm_*.mp3` **真的在 ZIP 里**。只断言到 API 层会漏掉「记录了但没打进包」 | 同上 |
> | §15.3 真·FFmpeg 失败 | 原 `test_render_error_locates_the_shot` 名不副实：它只测了空时间线的校验失败，**根本没模拟 FFmpeg 失败**。新增 `test_ffmpeg_failure_is_located_and_package_survives`：先造一个「校验必定通过」的最小时间线（1 分镜 + 真实存在的确认图），再只替换 `render` 模块看到的 `subprocess`，注入非 0 退出 | `tests/test_acceptance_15.py` |
> | §15.3 三条要求逐条验 | 对应原文「展示可定位错误、保留原任务记录、支持重试或降级，并保证素材包路径可用」：① `error_code=RENDER_ERROR` 且 `error_detail` 带 `ffmpeg_tail`/`located`/`hint`；② 任务仍可查、`attempt` 有值、`/jobs/{id}/retry` 可调；③ `POST /export/package` 仍 `SUCCEEDED`（§10.2） | 同上 |
> | 失败用例的防假绿 | 断言「时间线校验先通过」再注入故障，否则失败可能来自缺料而非 FFmpeg，测试会假绿。这是本轮最值得保留的习惯 | 同上 |
>
> **连带修正**：`bgm_count` / `dialogue_count` 在 `render["manifest"]` 里，不在 `render` 顶层 —— 第一次断言写错层级，脚本报了 FAIL 而代码是对的。`_locate_error` 返回的是**列表**且永不为空（定位不到时兜底 `unknown` 条目），不能按字典取键。

> **本轮已完成（2026-10-13 第六轮，项目级「角色与画风资产库」+ 两处静默数据丢失修复：后端 `pytest` `246 项全绿`，前端 `vitest` `135 例全绿`，真实 HTTP 核验 `check_project_assets.py` 39 项断言全部通过，`smoke_http.py` 端到端仍全通过）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | §5.1-6 项目级资产库 | 新增 `/projects/:projectId/assets`（`project-assets`）整页：角色卡片（主参考图、音色、语速、「待完善」提示）+ 建/改/归档 + 生成参考图；画风卡片（色板色块、镜头/光线、版本）+ 建/改/设为项目默认。后端接口早已存在，缺的只是这一页 | `views/ProjectAssetsView.vue`、`router/index.ts`、`views/ProjectDetailView.vue` |
> | 纯逻辑与视图分离 | 过滤/统计/校验/标签抽到 `utils/projectAssets.ts`，24 例单测直接钉住「搜索大小写不敏感」「空查询不清空」「语速缺省显示 1.00×」等易退化点，不必挂载整页 | `utils/projectAssets.ts`、`utils/projectAssets.spec.ts` |
> | **修复：`PATCH /styles/{id}` 静默清空字段** | `StyleIn` 是**创建**用的（字段带非 None 默认值），却被直接当成 PATCH body。后端判空是 `value is not None`，于是「只改描述」会把 `name` 重置成「默认画风」、`palette` 清空、`camera_style`/`lighting_style` 归零 —— 静默数据丢失。新增全 `None` 默认的 `StyleUpdate` 专供 PATCH | `app/schemas.py`、`app/api/routes/projects.py` |
> | **修复：归档角色刷新后复活** | `DELETE /characters/{id}` 只把 `status` 置 `ARCHIVED`（软归档），但列表接口不过滤，前端「归档」后一刷新角色又回来了，看起来像没生效。现按 `GET /projects` 的既有约定默认过滤，需要看历史角色传 `include_archived=true` | `app/api/routes/projects.py` |
> | 前端类型纠错 | `stylesApi.apply` 声明的返回类型是 `StyleAsset`，实际后端返回 `{project_id, style_id, affected_shots}`。改为真实的 `StyleApplyResult`，并据此在 UI 上如实告知「N 个分镜已标记为需重新生成」 | `api/characters.ts`、`types/index.ts` |
> | **修复：加载失败被显示成「没有数据」** | 6 个视图在 catch 里写 `err instanceof ApiError ? err : null` —— 非 `ApiError` 一律变 `null`，页面随即渲染成**空态**（「暂无数据」），用户看到的是「你没有数据」而不是「加载失败，可重试」。抽出唯一的 `toApiError()`（`api/client.ts`、`CostView` 早已用同一实现），7 处统一改用它，并删掉 `CostView` 里的重复定义 | `api/errors.ts`、`views/{Workspace,WorkspaceAssets,ProjectAssets,EpisodeAudio,EpisodeCharacters,EpisodeRender,EpisodeStory}View.vue` |
> | 文档更正 | §3.1 此前记「登录 / 工作区 ❌ 完全没有」，实际 `WorkspaceView.vue` 早已存在且挂在全局导航上；同时把已完成页混进了「缺」表。已按十二页逐页核验重写 | 本文档 §3.1 |
> | 验收补充 | 新增 8 例后端回归（角色建/查/改/重名冲突、项目自带默认画风、画风 PATCH 不清空、`apply` 返回形状、归档后不在列表）+ **首份视图层测试**（`ProjectAssetsView.spec.ts`，22 例）+ `toApiError` 4 例 + 真实 HTTP 核验 `scripts/check_project_assets.py`（39 项断言，含工作区页数据链路） | `tests/test_project_assets_fr020.py`、`views/ProjectAssetsView.spec.ts`、`scripts/check_project_assets.py` |
>
> **连带修正**：真实核验脚本一开始把「列表第一条」当成自己刚建的画风而失败 —— 项目**自带**一个默认画风，所以必须按 `id` 取。这暴露的其实是页面逻辑的同一个陷阱，已在单测里钉住。

> **本轮已完成（2026-10-13 第五轮，§12-4 TTS 降级 + §12-8 内容拦截申诉：后端 `pytest` `238 项全绿`，前端 `vitest` `85 例全绿`，真实 HTTP 核验 `check_degrade_appeal.py` 32 项断言全部通过，`smoke_http.py` 端到端仍全通过）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | §12-4 逐句状态是唯一依据 | 新增 `GET /tts/lines` 把**每条台词**（不只是已生成的音轨）列出来，`status` 分 `missing`/`skipped`/`stale`/`ready`。此前「还没生成」和「有意跳过」在数据上无法区分，降级入口只能靠猜 | `app/agents/audio.py`、`app/api/routes/generation.py` |
> | §12-4 上传音频兜底 | `POST /tts/manual` 用用户上传的音频顶替某句：要求素材 `kind == "audio"`，写 `source="manual"`、`provider="manual"`、`speed=1.0`（成品音频不套角色语速）；**同一句重复上传是替换而非并存**，否则时间线会重复播放 | `app/agents/audio.py`、`app/api/routes/generation.py` |
> | §12-4 跳过配音 | `POST /tts/skip` 落成 `source="skipped"` 且 `media_id=null`，让预览/导出可以继续；与 `missing` 明确区分，用户回头还能补 | 同上 |
> | §12-4 前端 | 音频页只在 `missing` 的行上出现「上传音频 / 跳过」，并提供「全部跳过，先出片」；已跳过的音轨在主表显示「已跳过」而非「无音频」 | `views/EpisodeAudioView.vue` |
> | §12-8 **闸门此前从未生效** | 关键发现：`safety_agent.episode_risk_summary()` 一直在算 `can_export`，但 `routes/exports.py` 与 `services/render.py` **零处引用** —— PRD「禁止确认和导出」实际未实现。现新增 `safety_agent.export_gate()` / `assert_export_allowed()`，接入预览、正式导出、素材包三个接口，**并接入门控审批 `approve`**（「确认」走的就是门控，只挡导出会漏掉一半） | `app/agents/safety.py`、`app/api/routes/exports.py`、`app/api/routes/workflow.py` |
> | §12-8 申诉是人工通道 | 申诉**不自动放行**：`submit_appeal` 只写 `PENDING`；只有 `POST /appeals/{id}/resolve` 人工置 `APPROVED` 才把该素材移出 `unresolved`。这样既守住「禁止导出」，又让申诉入口真的能解开死锁。同目标只允许一条待审申诉 | `app/agents/safety.py`、`app/api/routes/exports.py`、`app/api/routes/admin.py` |
> | §12-8 前端 | 渲染页显示「可导出 / 已禁止导出」徽标；被拦截时列出未解决素材 + 每条申诉状态，可就地填理由提交申诉；导出/预览/素材包按钮同时禁用并给出原因 | `views/EpisodeRenderView.vue` |
> | 数据模型 | 新增 `audio_tracks.source`（`tts`/`manual`/`skipped`，NOT NULL + `server_default`）与 `safety_appeals` 表（Alembic `4c8f2b6a91d7`）；既有 36 条音轨补 `tts` | `app/db/models.py`、`alembic/versions/20261013_2110_4c8f2b6a91d7_audio_source_safety_appeals.py` |
> | 验收补充 | 新增 14 例（逐句状态、兜底替换不重复、跳过与缺失区分、非音频/未知镜头拒绝、拦截后三个导出接口 422 且带 `trace_id`、申诉不解禁、批准后放行、驳回继续挡、重复申诉 409）+ 真实 HTTP 核验 `scripts/check_degrade_appeal.py`（32 项断言） | `tests/test_degrade_appeal.py`、`scripts/check_degrade_appeal.py` |
>
> **连带修正**：`appeal_to_dict` 的时间戳必须转 ISO 字符串 —— 这个 dict 会进错误体 `details`，而错误处理器用 `json.dumps`，`datetime` 会直接抛 `TypeError`（由 `test_rejected_appeal_keeps_block` 暴露）。

> **本轮已完成（2026-10-13 第四轮，FR-050/FR-051：后端 `pytest` `224 项全绿`，前端 `vitest` `85 例全绿`，真实 HTTP 核验 `check_fr050_voice.py` 全部通过）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | FR-050 音色强制绑定 | 原先「未绑音色就把那几句台词静默跳过」——成片缺声音、用户难发现。现在**提交前整体拒绝**：`POST /tts/generate` 返回 409 `MISSING_VOICE`（点名角色 + 镜号，不留任务、不占配额）；新增 `GET /episodes/{id}/tts/estimate` 给出 `can_generate` 与 `blocking_problems[]`；`audio_agent.dialogue_requirements()` 作为唯一判定来源，预估与提交共用 | `app/agents/audio.py`、`app/api/routes/generation.py` |
> | FR-051 语速 | 新增 `character_assets.voice_speed`（Alembic `7d2e4a91c5b3`，既有 14 行补 1.0）；绑定/改绑时可设（schema 层 `ge=0.5 le=2.0`）；合成时 `clamp_speed()` 夹区间并传给供应商；**音轨与素材元数据同时留档**；改音色或语速自动把已有配音标记过期 | `app/db/models.py`、`app/agents/audio.py`、`app/api/routes/projects.py` |
> | FR-051「保留原文/音色/语速/情绪/供应商」 | `track_to_dict` 原先漏了 `speed`/`emotion`/`provider`，导出时无法核对；现全部回传 | `app/agents/audio.py` |
> | 工作流默认音色 | 保留「最小闭环自动补音色」（否则链路会卡在配音），但把排序从只按 `created_at` 改为 `(created_at, id)` —— 时间戳相同时顺序不稳定会让两次运行的配音结果不同 | `app/workflow/nodes.py` |
> | 前端（角色页） | 每个角色卡内直接选音色 + 调语速（0.5~2.0）+ **试听**（`GET /voices/{id}/preview`，后端按需合成并缓存）+ 保存；已绑定则显示音色名与语速 | `views/EpisodeCharactersView.vue` |
> | 前端（音频页） | 前置条件不满足时**禁用「生成配音」并说明差什么、去哪补**（列出角色 + 镜号 + 跳转「角色」页），而不是让用户点下去才报错；标题栏显示「待合成 N 句 · 预估 ¥x」 | `views/EpisodeAudioView.vue` |
> | 验收补充 | 新增 10 例（预估阻断项、409 且无残留任务、绑定后放行、语速持久化/越界拒绝、音轨留档、改语速标记过期、按镜头预估）+ 真实 HTTP 核验脚本 `scripts/check_fr050_voice.py`（30 项断言） | `tests/test_voice_speed_fr050.py`、`scripts/check_fr050_voice.py` |
>
> **连带修正**：`tests/test_quota.py` 的两条 TTS 配额用例原先依赖「未绑音色也能提交」，现补上绑定步骤（前置条件是新的正确约束）。

> **本轮已完成（2026-10-13 第三轮，前端自动化落地：`vitest` `85 例全绿`，后端 `pytest` 仍 `214 项全绿`）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | §15.5 前端测试框架 | 引入 Vitest + `@vue/test-utils` + jsdom + v8 覆盖率；`vitest.config.ts` 独立于构建配置，`vitest.setup.ts` 只补 jsdom 缺失的浏览器能力（`matchMedia`/`createObjectURL`）并在每个用例前清空存储；新增 `npm test` / `test:watch` / `test:coverage` 三个脚本 | `vitest.config.ts`、`vitest.setup.ts`、`package.json` |
> | §15.5 自动保存回归（16 例） | 钉住 FR-003 的真实语义：连续输入只提交一次（防抖）、提交前本地已有草稿、失败留草稿并置 `error`、离线不发请求、`online` 后自动重试、版本一致才自动还原、**服务器版本前进时标记冲突且不覆盖**、`acceptDraft`/`discardDraft` 两条取舍路径、按剧集隔离、损坏草稿不崩溃 | `src/composables/useAutosave.spec.ts` |
> | §15.5 错误契约回归（20 例） | 钉住 `docs/API.md` §0.1：统一错误体字段与 **`trace_id` 必须透出**（用户报障时可检索）、`retryable` 如实传递、能力探测 `details.problems` 原样保留、非统一体（FastAPI `detail`）按 HTTP 语义翻译、非 JSON/空响应体兜底、`loc` → 人话定位、`errors[]`/`warnings[]` 批量抽取 | `src/api/errors.spec.ts` |
> | §15.5 金额与展示回归（20 例） | 金额一律以**整数分**传输，断言 `0.1+0.2` 类浮点误差不会污染显示；缺失值显示 `—` 而非 `¥0.00`；时长/体积/相对时间分档、`percent` 不产生 `Infinity`/`NaN` | `src/utils/format.spec.ts` |
> | §15.5 媒体组件回归（16 例） | 钉住 FR-032 在共用组件上的行为（回退即全站回退）：点击开灯箱、点背景关、点图不关、Esc 关、关闭按钮关、卸载后监听已摘；下载走与展示同源路径且带扩展名；失败交给统一上报；`zoomable`/`downloadable` 可关；视频/音频不提供；无 `mediaId` 空态；加载失败撤入口且换图后重置 | `src/components/MediaThumb.spec.ts` |
> | §15.5 剧集 store 回归（13 例） | 通过捕获 store 注册给 `useSse` 的 `onEvent` 回调驱动事件分发（与真实路径一致，而非直接调内部函数）：`shot.confirmed` 按镜号就地更新且缺字段时保留原值、事件流面板倒序且上限 40、任务事件转交 job watcher、未知事件不抛异常、`trackJob` 去重、派生状态（总时长/已确认数/`isBlocked`） | `src/stores/episode.spec.ts` |


> **本轮已完成（2026-10-13 第二轮，`pytest` `214 项全绿`）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | FR-030 分镜表格视图 | 新增「卡片 / 表格」视图切换；表格逐镜展示镜号/时长/角色/动作/运镜/状态，**台词行内可增删改**，支持单镜保存与「保存全部」（单镜失败不阻断其余，失败项保留重试）；表头吸顶、横向可滚 | `views/EpisodeShotsView.vue` |
> | FR-032 候选图放大 / 下载 | `MediaThumb` 统一加灯箱放大（Esc/点背景关闭）与下载按钮，**所有媒体面（候选图、角色参考图、成片）一次性获得该能力**；下载走 `/media/{id}/content`，与展示同源 | `components/MediaThumb.vue` |
> | FR-052 BGM 上传 + 试听 | 「挂载音轨」弹窗内直接上传音频（`/media/upload` kind=audio，自动声明权利），上传后自动选中；挂载前可试听 | `views/EpisodeAudioView.vue`、`api/media.ts` |
> | FR-053 字幕编辑 UI | 字幕条目改为可编辑：文本 + 起止时间（毫秒）行内修改，支持单条保存与批量保存；**前端与后端双重拦截时间轴倒置** | `views/EpisodeAudioView.vue`、`api/audio.ts`、`shared/types` |
> | 修复真实缺陷（字幕死结） | `generate_subtitles` 的台词来源只有 `AudioTrack`（已合成 TTS），导致**不先跑配音就永远生不出字幕**——而字幕本该在配音前就能校正。现增加「分镜脚本台词/旁白」兜底来源，按镜头起点在时长内均分估算；TTS 跑完重新生成即得精确时间轴 | `app/agents/edit.py` |
> | 修复真实缺陷（字幕时间轴） | `PATCH /subtitles/{cue_id}` **完全没有校验**，可存下 `start_ms=9000 > end_ms=1000` 的倒置字幕（播放器不显示、FFmpeg `subtitles` 滤镜跳过，成片与导出 SRT 对不上）。现写入前校验：非整数/负数/`end <= start` 一律 422 且不改库 | `app/api/routes/generation.py` |
> | 验收补充 | 新增 10 例：BGM 上传-挂载-试听-删除闭环、字幕文本/时间校正、倒置/相等/负值/非整数时间被拒、404、校正结果出现在导出 SRT 中 | `backend/tests/test_audio_subtitles.py` |
>
> **上一轮已完成（2026-10-13，`pytest` `204 项全绿` + `smoke_http.py` 端到端通过）**
>
> | 项 | 实现要点 | 关键文件 |
> |----|----------|----------|
> | FR-003 自动保存 | 新增 `useAutosave` composable：1.2s 静默期自动提交、localStorage 按 `剧集+分区` 存草稿、恢复时用「草稿基准版本 vs 服务器版本」判定冲突，**冲突交用户取舍而非静默覆盖**；离线保留草稿、`online` 后重试 | `frontend/src/composables/useAutosave.ts`、`views/EpisodeStoryView.vue` |
> | FR-010 成本预估 | 故事输入区展示预计成本、计费字符数（输入＋提示词）、单价与长文本警告；600ms 防抖调用 `/story/estimate`；供应商未声明单价时明确提示「实际可能产生费用」 | `views/EpisodeStoryView.vue`、`api/story.ts` |
> | FR-040 视频参数 | 新增「视频参数」面板：项目级画幅比例下拉（显示每个比例的输出尺寸）、逐镜时长/运动强度/运镜编辑；写入 `shot_json.video_params`，生成时按此执行 | `views/EpisodeVideoView.vue`、`api/video.ts` |
> | FR-004 版本记录 | 工作台右栏新增「版本记录」卡片：列出剧本/角色/分镜/时间线版本，展开可看与上一版的字段级差异 | `layouts/WorkbenchLayout.vue` |
> | API.md §0.1 统一错误体 | **补上遗漏的 `trace_id`**：请求级 ContextVar 生成，错误体与响应头 `x-trace-id` 同值；并新增 `RequestValidationError` 处理器，参数校验失败也返回统一错误体（此前是 FastAPI 默认的 `detail` 结构） | `app/core/errors.py`、`app/main.py` |
> | §15.2/§15.3/§15.4 验收 | 新增 17 例自动化验收：刷新后内容留存、幂等连点、schema/超时/存储错误可定位、错误体统一、供应商调用可追溯、版本链可查、台账无悬空预留 | `backend/tests/test_acceptance_15.py` |
> | 修复真实缺陷 | **两处 `EntityVersion` 手写路径漏了 `input_digest`**（`save_script`、`update_shot`、StoryAgent 生成共三处），导致版本记录「有版本但无法判断内容是否真变了」。已全部改走 `versions.record()`，并加回归用例扫库兜底 | `app/api/routes/story.py`、`app/services/executor.py` |
> | 修复测试污染 | `test_restore_keeps_a_safety_copy` 在连接池仍持有时强制在线恢复主库，产生 `database disk image is malformed` 并**污染同进程后续所有用例**。改为先 `engine.dispose()` 再恢复，并断言恢复后库完好 | `backend/tests/test_backup.py` |

> **P1 本轮已完成（2026-10-12/13，`pytest` 186 项全绿 + `smoke_http.py` 端到端通过）**
>
> | 需求 | 实现要点 | 关键文件 |
> |------|----------|----------|
> | FR-090 供应商主备 | `FallbackProvider` 透明代理，仅在 `PROVIDER_TIMEOUT/RATE_LIMITED/UNAVAILABLE/ERROR` 或 `retryable` 时切备用；`estimate_cost` **不切**（报价必须与实际执行方同源） | `app/providers/fallback.py` |
> | FR-091 能力探测 | 尺寸/像素上限/参考图数/候选数/画幅比/时长/图生视频/格式，**提交前**阻断并返回 422 | `app/services/capabilities.py` |
> | FR-061 自动合成 | `xfade` 接入合成链，转场与上一镜重叠，全片时长相应收缩；时间线校验 4 条新规则；前端可编辑 | `app/agents/edit.py`、`app/services/render.py` |
> | §12-6 配额拦截 | 图像 / TTS / 合成全部 `reserve`，不足在建任务前 402 阻断；补齐三处**成本丢失**缺陷 | `app/agents/image.py`、`app/agents/audio.py`、`app/services/executor.py` |
> | §11.3-2 上传配额 | 单文件大小 → 格式白名单 → FR-091 图像格式 → 工作区总容量，全部在落盘前校验 | `app/services/uploads.py` |
> | §10.1 状态机 | 新增 `STALE` 成员；`stage_before_block` 由「只写不读」改为解除阻塞后**回到原阶段**；上游变更只标记不删资源 | `app/workflow/engine.py`、`app/agents/shot.py` |
> | FR-004 版本记录 | 复用既有 `EntityVersion` 表，新增 `app/services/versions.py`（指纹去重、避免空版本）与 `GET /episodes/{id}/versions[/{version_id}]`；角色描述更新与正式导出时间线落版本；`versions.record()` 内容未变时返回 `None` | `app/services/versions.py`、`app/api/routes/story.py`、`app/api/routes/projects.py`、`app/api/routes/exports.py` |
> | FR-010 文本成本预估 | 抽出 `story.build_script_prompt()` 让**预估与真实调用共用同一份提示词**（否则预估对不上实扣）；`POST /episodes/{id}/story/estimate` 返回字符数、计费字符数、预计成本、`declared` 与 `long_text` 标记 | `app/agents/story.py`、`app/api/routes/story.py` |
> | FR-014 节拍级重生成 | `POST /episodes/{id}/beats/regenerate` 按 `beat_sheet[].shots` 换算镜号后复用分镜链路；未选中节拍的分镜保持不变；无节拍表时 409 并提示改用分镜入口 | `app/api/routes/story.py`、`app/schemas.py` |
> | §6.6 工具白名单 | 七类允许能力 + 四项明确禁止，`assert_tool_allowed()` 越权即 403；`GET /agent/tools` 暴露清单；测试用 AST 静态扫描 agent 源码确认无裸 SQL / 删素材 / 改配额 | `app/agents/tools.py`、`tests/test_agent_tools.py` |
> | §12 异常降级 | 进程内熔断表 `app/services/degraded.py`：连续 3 次依赖侧失败即暂停该类**新任务**（503 + 「什么仍可用」提示），成功即自愈、TTL 到点自动放行；视频/图像/语音/对象存储四个入口接入；`GET /degraded` + `POST /degraded/{kind}/resume` | `app/services/degraded.py`、`app/services/executor.py`、`app/api/routes/generation.py`、`app/api/routes/exports.py` |


---

## 1. 必须优先修的缺陷（P0）

这些不是「PRD 没做到」，而是**当前实现有错**，会破坏数据或让功能不可用。

| # | 缺陷 | 证据 | 影响 | 状态 |
|---|------|------|------|------|
| 1 | 角色卡读路径未拍平：`character_dict` 只回传 `spec_json` 嵌套对象，前端按顶层字段读 | `backend/app/api/deps.py:141-162` + `frontend/src/views/EpisodeCharactersView.vue:176-211` | **角色详情恒显示「—」；点一次「编辑保存」会把身份/外貌/服饰/性格/说话风格/禁用变化 6 个字段写成空串（数据被清空）** | ✅ 已修 |
| 2 | 带画风创建项目必 422：前端传字符串，后端要 `StyleIn` 对象 | `frontend/src/views/ProjectsView.vue:68` + `backend/app/schemas.py:186-191` | 创建项目时填「画风备注」直接失败 | ✅ 已修 |
| 3 | 项目归属用户 id 悬空：建项目写 `local-user`，种子用户是 `usr_local` | `backend/app/api/routes/projects.py:95,100` + `backend/app/main.py:93-101` | `User 1—N Project` 关系实际不成立 | ✅ 已修 |
| 4 | 重命名查重未限定用户 | `backend/app/api/routes/projects.py:155-157` | 与创建口径不一致 | ✅ 已修 |
| 5 | 故事分析面板渲染不出来：后端存 `analysis_json`，前端读 `story.analysis`；且 `GET /story` 的 `{story:…}` 信封未解包（刷新后故事正文也变空） | `backend/app/api/routes/story.py:103,168` + `frontend/src/views/EpisodeStoryView.vue:384` | FR-011 的「主题/人物关系/场景清单/冲突目标」用户看不到 | ✅ 已修 |
| 6 | 「重新规划后续镜头」是死开关：`replan_downstream` 只写进 `job.input_json`，无人读取 | `backend/app/api/routes/story.py:275` + `backend/app/services/executor.py:162-200` | FR-014 该选项点了没反应 | ✅ 已修 |
| 7 | 自动重试根本不存在：`max_attempts` / `auto_retry_limit` 从未被 worker/executor 消费 | `backend/app/core/config.py:85`、`backend/app/services/jobs.py:94`（仅写入回显），`docs/MVP-实现说明.md:139` 却宣称有 | FR-072「系统有限自动重试」与 §10.2 `FAILED→RETRYING→QUEUED` 整条缺失；`RETRYING`、`EXPIRED` 是死枚举 | ✅ 已修 |
| 8 | 批量确认绕过安全检查：`confirm-batch` 不校验 `safety_status=BLOCKED` | `backend/app/api/routes/story.py:441-472` | FR-081 高风险内容可经批量入口进入导出 | ✅ 已修 |
| 9 | 导出任务忽略传入的 `watermark` 开关，恒为 False | `backend/app/services/executor.py:389-392` | FR-062 水印开关对正式导出无效 | ✅ 已修 |
| 10 | 候选安全态字段名不一致：前端读 `cand.safety`，接口返回 `safety_status` | `frontend/src/components/CandidatePicker.vue:136` + `backend/app/services/media.py:211` | FR-034 候选图安全检查结果恒显示「未检查」 | ✅ 已修 |
| 11 | 角色资产 `asset_kind` 只嵌在 `metadata_json` 里，前端顶层取不到 | `backend/app/services/media.py:192-223` + `frontend/src/views/EpisodeCharactersView.vue:639` | 四类角色图（设定/正面/半身/表情）分类恒显示「—」 | ✅ 已修 |
| 12 | 日志脱敏是死代码：`redact()` 零调用点，故事正文与提示词原样入库并回传任务详情 | `backend/app/core/security.py:93-107` + `backend/app/api/routes/jobs.py:58` | FR-093 / §11.1 脱敏要求未落地，且任务详情接口泄露全文 | ✅ 已修 |
| 13 | MP4 元数据缺「生成时间」；且 `-metadata project_id=…` 因缺 `use_metadata_tags` **从未真正写入文件** | `backend/app/services/render.py:302-304` | FR-063 元数据不可追溯 | ✅ 已修 |

**P0 全部完成（13/13）**，`python -m pytest` 13 项全绿，端到端 HTTP 联调通过。修复过程中额外发现并处理：

- `sync_shots_from_script` 给**所有**分镜递增版本号（含未改动者），违反 FR-014「不得修改未选中的分镜」→ 改为按内容指纹比较，只有真的变了才递增。
- 局部重生成把**同一个 patch 套用到所有选中分镜**（多选时内容重复）→ 改为按镜号逐条匹配（兼容模型返回 `{"shots":[…]}`／裸列表／键值表／单个 patch 四种形态）。
- `instruction`（用户手写的重生成指令）被误列入 strict 截断字段 → 移出，避免 40 字截断造成功能回归。
- 重试退避需要新列 `jobs.next_attempt_at` → 新增 Alembic 迁移 `20261012_1540_8c41d0a7f2b9`。
- 自动重试仅在 **worker 路径**生效；工作流节点的内联执行保持 `allow_auto_retry=False`，避免与节点级重试/回滚重复执行同一任务。

---

## 2. 后端能力缺口（P1）

| 需求 | 现状 | 缺口 | 量 |
|------|------|------|---|
| FR-090 供应商主备 | ✅ 已补：`FallbackProvider` + 5 个 `fallback_*` 列 + 管理接口读写 | 仅剩前端设置页的备用配置 UI（已一并补上） | ✅ |
| FR-091 能力探测 | ✅ 已补：尺寸/参考图数/画幅比/候选数/时长/格式**提交前**阻断（422 + `details.problems[]`） | — | ✅ |
| FR-061 自动合成 | ✅ 已补：`xfade` 转场接入合成链，12 种转场可选，含 4 条时间线校验规则 | — | ✅ |
| FR-080 输入检查 | ✅ 已补：`safety.check_fields` 覆盖台词/提示词/负向提示词/动作/旁白，分镜 PATCH 前拦截（422，**不落库**） | — | ✅ |
| FR-082 权利声明 | ✅ 已补：**任何**上传（含图片/文本）都必须声明使用权；签名 URL TTL 服务端强制上限（§11.1-2） | 导出前对上传分镜图的遍历仍待补 | 🟡 |
| FR-060 时间线校验 | 10 类问题可定位 | 缺分辨率/比例、BGM 文件存在性、镜头顺序报错（字幕时间已补） | M |
| §12-6 配额不足 | ✅ 已补：图像 / TTS / 合成全部 `reserve`，不足在建任务前 402 阻断 | — | ✅ |
| FR-004 版本记录 | ✅ 已补：`app/services/versions.py` 复用既有 `EntityVersion` 表做统一留档（指纹去重，内容没变不产生空版本）；角色描述更新、正式导出时间线都落版本；`GET /episodes/{id}/versions` 列出、`/versions/{id}` 给出与上一版的字段差异 | — | ✅ |
| §10.1 状态机 | ✅ 已补：新增 `STALE`；解除阻塞回到 `stage_before_block`；上游变更只标记不删资源 | — | ✅ |
| FR-023 引用完整性 | ✅ 已补：`MISSING_CHARACTER` / `MISSING_REFERENCE` 在**提交接口** 409 阻断（不留任务、不占配额）；预估接口提前给出 `can_generate`；新增 `no_character` 合法绕过 | — | ✅ |
| FR-010 故事输入 | ✅ 已补：抽出 `story.build_script_prompt()` 让预估与真实调用共用同一份提示词；`POST /episodes/{id}/story/estimate` 返回 `source_chars` / `billing_chars` / `estimate_cents` / `declared` / `long_text`；`StoryInput` 新增 `shot_count` | — | ✅ |
| FR-014 局部重生成 | ✅ 已补：`POST /episodes/{id}/beats/regenerate` 按 `beat_sheet[].shots` 换算镜号后复用分镜链路，未选中节拍的分镜不动；无节拍表时 409 并指向分镜入口；`replan_downstream` 已生效 | — | ✅ |
| FR-022 画风注入 | ✅ 已补：`palette`（dict/list/str 三种形态）进入提示词，5/5 项画风全覆盖；`is_default` 后端返回 | — | ✅ |
| FR-040 视频参数 | ✅ 后端已补：`GET/PUT /episodes/{id}/video/params` 持久化画幅比例与逐镜参数；图像与视频尺寸都随 `Project.aspect_ratio` 走（6 种预设），构图提示不再写死竖屏 | — | ✅ |
| FR-060 时间线校验 | ✅ 已补：新增镜头顺序（重复/非法镜号）、BGM 文件存在性、画幅一致性三类检查；输出分辨率改为跟随项目画幅比例（6 种比例 × 3 档位） | — | ✅ |
| FR-010 文本成本预估 | ✅ 见上（`/story/estimate`），与真实调用共用提示词 | — | ✅ |
| FR-004 版本记录 | ✅ 见上（`versions` 服务 + 两个查询接口） | — | ✅ |
| FR-014 局部重生成 | ✅ 见上（节拍级 + 分镜级） | — | ✅ |
| FR-050 音色 / FR-051 TTS | ✅ 已补：绑定+试听+过期齐全；**音色改为强制前置**（有台词未绑音色 → 提交前 409 `MISSING_VOICE`，不留任务不占配额；预估接口提前给 `can_generate` + `blocking_problems[]`）；**语速已落地**（`character_assets.voice_speed`，0.5~2.0，随音轨与素材留档，改语速自动让旧配音过期）；音轨新增保留 `speed`/`emotion`/`provider` | — | ✅ |
| FR-052/053 音字 | 后端齐全；**前端已补**（BGM 弹窗内上传+试听、字幕行内编辑+倒置时间拦截；并修复「不跑 TTS 就生不出字幕」的死结） | — | ✅ |
| §5.1-6 项目级资产库（第六轮） | ✅ 已补：后端接口本就齐全（项目角色/画风的建查改、`apply`、音色列表），第六轮补齐前端整页与入口 | — | ✅ |
| PATCH 局部更新语义（第六轮） | ✅ 已修：`PATCH /styles/{id}` 曾把创建用的 `StyleIn` 当 body，导致未提供的字段被重置成默认值（`name`→「默认画风」、`palette`→`[]`）。新增 `StyleUpdate` 全 `None` 默认 | — | ✅ |
| 归档可见性（第六轮） | ✅ 已修：`DELETE /characters/{id}` 是软归档，但列表不过滤，前端归档后刷新角色复活。现默认过滤 `ARCHIVED`，`include_archived=true` 可查历史（对齐 `GET /projects`） | — | ✅ |
| FR-032 候选管理 | 历史/驳回/恢复齐全；**前端已补**（`MediaThumb` 灯箱放大 + 下载，所有媒体面通用） | — | ✅ |
| FR-030 分镜表 | **前端已补**（卡片/表格双视图 + 台词行内增删改 + 批量保存） | 场景（`scene_no`）仍不支持 PATCH | ✅ |
| §6.6 工具白名单 | ✅ 已补：`app/agents/tools.py` 声明七类允许能力（各指向真实函数）与四项明确禁止；`assert_tool_allowed()` 越权即 403；`GET /agent/tools` 暴露清单；测试用 AST 静态扫描 agent 源码确认无裸 SQL / 删素材 / 改配额 | — | ✅ |
| §12-2/12-3/12-8/12-9 | ✅ 已补：`app/services/degraded.py` 进程内熔断表——连续 3 次**依赖侧**失败即暂停该类新任务（503 + 「什么仍可用」可执行提示），成功自愈、TTL 到点自动放行；视频/图像/语音/对象存储四入口接入；`GET /degraded` 与 `POST /degraded/{kind}/resume` | — | ✅ |
| §12-4 TTS 失败降级 | ✅ 已补：`GET /tts/lines` 给逐句状态（`missing`/`skipped`/`stale`/`ready`）；`POST /tts/manual` 用上传音频顶替某句（`source="manual"`，同句重复上传为替换）；`POST /tts/skip` 落 `source="skipped"` 后继续出片。前端只在 `missing` 行给「上传音频 / 跳过」 | — | ✅ |
| §12-8 输出内容被拦截 | ✅ 已补（**此前是空档**）：`episode_risk_summary().can_export` 一直只算不用，导出侧零引用。现 `safety_agent.export_gate()` 接入预览/正式导出/素材包 + 门控 `approve`（即「确认」）；申诉走 `POST /episodes/{id}/appeals`，**只有人工 `POST /appeals/{id}/resolve` 置 `APPROVED` 才放行** | — | ✅ |
| §11.3-2 上传配额 | ✅ 已补：单文件大小 + 格式白名单 + 工作区总容量，均在落盘前校验 | — | ✅ |
| §11.1-3 审计 | ✅ 已补：`app/services/audit.py` 统一入口（动作常量 + 自动脱敏），已接入 admin / exports / 配额 / 供应商等路径；`GET /audit-logs` 可查；`tests/test_audit.py` 覆盖 | — | ✅ |
| §11.1-2 签名 URL | ✅ 已补：`clamp_ttl()` 服务端强制上限（`SIGNED_URL_MAX_TTL_SECONDS`），接口回传实际生效 TTL 与 `clamped` | — | ✅ |
| §11.2-2 备份 | ✅ 已补：`VACUUM INTO` 在线一致快照（主库 + checkpoint 库）、按库分组 7 点保留、启动即备 + 24h 周期、`scripts/backup_db.py`、`/backups` 三个接口；恢复默认拒绝在线执行 | — | ✅ |

---

## 3. 前端缺口（P2）

### 3.1 页面（PRD §5.1 十二页）

**十二页已全部落地**（第六轮补齐最后两页）。下表为逐页核验结果：

| # | PRD §5.1 页面 | 路由 | 实现 | 现状 |
|---|---------------|------|------|------|
| 1 | 登录 / 工作区 | `/workspace` | `WorkspaceView.vue` | ✅ 单人本地工作区（创作者资料改名、配额条、概览计数、Provider 状态）。无登录是因为 §3.2 只排除多租户企业权限，本地单用户不需要鉴权 |
| 2 | 项目列表 | `/` | `ProjectsView.vue` | ✅ 含归档筛选 |
| 3 | 项目概览 | `/projects/:projectId` | `ProjectDetailView.vue` | ✅ 概览 + 剧集列表 + 成本 + 审计，第六轮加入「角色与画风资产库」入口 |
| 4 | 剧集工作台 | `/episodes/:episodeId` | `WorkbenchLayout.vue` | ✅ 见 §3.2 |
| 5 | 剧本编辑器 | `…/story` | `EpisodeStoryView.vue` | ✅ FR-003 自动草稿 |
| 6 | 角色与画风资产库 | `/projects/:projectId/assets` | `ProjectAssetsView.vue` | ✅ **第六轮新增**：项目内角色的建/改/归档/生成参考图，画风的建/改/设为默认 |
| 7 | 分镜工作台 | `…/shots` | `EpisodeShotsView.vue` | ✅ |
| 8 | 视频与音频工作台 | `…/video`、`…/audio` | `EpisodeVideoView.vue`、`EpisodeAudioView.vue` | ✅ 拆为两个子路由，同一工作台内切换 |
| 9 | 合成预览页 | `…/render` | `EpisodeRenderView.vue` | ✅ 含 §12-8 导出闸门与申诉入口 |
| 10 | 任务中心 | `/jobs` | `JobsView.vue` | ✅ |
| 11 | 成本与运行记录 | `/cost` | `CostView.vue` | ✅ |
| 12 | 设置页 | `/settings` | `SettingsView.vue` | ✅ |

> 另有 `/workspace/assets`（`WorkspaceAssetsView.vue`）：**跨项目**只读资产汇总，与第 6 页
> （单项目资产主控台）职责不同，两者互补而非重复。

### 3.2 剧集工作台五区（PRD §5.2）

| 区 | 现状 |
|----|------|
| 顶部 | ✅ 剧集名/状态/总进度/累计成本/保存状态 齐备 |
| 左侧 | ✅ 生产阶段纵向导航 + 门控状态 + 节点进度（纵向变体） |
| 中部 | ✅ 当前阶段主工作区（原 RouterView，行为未变） |
| 右侧 | ✅ 智能体建议 / 错误提示 / 版本和操作日志（三段，可折叠） |
| 底部 | ✅ 任务进度（真实任务 chip + SSE）/ 批量操作 / 下一步（门控未确认时禁用） |

> 已知边界（未造假，均按「拿不到数据就显示空态」处理）：
> 后端**没有**智能体建议接口，右侧「智能体建议」展示当前节点的真实日志 message；
> 无版本历史接口，只显示 workflow run 的 `state.version`；
> `STALE` 会覆盖原阶段，故阶段进度显示 `—` 而非编造数字。

### 3.3 交互
- ~~FR-003 自动保存 + 浏览器本地草稿 + 断网重连合并~~ ✅ **已完成**（`useAutosave`，含版本冲突取舍）
- ~~FR-010 长文本成本预估~~ ✅ **已完成**（`/story/estimate` + 输入区实时展示）
- ~~FR-040 视频参数控件~~ ✅ **已完成**（画幅比例 + 逐镜时长/运动强度/运镜）
- ~~FR-004 版本历史查看~~ ✅ **已完成**（右栏「版本记录」+ 字段级差异）
- ~~FR-032 候选图放大 / 下载~~ ✅ **本轮完成**（`MediaThumb` 灯箱 + 下载，所有媒体面通用）
- ~~FR-030 分镜表格视图 + 台词编辑~~ ✅ **本轮完成**（卡片/表格切换 + 台词行内增删改）
- ~~FR-053 字幕编辑 UI~~ ✅ **本轮完成**（文本 + 起止时间行内编辑，前后端双重校验）
- ~~FR-052 BGM 上传入口 + 试听~~ ✅ **本轮完成**（弹窗内上传 + 试听 + 自动选用）
- ~~FR-050 音色强制绑定 + FR-051 语速控件~~ ✅ **已完成**（音色成为提交前置条件；语速可设、随音轨留档、改动即让旧配音过期）
- ~~§12-4 上传音频兜底入口~~ ✅ **第五轮完成**（逐句状态表 + 上传顶替 + 跳过，三处后端接口 + 音频页入口）
- ~~§12-8 申诉入口~~ ✅ **第五轮完成**（导出闸门真正生效 + 申诉表单/状态列表 + 人工复核接口）
- ~~登录 / 个人工作区页~~ ✅ **早已存在**（`WorkspaceView.vue` + 全局导航，本文档此前误记为「完全没有」，第六轮已更正 §3.1）
- ~~项目级「角色与画风资产库」~~ ✅ **第六轮完成**（`ProjectAssetsView.vue` + `/projects/:projectId/assets`）

---

## 4. 验收缺口（P3，PRD §15）

| 验收面 | 现状 | 缺 |
|--------|------|-----|
| §15.1 端到端 | ✅ **本轮补齐**：`smoke_http.py` 在门控⑤前上传并挂载 BGM，断言 BGM 进音轨列表、进预览混音（`manifest.bgm_count=1`）、进素材包 manifest 与时间线、且 `audio/bgm_*.mp3` 真的在 ZIP 里 | 已全覆盖（6 分镜/角色引用/视频/预览/成片/素材包/字幕/TTS/BGM/配额） |
| §15.2 恢复 | ✅ 本轮补齐：刷新后内容留存、幂等连点同 id、任务/工作流 run 落库可查 | — |
| §15.3 失败 | ✅ **本轮补齐**：新增 `test_ffmpeg_failure_is_located_and_package_survives` —— 注入 ffmpeg 非 0 退出，断言错误可定位（`ffmpeg_tail` + `located` 归到视频轨 + `hint`）、原任务记录保留且可重试、素材包仍能导出（§10.2 降级）。另有 schema 错误、供应商超时、存储错误、统一错误体（含 `trace_id`）、字幕非法时间轴 | 无 |
| §15.4 数据可追溯 | ✅ 本轮补齐：供应商调用含 provider/model/成本、版本链含指数字段差异、台账无悬空预留 | — |
| §15.5 前端 | ✅ **已补齐**：Vitest + `@vue/test-utils` + jsdom，7 个文件 **135 例**（自动保存 16 / 错误契约 24 / 金额与展示 20 / 媒体组件 16 / 剧集 store 13 / 资产库纯逻辑 24 / **资产库视图 22**），`npm test` 一键跑 | 视图层已起步（资产库视图 22 例）；其余 `views/*.vue` 尚未覆盖，覆盖率报告见 `npm run test:coverage` |

---

## 5. 升级分期

### Phase 1 · 缺陷修复与契约对齐（P0）
修完上表 13 项，重点是数据正确性（角色卡、画风创建、用户归属）与死代码接线（脱敏、自动重试、replan）。
**出口标准**：pytest 全绿 + 新增回归用例；手工核对角色卡编辑不再清空字段。

### Phase 2 · 后端能力补全（P1）
备用供应商 + 能力探测 + 转场 + 全链路安全检查 + 权利声明补全 + 配额全面拦截 + 时间线校验补全 +
状态机（STALE / 解除阻塞回原阶段）+ 版本记录机制化 + 上传配额 + 审计补全 + 备份。
**出口标准**：上表 P1 全部 done；`smoke_http.py` 覆盖转场与备用供应商切换。

**已完成（21/21）**：FR-090 备用供应商、FR-091 能力探测、FR-061 转场、§12-6 配额全面拦截、
§11.3-2 上传配额、§10.1 状态机、FR-080 输入检查、FR-082 权利声明、FR-023 引用完整性、
FR-022 画风注入、FR-040 视频参数、FR-060 时间线校验、§11.1-2 签名 URL 上限、
§11.1-3 审计补全、§11.2-2 备份、FR-004 版本记录、FR-010 文本成本预估、FR-014 节拍级重生成、
§6.6 工具白名单、§12-2/12-3/12-8/12-9 降级。

新增/扩展测试文件：`test_providers.py`、`test_capabilities.py`、`test_quota.py`、
`test_cost_settlement.py`、`test_state_machine.py`、`test_style_and_video_params.py`、
`test_timeline_validation.py`、`test_audit.py`、`test_backup.py`、`test_workspace.py`、
`test_versions.py`、`test_beat_regeneration.py`、`test_degraded.py`、`test_agent_tools.py`、
`test_story_estimate.py`。`python -m pytest` **186 passed**。

> **顺带修掉的成本记账缺陷**（不修则「配额拦截」形同虚设）：
> 1. `image_agent.generate_for_shots` / `audio_agent.synthesize_for_shots` **丢弃了
>    `ProviderResult.cost_cents`**，executor 只能退回按预留兜底结算 → 实际单价永远对不上；
> 2. `character_agent.generate_assets` 同样丢成本，且返回类型是裸 `list`，改为 `(assets, cost_cents)`；
> 3. 合成结算按 `manifest["total_duration_ms"]` 取时长，而真实键名是 `duration_ms` →
>    **合成永远按 0 结算**。现由 `test_cost_settlement.py` 守住这三条。

**收尾顺序（已完成）**：FR-080 输入检查（台词/提示词）→ FR-082 权利声明补全 →
FR-023 引用完整性提前到提交接口 → FR-010 文本侧成本预估 → FR-060 剩余校验 →
§11.1-2 签名 URL 上限 → §11.1-3 审计补全 → §11.2-2 备份 →
FR-004 版本记录 → FR-014 节拍级重生成 → §6.6 工具白名单 → §12 降级路径。

### Phase 3 · 前端信息架构与体验（P2）
五区布局（左栏/右栏/底栏/顶部补全）+ 登录工作区页 + 项目级资产库 + 成本与运行记录页 +
自动保存与本地草稿 + 分镜表格视图 + 候选放大下载 + 字幕编辑 + 视频参数控件。
**出口标准**：§5.1 十二页齐全、§5.2 五区齐全；浏览器实测走查。

### Phase 4 · 非功能与验收（P3）
§15.1~§15.5 补齐，含前端自动化测试；备份策略；签名 URL 上限；上传格式白名单。
**出口标准**：`python -m pytest` 覆盖 §15 五个验收面；前端有可重复运行的验收脚本。
**状态**：✅ **P3 全部完成**。§15.1 端到端（含 BGM）、§15.2 恢复、§15.3 失败（含 FFmpeg 失败）、§15.4 数据可追溯均有自动化断言；§15.5 前端自动化见 `cd frontend && npm test`（7 文件 135 例）。

---

## 6. 保持不动的设计决策

这些是 MVP 阶段的有意选择，升级时不应回退：

1. **请求级 Session 必须在响应前提交**——`RequestSessionMiddleware` 拦截 `http.response.start`；
   改回 `yield` 依赖会让「创建项目→立刻建剧集」约 6 成概率 404。
2. **工作流响应体是扁平 WorkflowRun**——`GET/POST /episodes/{id}/workflow*` 返回 run 本身
   （未启动为 `null`），节点清单只走 `/workflow/graph`。
3. **门控节点在 `interrupt()` 前必须无副作用**——LangGraph 恢复时会重跑节点体。
4. **幂等键带时间桶**——`IDEMPOTENCY_WINDOW_SECONDS`（默认 5 秒）折叠连点，窗口外允许重新生成。
5. **素材包不依赖渲染成功**——合成失败也必须能交付 ZIP。
6. **AIGC 标识不可关闭**——图片、ASS 字幕、成片、素材包四处硬编码。
7. **Mock 供应商是真实生成**——Pillow 出图、FFmpeg 出视频/TTS，未配密钥时自动回退，闭环始终可跑。
