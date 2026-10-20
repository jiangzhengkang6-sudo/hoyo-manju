import { http } from './client'
import { asList } from './projects'
import type {
  ExportGate,
  ExportRecord,
  Job,
  RenderExportInput,
  RenderPreviewInput,
  SafetyAppeal,
  Timeline,
  TimelineValidation,
} from '@/types'

/** 时间线、合成与导出（API.md §12）。 */
export const renderApi = {
  timeline: (episodeId: string) => http.get<Timeline>(`/episodes/${episodeId}/timeline`),

  validateTimeline: (episodeId: string) =>
    http.get<TimelineValidation>(`/episodes/${episodeId}/timeline/validate`),

  /** 低码率预览 MP4（码率取自 /config/limits.preview_bitrate_kbps）。 */
  preview: (episodeId: string, body: RenderPreviewInput = {}) =>
    http.post<Job>(`/episodes/${episodeId}/render/preview`, body),

  /** 正式导出。 */
  export: (episodeId: string, body: RenderExportInput) =>
    http.post<Job>(`/episodes/${episodeId}/render/export`, body),

  exports: (episodeId: string) =>
    http.get<ExportRecord[] | { items: ExportRecord[] }>(`/episodes/${episodeId}/exports`),

  exportDetail: (exportId: string) => http.get<ExportRecord>(`/exports/${exportId}`),

  /** §12-8：导出闸门状态（被拦截时含原因与已提交的申诉）。 */
  exportGate: (episodeId: string) => http.get<ExportGate>(`/episodes/${episodeId}/export-gate`),

  /** §12-8：提交内容拦截申诉。人工复核，不会自动放行。 */
  createAppeal: (
    episodeId: string,
    body: {
      target_type?: 'asset' | 'input' | 'episode'
      target_id?: string | null
      reason: string
      detail?: string
    },
  ) => http.post<SafetyAppeal>(`/episodes/${episodeId}/appeals`, body),

  appeals: (episodeId: string) =>
    http.get<{ items: SafetyAppeal[]; pending: number; approved: number; rejected: number }>(
      `/episodes/${episodeId}/appeals`,
    ),

  /** 下载（后端 302 到签名 URL），前端直接跳转即可。 */
  exportDownloadUrl: (exportId: string) => `/exports/${exportId}/download`,

  /** 生成 ZIP 素材包（不依赖渲染成功）。 */
  package: (episodeId: string) =>
    http.post<Job>(`/episodes/${episodeId}/export/package`),
}

export async function fetchExports(episodeId: string): Promise<ExportRecord[]> {
  return asList<ExportRecord>(await renderApi.exports(episodeId))
}

export const EXPORT_KIND_LABEL: Record<string, string> = {
  preview: '预览',
  export: '正式导出',
  package: '素材包',
}

export const EXPORT_STATUS_LABEL: Record<string, string> = {
  CREATED: '已创建',
  QUEUED: '排队中',
  RUNNING: '生成中',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
  EXPIRED: '已过期',
}

/** 时间线片段是否可播放。 */
export function clipPlayable(videoMediaId?: string | null): boolean {
  return Boolean(videoMediaId)
}
