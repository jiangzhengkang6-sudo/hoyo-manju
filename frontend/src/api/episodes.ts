import { http } from './client'
import { asList } from './projects'
import type {
  CostSummary,
  Episode,
  EpisodeCreateInput,
  EpisodeStage,
} from '@/types'

/** 剧集（API.md §3）。 */
export const episodesApi = {
  list: (projectId: string) =>
    http
      .get<Episode[] | { items: Episode[] }>(`/projects/${projectId}/episodes`)
      .then(asList<Episode>),

  create: (projectId: string, body: EpisodeCreateInput) =>
    http.post<Episode>(`/projects/${projectId}/episodes`, body),

  get: (episodeId: string) => http.get<Episode>(`/episodes/${episodeId}`),

  patch: (episodeId: string, body: { title?: string; target_duration_ms?: number }) =>
    http.patch<Episode>(`/episodes/${episodeId}`, body),

  copy: (episodeId: string, title?: string) =>
    http.post<Episode>(`/episodes/${episodeId}/copy`, title ? { title } : {}),

  archive: (episodeId: string) => http.post<Episode>(`/episodes/${episodeId}/archive`),

  restore: (episodeId: string) => http.post<Episode>(`/episodes/${episodeId}/restore`),

  /**
   * **彻底删除**剧集（不可恢复）：连同剧本、分镜、角色卡、音视频、导出记录一起删。
   * 只想隐藏请用 `archive()`（可恢复）。
   */
  remove: (episodeId: string) =>
    http.del<{ deleted: boolean; episode_id: string; media_files: number }>(
      `/episodes/${episodeId}`,
    ),

  cost: (episodeId: string) => http.get<CostSummary>(`/episodes/${episodeId}/cost`),
}

/** 阶段流水线顺序（不含 BLOCKED，BLOCKED 是异常态）。 */
export const STAGE_ORDER: EpisodeStage[] = [
  'DRAFT',
  'SCRIPT_REVIEW',
  'ASSET_REVIEW',
  'SHOT_REVIEW',
  'VIDEO_REVIEW',
  'AUDIO_REVIEW',
  'RENDERING',
  'PREVIEW_READY',
  'EXPORTED',
]

export const STAGE_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  SCRIPT_REVIEW: '剧本确认',
  ASSET_REVIEW: '角色与画风',
  SHOT_REVIEW: '分镜画面',
  VIDEO_REVIEW: '视频片段',
  AUDIO_REVIEW: '音频字幕',
  RENDERING: '自动合成',
  PREVIEW_READY: '预览就绪',
  EXPORTED: '已导出',
  BLOCKED: '已阻断',
  // §10.1：上游变更后有受影响资源待确认（仅标记，未删除）
  STALE: '有内容待更新',
}

/** 阶段 → 工作台子路由。 */
export const STAGE_ROUTE: Record<string, string> = {
  DRAFT: 'story',
  SCRIPT_REVIEW: 'story',
  ASSET_REVIEW: 'characters',
  SHOT_REVIEW: 'shots',
  VIDEO_REVIEW: 'video',
  AUDIO_REVIEW: 'audio',
  RENDERING: 'render',
  PREVIEW_READY: 'render',
  EXPORTED: 'render',
  BLOCKED: 'story',
  // 受影响的是画面，因此直接落在分镜页让用户处理
  STALE: 'shots',
}

export function stageIndex(stage?: string | null): number {
  if (!stage) return 0
  const i = STAGE_ORDER.indexOf(stage as EpisodeStage)
  return i < 0 ? 0 : i
}

export function stageLabel(stage?: string | null): string {
  if (!stage) return '未知'
  return STAGE_LABEL[stage] ?? stage
}
