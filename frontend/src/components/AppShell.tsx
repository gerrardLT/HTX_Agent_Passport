'use client';

import { useAuth } from '@/hooks/useAuth';

/**
 * 应用外壳。
 * 已登录时为 Sidebar 留出左侧 240px 空间；未登录时全宽。
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isInitialized } = useAuth();
  const showShell = isInitialized && isAuthenticated;

  return (
    <div className={showShell ? 'ml-[240px]' : ''}>
      {children}
    </div>
  );
}
