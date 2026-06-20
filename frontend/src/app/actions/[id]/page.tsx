'use client';

import { useParams } from 'next/navigation';
import { useActionPolling } from '@/hooks/useActionPolling';
import { useActionWebSocket } from '@/hooks/useActionWebSocket';
import { useAuth } from '@/hooks/useAuth';
import { FeedbackLayer } from '@/components/FeedbackLayer';
import { ApprovalModal } from '@/components/ApprovalModal';

export default function ActionPage() {
  const params = useParams();
  const actionId = params.id as string;
  const { isAuthenticated, isInitialized } = useAuth();

  const ws = useActionWebSocket(isAuthenticated ? actionId : null);
  const polling = useActionPolling(isAuthenticated && !ws.isConnected ? actionId : null);

  const action = ws.action ?? polling.action;
  const isLoading = ws.isConnected ? !ws.action && !ws.error : polling.isLoading;
  const error = ws.error ?? polling.error;
  const refetch = polling.refetch;

  if (!isInitialized) return null;

  if (!isAuthenticated) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <div className="card-surface p-8 text-center">
          <p className="text-sm text-t-3">请先登录</p>
        </div>
      </main>
    );
  }

  if (isLoading) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <div className="flex items-center justify-center gap-3 py-16">
          <svg className="h-5 w-5 animate-spin text-brand" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <span className="text-sm text-t-3">加载操作状态...</span>
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <div className="rounded-lg border border-status-red/40 bg-status-red-bg px-5 py-4 text-sm text-status-red">
          {error}
        </div>
      </main>
    );
  }

  if (!action) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <div className="card-surface p-8 text-center">
          <p className="text-sm text-t-3">操作不存在</p>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <div className="mb-6">
        <h1 className="text-[20px] font-semibold text-t-1">操作详情</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-t-3">
          <span>状态: <span className="font-mono text-t-2">{action.state}</span></span>
          <span>模式: <span className="font-mono text-t-2">{action.execution_mode}</span></span>
          {action.trace_id && (
            <span>Trace: <span className="font-mono text-t-2">{action.trace_id.slice(0, 8)}...</span></span>
          )}
        </div>
      </div>

      <div className="card-surface mb-6 px-5 py-3">
        <p className="text-2xs text-t-4">任务描述</p>
        <p className="mt-1 text-sm text-t-2">{action.natural_language_request}</p>
      </div>

      <FeedbackLayer action={action} />

      {action.state === 'APPROVAL_REQUIRED' && (
        <ApprovalModal action={action} onComplete={refetch} />
      )}
    </main>
  );
}
