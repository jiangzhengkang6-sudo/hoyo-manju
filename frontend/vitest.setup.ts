/**
 * Vitest 全局启动：补齐 jsdom 缺失、但被测代码依赖的浏览器能力。
 *
 * 只补「环境能力」，不 mock 业务模块 —— 业务 mock 放在各用例里，
 * 避免全局替身让测试与实际行为脱节。
 */
import { beforeEach, vi } from 'vitest'

// jsdom 不实现 matchMedia，部分组件（响应式布局）会调用
if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }),
  })
}

// jsdom 不实现 URL.createObjectURL，下载链路依赖它
if (!URL.createObjectURL) {
  Object.defineProperty(URL, 'createObjectURL', { writable: true, value: vi.fn(() => 'blob:mock') })
}
if (!URL.revokeObjectURL) {
  Object.defineProperty(URL, 'revokeObjectURL', { writable: true, value: vi.fn() })
}

beforeEach(() => {
  // 每个用例都从干净的存储开始，避免草稿在用例之间串味
  window.localStorage.clear()
  window.sessionStorage.clear()
})
