import { onScopeDispose, ref, type Ref } from 'vue'
import { jobsApi, isTerminalJobStatus } from '@/api/jobs'
import { reportError } from '@/api/client'
import type { Job } from '@/types'

export interface UseJobWatcherOptions {
  /** 轮询间隔（毫秒），默认 2500。 */
  intervalMs?: number
  /** 轮询期间是否静默失败（默认 true，避免断线时刷屏 toast）。 */
  silent?: boolean
}

export interface UseJobWatcherResult {
  /** job_id → 最新任务快照。 */
  jobs: Ref<Record<string, Job>>
  /** 是否还有未结束的任务（true 时轮询继续）。 */
  active: Ref<boolean>
  /** 手动写入一个任务快照（如接口刚返回的 Job）。 */
  track: (job: Job | null | undefined) => void
  /** 用 SSE 事件数据更新任务快照。 */
  applyEvent: (data: Record<string, unknown>) => void
  /** 立即刷新所有未结束任务。 */
  refresh: () => Promise<void>
  start: () => void
  stop: () => void
}

function mergeJob(prev: Job | undefined, patch: Partial<Job>): Job {
  return { ...(prev ?? ({} as Job)), ...patch } as Job
}

/**
 * 任务观察器：SSE 降级（或未连接）时通过 `GET /jobs/{id}` 轮询兜底（API.md §10）。
 * 只轮询非终态任务，全部结束即自动停止。
 */
export function useJobWatcher(
  jobIds: () => string[],
  options: UseJobWatcherOptions = {},
): UseJobWatcherResult {
  const intervalMs = options.intervalMs ?? 2500
  const jobs = ref<Record<string, Job>>({})
  const active = ref(false)

  let timer: number | null = null
  let disposed = false

  function clear(): void {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  function pendingIds(): string[] {
    const ids = jobIds().filter(Boolean)
    return ids.filter((id) => {
      const job = jobs.value[id]
      return !job || !isTerminalJobStatus(job.status)
    })
  }

  async function pollOnce(): Promise<void> {
    const ids = pendingIds()
    if (!ids.length) {
      active.value = false
      return
    }
    await Promise.all(
      ids.map(async (id) => {
        try {
          const job = await jobsApi.get(id)
          jobs.value = { ...jobs.value, [id]: job }
        } catch (err) {
          if (!options.silent) reportError(err, '任务状态轮询')
        }
      }),
    )
  }

  function schedule(): void {
    if (disposed || !active.value) return
    clear()
    timer = window.setTimeout(async () => {
      timer = null
      await pollOnce()
      if (pendingIds().length) schedule()
      else active.value = false
    }, intervalMs)
  }

  function start(): void {
    disposed = false
    if (!pendingIds().length) {
      active.value = false
      return
    }
    active.value = true
    schedule()
  }

  function stop(): void {
    active.value = false
    clear()
  }

  function track(job: Job | null | undefined): void {
    if (!job?.id) return
    jobs.value = { ...jobs.value, [job.id]: job }
    if (!isTerminalJobStatus(job.status)) start()
  }

  function applyEvent(data: Record<string, unknown>): void {
    const id = typeof data.job_id === 'string' ? data.job_id : null
    if (!id) return
    const prev = jobs.value[id]
    const patch: Partial<Job> = {}
    if (typeof data.status === 'string') patch.status = data.status as Job['status']
    if (typeof data.progress === 'number') patch.progress = data.progress
    if (typeof data.error_code === 'string') patch.error_code = data.error_code
    if (typeof data.error_message === 'string') patch.error_message = data.error_message
    if (typeof data.stage === 'string') patch.stage = data.stage
    jobs.value = { ...jobs.value, [id]: mergeJob(prev, patch) }
  }

  async function refresh(): Promise<void> {
    await pollOnce()
  }

  onScopeDispose(() => {
    disposed = true
    clear()
  })

  return { jobs, active, track, applyEvent, refresh, start, stop }
}
