/// <reference types="node" />
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 端口刻意选不常见的高位端口，避免与常见开发服务（3000/5173/8000/8080）冲突。
// 需要临时改端口时用环境变量覆盖，不要改这里：
//   FRONTEND_PORT=15731  BACKEND_ORIGIN=http://127.0.0.1:18437
const FRONTEND_PORT = Number(process.env.FRONTEND_PORT) || 15731
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || 'http://127.0.0.1:18437'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: FRONTEND_PORT,
    strictPort: true,
    proxy: {
      // 所有 API 与 SSE 都走 /api，转发到本地 FastAPI。
      // 关闭缓冲与超时，保证 text/event-stream 逐条到达浏览器。
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
        ws: false,
        timeout: 0,
        proxyTimeout: 0,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type'] || '').includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform'
              proxyRes.headers['x-accel-buffering'] = 'no'
              delete proxyRes.headers['content-length']
            }
          })
        },
      },
    },
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
