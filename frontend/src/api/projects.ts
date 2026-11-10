import { http } from './client'
import type { AuditEntry, CostSummary, Project, ProjectCreateInput, ProjectPatch } from '@/types'

/** 项目（API.md §2）。 */
export const projectsApi = {
  list: (includeArchived = false) =>
    http.get<Project[] | { items: Project[] }>('/projects', {
      include_archived: includeArchived ? 'true' : undefined,
    }),

  get: (projectId: string) => http.get<Project>(`/projects/${projectId}`),

  create: (body: ProjectCreateInput) => http.post<Project>('/projects', body),

  patch: (projectId: string, body: ProjectPatch) =>
    http.patch<Project>(`/projects/${projectId}`, body),

  /** 把统一语速应用到项目下全部角色（只改设定，不重新合成）。 */
  voiceSpeed: (projectId: string, speed: number) =>
    http.post<{ default_voice_speed: number; count: number; retimed?: number }>(
      `/projects/${projectId}/voice-speed`,
      { speed },
    ),

  archive: (projectId: string) => http.post<Project>(`/projects/${projectId}/archive`),

  restore: (projectId: string) => http.post<Project>(`/projects/${projectId}/restore`),

  cost: (projectId: string) => http.get<CostSummary>(`/projects/${projectId}/cost`),

  audit: (projectId: string) =>
    http.get<AuditEntry[] | { items: AuditEntry[] }>(`/projects/${projectId}/audit`),
}

/** 列表接口可能返回裸数组或分页包裹，统一取数组。 */
export function asList<T>(payload: T[] | { items: T[] } | null | undefined): T[] {
  if (!payload) return []
  if (Array.isArray(payload)) return payload
  if (Array.isArray((payload as { items?: T[] }).items)) return (payload as { items: T[] }).items
  return []
}
