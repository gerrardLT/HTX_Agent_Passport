'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { useToast, ToastContainer } from '@/hooks/useToast';
import { credentialApi } from '@/lib/api-client';
import { CredentialForm } from '@/components/CredentialForm';
import type { CredentialFormData } from '@/components/CredentialForm';
import type { Credential } from '@/lib/types';

const STATE_STYLES: Record<string, { label: string; className: string }> = {
  CREATED: { label: '已创建', className: 'bg-status-gray-bg text-status-gray' },
  VALIDATING: { label: '验证中', className: 'bg-brand-bg text-brand' },
  READ_ONLY: { label: '只读', className: 'bg-status-green-bg text-status-green' },
  TRADE_ENABLED: { label: '交易已启用', className: 'bg-status-green-bg text-status-green' },
  INVALID: { label: '无效', className: 'bg-status-red-bg text-status-red' },
  REVOKED: { label: '已撤销', className: 'bg-status-red-bg text-status-red' },
  DELETED: { label: '已删除', className: 'bg-status-gray-bg text-status-gray' },
};

export default function CredentialsPage() {
  const router = useRouter();
  const { token, isAuthenticated, isInitialized } = useAuth();
  const { toasts, addToast, removeToast } = useToast();
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isAdding, setIsAdding] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  useEffect(() => {
    if (isInitialized && !isAuthenticated) router.push('/');
  }, [isInitialized, isAuthenticated, router]);

  const fetchCredentials = useCallback(async () => {
    if (!token) return;
    try {
      const res = await credentialApi.list(token);
      setCredentials(res.credentials);
    } catch { addToast('error', '加载凭证列表失败'); }
    finally { setIsLoading(false); }
  }, [token, addToast]);

  useEffect(() => { fetchCredentials(); }, [fetchCredentials]);

  const handleAdd = async (data: CredentialFormData) => {
    if (!token) return;
    setIsAdding(true);
    try {
      await credentialApi.create(token, data);
      setShowForm(false);
      addToast('success', '凭证添加成功');
      await fetchCredentials();
    } catch (err: unknown) {
      addToast('error', err instanceof Error ? err.message : '添加凭证失败');
    } finally { setIsAdding(false); }
  };

  const handleValidate = async (id: string) => {
    if (!token) return;
    setActionLoading(id);
    try {
      await credentialApi.validate(token, id);
      addToast('success', '凭证验证完成');
      await fetchCredentials();
    } catch (err: unknown) {
      addToast('error', err instanceof Error ? err.message : '验证失败');
    } finally { setActionLoading(null); }
  };

  const handleDelete = async (id: string) => {
    if (!token) return;
    setActionLoading(id);
    try {
      await credentialApi.remove(token, id);
      addToast('success', '凭证已删除');
      await fetchCredentials();
    } catch (err: unknown) {
      addToast('error', err instanceof Error ? err.message : '删除失败');
    } finally { setActionLoading(null); }
  };

  if (!isAuthenticated) return null;

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <ToastContainer toasts={toasts} onRemove={removeToast} />
      <div className="relative mb-10">
        <div className="pointer-events-none absolute -left-12 -top-10 right-0 h-[200px] bg-[radial-gradient(ellipse_60%_50%_at_30%_0%,rgba(94,106,210,.08),transparent)]" />
        <div className="flex items-center justify-between">
          <h1 className="page-heading">凭证管理</h1>
          <button onClick={() => setShowForm(!showForm)} className="btn-primary">
            {showForm ? '取消' : '添加凭证'}
          </button>
        </div>
      </div>

      {showForm && (
        <div className="card-surface mb-6 p-6">
          <h2 className="mb-4 text-base font-medium text-t-1">添加 HTX API 凭证</h2>
          <CredentialForm onSubmit={handleAdd} isLoading={isAdding} />
        </div>
      )}

      {isLoading ? (
        <div className="py-12 text-center text-sm text-t-3">加载中...</div>
      ) : credentials.length === 0 ? (
        <div className="card-surface p-8 text-center">
          <p className="text-sm text-t-3">暂无凭证</p>
          <p className="mt-1 text-xs text-t-4">点击&ldquo;添加凭证&rdquo;开始添加您的 HTX API 密钥</p>
        </div>
      ) : (
        <div className="space-y-3">
          {credentials.map((cred) => {
            const stateConfig = STATE_STYLES[cred.state] ?? STATE_STYLES.CREATED;
            return (
              <div key={cred.id} className="card-surface p-5">
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="text-sm font-medium text-t-1">{cred.label}</h3>
                    <p className="mt-1 font-mono text-2xs text-t-4">ID: {cred.id.slice(0, 8)}...</p>
                  </div>
                  <span className={`badge-dot ${stateConfig.className}`}>
                    {stateConfig.label}
                  </span>
                </div>

                <div className="mt-3 flex items-center gap-4 text-xs text-t-3">
                  <span>提供商: {cred.provider}</span>
                  {cred.permissions.read && <span className="text-status-green">读取 ✓</span>}
                  {cred.permissions.trade && <span className="text-status-green">交易 ✓</span>}
                  {cred.last_validated_at && (
                    <span>上次验证: {new Date(cred.last_validated_at).toLocaleString('zh-CN')}</span>
                  )}
                </div>

                <div className="mt-4 flex gap-2">
                  <button
                    onClick={() => handleValidate(cred.id)}
                    disabled={actionLoading === cred.id}
                    className="btn-outline py-1.5 px-3 text-xs disabled:opacity-50"
                  >
                    {actionLoading === cred.id ? '处理中...' : '验证'}
                  </button>
                  <button
                    onClick={() => handleDelete(cred.id)}
                    disabled={actionLoading === cred.id}
                    className="btn-outline border-status-red/50 py-1.5 px-3 text-xs text-status-red hover:bg-status-red-bg disabled:opacity-50"
                  >
                    删除
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </main>
  );
}
