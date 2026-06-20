'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import { useAuth } from '@/hooks/useAuth';
import { passportApi } from '@/lib/api-client';
import type { PassportSummary } from '@/components/PassportCard';

interface QuickLink {
  href: string;
  title: string;
  description: string;
  iconColor: string;
  icon: React.ReactNode;
}

const QUICK_LINKS: QuickLink[] = [
  {
    href: '/credentials',
    title: '凭证管理',
    description: '查看和管理 API 密钥、Token 等访问凭证的生命周期',
    iconColor: 'bg-brand-border text-brand',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-[18px] w-[18px]">
        <rect x="3" y="5" width="10" height="8" rx="1.5" />
        <circle cx="8" cy="5" r="2.5" />
      </svg>
    ),
  },
  {
    href: '/passports',
    title: '代理护照',
    description: '浏览所有已签发的代理身份护照，管理权限与策略',
    iconColor: 'bg-brand-border text-brand',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-[18px] w-[18px]">
        <rect x="2" y="1.5" width="12" height="13" rx="2" />
        <circle cx="8" cy="6.5" r="2" />
      </svg>
    ),
  },
  {
    href: '/passports/new',
    title: '创建护照',
    description: '为新的 AI 代理签发身份护照，配置操作权限边界',
    iconColor: 'bg-status-green-bg text-status-green',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-[18px] w-[18px]">
        <circle cx="8" cy="8" r="6" />
        <path d="M8 5v6" />
        <path d="M5 8h6" />
      </svg>
    ),
  },
  {
    href: '/demo',
    title: '预设场景',
    description: '快速体验 DeFi 套利、风控审计等预设演示场景',
    iconColor: 'bg-status-yellow-bg text-status-yellow',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-[18px] w-[18px]">
        <path d="M4 2v12" />
        <path d="M4 4h6l3 3-3 3H4" />
      </svg>
    ),
  },
];

export default function DashboardPage() {
  const router = useRouter();
  const { user, token, isAuthenticated, isInitialized } = useAuth();
  const [passports, setPassports] = useState<PassportSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (isInitialized && !isAuthenticated) router.push('/');
  }, [isInitialized, isAuthenticated, router]);

  const fetchPassports = useCallback(async () => {
    if (!token) return;
    try {
      const res = await passportApi.list(token);
      setPassports(res.passports);
    } catch {
      // silent
    } finally {
      setIsLoading(false);
    }
  }, [token]);

  useEffect(() => { fetchPassports(); }, [fetchPassports]);

  if (!isInitialized || !isAuthenticated || !user) return null;

  const activeCount = passports.filter((p) => p.state === 'ACTIVE').length;
  const shortWallet = user.wallet ? `${user.wallet.slice(0, 6)}···${user.wallet.slice(-4)}` : '—';

  return (
    <main>
      {/* Sticky top bar */}
      <header className="sticky top-0 z-50 flex items-center justify-between border-b border-border bg-[rgba(10,11,14,.8)] px-12 py-4 backdrop-blur-xl">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-t-1">仪表盘</span>
          <span className="text-xs text-t-3">
            HTX Agent Passport <span className="text-t-2">/ 概览</span>
          </span>
        </div>
        <div className="flex items-center gap-3">
          <button className="flex items-center gap-1.5 rounded-xs border border-border-subtle bg-surface-1 px-2.5 py-1 text-xs text-t-3 transition-colors hover:border-border-hover hover:text-t-2">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="7" cy="7" r="4.5" />
              <path d="M10.5 10.5L14 14" />
            </svg>
            搜索…<kbd className="rounded-[3px] bg-surface-3 px-1 py-0.5 text-2xs text-t-4">⌘K</kbd>
          </button>
        </div>
      </header>

      <div className="mx-auto max-w-content px-12 pb-20 pt-10">
        {/* Page heading with brand glow */}
        <div className="relative mb-10">
          <div className="pointer-events-none absolute -left-12 -top-10 right-0 h-[200px] bg-[radial-gradient(ellipse_60%_50%_at_30%_0%,rgba(94,106,210,.08),transparent)]" />
          <h1 className="page-heading">控制平面概览</h1>
          <p className="mt-2 max-w-lg text-sm text-t-3">
            管理 AI 代理的身份护照、权限凭证与风险审计。所有代理的操作行为均经过策略引擎校验并记录于不可篡改的审计链。
          </p>
        </div>

        {/* Stats */}
        <section className="mb-12 animate-fade-up">
          <div className="mb-4 flex items-baseline">
            <h2 className="section-title">数据概览</h2>
            <span className="section-desc">当前钱包下的代理护照统计</span>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="card-surface card-surface-hover p-5 after:pointer-events-none after:absolute after:left-0 after:right-0 after:top-0 after:h-px after:bg-gradient-to-r after:from-transparent after:via-brand-bg after:to-transparent after:opacity-0 after:transition-opacity hover:after:opacity-100">
              <div className="mb-2.5 flex items-center gap-1.5 text-xs font-medium text-t-3">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5 opacity-50">
                  <rect x="1.5" y="3" width="13" height="10" rx="2" />
                  <path d="M1.5 7h13" />
                </svg>
                当前钱包地址
              </div>
              <div className="text-lg font-semibold tracking-wide text-t-2 tabular-nums">{shortWallet}</div>
              <div className="mt-2 text-2xs text-t-4">已连接 <span className="mx-1.5 inline-block h-1 w-1 rounded-full bg-status-green align-middle" /> 主网环境</div>
            </div>
            <div className="card-surface card-surface-hover p-5">
              <div className="mb-2.5 flex items-center gap-1.5 text-xs font-medium text-t-3">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5 opacity-50">
                  <rect x="2" y="1.5" width="12" height="13" rx="2" />
                  <path d="M5 5h6" />
                </svg>
                护照总数
              </div>
              <div className="text-[32px] font-bold tracking-tight text-t-1">{isLoading ? '—' : passports.length}</div>
              <div className="mt-2 text-2xs text-t-4">较上周 +2</div>
            </div>
            <div className="card-surface card-surface-hover p-5">
              <div className="mb-2.5 flex items-center gap-1.5 text-xs font-medium text-t-3">
                <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="h-3.5 w-3.5 opacity-50">
                  <circle cx="8" cy="8" r="6" />
                  <path d="M8 5v3l2 2" />
                </svg>
                活跃护照
              </div>
              <div className="text-[32px] font-bold tracking-tight text-t-1">{isLoading ? '—' : activeCount}</div>
              <div className="mt-2 text-2xs text-t-4"><span className="text-status-green">↑ 1</span> 较昨日</div>
            </div>
          </div>
        </section>

        {/* Passport list preview */}
        {!isLoading && passports.length > 0 && (
          <section className="mb-12 animate-fade-up" style={{ animationDelay: '.1s' }}>
            <div className="mb-4 flex items-baseline justify-between">
              <div className="flex items-baseline">
                <h2 className="section-title">代理护照</h2>
                <span className="section-desc">已签发的 AI 代理身份与权限护照</span>
              </div>
              <Link href="/passports" className="text-xs font-medium text-brand hover:underline">查看全部 →</Link>
            </div>
            <div className="overflow-hidden rounded-lg border border-border bg-surface-0">
              {/* Table header */}
              <div className="grid grid-cols-[2fr_1fr_100px_160px] border-b border-border bg-surface-1 px-5 py-2.5">
                <span className="text-2xs font-medium uppercase tracking-wider text-t-4">名称</span>
                <span className="text-2xs font-medium uppercase tracking-wider text-t-4">代理类型</span>
                <span className="text-2xs font-medium uppercase tracking-wider text-t-4">状态</span>
                <span className="text-2xs font-medium uppercase tracking-wider text-t-4">声誉分</span>
              </div>
              {passports.slice(0, 4).map((p) => {
                const stateColors: Record<string, string> = {
                  ACTIVE: 'bg-status-green-bg text-status-green',
                  PAUSED: 'bg-status-yellow-bg text-status-yellow',
                  DRAFT: 'bg-status-gray-bg text-status-gray',
                  REVOKED: 'bg-status-red-bg text-status-red',
                  EXPIRED: 'bg-status-gray-bg text-status-gray',
                  DELETED: 'bg-status-gray-bg text-status-gray',
                };
                const stateLabels: Record<string, string> = {
                  ACTIVE: '活跃', PAUSED: '暂停', DRAFT: '草稿',
                  REVOKED: '已撤销', EXPIRED: '已过期', DELETED: '已删除',
                };
                const barColor = p.reputation_score >= 70 ? 'bg-status-green' : p.reputation_score >= 40 ? 'bg-status-yellow' : 'bg-status-red';
                return (
                  <Link key={p.id} href={`/passports/${p.id}`} className="grid grid-cols-[2fr_1fr_100px_160px] items-center border-b border-border px-5 py-3.5 transition-colors last:border-b-0 hover:bg-hover group">
                    <div className="flex items-center gap-3">
                      <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-gradient-to-br from-brand-border to-brand-bg text-brand">
                        <span className="text-sm">◈</span>
                      </div>
                      <div>
                        <div className="text-sm font-medium tracking-tight text-t-1 group-hover:text-brand transition-colors">{p.name}</div>
                        <div className="text-2xs text-t-4 tabular-nums">{p.id.slice(0, 12)}...</div>
                      </div>
                    </div>
                    <div className="text-xs text-t-2 tabular-nums">{p.agent_type}</div>
                    <div>
                      <span className={`badge-dot ${stateColors[p.state] ?? ''}`}>
                        {stateLabels[p.state] ?? p.state}
                      </span>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/[.06]">
                        <div className={`h-full rounded-full ${barColor}`} style={{ width: `${Math.min(100, Math.max(0, p.reputation_score))}%` }} />
                      </div>
                      <span className="min-w-[24px] text-right text-xs font-semibold tabular-nums text-t-2">{p.reputation_score}</span>
                    </div>
                  </Link>
                );
              })}
            </div>
          </section>
        )}

        {/* Quick links */}
        <section className="animate-fade-up" style={{ animationDelay: '.15s' }}>
          <div className="mb-4 flex items-baseline">
            <h2 className="section-title">快捷入口</h2>
            <span className="section-desc">常用功能快速导航</span>
          </div>
          <div className="grid grid-cols-4 gap-3">
            {QUICK_LINKS.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className="card-surface card-surface-hover group relative overflow-hidden p-5"
              >
                <div className={`mb-3.5 flex h-9 w-9 items-center justify-center rounded-sm ${link.iconColor}`}>
                  {link.icon}
                </div>
                <div className="mb-1 text-sm font-semibold tracking-tight text-t-1">{link.title}</div>
                <div className="text-xs leading-relaxed text-t-3">{link.description}</div>
                <span className="absolute right-4 top-1/2 -translate-y-1/2 translate-x-1 text-base text-t-3 opacity-0 transition-all group-hover:translate-x-0 group-hover:opacity-100">→</span>
              </Link>
            ))}
          </div>
        </section>
      </div>
    </main>
  );
}
