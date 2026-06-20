/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // 后端 API 默认端口 8000（docker-compose / 本地 uvicorn）
  // NEXT_PUBLIC_API_BASE_URL 优先于此默认值
  env: {
    NEXT_PUBLIC_API_BASE_URL:
      process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000',
  },
};

module.exports = nextConfig;
