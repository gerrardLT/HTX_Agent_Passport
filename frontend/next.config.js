/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // 后端 API 默认端口 8000（docker-compose / 本地 uvicorn）
  // NEXT_PUBLIC_API_BASE_URL 优先于此默认值
  env: {
    // 用 ?? 而非 ||：生产构建注入空字符串表示「同域相对路径」（请求 /api/...），
    // 空字符串是合法值不应被 localhost 默认顶替；仅未定义时才回退本地开发地址。
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000',
  },
};

module.exports = nextConfig;
