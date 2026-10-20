import { onScopeDispose, ref } from 'vue'

export interface UsePollingOptions {
  /** 轮询间隔（毫秒），默认 3000。 */
  intervalMs?: number
  /** 是否立即执行一次，默认 true。 */
  immediate?: boolean
  /** 返回 false 时本轮跳过（例如 SSE 已恢复实时）。 */
  shouldRun?: () => boolean
  /** 每次执行失败时回调（不会中断轮询）。 */
  onError?: (err: unknown) => void
}

/**
 * 轮询兜底（SSE 降级时使用）。
 * 采用递归 setTimeout 而非 setInterval，保证上一轮结束前不会叠加下一轮。
 */
export function usePolling(task: () => Promise<void> | void, options: UsePollingOptions = {}) {
  const intervalMs = options.intervalMs ?? 3000
  const active = ref(false)
  const running = ref(false)
  const lastError = ref<string | null>(null)
  const ticks = ref(0)

  let timer: number | null = null
  let disposed = false

  function clear(): void {
    if (timer !== null) {
      window.clearTimeout(timer)
      timer = null
    }
  }

  async function runOnce(): Promise<void> {
    if (disposed) return
    if (options.shouldRun && !options.shouldRun()) return
    if (running.value) return
    running.value = true
    try {
      await task()
      lastError.value = null
      ticks.value += 1
    } catch (err) {
      lastError.value = err instanceof Error ? err.message : String(err)
      options.onError?.(err)
    } finally {
      running.value = false
    }
  }

  function schedule(): void {
    if (disposed || !active.value) return
    clear()
    timer = window.setTimeout(async () => {
      timer = null
      await runOnce()
      schedule()
    }, intervalMs)
  }

  function start(): void {
    if (active.value) return
    disposed = false
    active.value = true
    if (options.immediate !== false) {
      void runOnce().then(schedule)
    } else {
      schedule()
    }
  }

  function stop(): void {
    active.value = false
    clear()
  }

  /** 立刻执行一次（不改变轮询节奏）。 */
  function refresh(): Promise<void> {
    return runOnce()
  }

  onScopeDispose(() => {
    disposed = true
    active.value = false
    clear()
  })

  return { active, running, lastError, ticks, start, stop, refresh }
}
