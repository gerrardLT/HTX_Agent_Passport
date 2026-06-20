'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { EnvironmentBadge } from './EnvironmentBadge';
import { useAuth } from '@/hooks/useAuth';

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
}

const NAV_ITEMS: NavItem[] = [
  {
    href: '/dashboard',
    label: '仪表盘',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="2" y="2" width="5" height="5" rx="1" />
        <rect x="9" y="2" width="5" height="5" rx="1" />
        <rect x="2" y="9" width="5" height="5" rx="1" />
        <rect x="9" y="9" width="5" height="5" rx="1" />
      </svg>
    ),
  },
  {
    href: '/credentials',
    label: '凭证管理',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="3" y="5" width="10" height="8" rx="1.5" />
        <circle cx="8" cy="5" r="2.5" />
      </svg>
    ),
  },
  {
    href: '/passports',
    label: '代理护照',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <rect x="2" y="1.5" width="12" height="13" rx="2" />
        <circle cx="8" cy="6.5" r="2" />
      </svg>
    ),
  },
  {
    href: '/demo',
    label: '预设场景',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M4 2v12" />
        <path d="M4 4h6l3 3-3 3H4" />
      </svg>
    ),
  },
  {
    href: '/audit',
    label: '审计日志',
    icon: (
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M3 3h10v10H3z" />
        <path d="M6 6h4" />
        <path d="M6 8.5h4" />
      </svg>
    ),
  },
];

/**
 * Linear 风格左侧导航栏。
 * - 固定在页面左侧
 * - 品牌 logo + 名称
 * - 导航链接（带 SVG 图标 + 活跃指示条）
 * - 底部用户信息 + 退出按钮
 */
export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, isAuthenticated, isInitialized, logout } = useAuth();

  const handleLogout = () => {
    logout();
    router.push('/');
  };

  // 未初始化或未登录时不渲染侧边栏
  if (!isInitialized || !isAuthenticated) {
    return null;
  }

  const initials = user?.wallet
    ? user.wallet.slice(0, 2).toUpperCase()
    : 'U';

  return (
    <aside
      className="fixed left-0 top-0 bottom-0 z-[100] flex w-[240px] min-w-[240px] flex-col border-r border-border bg-surface-0"
    >
      {/* ── Brand ─────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2.5 px-4 pb-4 pt-5">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-xs bg-gradient-to-br from-brand to-brand-light">
          <div className="h-2.5 w-2.5 rotate-45 rounded-[2px] border-2 border-white/90" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight text-t-1">
            Agent Passport
          </div>
          <div className="text-2xs font-medium text-t-3">HTX Genesis</div>
        </div>
      </div>

      {/* ── Nav ───────────────────────────────────────────────────────── */}
      <nav className="px-2">
        <div className="px-2 pb-1.5 pt-2 text-2xs font-medium uppercase tracking-wider text-t-4">
          主菜单
        </div>
        {NAV_ITEMS.map((item) => {
          const isActive =
            pathname === item.href || pathname.startsWith(`${item.href}/`);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`relative flex items-center gap-2 rounded-xs px-2 py-[7px] text-sm font-medium transition-all duration-150 ${
                isActive
                  ? 'bg-active text-t-1'
                  : 'text-t-2 hover:bg-hover hover:text-t-1'
              }`}
            >
              {isActive && (
                <span className="absolute -left-2 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-[3px] bg-brand" />
              )}
              <span
                className={`h-4 w-4 shrink-0 ${
                  isActive ? 'opacity-100' : 'opacity-60'
                }`}
              >
                {item.icon}
              </span>
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* ── Footer: User ──────────────────────────────────────────────── */}
      <div className="mt-auto border-t border-border p-3">
        <div className="group flex items-center gap-2 rounded-xs px-2 py-1.5 transition-colors hover:bg-hover">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand to-brand-light text-2xs font-semibold text-white">
            {initials}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium text-t-1">
              Admin
            </div>
            <div className="truncate text-2xs text-t-3">
              管理员
            </div>
          </div>
          <EnvironmentBadge />
          <button
            onClick={handleLogout}
            title="退出登录"
            className="flex h-6 w-6 items-center justify-center rounded-xs text-t-3 opacity-0 transition-all hover:bg-surface-2 hover:text-t-1 group-hover:opacity-100"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M6 2H4a2 2 0 00-2 2v8a2 2 0 002 2h2" />
              <path d="M10 12l4-4-4-4" />
              <path d="M14 8H6" />
            </svg>
          </button>
        </div>
      </div>
    </aside>
  );
}
