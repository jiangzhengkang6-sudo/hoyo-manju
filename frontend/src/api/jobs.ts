import { http, pushToast, type RequestOptions } from './client'
import { asList } from './projects'
import type { Job, JobQuery, Paginated, ProviderCall } from '@/types'

/** 任务中心（API.md §10）。 */
export const jobsApi = {
  list: (query: JobQuery = {}, opts?: RequestOptions) =>
    http.get<Paginated<Job> | Job[]>(
      '/jobs',
      {
        episode_id: query.episode_id,
        project_id: query.project_id,
        status: query.status,
        type: query.type,
        page: query.page,
        page_size: query.page_size,
      },
      opts,
    ),

  get: (jobId: string) => http.get<Job>(`/jobs/${jobId}`),

  retry: (jobId: string) => http.post<Job>(`/jobs/${jobId}/retry`),

  /** 排队任务可取消；执行中为尽力取消，返回体带 `note`。 */
  cancel: (jobId: string) =>
    http.post<{ job?: Job; note?: string; status?: string }>(`/jobs/${jobId}/cancel`),

  providerCalls: (jobId: string) =>
    http.get<ProviderCall[] | { items: ProviderCall[] }>(`/jobs/${jobId}/provider-calls`),
}

export async function fetchProviderCalls(jobId: string): Promise<ProviderCall[]> {
  return asList<ProviderCall>(await jobsApi.providerCalls(jobId))
}

/**
 * 停止任务（排队中立即取消；执行中在下一个检查点停）。
 *
 * 统一在这里弹提示：取消不是瞬时生效的——正在生成的那一张/那一条会先跑完，
 * 费用照常结算。把这句话说清楚，用户才不会以为按钮没反应。
 */
export async function cancelJob(jobId: string): Promise<Job | null> {
  const res = await jobsApi.cancel(jobId)
  if (res.note) {
    pushToast({ kind: 'info', title: '已请求停止', message: res.note })
  }
  return res.job ?? null
}

/** 任务列表归一化（后端可能返回裸数组）。`opts.silent` 用于轮询等不希望弹 toast 的场景。 */
export async function fetchJobs(
  query: JobQuery = {},
  opts?: RequestOptions,
): Promise<Paginated<Job>> {
  const raw = await jobsApi.list(query, opts)
  if (Array.isArray(raw)) {
    return {
      items: raw,
      total: raw.length,
      page: query.page ?? 1,
      page_size: query.page_size ?? raw.length,
    }
  }
  const page = raw as Paginated<Job>
  return {
    items: page.items ?? [],
    total: page.total ?? page.items?.length ?? 0,
    page: page.page ?? 1,
    page_size: page.page_size ?? 20,
  }
}

/* ------------------------------------------------------------------ *
 * 状态展示辅助（枚举严格来自 API.md §0.2）
 * ------------------------------------------------------------------ */

export const JOB_STATUS_LABEL: Record<string, string> = {
  CREATED: '已创建',
  QUEUED: '排队中',
  RUNNING: '执行中',
  WAITING_CONFIRMATION: '等待确认',
  SUCCEEDED: '成功',
  FAILED: '失败',
  RETRYING: '重试中',
  CANCELLING: '取消中',
  CANCELLED: '已取消',
  EXPIRED: '已过期',
}

export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'accent' | 'muted'

export function jobStatusLabel(status?: string | null): string {
  if (!status) return '未知'
  return JOB_STATUS_LABEL[status] ?? status
}

export function jobStatusTone(status?: string | null): Tone {
  switch (status) {
    case 'SUCCEEDED':
      return 'ok'
    case 'RUNNING':
    case 'RETRYING':
    case 'CANCELLING':
      return 'info'
    case 'QUEUED':
    case 'CREATED':
    case 'WAITING_CONFIRMATION':
      return 'warn'
    case 'FAILED':
    case 'EXPIRED':
      return 'danger'
    case 'CANCELLED':
      return 'muted'
    default:
      return 'muted'
  }
}

/** 终态：不会再变化，可停止轮询。 */
export function isTerminalJobStatus(status?: string | null): boolean {
  return status === 'SUCCEEDED' || status === 'FAILED' || status === 'CANCELLED' || status === 'EXPIRED'
}

/** 任务类型中文名。键名对齐后端 executor 的 handler 名称。 */
export const JOB_TYPE_LABEL: Record<string, string> = {
  STORY_GENERATION: '故事/剧本生成',
  IMAGE_GENERATION: '分镜图生成',
  VIDEO_GENERATION: '视频生成',
  TTS_GENERATION: '配音生成',
  BGM_GENERATION: '氛围音生成',
  SUBTITLE_GENERATION: '字幕生成',
  CHARACTER_ASSET_GENERATION: '角色资产生成',
  SCRIPT_REGENERATE: '分镜重生成',
  RENDER_PREVIEW: '预览合成',
  RENDER_EXPORT: '正式导出',
  EXPORT_PACKAGE: '素材包打包',
  // 兼容旧枚举名
  RENDER: '视频合成',
  EXPORT: '正式导出',
  PACKAGE: '素材包打包',
}

export function jobTypeLabel(type?: string | null): string {
  if (!type) return '任务'
  return JOB_TYPE_LABEL[type] ?? type
}
