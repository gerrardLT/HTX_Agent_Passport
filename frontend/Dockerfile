# =============================================================================
# HTX Agent Passport — 前端 Dockerfile（生产部署）
# Next.js 14 + standalone output + Alpine 最小运行时
# =============================================================================

# Stage 1: 安装依赖
FROM node:18-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --only=production && \
    cp -R node_modules prod_node_modules && \
    npm ci

# Stage 2: 构建
FROM node:18-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# 构建时注入环境变量（NEXT_PUBLIC_ 前缀会编译进客户端代码）
# NEXT_PUBLIC_API_BASE_URL 默认空 = 同域相对路径（请求 /api/...，由 nginx 转发到后端）。
# 前端各接口 path 已自带 /api 前缀，故此处必须为空，否则会出现 /api/api 重复。
ARG NEXT_PUBLIC_API_BASE_URL=
ARG NEXT_PUBLIC_EXECUTION_MODE=SIMULATION
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_EXECUTION_MODE=$NEXT_PUBLIC_EXECUTION_MODE

RUN npm run build

# Stage 3: 运行时（最小镜像）
FROM node:18-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production

# 非 root 用户
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# 复制 standalone 输出
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
