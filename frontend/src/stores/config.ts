import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/api/errors'
import { LIMITS_FALLBACK, configApi, fetchProviders, normalizePresets } from '@/api/config'
import { costApi } from '@/api/cost'
import type { HealthStatus, Limits, ProviderInfo, Quota } from '@/types'

/**
 * 全局配置：限制项、供应商清单、配额、健康状态。
 * **所有配额/限制一律来自后端**，本 store 内的默认值只用于后端不可用时的兜底展示。
 */
export const useConfigStore = defineStore('config', () => {
  const limits = ref<Limits>({ ...LIMITS_FALLBACK })
  const limitsLoaded = ref(false)
  const providers = ref<ProviderInfo[]>([])
  const quota = ref<Quota | null>(null)
  const health = ref<HealthStatus | null>(null)
  const healthOk = ref<boolean | null>(null)
  const loading = ref(false)
  const error = ref<ApiError | null>(null)

  let inflight: Promise<void> | null = null

  /* ---------------- 限制项派生 ---------------- */

  const candidateDefault = computed(
    () => limits.value.candidates_per_shot_default ?? LIMITS_FALLBACK.candidates_per_shot_default,
  )
  const candidateMax = computed(
    () => limits.value.candidates_per_shot_max ?? LIMITS_FALLBACK.candidates_per_shot_max,
  )
  const shotMinMs = computed(
    () => limits.value.shot_duration_ms_min ?? LIMITS_FALLBACK.shot_duration_ms_min,
  )
  const shotMaxMs = computed(
    () => limits.value.shot_duration_ms_max ?? LIMITS_FALLBACK.shot_duration_ms_max,
  )
  const episodeMaxMs = computed(
    () => limits.value.episode_duration_ms_max ?? LIMITS_FALLBACK.episode_duration_ms_max,
  )
  const previewBitrateKbps = computed(
    () => limits.value.preview_bitrate_kbps ?? LIMITS_FALLBACK.preview_bitrate_kbps,
  )
  const aspectRatios = computed(() =>
    limits.value.aspect_ratios?.length ? limits.value.aspect_ratios : LIMITS_FALLBACK.aspect_ratios,
  )
  const defaultLanguage = computed(
    () => limits.value.default_language || LIMITS_FALLBACK.default_language,
  )
  const presets = computed(() => normalizePresets(limits.value))
  const maxConcurrentJobs = computed(
    () => limits.value.max_concurrent_jobs ?? LIMITS_FALLBACK.max_concurrent_jobs,
  )
  /** FR-061 转场可选类型（后端为权威来源）。 */
  const transitionTypes = computed(() =>
    limits.value.transition_types?.length
      ? limits.value.transition_types
      : (LIMITS_FALLBACK.transition_types ?? []),
  )
  const transitionDefaultMs = computed(
    () => limits.value.transition_default_ms ?? LIMITS_FALLBACK.transition_default_ms ?? 300,
  )
  const transitionMaxMs = computed(
    () => limits.value.transition_max_ms ?? LIMITS_FALLBACK.transition_max_ms ?? 2000,
  )

  /** 按模态取供应商（API.md §1 `/config/providers`）。 */
  function providersByKind(kind: string): ProviderInfo[] {
    return providers.value.filter((p) => p.kind === kind)
  }

  /* ---------------- 加载 ---------------- */

  async function load(force = false): Promise<void> {
    if (limitsLoaded.value && !force) return
    if (inflight) return inflight
    loading.value = true
    error.value = null
    inflight = (async () => {
      try {
        const [l, p] = await Promise.all([
          configApi.limits(),
          fetchProviders().catch(() => [] as ProviderInfo[]),
        ])
        limits.value = { ...LIMITS_FALLBACK, ...l }
        providers.value = p
        limitsLoaded.value = true
      } catch (err) {
        error.value = err instanceof ApiError ? err : new ApiError({
          code: 'NETWORK_ERROR',
          message: err instanceof Error ? err.message : String(err),
          status: 0,
        })
      } finally {
        loading.value = false
        inflight = null
      }
    })()
    return inflight
  }

  async function loadQuota(): Promise<void> {
    try {
      quota.value = await costApi.quota()
    } catch (err) {
      error.value = err instanceof ApiError ? err : error.value
    }
  }

  async function loadHealth(): Promise<void> {
    try {
      health.value = await configApi.health()
      healthOk.value = health.value.status === 'ok' || health.value.status === 'healthy'
    } catch {
      health.value = null
      healthOk.value = false
    }
  }

  return {
    limits,
    limitsLoaded,
    providers,
    quota,
    health,
    healthOk,
    loading,
    error,
    candidateDefault,
    candidateMax,
    shotMinMs,
    shotMaxMs,
    episodeMaxMs,
    previewBitrateKbps,
    aspectRatios,
    defaultLanguage,
    presets,
    maxConcurrentJobs,
    transitionTypes,
    transitionDefaultMs,
    transitionMaxMs,
    providersByKind,
    load,
    loadQuota,
    loadHealth,
  }
})
