/**
 * hoyo漫剧 前端类型定义。
 * 字段名严格对齐 docs/API.md，不做任何臆造。
 */

/* ------------------------------------------------------------------ *
 * 0. 通用
 * ------------------------------------------------------------------ */

export type ErrorCode =
  | 'VALIDATION_ERROR'
  | 'NOT_FOUND'
  | 'CONFLICT'
  | 'STATE_CONFLICT'
  | 'IDEMPOTENCY_REPLAY'
  | 'PROVIDER_ERROR'
  | 'PROVIDER_TIMEOUT'
  | 'SCHEMA_VALIDATION_FAILED'
  | 'SAFETY_BLOCKED'
  | 'QUOTA_EXCEEDED'
  | 'STORAGE_ERROR'
  | 'RENDER_ERROR'
  | 'CHECKPOINT_STALE'
  | 'INTERNAL_ERROR'
  | 'NETWORK_ERROR'
  | 'CLIENT_ERROR'

/** 统一错误体 `{error:{code,message,details,retryable,trace_id}}`。 */
export interface ApiErrorBody {
  code: ErrorCode | string
  message: string
  details?: Record<string, unknown> | null
  retryable?: boolean
  trace_id?: string | null
}

/** 字段级校验错误（前端归一化后的形态）。 */
export interface FieldIssue {
  /** 表单字段路径，如 `shots.2.duration_ms`。 */
  path: string
  /** 数组路径可读化，如 `第 3 镜 · 时长`。 */
  label?: string
  message: string
  /** 后端 details.loc 原始数组。 */
  loc?: (string | number)[]
}

export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/* ------------------------------------------------------------------ *
 * 1. 配置
 * ------------------------------------------------------------------ */

export interface ExportPreset {
  id?: string
  /** 后端可能返回字符串数组，也可能返回对象数组。 */
  label?: string
  width?: number
  height?: number
  bitrate_kbps?: number
  [key: string]: unknown
}

export interface Limits {
  max_concurrent_jobs: number
  candidates_per_shot_default: number
  candidates_per_shot_max: number
  shot_duration_ms_min: number
  shot_duration_ms_max: number
  episode_duration_ms_max: number
  preview_bitrate_kbps: number
  export_presets: (ExportPreset | string)[]
  aspect_ratios: string[]
  default_language: string
  /** FR-061 转场可选类型与时长上限（来自 /config/limits，前端不写死） */
  transition_types?: { id: string; label: string }[]
  transition_default_ms?: number
  transition_max_ms?: number
  /** §11.3 上传限制 */
  upload_max_bytes?: number
  upload_workspace_quota_bytes?: number
  upload_allowed_mimes?: string[]
  [key: string]: unknown
}

/** FR-061 转场（从上一镜切入本镜）。 */
export interface TransitionSpec {
  type: string
  duration_ms?: number
}

export type ProviderKind = 'text' | 'image' | 'video' | 'speech' | 'audio' | string

export interface ProviderInfo {
  kind: ProviderKind
  id: string
  display_name: string
  models?: (string | { id: string; display_name?: string })[]
  capabilities?: Record<string, unknown>
  configured?: boolean
}

export interface HealthStatus {
  status: string
  version?: string
  db?: string
  storage?: string
  ffmpeg?: string
  queue?: string
  providers?: Record<string, unknown> | string
  graph_engine?: string
}

/* ------------------------------------------------------------------ *
 * 2. 项目
 * ------------------------------------------------------------------ */

export interface Project {
  id: string
  name: string
  genre?: string | null
  aspect_ratio?: string | null
  default_language?: string | null
  style?: string | null
  default_style_id?: string | null
  archived?: boolean
  archived_at?: string | null
  created_at?: string
  updated_at?: string
  episode_count?: number
  total_cents?: number
  [key: string]: unknown
}

export interface ProjectCreateInput {
  name: string
  genre: string
  aspect_ratio: string
  default_language: string
  style?: string
}

export type ProjectPatch = Partial<ProjectCreateInput>

export interface CostBucket {
  stage?: string
  kind?: string
  cents: number
}

export interface CostSummary {
  total_cents: number
  by_stage?: CostBucket[]
  by_kind?: CostBucket[]
  remaining_quota_cents?: number
  [key: string]: unknown
}

export interface AuditEntry {
  id?: string
  action?: string
  actor?: string
  target?: string
  detail?: string
  created_at?: string
  [key: string]: unknown
}

export interface Quota {
  limit_cents: number
  used_cents: number
  reserved_cents: number
  remaining_cents: number
  concurrent_limit: number
  running_jobs: number
}

/* ------------------------------------------------------------------ *
 * 3. 剧集
 * ------------------------------------------------------------------ */

export type EpisodeStage =
  | 'DRAFT'
  | 'SCRIPT_REVIEW'
  | 'ASSET_REVIEW'
  | 'SHOT_REVIEW'
  | 'VIDEO_REVIEW'
  | 'AUDIO_REVIEW'
  | 'RENDERING'
  | 'PREVIEW_READY'
  | 'EXPORTED'
  | 'BLOCKED'
  | 'STALE'

export type GateId =
  | 'gate_script_confirmation'
  | 'gate_character_confirmation'
  | 'gate_shot_confirmation'
  | 'gate_video_cost_confirmation'
  | 'gate_video_confirmation'
  | 'gate_export_confirmation'

export interface Episode {
  id: string
  project_id?: string
  title: string
  episode_no?: number | null
  target_duration_ms?: number | null
  stage: EpisodeStage
  stale_flags?: string[] | Record<string, unknown> | null
  /** §10.1 阻塞前的阶段；解除阻塞后回到这里 */
  stage_before_block?: string | null
  blocked_reason?: string | null
  /** §10.1 受影响资源待用户决定：只标记 STALE，不删除下游产物 */
  stale_shot_count?: number
  stale_reasons?: string[]
  archived?: boolean
  archived_at?: string | null
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export interface EpisodeCreateInput {
  title: string
  episode_no?: number
  target_duration_ms: number
}

export interface WorkflowRun {
  id: string
  status: string
  current_node?: string | null
  checkpoint_id?: string | null
  pending_gate?: GateId | string | null
  state?: Record<string, unknown> | null
  updated_at?: string
}

export interface WorkflowGraphNode {
  node?: string
  id?: string
  status?: string
  label?: string
  gate?: string | null
  [key: string]: unknown
}

export interface WorkflowLogEntry {
  node?: string
  level?: string
  message?: string
  created_at?: string
  [key: string]: unknown
}

export interface EpisodeOverview {
  stage?: EpisodeStage
  pending_gate?: GateId | string | null
  workflow?: WorkflowRun | null
  job_stats?: Record<string, number> | null
  asset_counts?: Record<string, number> | null
  stale_flags?: string[] | Record<string, unknown> | null
  [key: string]: unknown
}

export interface ResumeInput {
  workflow_run_id: string
  checkpoint_id: string
  gate: GateId | string
  decision: 'approve' | 'reject' | 'edit'
  payload?: Record<string, unknown>
  target_version?: number
}

/* ------------------------------------------------------------------ *
 * 4. 故事与剧本
 * ------------------------------------------------------------------ */

export type StoryInputType = 'idea' | 'text' | 'manual'

export interface StoryDocument {
  id?: string
  input_type?: StoryInputType
  source_text?: string
  version?: number
  analysis?: StoryAnalysis | null
  created_at?: string
  [key: string]: unknown
}

export interface StoryAnalysis {
  theme?: string
  characters?: string[]
  /** 人物关系（PRD FR-011 要求五项之一）。 */
  relationships?: { from?: string; from_?: string; to?: string; relation?: string }[]
  scenes?: string[]
  beats?: string[]
  conflict?: { goal?: string; obstacle?: string; stakes?: string }
  raw_summary?: Record<string, unknown>
  [key: string]: unknown
}

export interface StoryVersion {
  version: number
  created_at?: string
  input_type?: StoryInputType
  source_text?: string
  [key: string]: unknown
}

/** FR-010：长文本入口的字符数与预计生成成本。 */
export interface StoryEstimate {
  episode_id: string
  /** 用户输入的原始字符数 */
  source_chars: number
  /** 提示词字符数（与真实调用同一份） */
  prompt_chars: number
  system_chars: number
  /** 计费字符数 = 提示词 + 系统提示 */
  billing_chars: number
  estimate_cents: number
  billing_unit: string
  unit_price_cents: number
  /** 供应商是否声明了单价。false 时 estimate_cents 为 0，**不代表免费** */
  declared: boolean
  provider: string
  model: string
  shot_count: number
  estimated_wait_seconds?: number
  long_text: boolean
  long_text_threshold: number
}

/* ------------------------------------------------------------------ *
 * FR-004 版本记录
 * ------------------------------------------------------------------ */

export interface VersionRecord {
  id: string
  entity_type: string
  /** 后端给的中文标签（剧本/角色描述/分镜提示词/时间线…） */
  entity_label: string
  entity_id: string
  version: number
  input_digest?: string
  note: string
  episode_id?: string | null
  created_at?: string
  [key: string]: unknown
}

export interface VersionDiffField {
  field: string
  before: unknown
  after: unknown
}

export interface VersionDiff {
  changed_fields: VersionDiffField[]
  changed_count: number
}

export interface StoryBibleCharacter {
  name: string
  identity?: string
  age_range?: string
  appearance?: string
  outfit?: string
  personality?: string
  speech_style?: string
  forbidden_changes?: string[]
}

export interface StoryBibleScene {
  scene_no: number
  location?: string
  time_of_day?: string
  summary?: string
}

export interface StoryBibleConflict {
  goal?: string
  obstacle?: string
  stakes?: string
}

export interface StoryBibleRelationship {
  from: string
  to: string
  relation: string
}

export interface StoryBible {
  logline?: string
  theme?: string
  tone?: string
  setting?: string
  characters: StoryBibleCharacter[]
  scenes: StoryBibleScene[]
  conflict?: StoryBibleConflict
  relationships?: StoryBibleRelationship[]
}

export interface Beat {
  beat_no: number
  name?: string
  summary?: string
  scene_no?: number
  shots?: number[]
}

export interface DialogueLine {
  character: string
  text: string
  emotion?: string
}

export interface VideoParams {
  duration_ms?: number
  motion_strength?: number
  camera_movement?: string
  [key: string]: unknown
}

export interface ShotScript {
  shot_no: number
  duration_ms: number
  scene_no?: number
  characters?: string[]
  action?: string
  camera_movement?: string
  composition?: string
  emotion?: string
  dialogue?: DialogueLine[]
  narration?: string
  sfx?: string[]
  image_prompt?: string
  negative_prompt?: string
  video_params?: VideoParams
  required?: boolean
  [key: string]: unknown
}

/** 剧本 JSON：StoryBible + BeatSheet + ShotScript。 */
export interface StoryJson {
  story_bible: StoryBible
  beat_sheet: Beat[]
  shots: ShotScript[]
}

export interface ScriptDocument {
  story_json: StoryJson
  version: number
  updated_at?: string
  [key: string]: unknown
}

export interface ScriptValidationResult {
  valid: boolean
  errors: { loc?: (string | number)[]; msg: string }[]
  warnings: { loc?: (string | number)[]; msg: string }[]
}

export interface RegenerateShotsInput {
  shot_nos: number[]
  replan_downstream?: boolean
  instruction?: string
}

/* ------------------------------------------------------------------ *
 * 5. 角色与画风
 * ------------------------------------------------------------------ */

export type CharacterAssetKind = 'setting' | 'front' | 'half_body' | 'expression'

export interface Character {
  id: string
  project_id?: string
  name: string
  identity?: string
  age_range?: string
  appearance?: string
  outfit?: string
  personality?: string
  speech_style?: string
  forbidden_changes?: string[]
  primary_media_id?: string | null
  voice_id?: string | null
  /** FR-051：语速倍率（0.5~2.0），默认 1.0。 */
  voice_speed?: number | null
  /** 后端返回 `AssetStatus`（DRAFT / CONFIRMED / STALE …）。 */
  status?: string | null
  version?: number | null
  /** 是否主角；false 表示配角。 */
  is_main?: boolean | null
  archived?: boolean
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export type CharacterInput = Omit<Character, 'id' | 'created_at' | 'updated_at'> &
  Partial<Pick<Character, 'id' | 'created_at' | 'updated_at'>>

export interface CharacterAsset {
  media_id: string
  version?: number
  asset_kind?: CharacterAssetKind | string
  url?: string
  model?: string
  prompt?: string
  params?: Record<string, unknown>
  created_at?: string
  status?: string
  [key: string]: unknown
}

export interface CharacterGenerateInput {
  asset_kinds: CharacterAssetKind[]
  count_per_kind: number
}

export interface ReplacePrimaryResult {
  affected_shot_nos?: number[]
  affected_shots?: { shot_no: number; shot_id?: string }[]
  impact?: string
  [key: string]: unknown
}

export interface StyleApplyResult {
  project_id?: string
  style_id?: string
  /** 切换画风后被标记过期的分镜数（后端真实返回，前端必须如实告知用户） */
  affected_shots?: number
  [key: string]: unknown
}

/** §5.1-6：项目级资产库的概览计数（纯函数产出，便于单测）。 */
export interface ProjectAssetCounts {
  characters: number
  styles: number
  /** 已确认主参考图的角色数 */
  withReference: number
  /** 已绑定音色的角色数（FR-050 前置条件） */
  withVoice: number
  defaultStyleId?: string | null
}

export interface StyleAsset {
  id: string
  project_id?: string
  name?: string
  description?: string
  palette?: string[] | string
  camera_style?: string
  lighting_style?: string
  negative_prompt?: string
  is_default?: boolean
  /** 后端返回 `AssetStatus`；与角色共用同一套标签映射。 */
  status?: string | null
  /** 每次 PATCH 递增，界面据此显示 `v1`/`v2`。 */
  version?: number | null
  created_at?: string
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 6. 分镜与画面
 * ------------------------------------------------------------------ */

export type MediaStatus = 'PENDING' | 'READY' | 'FAILED' | 'REJECTED' | string
export type SafetyState = 'PASS' | 'REVIEW' | 'BLOCK' | string

export interface ShotCandidate {
  media_id: string
  version?: number
  url?: string
  status?: MediaStatus
  safety?: SafetyState
  created_at?: string
  rejected_reason?: string | null
}

export interface Shot {
  id: string
  episode_id?: string
  shot_no: number
  duration_ms: number
  scene_no?: number
  characters?: string[]
  action?: string
  camera_movement?: string
  composition?: string
  emotion?: string
  dialogue?: DialogueLine[]
  narration?: string
  sfx?: string[]
  image_prompt?: string
  negative_prompt?: string
  video_params?: VideoParams
  required?: boolean
  /** FR-061 转场（从上一镜切入本镜）；null/缺省表示硬切。 */
  transition?: TransitionSpec | null

  /** 候选图与确认状态。 */
  candidates?: ShotCandidate[]
  selected_media_id?: string | null
  confirmed_media_id?: string | null
  confirmed_at?: string | null
  stale?: boolean
  stale_reason?: string | null

  /** 视频 / 音频状态。 */
  video_status?: string | null
  video_media_id?: string | null
  video_skipped?: boolean
  video_fallback?: string | null
  audio_status?: string | null
  audio_stale?: boolean

  [key: string]: unknown
}

export interface ShotPatch {
  duration_ms?: number
  dialogue?: DialogueLine[]
  characters?: string[]
  composition?: string
  camera_movement?: string
  action?: string
  emotion?: string
  image_prompt?: string
  negative_prompt?: string
  narration?: string
  sfx?: string[]
  required?: boolean
  /** FR-061 转场；传 null 表示清除 */
  transition?: TransitionSpec | null
}

export interface GenerateImagesInput {
  shot_nos: number[]
  candidates_per_shot: number
}

export interface ShotVideoClip {
  media_id?: string
  url?: string
  status?: string
  duration_ms?: number
  job_id?: string
  created_at?: string
  error?: string | null
  [key: string]: unknown
}

export interface ShotVideoState {
  status?: string
  confirmed_media_id?: string | null
  skipped?: boolean
  fallback?: string | null
  history?: ShotVideoClip[]
  clips?: ShotVideoClip[]
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 7. 视频
 * ------------------------------------------------------------------ */

export interface VideoEstimateInput {
  shot_nos: number[]
  params?: Record<string, unknown>
}

export interface VideoEstimatePerShot {
  shot_no: number
  estimate_cents?: number
  duration_ms?: number
  [key: string]: unknown
}

export interface VideoQuote {
  quote_id: string
  provider?: string
  model?: string
  estimate_cents: number
  quota_after_cents?: number
  estimated_wait_seconds?: number
  per_shot?: VideoEstimatePerShot[]
  expires_at?: string
  [key: string]: unknown
}

export interface VideoGenerateInput {
  shot_nos: number[]
  params?: Record<string, unknown>
  quote_id: string
  idempotency_key: string
  confirm: true
}

/* ------------------------------------------------------------------ *
 * FR-040 视频参数（画幅比例在项目上，逐镜参数在 shot_json.video_params）
 * ------------------------------------------------------------------ */

export interface AspectPreset {
  value: string
  width: number
  height: number
}

export interface ShotVideoParams {
  shot_id: string
  shot_no: number
  duration_ms: number
  motion_strength: number
  camera_movement: string
  [key: string]: unknown
}

export interface VideoParamsView {
  episode_id: string
  aspect_ratio: string | null
  width: number
  height: number
  aspect_presets?: AspectPreset[]
  duration_ms_min?: number
  duration_ms_max?: number
  motion_strength_min?: number
  motion_strength_max?: number
  shots?: ShotVideoParams[]
}

/* ------------------------------------------------------------------ *
 * 8. 音频与字幕
 * ------------------------------------------------------------------ */

export interface Voice {
  id: string
  name?: string
  provider?: string
  language?: string
  gender?: string
  sample_url?: string
  preview_url?: string
  builtin?: boolean
  [key: string]: unknown
}

export interface TtsLine {
  shot_no?: number
  shot_id?: string
  character?: string
  character_id?: string
  text?: string
  voice_id?: string
  media_id?: string | null
  url?: string | null
  status?: string
  stale?: boolean
  /** §12-4：tts / manual（用户上传顶替）/ skipped（有意跳过） */
  source?: string
  provider?: string
  speed?: number
  emotion?: string
  created_at?: string
  [key: string]: unknown
}

export type TrackKind = 'bgm' | 'ambience' | 'sfx'

/** §12-4：单句台词的配音状态。`missing` 才需要给「上传音频 / 跳过」入口。 */
export type TtsLineState = 'ready' | 'missing' | 'stale' | 'skipped'

export interface TtsLineStatus {
  shot_id?: string
  shot_no?: number
  character?: string
  text?: string
  emotion?: string
  status: TtsLineState
  /** tts（合成）/ manual（用户上传顶替）/ skipped（有意跳过） */
  source?: string
  media_id?: string | null
  track_id?: string | null
  stale?: boolean
  stale_reason?: string
}

/** §12-8：内容拦截申诉。 */
export interface SafetyAppeal {
  id: string
  episode_id?: string
  project_id?: string | null
  target_type: 'asset' | 'input' | 'episode' | string
  target_id?: string | null
  reason?: string
  detail?: string
  status: 'PENDING' | 'APPROVED' | 'REJECTED' | string
  resolution_note?: string
  created_at?: string | null
  updated_at?: string | null
}

/** §12-8：导出闸门状态。 */
export interface ExportGate {
  can_export: boolean
  blocked: string[]
  unresolved: string[]
  pending?: string[]
  rejected?: string[]
  approved?: string[]
  appeals?: SafetyAppeal[]
  aigc?: Record<string, unknown>
  [key: string]: unknown
}

/** FR-050：TTS 前置条件里的一条阻断项（哪个角色的哪几镜没绑音色）。 */
export interface TtsBlockingProblem {
  code: string
  character: string
  shot_nos: number[]
  message: string
}

/** FR-050 / FR-051：TTS 预估 + 前置条件。 */
export interface TtsEstimate {
  episode_id?: string
  shot_count?: number
  provider?: string
  model?: string
  estimate_cents: number
  remaining_cents?: number
  quota_after_cents?: number
  line_count: number
  skipped_count: number
  char_count?: number
  unit_price_cents?: number
  /** 前置条件是否满足；false 时提交必被 409 拒绝。 */
  can_generate: boolean
  blocking_problems: TtsBlockingProblem[]
  speaking_characters?: string[]
  has_dialogue?: boolean
  [key: string]: unknown
}

export interface AudioTrack {
  id?: string
  media_id: string
  kind: TrackKind
  start_ms: number
  end_ms: number
  volume_db: number
  fade_in_ms: number
  fade_out_ms: number
  rights_declared: boolean
  name?: string
  url?: string
  [key: string]: unknown
}

export interface SubtitleCue {
  /** 字幕条目主键，PATCH /subtitles/{cue_id} 用它定位。 */
  id: string
  index?: number
  start_ms?: number
  end_ms?: number
  text: string
  shot_no?: number
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 9. 时间线 / 合成 / 导出
 * ------------------------------------------------------------------ */

export interface TimelineClip {
  shot_no: number
  shot_id?: string
  start_ms?: number
  duration_ms?: number
  video_media_id?: string | null
  skipped?: boolean
  fallback?: string | null
  dialogue?: unknown[]
  [key: string]: unknown
}

export interface Timeline {
  episode_id?: string
  total_duration_ms?: number
  clips?: TimelineClip[]
  shots?: TimelineClip[]
  audio_tracks?: AudioTrack[]
  subtitles?: SubtitleCue[]
  [key: string]: unknown
}

export interface TimelineError {
  code: string
  message: string
  shot_no?: number | null
  track?: string | null
}

export interface TimelineValidation {
  valid: boolean
  errors: TimelineError[]
  warnings: TimelineError[]
}

export type ExportKind = 'preview' | 'export' | 'package' | string

export interface ExportRecord {
  id: string
  status: string
  kind: ExportKind
  media_id?: string | null
  url?: string | null
  error?: string | null
  manifest?: Record<string, unknown> | null
  preset?: string | null
  created_at?: string
  [key: string]: unknown
}

export interface RenderPreviewInput {
  watermark?: boolean
}

export interface RenderExportInput {
  preset: string
  watermark: boolean
}

/* ------------------------------------------------------------------ *
 * 10. 任务
 * ------------------------------------------------------------------ */

export type JobStatus =
  | 'CREATED'
  | 'QUEUED'
  | 'RUNNING'
  | 'WAITING_CONFIRMATION'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'RETRYING'
  | 'CANCELLING'
  | 'CANCELLED'
  | 'EXPIRED'

export type JobType =
  | 'STORY_GENERATION'
  | 'IMAGE_GENERATION'
  | 'VIDEO_GENERATION'
  | 'TTS_GENERATION'
  | 'RENDER'
  | 'EXPORT'
  | 'PACKAGE'
  | string

export interface JobResultRef {
  media_ids?: string[]
  [key: string]: unknown
}

export interface Job {
  id: string
  workflow_run_id?: string | null
  episode_id?: string | null
  project_id?: string | null
  type: JobType
  status: JobStatus
  stage?: string | null
  progress?: number
  attempt?: number
  max_attempts?: number
  idempotency_key?: string | null
  input_summary?: Record<string, unknown> | null
  provider?: string | null
  model?: string | null
  cost_reserved_cents?: number
  cost_settled_cents?: number
  error_code?: string | null
  error_message?: string | null
  parent_job_id?: string | null
  result_ref?: JobResultRef | null
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
  duration_ms?: number | null
  cancelable?: boolean
}

export interface JobQuery {
  episode_id?: string
  project_id?: string
  status?: string
  type?: string
  page?: number
  page_size?: number
}

export interface ProviderCall {
  id?: string
  provider?: string
  model?: string
  request_id?: string
  price_version?: string
  billing_unit?: string
  usage?: Record<string, unknown> | number | string | null
  estimated_cost_cents?: number
  settled_cost_cents?: number
  status?: string
  created_at?: string
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 11. 媒体
 * ------------------------------------------------------------------ */

export type MediaKind = 'text' | 'image' | 'audio' | 'video'

export interface MediaAsset {
  id: string
  kind?: MediaKind
  url?: string
  sha256?: string
  width?: number
  height?: number
  duration_ms?: number
  safety?: SafetyState | Record<string, unknown>
  aigc?: boolean
  created_at?: string
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 12. 成本与配额
 * ------------------------------------------------------------------ */

export interface CostLedgerEntry {
  id?: string
  episode_id?: string | null
  project_id?: string | null
  kind?: string
  stage?: string
  provider?: string
  model?: string
  amount_cents?: number
  settled_cents?: number
  reserved_cents?: number
  created_at?: string
  [key: string]: unknown
}

/** 审计日志条目（API.md §13 `/audit`）。 */
export interface AuditLogEntry {
  id?: string
  action?: string
  target_type?: string | null
  target_id?: string | null
  actor_id?: string | null
  project_id?: string | null
  episode_id?: string | null
  /** 后端存 JSON，可能为对象或字符串。 */
  detail?: Record<string, unknown> | string | null
  created_at?: string
  [key: string]: unknown
}

/** 工作区统计（API.md §13 `/stats/workspace`）。 */
export interface WorkspaceStats {
  projects?: number
  episodes?: number
  jobs?: number
  media?: number
  quota?: Quota
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 13. 设置
 * ------------------------------------------------------------------ */

export interface ProviderSlotConfig {
  provider: string
  model?: string
  /** 仅提交时使用；回显永远为空。 */
  api_key?: string
  base_url?: string
  configured?: boolean
  masked?: string | null
  /** FR-090 备用供应商；provider 留空表示清除备用配置。 */
  fallback_provider?: string
  fallback_model?: string
  fallback_api_key?: string
  fallback_base_url?: string
  fallback_configured?: boolean
  fallback_masked?: string | null
}

export interface SettingsProviders {
  text?: ProviderSlotConfig
  image?: ProviderSlotConfig
  video?: ProviderSlotConfig
  speech?: ProviderSlotConfig
  [key: string]: ProviderSlotConfig | undefined
}

export interface PricingEntry {
  kind?: string
  provider?: string
  model?: string
  unit?: string
  unit_price_cents?: number
  currency?: string
  [key: string]: unknown
}

export interface PricingTable {
  version?: string
  updated_at?: string
  entries?: PricingEntry[]
  items?: PricingEntry[]
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 14. SSE
 * ------------------------------------------------------------------ */

export type SseEventType =
  | 'job.created'
  | 'job.status_changed'
  | 'job.progress'
  | 'job.succeeded'
  | 'job.failed'
  | 'job.cancelled'
  | 'workflow.gate_waiting'
  | 'workflow.resumed'
  | 'workflow.node_changed'
  | 'episode.stage_changed'
  | 'shot.confirmed'
  | 'media.created'
  | 'render.progress'
  | 'heartbeat'

export interface SseEvent {
  id: string | null
  type: SseEventType | string
  data: Record<string, unknown>
  receivedAt: number
}

export interface JobStatusEventData {
  job_id: string
  status: JobStatus
  progress?: number
  episode_id?: string
  ts?: string
  error_code?: string | null
  error_message?: string | null
  [key: string]: unknown
}

export type SseConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed' | 'error'

/* ------------------------------------------------------------------ *
 * 15. 审批 / 安全
 * ------------------------------------------------------------------ */

export interface Approval {
  id?: string
  gate?: string
  action?: string
  operator?: string
  actor?: string
  media_id?: string | null
  asset_version?: number | null
  created_at?: string
  [key: string]: unknown
}

export interface SafetyResult {
  allowed: boolean
  risk_level?: string
  categories?: string[]
  suggestions?: string[]
  [key: string]: unknown
}

export interface SafetySummary {
  items?: { kind?: string; media_id?: string; result?: SafetyResult }[]
  blocked?: number
  review?: number
  [key: string]: unknown
}
