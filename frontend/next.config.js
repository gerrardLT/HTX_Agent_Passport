/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // 不在此手动声明 env：Next.js 会自动内联所有 NEXT_PUBLIC_ 前缀变量。
  // 之前用 env 字段 + `?? 'http://localhost:8000'` 会在生产把 base 误回退到 localhost
  // （空字符串 env 被 Next 忽略）。base 取值与回退逻辑统一收敛到 src/lib/api.ts 的 getApiBaseUrl()。
};

module.exports = nextConfig;
