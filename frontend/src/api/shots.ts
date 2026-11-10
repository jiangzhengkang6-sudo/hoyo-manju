import { http } from './client'
import { asList } from './projects'
import type {
  GenerateImagesInput,
  Job,
  Shot,
  ShotCandidate,
  ShotPatch,
  ShotVideoState,
} from '@/types'

/** 分镜与画面（API.md §6）。 */
export const shotsApi = {
  /** 分镜表（含候选图、确认状态、过期标记、视频/音频状态）。 */
  list: (episodeId: string) =>
    http.get<Shot[] | { items: Shot[] }>(`/episodes/${episodeId}/shots`),

  get: (shotId: string) => http.get<Shot>(`/shots/${shotId}`),

  patch: (shotId: string, body: ShotPatch) => http.patch<Shot>(`/shots/${shotId}`, body),

  generateImages: (episodeId: string, body: GenerateImagesInput) =>
    http.post<Job>(`/episodes/${episodeId}/shots/generate-images`, body),

  /** 生成前的成本预估（含配额影响与引用完整性阻断项）。 */
  estimateImages: (episodeId: string, body: GenerateImagesInput) =>
    http.post<ImageEstimate>(`/episodes/${episodeId}/shots/estimate-images`, body),

  candidates: (shotId: string) =>
    http.get<ShotCandidate[] | { items: ShotCandidate[] }>(`/shots/${shotId}/candidates`),

  /** 选中候选（不写入 approvals）。 */
  select: (shotId: string, mediaId: string) =>
    http.post<Shot>(`/shots/${shotId}/select`, { media_id: mediaId }),

  /** 确认画面（写入 approvals）。 */
  confirm: (shotId: string, mediaId: string) =>
    http.post<Shot>(`/shots/${shotId}/confirm`, { media_id: mediaId }),

  /** 标记问题候选。 */
  reject: (shotId: string, mediaId: string, reason: string) =>
    http.post<Shot>(`/shots/${shotId}/reject`, { media_id: mediaId, reason }),

  /** 恢复历史候选。 */
  restoreCandidate: (shotId: string, mediaId: string) =>
    http.post<Shot>(`/shots/${shotId}/restore-candidate`, { media_id: mediaId }),

  /** 批量确认画面。 */
  confirmBatch: (episodeId: string, shotNos: number[]) =>
    http.post<{ confirmed?: number[]; failed?: unknown[] }>(
      `/episodes/${episodeId}/shots/confirm-batch`,
      { shot_nos: shotNos },
    ),

  /** 片段状态与历史（API.md §7）。 */
  video: (shotId: string) => http.get<ShotVideoState>(`/shots/${shotId}/video`),
}

/**
 * 后端分镜是「行 + `shot_json`」两层结构，前端统一按扁平对象使用。
 *
 * 不摊平的话 `shot.action` / `dialogue` / `image_prompt` 永远是 undefined，
 * 表现就是「保存了镜头，但切到别的镜头或刷新后编辑区又变空」——
 * 数据其实存住了，只是读的时候没取到。
 */
export function normalizeShot(raw: Shot): Shot {
  const json = (raw.shot_json ?? {}) as Record<string, unknown>
  // 行上的字段（id / duration_ms / confirmed_media_id…）优先，覆盖 shot_json 里的同名字段
  const merged = { ...json, ...raw } as Shot
  // 候选图列表也要逐项归一：后端媒体行的主键叫 `id`，卡片按 `media_id` 取。
  // 之前只归一了 shot 这一层，`candidates[0].media_id` 一直是 undefined，
  // 表现就是「任务显示生成成功、候选数也对，但缩略图是空的 / 暂无素材」。
  // 第 1 镜之所以正常，只是因为它已经确认过，走的是 confirmed_media_id 那条路。
  merged.candidates = (raw.candidates ?? []).map((item) => normalizeCandidate(item))
  return merged
}

export async function fetchShots(episodeId: string): Promise<Shot[]> {
  const shots = asList<Shot>(await shotsApi.list(episodeId))
  return shots.map(normalizeShot).sort((a, b) => a.shot_no - b.shot_no)
}

/**
 * 后端候选图的 `safety` 是对象 `{status, detail}`（媒体行上还有扁平的
 * `safety_status`），前端卡片只想要一个状态字符串。不归一的话
 * `safetyLabel(obj)` 会把整个对象当成 label 渲染出来，界面直接变成一坨 JSON。
 */
export function normalizeCandidate(
  raw: ShotCandidate & {
    id?: string
    safety?: unknown
    safety_status?: string
    safety_detail?: Record<string, unknown>
  },
): ShotCandidate {
  const safety = raw.safety
  const fromObject =
    safety && typeof safety === 'object'
      ? String((safety as { status?: unknown }).status ?? '')
      : ''
  const status =
    (typeof safety === 'string' ? safety : fromObject) || raw.safety_status || ''
  const detail =
    safety && typeof safety === 'object'
      ? ((safety as { detail?: Record<string, unknown> }).detail ?? raw.safety_detail)
      : raw.safety_detail
  return {
    ...raw,
    // 后端媒体行主键叫 `id`，候选卡片按 `media_id` 用（同角色素材的 id/media_id 坑）
    media_id: raw.media_id ?? raw.id ?? '',
    safety: (status || undefined) as ShotCandidate['safety'],
    safety_detail: detail,
    stale_reason: raw.stale_reason ?? '',
    broken: Boolean(raw.broken ?? raw.stale_reason),
  }
}

export async function fetchCandidates(shotId: string): Promise<ShotCandidate[]> {
  const rows = asList<ShotCandidate & { safety?: unknown }>(
    await shotsApi.candidates(shotId),
  )
  return rows.map(normalizeCandidate)
}

/** 镜头是否已确认画面。 */
export function isShotConfirmed(shot: Shot): boolean {
  return Boolean(shot.confirmed_media_id)
}

/** 分镜图生成前的预估（后端 `/shots/estimate-images`）。 */
export interface ImageEstimate {
  provider: string
  model: string
  estimate_cents: number
  quota_after_cents: number
  remaining_cents: number
  shot_count: number
  candidates_per_shot: number
  image_count: number
  unit_price_cents: number
  billing_unit?: string
  estimated_latency_seconds?: number
  can_generate?: boolean
  problems?: { shot_no: number; reason: string; message: string }[]
  blocking_problems?: { shot_no: number; reason: string; message: string }[]
}

/** 分镜「按大纲生成」返回的建议值（不写库，由用户改完再保存）。 */
export interface ShotSuggestion {
  duration_ms: number
  composition: string
  camera_movement: string
  characters: string[]
  action: string
  /** 情绪基调（分镜画面页直接用它填「情绪」） */
  emotion?: string
  dialogue: { character: string; text: string; emotion?: string }[]
  narration: string
  image_prompt: string
  negative_prompt: string
}

export interface SuggestShotResult {
  shot_no: number
  suggestion: ShotSuggestion
  cost_cents: number
  script_version?: number
}

/** 按大纲给单个分镜生成可编辑草稿（同步接口，不走任务队列）。 */
export async function suggestShot(
  episodeId: string,
  shotNo: number,
  instruction = '',
): Promise<SuggestShotResult> {
  return http.post<SuggestShotResult>(`/episodes/${episodeId}/shots/${shotNo}/suggest`, {
    instruction,
  })
}

/** 同上，但以「分镜画面页正在编辑的这一镜」为准（按 shot_id）。 */
export async function suggestShotById(
  shotId: string,
  instruction = '',
): Promise<SuggestShotResult> {
  return http.post<SuggestShotResult>(`/shots/${shotId}/suggest`, { instruction })
}

/** 候选图安全态中文名。 */
export const SAFETY_LABEL: Record<string, string> = {
  // 后端 media.safety_status 用的是 PASSED/BLOCKED，旧枚举是 PASS/BLOCK——
  // 两套都收，否则界面上会直接显示英文原值。
  PASS: '安全通过',
  PASSED: '安全通过',
  REVIEW: '待复核',
  BLOCK: '已拦截',
  BLOCKED: '已拦截',
  UNKNOWN: '未检查',
}

export function safetyLabel(safety?: string | null): string {
  if (!safety) return '未检查'
  return SAFETY_LABEL[safety] ?? '已检查'
}

export const MEDIA_STATUS_LABEL: Record<string, string> = {
  PENDING: '生成中',
  READY: '就绪',
  FAILED: '失败',
  REJECTED: '已驳回',
}
