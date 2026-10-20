import { apiUrl } from './client'

/**
 * SSE 事件流地址（API.md §10.1）。
 * `EventSource` 无法自定义请求头，因此断线续传统一走 `?last_event_id=` 查询参数
 * （API.md：支持 `?last_event_id=` 断线补齐；无该参数时服务端补发最近 50 条）。
 */
export function episodeEventsUrl(episodeId: string, lastEventId?: string | null): string {
  const base = apiUrl(`/episodes/${episodeId}/events`)
  if (!lastEventId) return base
  return `${base}?last_event_id=${encodeURIComponent(lastEventId)}`
}

/** 工作区级事件流。 */
export function workspaceEventsUrl(lastEventId?: string | null): string {
  const base = apiUrl('/events')
  if (!lastEventId) return base
  return `${base}?last_event_id=${encodeURIComponent(lastEventId)}`
}

/** API.md §10.1 列出的全部事件类型，用于逐个 addEventListener。 */
export const SSE_EVENT_TYPES = [
  'job.created',
  'job.status_changed',
  'job.progress',
  'job.succeeded',
  'job.failed',
  'job.cancelled',
  'workflow.gate_waiting',
  'workflow.resumed',
  'workflow.node_changed',
  'episode.stage_changed',
  'shot.confirmed',
  'media.created',
  'render.progress',
  'heartbeat',
] as const

export const SSE_EVENT_LABEL: Record<string, string> = {
  'job.created': '任务创建',
  'job.status_changed': '任务状态变更',
  'job.progress': '任务进度',
  'job.succeeded': '任务成功',
  'job.failed': '任务失败',
  'job.cancelled': '任务取消',
  'workflow.gate_waiting': '等待门控确认',
  'workflow.resumed': '工作流已继续',
  'workflow.node_changed': '工作流节点切换',
  'episode.stage_changed': '剧集阶段变更',
  'shot.confirmed': '镜头已确认',
  'media.created': '媒体已生成',
  'render.progress': '合成进度',
  heartbeat: '心跳',
}

/** 心跳只用于保活，不参与界面刷新。 */
export function isHeartbeat(type: string): boolean {
  return type === 'heartbeat'
}
