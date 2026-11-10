import { ApiError, networkError, parseErrorResponse } from './errors'
import { API_PREFIX, apiUrl } from './base'

// 基址/前缀的实现搬到 ./base（纯函数，避免被 mock 后媒体地址拼不出来），这里转发保持旧引用可用
export { API_PREFIX, apiUrl }

/* ------------------------------------------------------------------ *
 * 全局 toast 通道（避免 api 层直接依赖 Pinia store 造成循环引用）
 * ------------------------------------------------------------------ */

export interface ToastMessage {
  id: number
  kind: 'error' | 'success' | 'info' | 'warning'
  title: string
  message?: string
  detail?: string
  traceId?: string | null
}

type ToastListener = (t: ToastMessage) => void

const toastListeners = new Set<ToastListener>()
let toastSeq = 0

export function onToast(fn: ToastListener): () => void {
  toastListeners.add(fn)
  return () => toastListeners.delete(fn)
}

export function pushToast(t: Omit<ToastMessage, 'id'>): void {
  const msg: ToastMessage = { ...t, id: (toastSeq += 1) }
  for (const fn of toastListeners) fn(msg)
}

/* ------------------------------------------------------------------ *
 * 写操作活动通道
 * 顶部「保存状态」需要真实反映写操作（保存剧本/分镜参数、提交任务、门控决策…），
 * 因此在这里统一广播：任何非 GET 请求开始/成功/失败都会通知订阅者。
 * ------------------------------------------------------------------ */

export type WritePhase = 'start' | 'success' | 'error'

export interface WriteActivity {
  phase: WritePhase
  method: string
  path: string
  /** 失败时的原因（后端 message 或网络错误）。 */
  message?: string
}

type WriteListener = (activity: WriteActivity) => void

const writeListeners = new Set<WriteListener>()

/** 订阅写操作活动；返回取消订阅函数。 */
export function onWriteActivity(fn: WriteListener): () => void {
  writeListeners.add(fn)
  return () => writeListeners.delete(fn)
}

function notifyWrite(activity: WriteActivity): void {
  for (const fn of writeListeners) fn(activity)
}

/** 把 ApiError 统一送进 toast。 */
export function reportError(err: unknown, context?: string): ApiError {
  const apiErr = err instanceof ApiError ? err : networkError(err)
  const detailParts: string[] = []
  if (apiErr.issues.length) {
    detailParts.push(apiErr.issues.map((i) => `${i.label}：${i.message}`).join('；'))
  }
  if (apiErr.traceId) detailParts.push(`trace_id ${apiErr.traceId}`)
  pushToast({
    kind: 'error',
    title: context ? `${context} · ${apiErr.title}` : apiErr.title,
    message: apiErr.message,
    detail: detailParts.join(' · ') || undefined,
    traceId: apiErr.traceId,
  })
  return apiErr
}

/* ------------------------------------------------------------------ *
 * 核心请求
 * ------------------------------------------------------------------ */

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  query?: Record<string, string | number | boolean | null | undefined>
  body?: unknown
  /** multipart 上传时直接传 FormData。 */
  formData?: FormData
  signal?: AbortSignal
  /** 静默模式：不弹 toast，由调用方自己处理错误。 */
  silent?: boolean
  headers?: Record<string, string>
}

function buildQuery(query?: RequestOptions['query']): string {
  if (!query) return ''
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    sp.append(k, String(v))
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, formData, signal, silent, headers } = options
  const url = `${apiUrl(path)}${buildQuery(query)}`
  /** 写操作（非 GET）会广播活动事件，供顶部「保存状态」展示。 */
  const isWrite = method !== 'GET'
  if (isWrite) notifyWrite({ phase: 'start', method, path })

  const init: RequestInit = { method, signal, headers: { Accept: 'application/json', ...headers } }
  if (formData) {
    // 不要手动设置 Content-Type，浏览器需要自己带 boundary
    init.body = formData
  } else if (body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json; charset=utf-8' }
    init.body = JSON.stringify(body)
  }

  let res: Response
  try {
    res = await fetch(url, init)
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    const apiErr = networkError(err)
    if (isWrite) notifyWrite({ phase: 'error', method, path, message: apiErr.message })
    if (!silent) reportError(apiErr)
    throw apiErr
  }

  if (!res.ok) {
    const apiErr = await parseErrorResponse(res)
    if (isWrite) notifyWrite({ phase: 'error', method, path, message: apiErr.message })
    if (!silent) reportError(apiErr)
    throw apiErr
  }

  if (res.status === 204) {
    if (isWrite) notifyWrite({ phase: 'success', method, path })
    return undefined as T
  }

  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('json')) {
    const text = (await res.text()) as unknown as T
    if (isWrite) notifyWrite({ phase: 'success', method, path })
    return text
  }
  const text = await res.text()
  if (!text) {
    if (isWrite) notifyWrite({ phase: 'success', method, path })
    return undefined as T
  }
  try {
    const parsed = JSON.parse(text) as T
    if (isWrite) notifyWrite({ phase: 'success', method, path })
    return parsed
  } catch {
    if (isWrite) notifyWrite({ phase: 'error', method, path, message: '后端返回了无法解析的 JSON' })
    throw new ApiError({
      code: 'INTERNAL_ERROR',
      message: '后端返回了无法解析的 JSON',
      status: res.status,
    })
  }
}

/** 下载文本（SRT/VTT）。 */
export async function requestText(path: string, query?: RequestOptions['query']): Promise<string> {
  const res = await fetch(`${apiUrl(path)}${buildQuery(query)}`, {
    headers: { Accept: 'text/plain, */*' },
  })
  if (!res.ok) {
    const apiErr = await parseErrorResponse(res)
    reportError(apiErr)
    throw apiErr
  }
  return res.text()
}

/** 触发浏览器下载（用于 SRT/VTT/导出）。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 4000)
}

export async function downloadFile(
  path: string,
  filename: string,
  query?: RequestOptions['query'],
): Promise<void> {
  const res = await fetch(`${apiUrl(path)}${buildQuery(query)}`)
  if (!res.ok) {
    const apiErr = await parseErrorResponse(res)
    reportError(apiErr)
    throw apiErr
  }
  downloadBlob(await res.blob(), filename)
}

export const http = {
  get: <T>(path: string, query?: RequestOptions['query'], opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'GET', query }),
  post: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),
  del: <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'DELETE' }),
  upload: <T>(path: string, formData: FormData, opts?: RequestOptions) =>
    request<T>(path, { ...opts, method: 'POST', formData }),
}
