/**
 * API 基址与前缀（纯函数，无副作用）。
 *
 * 单独成文件的原因：`client.ts` 在测试里经常被整体 mock（只给 reportError/pushToast），
 * 而 `<img src>` / `<video src>` 这类**直接交给 DOM 的地址**也需要拼前缀。
 * 若从 client 取，被 mock 后就变成 undefined，媒体地址拼不出来 → 黑屏。
 */

/** 统一 API 前缀。API.md：所有接口前缀 `/api/v1`。 */
export const API_PREFIX = '/api/v1'

/** 允许通过 VITE_API_BASE 指向别的后端（如 http://127.0.0.1:18437）。 */
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export function apiUrl(path: string): string {
  const clean = path.startsWith('/') ? path : `/${path}`
  if (clean.startsWith('/api/')) return `${API_BASE}${clean}`
  return `${API_BASE}${API_PREFIX}${clean}`
}
