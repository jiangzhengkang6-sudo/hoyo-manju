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
    // 不能因为「此刻没有待轮询 id」就 return：调用方往往先 track(job) 再补 jobIds，
    // 早退会让这次轮询永远不启动，任务卡停在创建时的快照（排队中 0%）不动。
    // 真正的空集合由 pollOnce() 处理（无 id 时立即把 active 置回 false）。
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
    const patch: Partial<Job> = { id }
    if (typeof data.status === 'string') patch.status = data.status as Job['status']
    if (typeof data.progress === 'number') patch.progress = data.progress
    if (typeof data.error_code === 'string') patch.error_code = data.error_code
    if (typeof data.error_message === 'string') patch.error_message = data.error_message
    if (typeof data.stage === 'string') patch.stage = data.stage
    // 只带 progress 的事件没有 type/episode_id；首次出现的任务要能补齐，
    // 否则底栏拿到的是「有进度但没有 id/类型」的半个对象，卡片直接渲染不出来。
    if (typeof data.type === 'string') patch.type = data.type
    if (typeof data.episode_id === 'string') patch.episode_id = data.episode_id
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
