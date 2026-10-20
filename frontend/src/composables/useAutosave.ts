/**
 * FR-003：自动保存 + 浏览器本地草稿 + 断网重连合并。
 *
 * PRD 要求「编辑内容必须自动保存；浏览器意外关闭后，未提交的改动不得丢失；
 * 网络恢复后应把本地草稿与服务器版本合并，冲突时提示用户而不是静默覆盖」。
 *
 * 设计要点（不造假）：
 * - 草稿按 `episodeId + key` 存 localStorage，并记录写入时间与所基于的**服务器版本号**；
 * - 恢复时对比服务器当前版本：一致 → 可直接还原；服务器已前进 → 标记 `conflict`，
 *   由用户选择「用本地草稿」还是「丢弃本地草稿」，不自动覆盖；
 * - 自动保存走调用方传入的 `save()`，失败不抛给用户，只把 `state` 置为 `error`，
 *   并在 `online` 事件恢复后重试一次。
 */
import { computed, onBeforeUnmount, ref, shallowRef, watch, type Ref } from 'vue'

export interface DraftEnvelope<T> {
  /** 写入时间（毫秒时间戳），用于展示「草稿保存于…」 */
  savedAt: number
  /** 草稿基于的服务器版本号；服务器版本已变则视为冲突 */
  baseVersion: number | null
  /** 剧集 id，防止跨剧集串味 */
  episodeId: string
  value: T
}

export type AutosaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error' | 'offline'

export interface AutosaveOptions<T> {
  /** 剧集 id（响应式） */
  episodeId: Ref<string | null>
  /** 草稿分区键，例如 'story-text' / 'script-draft' */
  key: string
  /** 被编辑的值（响应式） */
  value: Ref<T>
  /** 真正的保存动作；返回服务器最新版本号（可为 null） */
  save: (value: T) => Promise<number | null>
  /** 服务器当前版本号，用于恢复时判定冲突 */
  serverVersion: () => number | null
  /** 默认是否启用；关闭时仅保留本地草稿，不自动提交 */
  enabled?: boolean
  /** 静默期（毫秒），输入停止多久后才触发保存 */
  debounceMs?: number
  /** 判空：返回 true 表示不需要保存（例如全是空白） */
  isEmpty?: (value: T) => boolean
}

const STORAGE_PREFIX = 'hoyomanju:draft:'

function storageKey(episodeId: string, key: string): string {
  return `${STORAGE_PREFIX}${episodeId}:${key}`
}

function readDraft<T>(episodeId: string, key: string): DraftEnvelope<T> | null {
  if (typeof localStorage === 'undefined') return null
  try {
    const raw = localStorage.getItem(storageKey(episodeId, key))
    if (!raw) return null
    const parsed = JSON.parse(raw) as DraftEnvelope<T>
    if (!parsed || parsed.episodeId !== episodeId) return null
    return parsed
  } catch {
    return null
  }
}

function writeDraft<T>(episodeId: string, key: string, envelope: DraftEnvelope<T>): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(storageKey(episodeId, key), JSON.stringify(envelope))
  } catch {
    // 隐私模式 / 配额满：本地草稿不可用不影响主流程
  }
}

export function clearDraft(episodeId: string, key: string): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.removeItem(storageKey(episodeId, key))
  } catch {
    /* 同上 */
  }
}

export function useAutosave<T>(options: AutosaveOptions<T>) {
  const {
    episodeId,
    key,
    value,
    save,
    serverVersion,
    enabled = true,
    debounceMs = 1200,
    isEmpty,
  } = options

  const state = ref<AutosaveState>('idle')
  const lastSavedAt = ref<number | null>(null)
  const lastError = ref<string | null>(null)
  /**
   * 检测到本地草稿与服务器版本不一致时置位，由用户决定取舍。
   * 用 `shallowRef` 是刻意的：`ref` 会把 `DraftEnvelope<T>` 深度解包成
   * `UnwrapRef<T>`，回填时类型对不上（TS2345）。
   */
  const conflict = shallowRef<{ draft: DraftEnvelope<T>; serverVersion: number | null } | null>(null)
  const autoSaveOn = ref(enabled)

  let timer: ReturnType<typeof setTimeout> | null = null
  let disposed = false
  /** 抑制标记：从草稿/服务器回填 value 时不应触发一次自动保存 */
  let suppress = 0

  const dirty = computed(() => state.value === 'pending' || state.value === 'saving')

  function snapshot(version: number | null): DraftEnvelope<T> {
    return {
      savedAt: Date.now(),
      baseVersion: version,
      episodeId: episodeId.value ?? '',
      value: value.value,
    }
  }

  function persistLocal(): void {
    const id = episodeId.value
    if (!id) return
    writeDraft(id, key, snapshot(serverVersion()))
  }

  async function flush(): Promise<void> {
    const id = episodeId.value
    if (!id || disposed) return
    if (isEmpty?.(value.value)) {
      state.value = 'idle'
      return
    }
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      state.value = 'offline'
      persistLocal()
      return
    }
    state.value = 'saving'
    try {
      const version = await save(value.value)
      lastSavedAt.value = Date.now()
      lastError.value = null
      state.value = 'saved'
      clearDraft(id, key)
      void version
    } catch (err) {
      lastError.value = err instanceof Error ? err.message : '自动保存失败'
      state.value = 'error'
      // 失败时把内容落本地，避免用户输入丢失
      persistLocal()
    }
  }

  function schedule(): void {
    if (!autoSaveOn.value) {
      // 自动保存关闭时仍写本地草稿，保证刷新/关页不丢内容
      persistLocal()
      return
    }
    state.value = 'pending'
    persistLocal()
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => void flush(), debounceMs)
  }

  watch(value, () => {
    if (suppress > 0) {
      suppress -= 1
      return
    }
    schedule()
  })

  /** 从草稿或服务器载入内容时调用，避免回填被当成用户编辑。 */
  function loadWithoutSaving(next: T): void {
    suppress += 1
    // `value` 是 Ref<T>，但经 reactive 解包后写回时需要断言回 T
    ;(value as Ref<T>).value = next
  }

  /**
   * 页面挂载时调用：若本地草稿比服务器新，提示用户是否恢复。
   * 返回 `true` 表示已自动恢复（内容一致），调用方无需再处理。
   */
  function restore(): boolean {
    const id = episodeId.value
    if (!id) return false
    const draft = readDraft<T>(id, key)
    if (!draft) return false
    const server = serverVersion()
    if (draft.baseVersion !== null && server !== null && draft.baseVersion !== server) {
      conflict.value = { draft, serverVersion: server }
      return false
    }
    loadWithoutSaving(draft.value)
    state.value = 'saved'
    lastSavedAt.value = draft.savedAt
    return true
  }

  /** 用户选择「用本地草稿」：回填内容并立即提交一次。 */
  async function acceptDraft(): Promise<void> {
    const pending = conflict.value
    conflict.value = null
    if (!pending) return
    loadWithoutSaving(pending.draft.value)
    await flush()
  }

  /** 用户选择「丢弃本地草稿」：清掉本地记录，保留服务器内容。 */
  function discardDraft(): void {
    const id = episodeId.value
    conflict.value = null
    if (id) clearDraft(id, key)
    state.value = 'idle'
  }

  function onOnline(): void {
    if (state.value === 'offline' || state.value === 'error') void flush()
  }

  if (typeof window !== 'undefined') {
    window.addEventListener('online', onOnline)
  }

  onBeforeUnmount(() => {
    disposed = true
    if (timer) clearTimeout(timer)
    if (typeof window !== 'undefined') {
      window.removeEventListener('online', onOnline)
    }
  })

  return {
    state,
    status: state,
    lastSavedAt,
    lastError,
    dirty,
    conflict,
    autoSaveOn,
    schedule,
    flush,
    restore,
    acceptDraft,
    discardDraft,
    loadWithoutSaving,
    persistLocal,
  }
}
