import { http } from './client'
import { asList } from './projects'
import type {
  Approval,
  PricingTable,
  ProviderSlotConfig,
  SafetyResult,
  SafetySummary,
  SettingsProviders,
} from '@/types'

/** 供应商设置与安全（API.md §14）。 */
export const settingsApi = {
  /**
   * 后端返回 `{items: [{kind, provider, model, ...}], available: [...]}`，
   * 这里归一成按槽位索引的扁平结构（`{text: {...}, image: {...}}`），
   * 与 PUT 的请求体形状保持一致，省得每个调用点自己解包。
   */
  getProviders: async (): Promise<SettingsProviders> => {
    const raw = await http.get<ProvidersResponse>('/settings/providers')
    return normalizeProviders(raw)
  },

  putProviders: async (body: SettingsProviders): Promise<SettingsProviders> => {
    const raw = await http.put<ProvidersResponse>('/settings/providers', body)
    return normalizeProviders(raw)
  },

  pricing: () => http.get<PricingTable>('/settings/pricing'),

  safetyCheck: (text: string) => http.post<SafetyResult>('/safety/check', { text }),

  approvals: (episodeId: string) =>
    http.get<Approval[] | { items: Approval[] }>(`/episodes/${episodeId}/approvals`),

  safety: (episodeId: string) =>
    http.get<SafetySummary>(`/episodes/${episodeId}/safety`),
}

interface ProvidersResponse {
  items?: ProviderSlotEntry[]
  available?: unknown[]
  [key: string]: unknown
}

interface ProviderSlotEntry extends ProviderSlotConfig {
  kind?: string
}

/**
 * 把 `{items: [...]}` 归一成 `{text: {...}, image: {...}, ...}`。
 * 也容忍后端将来直接返回扁平对象（此时原样透传）。
 */
export function normalizeProviders(raw: ProvidersResponse | undefined | null): SettingsProviders {
  if (!raw) return {}
  const list = Array.isArray(raw.items) ? raw.items : null
  if (!list) {
    const flat: SettingsProviders = {}
    for (const slot of PROVIDER_SLOTS) {
      const value = raw[slot]
      if (value && typeof value === 'object') flat[slot] = value as ProviderSlotConfig
    }
    return flat
  }
  const out: SettingsProviders = {}
  for (const entry of list) {
    const kind = entry?.kind
    if (!kind) continue
    const { kind: _kind, ...config } = entry
    out[kind as ProviderSlot] = config as ProviderSlotConfig
  }
  return out
}

export async function fetchApprovals(episodeId: string): Promise<Approval[]> {
  return asList<Approval>(await settingsApi.approvals(episodeId))
}

/** 供应商配置槽位（API.md §14 的四个模态）。 */
export const PROVIDER_SLOTS = ['text', 'image', 'video', 'speech'] as const

export type ProviderSlot = (typeof PROVIDER_SLOTS)[number]

export const SLOT_LABEL: Record<ProviderSlot, string> = {
  text: '文本 / 剧本',
  image: '图片 / 分镜图',
  video: '视频',
  speech: '语音 / 配音',
}

export function emptySlot(): ProviderSlotConfig {
  return {
    provider: '',
    model: '',
    api_key: '',
    base_url: '',
    configured: false,
    masked: null,
    fallback_provider: '',
    fallback_model: '',
    fallback_api_key: '',
    fallback_base_url: '',
    fallback_configured: false,
    fallback_masked: null,
  }
}
