# hoyo漫剧 前端镜像（Vite 构建 + Nginx 托管）
# 构建：docker build -f frontend/Dockerfile -t hoyomanju-web .
#
# 说明：构建产物是同源静态资源，Nginx 把 /api 反代到 api 服务，
# 因此浏览器端不存在跨域问题，SSE 也能直连（已关闭代理缓冲）。

# ---------- 构建阶段 ----------
FROM node:22-alpine AS build

WORKDIR /web

COPY frontend/package.json frontend/package-lock.json ./
# 有 lockfile 时用 npm ci 保证可复现
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------- 运行阶段 ----------
FROM nginx:1.27-alpine

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
