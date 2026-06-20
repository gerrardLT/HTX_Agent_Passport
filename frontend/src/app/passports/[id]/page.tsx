'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter, useParams } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { useToast, ToastContainer } from '@/hooks/useToast';
import { passportApi } from '@/lib/api-client';
import type { Passport, PassportState } from '@/lib/types';

interface ReputationEvent {
  score: number;
  created_at: string;
}

const STATE_STYLES: Record<PassportState, { label: string; className: string }> = {
  DRAFT: { label: '草稿', className: 'bg-status-gray-bg text-status-gray' },
  ACTIVE: { label: '活跃', className: 'bg-status-green-bg text-status-green' },
  PAUSED: { label: '暂停', className: 'bg-status-yellow-bg text-status-yellow' },
  REVOKED: { label: '已撤销', className: 'bg-status-red-bg text-status-red' },
  EXPIRED: { label: '已过期', className: 'bg-status-gray-bg text-status-gray' },
  DELETED: { label: '已删除', className: 'bg-status-gray-bg text-status-gray' },
};

export default function PassportDetailPage() {
  const router = useRouter();
  const params = useParams();
  const { token, isAuthenticated, isInitialized } = useAuth();
  const { toasts, addToast, removeToast } = useToast();
  const [passport, setPassport] = useState<Passport | null>(null);
  const [reputationHistory, setReputationHistory] = useState<ReputationEvent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [showPolicy, setShowPolicy] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const passportId = params?.id as string;

  useEffect(() => {
    if (isInitialized && !isAuthenticated) router.push('/');
  }, [isInitialized, isAuthenticated, router]);

  const fetchPassport = useCallback(async () => {
    if (!token || !passportId) return;
    try {
      const res = await passportApi.get(token, passportId);
      setPassport(res);
    } catch { addToast('error', '加载护照详情失败'); }
    finally { setIsLoading(false); }
  }, [token, passportId, addToast]);

  useEffect(() => { fetchPassport(); }, [fetchPassport]);

  const handleAction = async (action: 'pause' | 'revoke' | 'resume') => {
    if (!token || !passportId) return;
    setActionLoading(action);
    try {
      if (action === 'resume') await passportApi.resume(token, passportId);
      else await passportApi[action](token, passportId);
      const labelMap = { pause: '暂停', revoke: '撤销', resume: '恢复' };
      addToast('success', `护照已${labelMap[action]}`);
      await fetchPassport();
    } catch (err: unknown) {
      addToast('error', err instanceof Error ? err.message : '操作失败');
    } finally { setActionLoading(null); }
  };

  if (!isAuthenticated) return null;

  if (isLoading) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <p className="text-sm text-t-3">加载中...</p>
      </main>
    );
  }

  if (!passport) {
    return (
      <main className="mx-auto max-w-content px-12 py-10">
        <p className="text-sm text-status-red">护照未找到</p>
      </main>
    );
  }

  const stateConfig = STATE_STYLES[passport.state] ?? STATE_STYLES.DRAFT;
  const barColor =
    passport.reputation_score >= 70
      ? 'bg-status-green'
      : passport.reputation_score >= 40
        ? 'bg-status-yellow'
        : 'bg-status-red';

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <ToastContainer toasts={toasts} onRemove={removeToast} />

      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="page-heading">{passport.name}</h1>
          <p className="mt-1 text-sm text-t-3">
            {passport.agent_type} · 版本 {passport.version}
          </p>
        </div>
        <span className={`badge-dot text-xs ${stateConfig.className}`}>
          {stateConfig.label}
        </span>
      </div>

      {/* Reputation Score */}
      <div className="card-surface mt-6 p-6">
        <h2 className="text-sm font-medium text-t-2">声誉分</h2>
        <div className="mt-3 flex items-center gap-4">
          <div className="flex-1">
            <div className="flex items-center justify-between text-xs text-t-4">
              <span>当前分数</span>
              <span className="font-mono text-lg text-t-1">{passport.reputation_score}</span>
            </div>
            <div className="mt-2 h-3 w-full overflow-hidden rounded-full bg-white/[.06]">
              <div
                className={`h-full rounded-full transition-all ${barColor}`}
                style={{ width: `${Math.min(100, Math.max(0, passport.reputation_score))}%` }}
              />
            </div>
          </div>
        </div>

        {reputationHistory.length > 0 && (
          <div className="mt-4">
            <p className="mb-2 text-xs text-t-4">历史趋势</p>
            <div className="flex h-16 items-end gap-1">
              {reputationHistory.map((event, i) => (
                <div key={i} className="flex flex-1 flex-col items-center">
                  <div
                    className={`w-full rounded-sm ${
                      event.score >= 70 ? 'bg-status-green/60' : event.score >= 40 ? 'bg-status-yellow/60' : 'bg-status-red/60'
                    }`}
                    style={{ height: `${Math.max(4, (event.score / 100) * 64)}px` }}
                    title={`${event.score} - ${new Date(event.created_at).toLocaleDateString('zh-CN')}`}
                  />
                  <span className="mt-1 text-[10px] text-t-4">
                    {new Date(event.created_at).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Info */}
      <div className="card-surface mt-3 p-6">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <p className="text-xs text-t-4">ID</p>
            <p className="mt-1 font-mono text-xs text-t-2">{passport.id}</p>
          </div>
          <div>
            <p className="text-xs text-t-4">凭证 ID</p>
            <p className="mt-1 font-mono text-xs text-t-2">{passport.api_credential_id ?? '无'}</p>
          </div>
          <div>
            <p className="text-xs text-t-4">创建时间</p>
            <p className="mt-1 text-xs text-t-2">{new Date(passport.created_at).toLocaleString('zh-CN')}</p>
          </div>
          <div>
            <p className="text-xs text-t-4">更新时间</p>
            <p className="mt-1 text-xs text-t-2">{new Date(passport.updated_at).toLocaleString('zh-CN')}</p>
          </div>
          {passport.expires_at && (
            <div>
              <p className="text-xs text-t-4">过期时间</p>
              <p className="mt-1 text-xs text-t-2">{new Date(passport.expires_at).toLocaleString('zh-CN')}</p>
            </div>
          )}
        </div>
      </div>

      {/* Policy JSON */}
      <div className="card-surface mt-3">
        <button
          onClick={() => setShowPolicy(!showPolicy)}
          className="flex w-full items-center justify-between p-4 text-left"
        >
          <span className="text-sm font-medium text-t-2">策略 JSON</span>
          <span className="text-xs text-t-4">{showPolicy ? '收起' : '展开'}</span>
        </button>
        {showPolicy && (
          <div className="border-t border-border p-4">
            <pre className="max-h-80 overflow-auto rounded-xs bg-surface-2 p-3 font-mono text-xs text-t-2">
              {JSON.stringify(passport.policy, null, 2)}
            </pre>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="mt-6 flex gap-2">
        {passport.state === 'ACTIVE' && (
          <>
            <button onClick={() => handleAction('pause')} disabled={!!actionLoading}
              className="btn-outline border-status-yellow/50 text-status-yellow hover:bg-status-yellow-bg">
              {actionLoading === 'pause' ? '处理中...' : '暂停'}
            </button>
            <button onClick={() => handleAction('revoke')} disabled={!!actionLoading}
              className="btn-outline border-status-red/50 text-status-red hover:bg-status-red-bg">
              {actionLoading === 'revoke' ? '处理中...' : '撤销'}
            </button>
          </>
        )}
        {passport.state === 'PAUSED' && (
          <>
            <button onClick={() => handleAction('resume')} disabled={!!actionLoading}
              className="btn-outline border-status-green/50 text-status-green hover:bg-status-green-bg">
              {actionLoading === 'resume' ? '处理中...' : '恢复'}
            </button>
            <button onClick={() => handleAction('revoke')} disabled={!!actionLoading}
              className="btn-outline border-status-red/50 text-status-red hover:bg-status-red-bg">
              {actionLoading === 'revoke' ? '处理中...' : '撤销'}
            </button>
          </>
        )}
      </div>

      <button onClick={() => router.push('/passports')}
        className="mt-6 text-sm text-t-3 transition-colors hover:text-t-1">
        ← 返回护照列表
      </button>
    </main>
  );
}
