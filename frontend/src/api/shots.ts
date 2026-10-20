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

export async function fetchShots(episodeId: string): Promise<Shot[]> {
  const shots = asList<Shot>(await shotsApi.list(episodeId))
  return [...shots].sort((a, b) => a.shot_no - b.shot_no)
}

export async function fetchCandidates(shotId: string): Promise<ShotCandidate[]> {
  return asList<ShotCandidate>(await shotsApi.candidates(shotId))
}

/** 镜头是否已确认画面。 */
export function isShotConfirmed(shot: Shot): boolean {
  return Boolean(shot.confirmed_media_id)
}

/** 候选图安全态中文名。 */
export const SAFETY_LABEL: Record<string, string> = {
  PASS: '安全通过',
  REVIEW: '待复核',
  BLOCK: '已拦截',
}

export function safetyLabel(safety?: string | null): string {
  if (!safety) return '未检查'
  return SAFETY_LABEL[safety] ?? safety
}

export const MEDIA_STATUS_LABEL: Record<string, string> = {
  PENDING: '生成中',
  READY: '就绪',
  FAILED: '失败',
  REJECTED: '已驳回',
}
