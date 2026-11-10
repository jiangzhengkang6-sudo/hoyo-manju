import { http } from './client'
import { makeIdempotencyKey } from './errors'
import type {
  Job,
  VideoEstimateInput,
  VideoGenerateInput,
  VideoParamsView,
  VideoQuote,
} from '@/types'

/** 视频（API.md §7）。 */
export const videoApi = {
  /** 成本估算：返回 quote_id 与配额余量，前端必须展示给用户确认。 */
  estimate: (episodeId: string, body: VideoEstimateInput) =>
    http.post<VideoQuote>(`/episodes/${episodeId}/video/estimate`, body),

  /**
   * FR-040：读取画幅比例与逐镜视频参数（含可选值与范围）。
   * 画幅比例落在**项目**上，全剧集共享。
   */
  params: (episodeId: string) =>
    http.get<VideoParamsView>(`/episodes/${episodeId}/video/params`),

  /**
   * FR-040：持久化画幅比例与逐镜视频参数。
   * `shot_nos` 为空表示应用到全部镜头；参数写进 `shot_json.video_params`，
   * 与生成时读取的位置一致，因此会真正生效。
   */
  updateParams: (
    episodeId: string,
    body: {
      aspect_ratio?: string
      shot_nos?: number[]
      motion_strength?: number
      duration_ms?: number
      camera_movement?: string
    },
  ) => http.put<VideoParamsView & { updated_shots: number }>(
    `/episodes/${episodeId}/video/params`,
    body,
  ),

  /**
   * 生成视频。未确认成本（`confirm !== true` 或 quote 过期）后端返回 CONFLICT。
   * 幂等键由调用方生成，避免重复扣配额。
   */
  generate: (
    episodeId: string,
    body: { shot_nos: number[]; params?: Record<string, unknown>; quote_id: string },
  ) => {
    const payload: VideoGenerateInput = {
      ...body,
      idempotency_key: makeIdempotencyKey('vid'),
      confirm: true,
    }
    return http.post<Job>(`/episodes/${episodeId}/video/generate`, payload)
  },

  confirm: (shotId: string, mediaId: string) =>
    http.post<Job>(`/shots/${shotId}/video/confirm`, { media_id: mediaId }),

  /**
   * 用已上传的视频顶替该镜头，直接置为「片段已确认」。
   *
   * 给「供应商成功、本地拉取失败」这类情况兜底：片子已经花钱生成了，
   * 从供应商控制台下载后传回来即可，不需要再生成一次。
   */
  manual: (shotId: string, mediaId: string) =>
    http.post<{
      shot_no: number
      video_status: string
      video_media_id: string
      duration_ms?: number | null
      warnings: string[]
    }>(`/shots/${shotId}/video/manual`, { media_id: mediaId }),

  /**
   * 按供应商任务号补拉已生成的片段（不提交新任务、不产生新费用）。
   *
   * 上游成片只保留 24 小时，过期会返回明确提示「链接已过期，只能重新生成」。
   */
  fetchProvider: (shotId: string, taskId: string) =>
    http.post<{
      shot_no: number
      video_status: string
      video_media_id: string
      duration_ms?: number | null
      task_id: string
      billed_cents: number
      warnings: string[]
    }>(`/shots/${shotId}/video/fetch-provider`, { task_id: taskId }),

  /** 跳过该镜视频，使用静帧或空白兜底。 */
  skip: (shotId: string, fallback: 'static_image' | 'blank', reason?: string) =>
    http.post<Job>(`/shots/${shotId}/video/skip`, { reason, fallback }),

  /** 重试：新任务，关联原任务。 */
  retry: (shotId: string) => http.post<Job>(`/shots/${shotId}/video/retry`),
}

/** 成本确认剩余有效时间（毫秒）；quote 无 `expires_at` 时返回 null。 */
export function quoteRemainingMs(quote: VideoQuote | null, now = Date.now()): number | null {
  if (!quote?.expires_at) return null
  const t = Date.parse(quote.expires_at)
  if (Number.isNaN(t)) return null
  return t - now
}
