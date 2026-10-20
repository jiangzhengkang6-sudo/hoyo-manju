import { http } from './client'
import type { Quota } from '@/types'

/**
 * 工作区与项目级资产库（PRD §5.1-1「登录 / 工作区页」、§5.1-6「项目级资产库」）。
 * 字段名严格对齐后端 `app/api/routes/workspace.py`，不做任何臆造。
 */

/** 当前创作者画像。 */
export interface WorkspaceUser {
  id: string
  name: string
  workspace_id: string
}

/** 工作区概览计数（PRD §5.1-1 的概览卡片）。 */
export interface WorkspaceCounts {
  projects: number
  active_projects: number
  archived_projects: number
  episodes: number
  jobs: number
  media: number
  characters: number
  styles: number
}

/** 建项目等表单的默认值，前端不写死。 */
export interface WorkspaceDefaults {
  language: string
  aspect_ratio: string
}

/** 供应商状态：`mock_only` 为真时前端必须显著提示「演示模式」。 */
export interface WorkspaceProviders {
  mock_only: boolean
  configured: string[]
}

/** `GET /workspace` 的完整响应。 */
export interface WorkspaceProfile {
  user: WorkspaceUser
  workspace_id: string
  quota: Quota
  counts: WorkspaceCounts
  defaults: WorkspaceDefaults
  providers: WorkspaceProviders
}

/** `PATCH /workspace` 的响应（仅回显身份字段）。 */
export interface WorkspaceRenameResult {
  id: string
  name: string
  workspace_id: string
}

/** 资产库中的角色条目：在角色基础上补了来源项目名。 */
export interface WorkspaceCharacterAsset {
  id: string
  name: string
  project_id: string | null
  /** 来源项目名；后端用 `projects.get()` 取值，项目缺失时为 null。 */
  project_name?: string | null
  status?: string | null
  identity?: string | null
  appearance?: string | null
  primary_media_id?: string | null
  voice_id?: string | null
  created_at?: string | null
}

/**
 * 资产库中的画风条目。
 * `palette` 后端可能是数组 / 对象 / 字符串，渲染时必须防御式处理。
 */
export interface WorkspaceStyleAsset {
  id: string
  project_id?: string | null
  name?: string | null
  description?: string | null
  palette?: unknown
  camera_style?: string | null
  lighting_style?: string | null
  negative_prompt?: string | null
  version?: number | null
  status?: string | null
  is_default?: boolean
  created_at?: string | null
  /** 后端 `style_dict()` 通常带上的来源项目名。 */
  project_name?: string | null
}

/** 资产库的项目下拉选项。 */
export interface WorkspaceAssetProject {
  id: string
  name: string
}

/** `GET /workspace/assets` 的完整响应。 */
export interface WorkspaceAssets {
  characters: WorkspaceCharacterAsset[]
  styles: WorkspaceStyleAsset[]
  projects: WorkspaceAssetProject[]
  total: number
}

/** `kind` 取值：`character` / `style`，缺省返回两类。 */
export type WorkspaceAssetKind = 'character' | 'style'

export interface WorkspaceAssetsQuery {
  kind?: WorkspaceAssetKind
  project_id?: string
  q?: string
}

/** 工作区统计（`GET /stats/workspace`）。 */
export interface WorkspaceStats {
  projects?: number
  episodes?: number
  jobs?: number
  media?: number
  quota?: Quota
  [key: string]: unknown
}

/**
 * 工作区接口。
 * 幂等键：`PATCH /workspace` 是纯改名，后端不涉及扣费，无需 `idempotency_key`。
 */
export const workspaceApi = {
  /** 工作区画像 + 概览计数 + 配额快照。 */
  get: () => http.get<WorkspaceProfile>('/workspace'),

  /**
   * 更新创作者显示名。
   * 后端对空白名字直接忽略（返回当前名），超过 120 字返回 `409 STATE_CONFLICT`。
   */
  rename: (name: string) => http.patch<WorkspaceRenameResult>('/workspace', { name }),

  /** 项目级资产库：跨项目汇总角色与画风，可按类型/项目/名称过滤。 */
  assets: (query: WorkspaceAssetsQuery = {}) =>
    http.get<WorkspaceAssets>('/workspace/assets', {
      kind: query.kind,
      project_id: query.project_id,
      q: query.q,
    }),

  /** 工作区统计（与画像里的 counts 同源，供其它页面复用）。 */
  stats: () => http.get<WorkspaceStats>('/stats/workspace'),
}

/** 资产库请求的兜底归一化：任一数组缺失时补空数组，避免视图层判空。 */
export function normalizeAssets(raw: WorkspaceAssets | null | undefined): WorkspaceAssets {
  return {
    characters: Array.isArray(raw?.characters) ? raw.characters : [],
    styles: Array.isArray(raw?.styles) ? raw.styles : [],
    projects: Array.isArray(raw?.projects) ? raw.projects : [],
    total: typeof raw?.total === 'number' ? raw.total : 0,
  }
}

/** 角色状态 → 徽章色调（复用全局 `badge-*` 类）。 */
export function characterStatusTone(status?: string | null): string {
  switch (status) {
    case 'CONFIRMED':
      return 'badge-ok'
    case 'GENERATING':
      return 'badge-warn'
    case 'FAILED':
      return 'badge-danger'
    case 'DRAFT':
      return 'badge-muted'
    default:
      return 'badge-info'
  }
}

/** 角色状态 → 中文文案。未知状态原样透出，不臆造翻译。 */
export function characterStatusLabel(status?: string | null): string {
  if (!status) return '未标注'
  const map: Record<string, string> = {
    CONFIRMED: '已确认',
    DRAFT: '草稿',
    GENERATING: '生成中',
    FAILED: '生成失败',
    STALE: '已过期',
  }
  return map[status] ?? status
}

/**
 * 防御式展开色板：后端可能是字符串数组、对象（`{primary, ...}` 或任意键值）、
 * 或逗号分隔的字符串。统一摊平成字符串数组后再由视图决定画色块还是纯文本。
 */
export function normalizePalette(palette: unknown): string[] {
  if (!palette) return []
  if (Array.isArray(palette)) {
    return palette
      .map((item) => (typeof item === 'string' ? item.trim() : String(item ?? '').trim()))
      .filter(Boolean)
  }
  if (typeof palette === 'string') {
    return palette
      .split(/[,，;；\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
  }
  if (typeof palette === 'object') {
    const out: string[] = []
    for (const value of Object.values(palette as Record<string, unknown>)) {
      if (typeof value === 'string' && value.trim()) out.push(value.trim())
      else if (Array.isArray(value)) out.push(...normalizePalette(value))
    }
    return out
  }
  return []
}

/** 是否为可直接当 CSS 颜色渲染的值（#rgb / #rrggbb / rgb() / hsl()）。 */
export function isColorLike(value: string): boolean {
  return /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(value.trim()) ||
    /^(?:rgb|hsl)a?\(/i.test(value.trim())
}
