import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/api/errors'
import {
  fetchJobs,
  fetchProviderCalls,
  isTerminalJobStatus,
  jobsApi,
} from '@/api/jobs'
import type { Job, JobQuery, ProviderCall } from '@/types'

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: err instanceof Error ? err.message : String(err),
    status: 0,
    retryable: true,
  })
}

/** 任务中心（API.md §10）。支持 SSE 事件增量更新 + 轮询兜底。 */
export const useJobsStore = defineStore('jobs', () => {
  const items = ref<Job[]>([])
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const filters = ref<JobQuery>({})
  const loading = ref(false)
  const error = ref<ApiError | null>(null)

  const detail = ref<Job | null>(null)
  const detailLoading = ref(false)
  const providerCalls = ref<ProviderCall[]>([])

  const runningCount = computed(
    () => items.value.filter((j) => j.status === 'RUNNING' || j.status === 'RETRYING').length,
  )
  const queuedCount = computed(() => items.value.filter((j) => j.status === 'QUEUED').length)
  const failedCount = computed(() => items.value.filter((j) => j.status === 'FAILED').length)

  async function load(resetPage = false): Promise<void> {
    if (resetPage) page.value = 1
    loading.value = true
    error.value = null
    try {
      const result = await fetchJobs({
        ...filters.value,
        page: page.value,
        page_size: pageSize.value,
      })
      items.value = result.items
      total.value = result.total
      page.value = result.page
      pageSize.value = result.page_size
    } catch (err) {
      error.value = toApiError(err)
      items.value = []
      total.value = 0
    } finally {
      loading.value = false
    }
  }

  function setFilter(patch: Partial<JobQuery>): void {
    filters.value = { ...filters.value, ...patch }
    void load(true)
  }

  function setPage(next: number): void {
    page.value = next
    void load()
  }

  async function loadDetail(jobId: string): Promise<void> {
    detailLoading.value = true
    try {
      detail.value = await jobsApi.get(jobId)
      providerCalls.value = await fetchProviderCalls(jobId).catch(() => [])
    } catch (err) {
      error.value = toApiError(err)
      detail.value = null
      providerCalls.value = []
    } finally {
      detailLoading.value = false
    }
  }

  function closeDetail(): void {
    detail.value = null
    providerCalls.value = []
  }

  async function retry(jobId: string): Promise<Job | null> {
    const job = await jobsApi.retry(jobId)
    items.value = [job, ...items.value]
    return job
  }

  async function cancel(jobId: string): Promise<string | null> {
    const res = await jobsApi.cancel(jobId)
    const updated = res.job
    if (updated) upsert(updated)
    return res.note ?? null
  }

  /** 用一份最新任务快照覆盖列表中的同 id 项；不存在则插入。 */
  function upsert(job: Job): void {
    const idx = items.value.findIndex((j) => j.id === job.id)
    if (idx >= 0) {
      const next = [...items.value]
      next[idx] = { ...items.value[idx], ...job }
      items.value = next
    } else {
      items.value = [job, ...items.value]
      total.value += 1
    }
    if (detail.value?.id === job.id) detail.value = { ...detail.value, ...job }
  }

  /** SSE 事件 → 列表增量更新（API.md §10.1）。 */
  function applyEvent(data: Record<string, unknown>): void {
    const id = typeof data.job_id === 'string' ? data.job_id : null
    if (!id) return
    const idx = items.value.findIndex((j) => j.id === id)
    const patch: Partial<Job> = {}
    if (typeof data.status === 'string') patch.status = data.status as Job['status']
    if (typeof data.progress === 'number') patch.progress = data.progress
    if (typeof data.stage === 'string') patch.stage = data.stage
    if (typeof data.error_code === 'string') patch.error_code = data.error_code
    if (typeof data.error_message === 'string') patch.error_message = data.error_message
    if (typeof data.episode_id === 'string') patch.episode_id = data.episode_id
    if (idx >= 0) {
      const next = [...items.value]
      next[idx] = { ...items.value[idx], ...patch }
      items.value = next
    } else if (typeof data.status === 'string') {
      // 列表里没有的任务（例如其它剧集新建的）不强行插入，避免与筛选条件冲突
      return
    }
    if (detail.value?.id === id) detail.value = { ...detail.value, ...patch }
  }

  /** 当前列表里还未结束的任务 id（供轮询兜底使用）。 */
  function pendingIds(): string[] {
    return items.value.filter((j) => !isTerminalJobStatus(j.status)).map((j) => j.id)
  }

  return {
    items,
    total,
    page,
    pageSize,
    filters,
    loading,
    error,
    detail,
    detailLoading,
    providerCalls,
    runningCount,
    queuedCount,
    failedCount,
    load,
    setFilter,
    setPage,
    loadDetail,
    closeDetail,
    retry,
    cancel,
    upsert,
    applyEvent,
    pendingIds,
  }
})
