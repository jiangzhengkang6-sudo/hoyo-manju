/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}

interface ImportMetaEnv {
  /** API 基址，默认空字符串（走同源 /api 代理）。 */
  readonly VITE_API_BASE?: string
  /** 是否显示调试信息。 */
  readonly VITE_DEBUG?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
