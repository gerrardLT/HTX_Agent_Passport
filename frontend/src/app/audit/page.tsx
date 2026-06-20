'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { auditApi } from '@/lib/api-client';
import { AuditTimeline, type AuditEvent } from '@/components/AuditTimeline';
import { STHViewer } from '@/components/STHViewer';

export default function UserAuditCenterPage() {
  const router = useRouter();
  const { token, user, isAuthenticated, isInitialized } = useAuth();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isInitialized && !isAuthenticated) router.push('/');
  }, [isInitialized, isAuthenticated, router]);

  const fetchEvents = useCallback(async () => {
    if (!token || !user) return;
    setIsLoading(true);
    try {
      const data = await auditApi.listEvents(token, { user_id: user.id, limit: 50 });
      setEvents(data.events ?? []);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取审计事件失败');
    } finally { setIsLoading(false); }
  }, [token, user]);

  useEffect(() => {
    if (isInitialized && isAuthenticated) fetchEvents();
  }, [isInitialized, isAuthenticated, fetchEvents]);

  if (!isInitialized) return null;
  if (!isAuthenticated) return null;

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <div className="relative mb-8">
        <div className="pointer-events-none absolute -left-12 -top-10 right-0 h-[200px] bg-[radial-gradient(ellipse_60%_50%_at_30%_0%,rgba(94,106,210,.08),transparent)]" />
        <h1 className="page-heading">审计中心</h1>
        <p className="mt-1 text-xs text-t-3">
          查看你账户下所有审计事件，以及周期签发的 Signed Tree Head（防篡改承诺）。
        </p>
      </div>

      <section className="mb-8">
        <h2 className="mb-3 text-sm font-medium text-t-2">用户级 STH</h2>
        <STHViewer />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium text-t-2">最近 50 条事件</h2>
        {isLoading ? (
          <div className="flex items-center justify-center gap-3 py-12">
            <svg className="h-5 w-5 animate-spin text-brand" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            <span className="text-sm text-t-3">加载事件...</span>
          </div>
        ) : error ? (
          <div className="rounded-lg border border-status-red/40 bg-status-red-bg px-5 py-4 text-sm text-status-red">
            {error}
          </div>
        ) : (
          <AuditTimeline events={events} />
        )}
      </section>
    </main>
  );
}
