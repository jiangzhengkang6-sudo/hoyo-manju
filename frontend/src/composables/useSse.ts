import { onScopeDispose, ref } from 'vue'
import { SSE_EVENT_TYPES, isHeartbeat } from '@/api/events'
import type { SseConnectionState, SseEvent } from '@/types'

export interface UseSseOptions {
  /**
   * 根据当前续传游标构造应连接的地址；返回 null 表示暂不连接。
   *
   * 游标由 useSse 内部维护并作为参数传入，调用方**不要**在闭包里引用
   * `const sse = useSse({...})` 的返回值——`immediate` 为 true 时 `url` 会在
   * 初始化过程中被同步调用，自引用会触发 TDZ 的 `ReferenceError`。
   */
  url: (lastEventId: string | null) => string | null
  /** 收到业务事件（心跳已过滤）。 */
  onEvent?: (event: SseEvent) => void
  /** 降级状态变化：true 表示 SSE 已不可用，调用方应回退到轮询。 */
  onDegraded?: (degraded: boolean) => void
  /** 连续失败多少次后判定为降级（默认 2 次）。 */
  degradeAfter?: number
  /** 是否立即连接，默认 true。 */
  immediate?: boolean
  /** 用于跨刷新续传的 localStorage 键（保存 last_event_id）。 */
  storageKey?: string
  /** 是否已结束（不再重连）。 */
  stopped?: () => boolean
}

/** 重连退避序列（毫秒），超出后沿用最后一档。 */
const BACKOFF = [1000, 2000, 4000, 8000, 15000]

function readStored(key?: string): string | null {
  if (!key || typeof localStorage === 'undefined') return null
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStored(key: string | undefined, value: string | null): void {
  if (!key || typeof localStorage === 'undefined') return
  try {
    if (value) localStorage.setItem(key, value)
  } catch {
    /* 隐私模式下忽略 */
  }
}

/**
 * SSE 连接管理（API.md §10.1）。
 *
 * 浏览器原生 `EventSource` 在断线重连时也会带上 `Last-Event-ID` 头，
 * 但为了让后端在**首次**连接时就能补齐，且能在重连时换用带参数的新地址，
 * 这里统一由前端管理：每次重连都用最新的 `last_event_id` 重新构造 URL。
 *
 * 连续失败达到 `degradeAfter` 次后置 `degraded=true`，由调用方切到轮询兜底。
 */
export function useSse(options: UseSseOptions) {
  const state = ref<SseConnectionState>('idle')
  const lastEventId = ref<string | null>(readStored(options.storageKey))
  const lastEventAt = ref<number | null>(null)
  const errorCount = ref(0)
  const eventCount = ref(0)
  const degraded = ref(false)
  const lastError = ref<string | null>(null)

  let source: EventSource | null = null
  let retryTimer: number | null = null
  let disposed = false

  const degradeAfter = options.degradeAfter ?? 2

  function setDegraded(next: boolean): void {
    if (degraded.value === next) return
    degraded.value = next
    options.onDegraded?.(next)
  }

  function handle(type: string, raw: MessageEvent<string>): void {
    if (raw.lastEventId) {
      lastEventId.value = raw.lastEventId
      writeStored(options.storageKey, raw.lastEventId)
    }
    eventCount.value += 1
    lastEventAt.value = Date.now()
    errorCount.value = 0
    setDegraded(false)
    if (isHeartbeat(type)) return

    let data: Record<string, unknown> = {}
    if (typeof raw.data === 'string' && raw.data.length) {
      try {
        const parsed: unknown = JSON.parse(raw.data)
        if (parsed && typeof parsed === 'object') data = parsed as Record<string, unknown>
        else data = { value: parsed }
      } catch {
        data = { raw: raw.data }
      }
    }
    options.onEvent?.({
      id: raw.lastEventId || null,
      type,
      data,
      receivedAt: Date.now(),
    })
  }

  function scheduleReconnect(): void {
    if (disposed || options.stopped?.()) return
    const delay = BACKOFF[Math.min(errorCount.value - 1, BACKOFF.length - 1)] ?? 15000
    state.value = 'reconnecting'
    if (retryTimer !== null) window.clearTimeout(retryTimer)
    retryTimer = window.setTimeout(() => {
      retryTimer = null
      open()
    }, delay)
  }

  function open(): void {
    if (disposed || options.stopped?.()) return
    const target = options.url(lastEventId.value)
    if (!target) {
      state.value = 'idle'
      return
    }
    close(false)
    state.value = 'connecting'
    try {
      source = new EventSource(target)
    } catch (err) {
      lastError.value = err instanceof Error ? err.message : String(err)
      errorCount.value += 1
      if (errorCount.value >= degradeAfter) setDegraded(true)
      scheduleReconnect()
      return
    }

    source.onopen = () => {
      state.value = 'live'
      errorCount.value = 0
      lastError.value = null
      setDegraded(false)
    }
    source.onmessage = (raw) => handle('message', raw)
    for (const type of SSE_EVENT_TYPES) {
      source.addEventListener(type, (raw) => handle(type, raw as MessageEvent<string>))
    }
    source.onerror = () => {
      lastError.value = '事件流连接中断'
      errorCount.value += 1
      if (errorCount.value >= degradeAfter) setDegraded(true)
      state.value = 'error'
      // 关闭后由自己控制重连时机（避免浏览器内置重连忽略 last_event_id 参数）
      close(false)
      scheduleReconnect()
    }
  }

  function close(updateState = true): void {
    if (source) {
      source.onopen = null
      source.onmessage = null
      source.onerror = null
      source.close()
      source = null
    }
    if (updateState && state.value !== 'idle') state.value = 'closed'
  }

  function start(): void {
    disposed = false
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer)
      retryTimer = null
    }
    open()
  }

  function stop(): void {
    disposed = true
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer)
      retryTimer = null
    }
    close()
    state.value = 'closed'
  }

  function reset(): void {
    lastEventId.value = null
    writeStored(options.storageKey, null)
    errorCount.value = 0
    setDegraded(false)
  }

  if (options.immediate !== false) start()

  onScopeDispose(() => stop())

  return {
    state,
    lastEventId,
    lastEventAt,
    errorCount,
    eventCount,
    degraded,
    lastError,
    start,
    stop,
    reset,
  }
}
