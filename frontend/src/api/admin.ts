import { http } from './client'
import { asList } from './projects'
import type { AuditLogEntry, WorkspaceStats } from '@/types'

/**
 * 审计日志与工作区统计（API.md §13，后端 `app/api/routes/admin.py`）。
 * 这里只做请求封装与展示辅助，不含任何写死的配额/限制数值。
 */
export const adminApi = {
  /** 全局审计日志，可按项目过滤；`limit` 由后端约束（≤500）。 */
  audit: (query: { project_id?: string; limit?: number } = {}) =>
    http.get<AuditLogEntry[] | { items: AuditLogEntry[] }>('/audit', query),

  /** 工作区统计：项目 / 剧集 / 任务 / 素材数量 + 配额快照。 */
  workspaceStats: () => http.get<WorkspaceStats>('/stats/workspace'),
}

export async function fetchAuditLogs(
  query: { project_id?: string; limit?: number } = {},
): Promise<AuditLogEntry[]> {
  return asList<AuditLogEntry>(await adminApi.audit(query))
}

export async function fetchWorkspaceStats(): Promise<WorkspaceStats> {
  return adminApi.workspaceStats()
}

/** 审计动作中文名；未收录的动作直接回显原始 code。 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  'project.create': '创建项目',
  'project.update': '更新项目',
  'project.archive': '归档项目',
  'project.restore': '恢复项目',
  'episode.create': '创建剧集',
  'episode.copy': '复制剧集',
  'episode.archive': '归档剧集',
  'episode.restore': '恢复剧集',
  'episode.delete': '删除剧集',
  'provider.update': '更新供应商',
  'quota.update': '配额变更',
  'export.create': '发起导出',
  'export.download': '下载导出',
}

export function auditActionLabel(action?: string | null): string {
  if (!action) return '未知事件'
  return AUDIT_ACTION_LABEL[action] ?? action
}

/** 审计目标类型中文名。 */
export const AUDIT_TARGET_LABEL: Record<string, string> = {
  project: '项目',
  episode: '剧集',
  shot: '分镜',
  job: '任务',
  media_asset: '素材',
  provider_setting: '供应商配置',
  export_record: '导出记录',
  price_book: '价格表',
}

export function auditTargetLabel(targetType?: string | null): string {
  if (!targetType) return '—'
  return AUDIT_TARGET_LABEL[targetType] ?? targetType
}

/** 审计明细（后端为 JSON）→ 单行可读文本。 */
export function auditDetailText(detail?: Record<string, unknown> | string | null): string {
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  const parts: string[] = []
  for (const [key, value] of Object.entries(detail)) {
    if (value === null || value === undefined || value === '') continue
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value)
    parts.push(`${key}=${text}`)
  }
  return parts.join(' · ')
}
