import { http } from './client'
import { asList } from './projects'
import type { HealthStatus, Limits, ProviderInfo } from '@/types'

/**
 * 健康与配置（API.md §1）。
 * 前端不得写死任何配额/限制，一律从 `/config/limits` 读取。
 */
export const configApi = {
  /** 注意：`/health` 与其它接口一样经 `/api/v1` 前缀（dev 走 vite `/api` 代理）。 */
  health: () => http.get<HealthStatus>('/health', undefined, { silent: true }),

  limits: () => http.get<Limits>('/config/limits'),

  providers: () => http.get<ProviderInfo[] | { items: ProviderInfo[] }>('/config/providers'),
}

/** 供应商清单归一化为数组。 */
export async function fetchProviders(): Promise<ProviderInfo[]> {
  return asList<ProviderInfo>(await configApi.providers())
}

/**
 * 限制项兜底：仅在后端不可用/字段缺失时使用，避免界面直接崩掉。
 * 一旦后端返回真实值，永远以真实值为准。
 */
export const LIMITS_FALLBACK: Limits = {
  max_concurrent_jobs: 3,
  candidates_per_shot_default: 2,
  candidates_per_shot_max: 4,
  shot_duration_ms_min: 2000,
  shot_duration_ms_max: 10000,
  episode_duration_ms_max: 180000,
  preview_bitrate_kbps: 1500,
  export_presets: ['720p_9x16', '1080p_9x16'],
  aspect_ratios: ['9:16'],
  default_language: 'zh-CN',
  transition_types: [
    { id: 'fade', label: '淡入淡出' },
    { id: 'fadeblack', label: '黑场过渡' },
    { id: 'fadewhite', label: '白场过渡' },
    { id: 'dissolve', label: '叠化' },
    { id: 'wipeleft', label: '左擦除' },
    { id: 'wiperight', label: '右擦除' },
    { id: 'slideleft', label: '左滑入' },
    { id: 'slideright', label: '右滑入' },
    { id: 'circleopen', label: '圆形展开' },
    { id: 'hblur', label: '模糊过渡' },
    { id: 'pixelize', label: '像素化' },
  ],
  transition_default_ms: 300,
  transition_max_ms: 2000,
}

/** 归一化后端返回的导出预设（可能是字符串，也可能是对象）。 */
export function normalizePresets(limits: Limits): { id: string; label: string; meta: string }[] {
  const raw = limits.export_presets ?? []
  return raw.map((item) => {
    if (typeof item === 'string') return { id: item, label: item, meta: '' }
    const id = String(item.id ?? item.label ?? 'preset')
    const label = String(item.label ?? id)
    const bits: string[] = []
    if (item.width && item.height) bits.push(`${item.width}×${item.height}`)
    if (item.bitrate_kbps) bits.push(`${item.bitrate_kbps}kbps`)
    return { id, label, meta: bits.join(' · ') }
  })
}
