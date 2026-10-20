import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { onToast, pushToast, type ToastMessage } from '@/api/client'

/** 单条 toast 的存活时长（毫秒）。错误停留更久，方便看清 trace_id。 */
const TTL: Record<ToastMessage['kind'], number> = {
  success: 3200,
  info: 3600,
  warning: 5200,
  error: 9000,
}

/**
 * 全局提示。
 * 数据来源有两处：
 *  1. api 层通过 `onToast` 推送的错误（`reportError`）；
 *  2. 页面主动调用 `success/info/warn`。
 */
export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastMessage[]>([])
  const timers = new Map<number, number>()
  let seq = 0

  function dismiss(id: number): void {
    items.value = items.value.filter((t) => t.id !== id)
    const timer = timers.get(id)
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timers.delete(id)
    }
  }

  function clear(): void {
    for (const id of timers.keys()) window.clearTimeout(id)
    timers.clear()
    items.value = []
  }

  function push(message: Omit<ToastMessage, 'id'>): number {
    // 同标题同内容的重复提示直接合并，避免批量请求失败时刷屏
    const dup = items.value.find(
      (t) => t.kind === message.kind && t.title === message.title && t.message === message.message,
    )
    if (dup) {
      const timer = timers.get(dup.id)
      if (timer !== undefined) window.clearTimeout(timer)
      timers.set(
        dup.id,
        window.setTimeout(() => dismiss(dup.id), TTL[message.kind]),
      )
      return dup.id
    }
    seq += 1
    const id = seq
    const item: ToastMessage = { ...message, id }
    items.value = [...items.value, item].slice(-4)
    timers.set(
      id,
      window.setTimeout(() => dismiss(id), TTL[message.kind]),
    )
    return id
  }

  function success(title: string, message?: string): number {
    return push({ kind: 'success', title, message })
  }
  function info(title: string, message?: string): number {
    return push({ kind: 'info', title, message })
  }
  function warn(title: string, message?: string): number {
    return push({ kind: 'warning', title, message })
  }
  function error(title: string, message?: string, detail?: string): number {
    return push({ kind: 'error', title, message, detail })
  }

  // 订阅 api 层错误通道（store 只创建一次，订阅也只注册一次）
  const unsubscribe = onToast((t) => {
    push(t)
  })

  /** 供宿主在极端场景下解绑（正常不需要）。 */
  function dispose(): void {
    unsubscribe()
    clear()
  }

  const hasError = computed(() => items.value.some((t) => t.kind === 'error'))

  /** 演示兜底（默认关闭）：仅用于后端未启动时手动触发一条示例提示。 */
  function pushDemo(): void {
    if (import.meta.env.VITE_DEBUG !== 'true') return
    pushToast({ kind: 'info', title: '演示模式', message: '这是一条演示提示，可通过 VITE_DEBUG=true 开启。' })
  }

  return { items, hasError, push, success, info, warn, error, dismiss, clear, dispose, pushDemo }
})
