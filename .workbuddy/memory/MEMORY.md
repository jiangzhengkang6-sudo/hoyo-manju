# hoyomanju 项目长期笔记

## 项目：hoyo漫剧（AI 漫剧制作平台）
- 定位：Agent 驱动的漫剧生产流水线（剧本→分镜→画面→视频→配音→合成→导出），竖屏 9:16 短漫剧。
- 已确认决策（2026-10-09）：产品名 hoyo漫剧；个人独立开发项目（无团队，工期按个人全职估算约 15 周到内测）；图像生成走 API 不自部署；技术路线 Python + LangGraph + Vue 3 + PostgreSQL/Redis + FFmpeg；视频可灵/即梦、TTS 海螺/Azure。
- 当前状态：PRD V1.2 已确认待排期（PRD-AI漫剧制作平台-v1.0.md 为主文档，V1.0 存档于 archive/）；唯一遗留 Q-03 内测配额数值待实测校准。技术栈章节见 PRD 5.4。
- 核心设计决策：角色资产库保一致性；两段式生成（先静态图确认再图生视频）控成本；生成任务独立状态机、单向流转；AIGC 标识与版权授权声明为 Must；积分计费 V2 再做；先低代码验证 1 集再开发平台。

## MVP 实现（2026-10-12 完成，可交付）
- 交付物：`backend/`（FastAPI + SQLAlchemy 2 + SQLite + LangGraph）、`frontend/`（Vue 3 + Vite + Pinia）、`docker-compose.yml`、`docs/API.md`、`docs/MVP-实现说明.md`、`README.md`。
- 闭环已实测通过：故事输入 → 6 个人工门控 → 分镜图 → 视频 → TTS/字幕 → 合成预览 → 成片/素材包导出；三份产物 preview 700KB / final 2211KB / package 4680KB（32 条目）。
- 验证入口：`cd backend && python -m pytest`（8 passed）；`python scripts/smoke_http.py`（HTTP 全链路）、`python scripts/smoke_sse.py`（SSE 续传）、`python scripts/check_session_commit.py`（提交时序回归）。

### 必须记住的三个坑（已在代码里修掉，改动勿回退）
1. **请求级 Session 提交时机**：不能用 `yield` 依赖提交（响应发出后才执行），否则「创建项目 → 立刻建剧集」约 6 成概率 404。现由 `app.main.RequestSessionMiddleware` 拦截 `http.response.start` 统一提交。
2. **工作流响应体**：`GET/POST /episodes/{id}/workflow*` 一律返回扁平 run（未启动为 `null`），节点清单走 `/workflow/graph`。曾因返回 `{workflow, graph}` 导致前端「启动工作流」入口永不出现。
3. **`useSse` 的 url 回调**：签名是 `(lastEventId) => string | null`，不要在闭包里引用 `const sse = useSse({...})` 自身（TDZ → 视图白屏）。

## PRD 正式化升级（2026-10-12 起）
- 对照文档 `PRD-智能体漫剧平台-全新方案.md`；审计与计划见 `docs/PRD-差距分析与升级计划.md`。
- P0 已全部完成（13 项缺陷）。回归入口：`python -m pytest`（13 passed）、`python scripts/smoke_http.py`、`python scripts/check_p0_fixes.py`。

### 升级期新增的坑（同样勿回退）
4. **响应体不要包信封**：`GET /episodes/{id}/story` 必须是扁平文档（与 `/script`、`/workflow` 一致）。曾包成 `{"story": …}`，导致前端 `source_text`/`analysis` 全取不到——刷新后故事正文变空、分析面板空白。
5. **分镜版本号只按内容变化递增**：`sync_shots_from_script` 曾给所有分镜 `current_version + 1`，违反 FR-014「不得修改未选中的分镜」。现用 `shot_fingerprint()` 比较 `_SHOT_COMPARE_FIELDS`，只有真的变了才递增。
6. **局部重生成要按镜号逐条匹配**：曾把同一个 patch 套用到所有选中分镜（多选时内容重复）。现 `_parse_shot_patches()` 兼容 `{"shots":[…]}`／裸列表／键值表／单个 patch 四种形态。
7. **自动重试只在 worker 路径**：`run_job_inline`（工作流节点）必须 `allow_auto_retry=False`，否则与节点级重试/回滚重复执行同一任务。`create_job` 默认 `max_attempts = 1 + AUTO_RETRY_LIMIT`。
8. **`events.publish()` 没有 `job_id` 参数**（只有 `episode_id`/`project_id`）。
9. **MP4 非标准 metadata 键需要 `-movflags +use_metadata_tags`**，否则 muxer 静默丢弃（`project_id` 曾因此从未写入文件）。
10. **`FREE_TEXT_KEYS` 只放批量正文**：`instruction`（用户手写的重生成指令）不在其中，否则会被 strict 截断成 40 字。
11. **前端 `storyApi.analyze` 是同步接口**，返回 `{analysis, story}`，不走 job 轮询。
12. **新增列必须配 Alembic 迁移**：MVP 运行时用 `Base.metadata.create_all`，它不会 ALTER 已存在的表；改 `models.py` 后要补 `alembic/versions/` 里的 revision 并对已有库执行升级（本次 `jobs.next_attempt_at`、`provider_settings.fallback_*`）。
13. **成本必须从 agent 一路带回 executor**：`executor._run` 只认 handler 返回的 `result["cost_cents"]`。图像/TTS/角色资产生成曾把它丢掉（角色资产还返回裸 `list`，现改 `(assets, cost_cents)`），于是实际单价永远对不上、只能按预留兜底。加成本相关改动后跑 `tests/test_cost_settlement.py`。
14. **渲染结算取 `manifest["duration_ms"]`**，不是 `total_duration_ms`。取错键会**静默按 0 结算**——合成永远不花钱，且不报错。
15. **`fps` 必须放在每个 `-vf` 链的最末尾**：`tpad`/`trim`/`setpts` 会让 xfade 报 `current rate of 1/0 is invalid`。正确顺序 `scale,pad,setsar,tpad,trim,setpts,fps=25`。
16. **`{gate}_confirmation` 之外的 JSON 键不要瞎猜**：`episode_dict` 的 `stage_before_block` 一直只写不读；解除阻塞必须显式调 `workflow_engine.restore_stage_after_unblock()`。
17. **agent 模块常写 `from ...registry import resolve`**（导入期绑定），测试里 monkeypatch 要打在 **agent 自己的命名空间**（如 `image_agent.resolve`），打在 `registry.resolve` 上不生效。
18. **幂等键带 5 秒时间桶**：`RENDER_PREVIEW` 等按墙钟窗口生成，工作流内自动合成与手工点预览会命中同一键并返回既有任务（既有任务没有本次预留）。写测试时要么推进窗口，要么直接断言「返回既有任务」。
19. **画幅比例在 `Project` 上，不在 `Episode` 上**：`episode.aspect_ratio` 不存在（会 AttributeError）。取尺寸用 `video_agent.episode_resolution(session, episode)` 或 `edit.resolution_for(project.aspect_ratio, tier)`。
20. **合成输出尺寸必须跟随项目画幅**：`RESOLUTIONS` 是历史遗留的固定竖屏表；现在统一走 `edit.resolution_for()/preset_dimensions()`（短边 720/1080，长边按比例算并取偶数）。改渲染相关代码后跑 `tests/test_timeline_validation.py`。
21. **SQLite 文件级恢复必须在停服时做**：服务在跑时 `restore()` 会默认拒绝（WAL 存在即判定有活跃连接），因为覆盖主库会让索引与旧 `-wal` 不一致（`PRAGMA integrity_check` 报 `wrong # of entries in index ...`）。测试里要显式 `force=True` 并自担风险。
22. **镜号重复由数据库唯一约束兜底**：`shots(episode_id, shot_no)` 有 UNIQUE，应用层插不进重复镜号。`validate_timeline` 里的 `DUPLICATE_SHOT_NO` 只是纵深防御，测试不能靠插重复数据来触发。
23. **`/config/limits` 而不是 `/limits`**：写成 `/limits` 会被 SPA 兜底路由吞掉，返回 200 + HTML，`r.json()` 报 `Expecting value: line 1 column 1`。所有 `settings` 里的枚举/上限都从 `/config/limits` 读。
24. **`RecordingProvider` 不能把所有被记录的方法都当协程**：`estimate_cost` 在 `MockVideoProvider` 里是 async，但在 `BaseProvider` 里是**同步**的（文本/图像/语音走它）。曾一律包成 `async def wrapper`，于是 `await` 一个 `CostEstimate` 直接 `TypeError: object CostEstimate can't be used in 'await' expression`。现在按 `inspect.iscoroutinefunction(target)` 选包法——改 `RECORDED_METHODS` 时要留意这条。
25. **`EntityVersion` 是既有的版本表，不要另起炉灶**：字段是 `entity_type`/`entity_id`/`version`/`payload`/`input_digest`。曾新建一张 `version_records` 表，纯属重复。加新功能前先 `grep` 既有模型。
26. **`STORY_TEXT_MAX_CHARS` 默认只有 2000**：写测试用 5000 字会 422。FR-010 的「长文本」阈值在 `story.LONG_TEXT_THRESHOLD`（500 字符）。
27. **§12 熔断只暂停「新任务」**：`degraded.ensure_available()` 抛 503，但已成功素材、剧本、素材包导出一律不受影响；断言别写成「整条链路都挂了」。测试里务必 `degraded.reset_all()`（`conftest` 之外用 autouse fixture），否则跨用例串味。
28. **`StoryInput` 现在多了 `shot_count`**：预估与生成共用 `story.build_script_prompt()`，两处若各拼一份提示词，FR-010 的预估就会与实扣对不上。
29. **写 `EntityVersion` 必须走 `versions.record()`**：曾在 `save_script`、`update_shot`、StoryAgent 生成三处各自手写 `EntityVersion(...)`，全都漏了 `input_digest`，结果是版本记录「有版本号但无法判断内容是否真变了」（去重失效、差异对比失真）。`record()` 现在支持显式 `version=`，供调用方自己维护版本号的场景使用。`tests/test_versions.py::test_every_version_row_has_a_digest` 会扫库兜底。
30. **统一错误体必须带 `trace_id`（API.md §0.1）**：由 `RequestSessionMiddleware` 每请求用 ContextVar 生成，错误体与响应头 `x-trace-id` 同值。`AppError`/`INTERNAL_ERROR`/`ValueError`/`RequestValidationError` 四条路径都要带；`RequestValidationError` 是后补的——此前参数校验失败返回的是 FastAPI 默认 `{"detail": [...]}`，前端解析不到 `message`。
31. **测 `backup.restore(force=True)` 前必须先 `engine.dispose()`**：连接池还持有时强制覆盖主库会产生 `database disk image is malformed`，而且**会污染同一进程里后续所有用例**（表现为毫不相干的 `test_backup` 报 `DROP TABLE xxx` 失败）。恢复后顺手断言一次 `PRAGMA integrity_check == ok`。
32. **前端 `formatCents` 在 `@/api/cost`，不在 `@/utils`**：`src/utils` 只有 `format.ts`。`ref()` 包 `DraftEnvelope<T>` 这类泛型对象会被深度解包成 `UnwrapRef<T>`，回填时 TS2345；需要保持原类型就用 `shallowRef`。
33. **改完前端要验 `dist`，不能只看 typecheck**：接口路径会落在**共享 chunk**（如 `story-*.js`、`video-*.js`）里，模板文案和 CSS 才在视图自己的 chunk。`Select-String -Path dist\assets\*.js -Pattern '<路径>'` 才是有效证据。
34. **别用 `Select-Object -First N` 起 uvicorn**：管道关闭会把服务一起带走（表现为 smoke 跑到一半 `ConnectionResetError` / 启动即退出）。用 `Start-Process ... -WindowStyle Hidden` 或在后台 job 里跑。

35. **字幕的台词来源不能只认 `AudioTrack`**：`edit.build_timeline()` 的 dialogue 轨只从已合成的 TTS 音轨派生，于是形成死结——不先跑 TTS 就没有 dialogue 轨 → 生不出字幕 → 但字幕本该在配音前就能校正（`generate_subtitles` 自己的 docstring 写的是「按确认台词与实际音频」）。现在 `generate_subtitles` 增加第二来源：没有 TTS 时退回分镜脚本的 `dialogue` / `narration`，时间轴按镜头起点在时长内均分估算；TTS 跑完重新生成即得精确时间。旁白兜底同样依赖 dialogue 轨，所以旧写法的 `if not cues:` 分支**永远进不去**——改这块要同时看两个分支。

36. **`PATCH /episodes/{id}/subtitles/{cue_id}` 原本没有任何校验**：可以存下 `start_ms=9000 > end_ms=1000` 的倒置字幕（播放器不显示、FFmpeg 的 `subtitles` 滤镜直接跳过该条，成片与导出 SRT 对不上）。现在写入前先算出「改完之后」的完整时间轴再校验：非整数、负数、`end <= start` 一律 422 且**不改库**。只改一端时必须用「新值 + 另一端原值」判定，不能只看提交上来的字段。

37. **`shot.to_dict()` 不展平 `shot_json`**：分镜的 `dialogue`/`narration`/`image_prompt` 等都在 `shot["shot_json"]` 里，顶层没有。写测试或前端取值时读 `s0['shot_json']['dialogue']`，读 `s0['dialogue']` 会静默得到 `None`，很容易误判成「后端没存下来」。

38. **`SubtitleCue.id` 必须显式声明**：该接口有 `[key: string]: unknown` 索引签名，不写 `id: string` 的话 `cue.id` 推成 `unknown`，`cueDraft[cue.id]` 直接 TS2538。加字段时注意索引签名会把漏声明的属性全变成 `unknown`。

39. **`mediaApi.upload()` 的 `rights_declared` 是 boolean**，且支持 `episode_id`。传字符串 `'true'` 会 TS2322；不传 `episode_id` 则素材不会挂到剧集下，音频页的素材下拉里看不到。

40. **`build_timeline` 只收 `required=True` 的分镜**：`sync_shots_from_script` 默认给新镜 `required=True`，但剧本里显式移除的镜号会被置为 `required=False` 并标 `stale`。排查「时间线为空」时先确认分镜的 `required`，而不是只看镜头数量。

41. **`session_scope()` 要在 HTTP 请求提交之后再开**：`TestClient` 的写请求由中间件在响应发出时统一 commit，若在同一进程里先开 `session_scope()` 再发请求，读到的会是旧数据（表现为「PUT /script 返回 200 且 shot_count=3，但时间线里 0 个镜头」）。诊断脚本要「先请求、后开会话」。

42. **项目名唯一**：重复创建同名项目返回 409，诊断脚本里用 `uuid.uuid4().hex[:8]` 拼名字，否则 `prj['id']` 直接 KeyError。


### 环境备忘
- 依赖装在仓库内 `.deps/`（`PYTHONPATH=<repo>\.deps`）；npm 走 `--cache <repo>\.tmp\npm-cache --registry https://registry.npmmirror.com`。
- FFmpeg 由 imageio-ffmpeg 提供（`.deps/imageio_ffmpeg/binaries/`），也可用 `FFMPEG_BIN` 覆盖。
- **不要**用 PowerShell `Get-Content -Raw | Set-Content -Encoding utf8` 改写 Python 源文件（PS 5.1 会按 CP936 重编码，毁掉中文）；用 edit/write 工具或 Python `encoding="utf-8"`。

43. 前端测试用独立的 vitest.config.ts（Vitest 优先读它，不污染 vite.config.ts）。jsdom **不实现** matchMedia 与 URL.createObjectURL，必须在 vitest.setup.ts 里补，否则引用它们的组件一挂载就炸。
44. useAutosave 的测试里，**在 mount 之前**就赋好值的 
ef 永远不会触发 watch —— 用例会「通过」但其实什么都没测。必须 mount 之后再赋值。本轮 4 个用例就栽在这上面。
45. vi.mock 工厂被提升到文件顶部，引用外部变量必须用 vi.hoisted。另外替身必须补齐**消费者在构造期就会读的字段**（如 sse.lastEventId / sse.eventCount），少一个 store 就抛。
46. MediaThumb 的灯箱是 <Teleport to=body>，只能查 document.body.querySelector('.lightbox')，wrapper.find 找不到。
47. stores/episode.ts **没有对外暴露** handleEvent（它只是传给 useSse 的内部回调）。要测事件分发就捕获传给 useSse 的 onEvent 再手动喂事件，别去 store.handleEvent。
48. SseEvent 的字段是 receivedAt: number（不是 at），构造测试事件时漏了会 TS2352。
49. locToLabel(['shots', 2, 'duration_ms']) 的真实输出是 分镜 · 第 3 镜 · 时长 —— **会先输出容器名**，不是只有下标部分。
50. 测试文件放在 src/** 下会被 vue-tsc -p tsconfig.json 一起类型检查（所以它们必须严格类型正确）；但 vite build 不会打包它们（从 index.html 不可达）。已核实 dist 中不含测试代码。
51. 
pm test = vitest run（跑完退出，适合 CI）；
pm run test:watch 才是常驻。
52. `init_db()` 用 `create_all` 建表，**不会 ALTER 已有表**；而 `create_all` 建出来的开发库往往**从没 stamp 过**，`alembic_version` 是空的 —— 此时 `alembic upgrade head` 会从头重放 init 迁移、在「表已存在」上炸掉。正确姿势：先 `alembic stamp <上一个 head>`，再 `alembic upgrade head`。pytest 用临时库 + create_all，不受影响。
53. Alembic 给已有表加 NOT NULL 列必须带 `server_default`，否则既有行读出来是 NULL（`character_assets.voice_speed` 用了 `server_default="1.0"`；14 行既有数据补上 1.0 而不是 NULL）。
54. **用 Python 脚本往文件里追加文本时，普通字符串里的 `\v` / `\a` / `\r` 会被解释成控制字符，静默吃掉原文字母** —— 本轮就把 `vitest` 写成 `\vitest`（变成 VT+`itest`）、`at` 写成 `\at`（变成 BEL+`t`）。要么用 raw 字符串（前缀 r 的三引号串），要么写完立刻按字节扫一遍 `ord(c) < 32` 的控制字符。这次就是靠这个扫描才发现的。
55. 角色页模板本身是合法的（4 个 `<ModalDialog>`、51/51 个 `<div>` 配平）；但用 `s.index('</template>')` 取模板会**停在 footer 插槽的 `</template>`** 上，于是 tag 计数看起来全都不匹配。要取完整模板必须用 `rindex`。
56. 全局样式里**没有** `.visually-hidden`，也**没有** `.hint.warn`（只有 `.hint`）；`.badge-warn` 是有的。加 class 之前先 grep 一下 `src/styles/base.css`。
57. toast store 有 `success` / `info` / `warn` / `error` 四个方法（`success` 是有的）。
58. 试听接口 `GET /voices/{voice_id}/preview` 虽然是个 GET，但**可能触发一次真实供应商合成**（慢），结果会缓存进 `voice.preview_media_id`，之后不再重复计费；返回 `{media_id, url}`。
59. **把某个前置条件从「宽松」改成「严格」会打掉依赖宽松行为的既有用例** —— 本轮 FR-050 让「有台词未绑音色」直接 409，`tests/test_quota.py` 的两条 TTS 配额用例原先靠「未绑音色也能提交」，必须补上绑定步骤才能测到配额分支。改严格前置条件后一定要跑全量用例。
60. `Path.read_text()` 默认做通用换行转换（`\r\n` → `\n`），`write_text(..., newline='')` 会原样写出 —— 顺手就把整个文件的换行风格从 CRLF 改成 LF 了。改完要确认换行风格符合预期。
61. FR-050 的关键取舍：以前「未绑音色就把那几句台词静默跳过」，成片缺声音且用户很难发现；现在改成**提交前整体拒绝**（409 `MISSING_VOICE`，点名角色 + 镜号，不留任务、不占配额），并由 `audio_agent.dialogue_requirements()` 作为**唯一判定来源**，预估接口和提交接口共用它 —— 避免两边判定漂移。
62. FR-051 的语速要**三处同时留档**（角色 `voice_speed`、`AudioTrack.speed`、素材 `metadata.speed`），并且改音色或改语速都要把该角色已有的 `kind=="dialogue"` 音轨标 `stale` —— 否则导出时会拿旧配音配新语速。
63. 工作流里的「最小闭环自动补音色」不能删（`scripts/smoke_http.py` 从不绑音色，全靠它），但排序要写成 `(created_at, id)`：只按 `created_at` 排，时间戳相同时顺序不稳定，会让同一项目两次跑出来的配音不一样。
64. `init_db()` 用的是 `create_all`，它**只建表不改表** —— 任何新增列/新增表都必须配一条 Alembic revision。而且开发库历史上是 `create_all` 建起来的、从未 stamp 过，`alembic_version` 是空的，直接 `upgrade head` 会报「table already exists」。正确姿势：先 `alembic stamp <上一个 head>` 再 `upgrade head`。测试库每次用全新临时库 + `create_all`，所以**测试不需要迁移、开发库需要**，容易只顾一边。
65. 给既有表加 NOT NULL 列必须带 `server_default`（`audio_tracks.source` 用 `server_default="tts"`），否则既有行没法回填，迁移直接失败。
66. **「算出来的标志位」不等于「生效的门禁」** —— `safety_agent.episode_risk_summary()` 一直在算 `can_export`，但 `routes/exports.py` 与 `services/render.py` 里**零处引用**，PRD 的「禁止确认和导出」其实从未实现。凡是新增/修改闸门类逻辑，收尾一定要 grep 消费方，确认它真的被调用，而不是只在某个 summary 接口里露个脸。
67. 「确认」和「导出」是两条独立路径：**确认**走门控审批 `POST /workflow/resume`（`decision=approve`），**导出**走 `render/preview`、`render/export`、`export/package`。只在导出侧加闸门会漏掉一半 —— 闸门必须落在两侧。
68. 申诉必须是**人工通道**，系统不能自动放行：`submit_appeal` 只写 `PENDING`，只有 `resolve_appeal(status="APPROVED")` 才把该素材移出 `unresolved`。若提交即放行，申诉入口就是绕过审核的后门；若永远不放行，申诉入口又等于摆设。另外同一目标只允许一条待审申诉（`409`），否则工单会被重复提交刷屏。
69. 会进**错误体 `details`** 的 dict 不能含 `datetime` —— 错误处理器用 `json.dumps` 序列化，`datetime` 会直接抛 `TypeError: Object of type datetime is not JSON serializable`。`appeal_to_dict` 的时间戳统一转 `.isoformat()`。这个坑很隐蔽：只有「存在申诉」时 details 里才带 datetime，无申诉时空列表不报错，所以相邻用例是绿的，直到驳回/批准那条才炸。
70. 「还没生成音频」和「有意跳过配音」在数据上必须能区分 —— 这就是加 `audio_tracks.source`（`tts`/`manual`/`skipped`）的唯一理由。没有它，前端无法判断某句该不该显示降级入口，也没法把「用户已决定跳过」和「忘了生成」分开统计。
71. 同一句台词的兜底音频必须**替换**而不是并存：`attach_manual_audio` 先按「同一镜头 + 同一 `line_index`」找已有音轨再改，否则时间线会重复播放同一句。
72. 手写 multipart 上传时，文件分片的 `Content-Type` 要写成服务端白名单里的真实 MIME（`audio/mpeg`、`image/png`）。写 `application/octet-stream` 会被上传校验以 `409 STATE_CONFLICT` 拒掉。用 `requests`/`httpx` 时它们会按扩展名自动推断，所以**只有手写 multipart 才会踩到**这个坑。
73. **创建用的 Pydantic 模型不能直接当 PATCH body** —— `StyleIn` 的字段都带非 None 默认值（`name="默认画风"`、`palette=[]`），而后端 `update_style` 的判空是 `value is not None`，于是「只改描述」会把 `name` 重置、`palette` 清空、`camera_style`/`lighting_style` 归零，属于**静默数据丢失**。凡 `PATCH` 必须用独立的 `XxxUpdate`（全 `None` 默认）模型，并跑一条「只传一个字段，断言其余字段原样」的用例。
74. 软归档接口（`DELETE` 只置 `status=ARCHIVED`）**必须同时让列表默认过滤掉归档项**，否则前端「归档」后一刷新对象又回来了，用户会以为操作没生效。本项目统一约定 `include_archived: bool = Query(False)`（`GET /projects` 早已如此）。
75. 前端 API 层的返回类型要跟后端实际返回的形状对齐：`POST /styles/{id}/apply` 返回的是 `{project_id, style_id, affected_shots}`，早期前端却声明成 `StyleAsset`。类型对不上时 TS 不报错，但 UI 就拿不到 `affected_shots`，也就无法如实告知「切换画风会让 N 个分镜过期」。**核实返回形状要看后端代码，不要照抄接口名猜**。
76. **不要假设「列表第一条就是自己刚建的那条」** —— 项目自带一个默认画风，`GET /projects/{id}/styles` 的首条可能是它。真实核验脚本第一版就因为这个假设误报失败。按 `id` 取自己创建的对象。
77. 页面里的纯逻辑（过滤、统计、表单校验、标签映射）抽到 `utils/xxx.ts` 再单测，比为了测「搜索是否大小写不敏感」去挂载整个 `.vue` 便宜得多；本项目 `views/*.vue` 至今 0% 覆盖率，全靠这层兜住易退化点。
78. 项目已有组件/工具类不能凭印象写：本轮误用了 `.grid-tiles`（不存在）和 `.warn-text`（只存在于 `EpisodeRenderView.vue` 的 scoped CSS）。**概览数字网格在本项目的约定是每个视图局部定义 `.stat-row`**，`base.css` 里并没有它。写新页面前先 grep 类名定义处。
79. 本项目的「角色状态」与「画风状态」共用同一套 `AssetStatus`（`DRAFT`/`CONFIRMED`/`ARCHIVED`/…），所以 `characterStatusLabel`/`characterStatusTone` 可以直接复用到画风上，不必另写一份映射。
80. 页面 catch 里**绝不能写 `err instanceof ApiError ? err : null`** —— 非 `ApiError` 会变成 `null`，页面随即渲染成空态（「暂无数据」/「还没有内容」），用户看到的是「你没有数据」而不是「加载失败，可重试」。本项目曾有 7 处这么写。统一用 `api/errors.ts` 的 `toApiError(err)`（`err instanceof ApiError ? err : networkError(err)`），它保证**永不返回 null**。判据：凡是把异常写进 `loadError` / `error` 这类渲染状态的地方，都必须是 `toApiError`。
81. 新增 `.vue` 视图测试时，`ModalDialog` 会 `Teleport to="body"`，弹窗内容**不在 wrapper 里**，`wrapper.find('#id')` 会拿到空 DOMWrapper。用内联替身 stub 掉弹窗（`props: ['open','title']` + 渲染默认插槽与 `footer` 插槽），内容就留在 wrapper 内。测的是视图接线，不是弹窗机制。
82. jsdom 会把内联样式里的 `#hex` 规范化成 `rgb(...)`，断言 `style` 属性时要按 `rgb(27, 42, 65)` 写，别按 `#1b2a41` —— 否则测试失败但代码是对的。
83. 组件库/工具类不要凭印象写：本轮误用了 `.grid-tiles`（全项目不存在）与 `.warn-text`（只在某个视图的 scoped CSS 里）。本项目「概览数字网格」的约定是**每个视图局部定义 `.stat-row`**，`base.css` 里并没有它。写新页面前先 grep 类名定义处。
84. PowerShell 的 here-string（`@"..."@`）**会把反引号当转义符**，所以凡是含 Markdown 反引号或 Python 反引号的代码塞进 `python -c @"..."@` 都会被吃坏，报出莫名其妙的 `SyntaxError: unterminated string literal`。含反引号时一律写临时 `.py` 文件再执行。
85. `backend/scripts/*.py` 里要 `import app` 时，必须先 `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` —— 直接 `python scripts/x.py` 时 `sys.path[0]` 是 `scripts/` 而不是 `backend/`，否则 `ModuleNotFoundError: No module named 'app'`。`scripts/backup_db.py` 是现成范例。
86. 渲染结果的计数在 `render["manifest"]` 里（`bgm_count` / `dialogue_count` / `shot_count`），**不在 `render` 顶层**。写断言前先看 `handle_render_preview` 返回的 `job.result_ref` 实际形状。
87. `render._locate_error()` 返回的是**列表且永不为空**（定位不到时兜底一条 `track="unknown"`）。别写 `located.get("shot_no")`；要按列表断言，并检查每条都有 `track` + `message`。
88. 写失败路径的测试，**先断言前置条件成立再注入故障**。例：测 FFmpeg 失败要先 `assert validate_timeline(...)["valid"] is True`，否则失败可能来自「时间线缺料」而不是 FFmpeg —— 测试会假绿，看着过了其实什么都没验到。
89. 名字叫「XX 失败」的既有测试未必真的模拟了 XX。本轮发现 `test_render_error_locates_the_shot` 只测了空时间线的校验失败，**从没模拟过 FFmpeg 失败**。补验收缺口时先读既有测试的断言，别被函数名骗过去。
