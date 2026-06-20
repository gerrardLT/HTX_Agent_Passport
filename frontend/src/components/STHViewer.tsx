'use client';

import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import { auditApi } from '@/lib/api-client';
import { useAuth } from '@/hooks/useAuth';
import type { STHResponse } from '@/lib/types';

export type STHRecord = STHResponse;

interface STHViewerProps {
  /**
   * 链选择：传 passport_id 查护照级链；不传 = 用户级链。
   *
   * 关键点：传 `null` 与不传 ``undefined`` 都视为用户级链——后端
   * ``GET /api/audit/sth/latest`` 不带 query 等同于 ``passport_id IS NULL``。
   */
  passportId?: string | null;
}

/**
 * STH（Signed Tree Head）展示组件。
 *
 * 调用 ``GET /api/audit/sth/latest``；额外提供"立即签发"按钮触发
 * ``POST /api/audit/sth/issue``。
 *
 * 设计要点
 * --------
 * - **故障吞错**：后端 404（无 STH）/ 网络错均不让组件抛错；显示"暂无 STH"
 *   提示而非破坏整个父页面。
 * - **复制 root_hash**：评委 / 审计员经常需要把 root 拷给第三方验证。
 * - **签名缩写**：完整 64 字符 hex 在窄列布局会撑爆，截前 12 + 后 8。
 *
 * Validates: G10/G11 周期 STH 签发的产品级展示。
 */
export function STHViewer({ passportId }: STHViewerProps) {
  const { token, isAuthenticated, isInitialized } = useAuth();
  const [sth, setSth] = useState<STHRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isIssuing, setIsIssuing] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const fetchLatest = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    setErrorMessage(null);
    try {
      const data = await auditApi.latestSTH(token, passportId ?? undefined);
      setSth(data);
    } catch (err) {
      // 404 是常态（链上还没签 STH）——不当作错误,显示“暂无 STH”占位即可。
      if (err instanceof ApiError && err.status === 404) {
        setSth(null);
        setErrorMessage(null);
      } else {
        setSth(null);
        setErrorMessage(
          err instanceof Error ? err.message : '获取 STH 失败',
        );
      }
    } finally {
      setIsLoading(false);
    }
  }, [token, passportId]);

  const issueNow = useCallback(async () => {
    if (!token) return;
    setIsIssuing(true);
    setErrorMessage(null);
    try {
      const data = await auditApi.issueSTH(token, passportId ?? undefined);
      setSth(data);
    } catch (err) {
      setErrorMessage(
        err instanceof Error ? err.message : '签发 STH 失败',
      );
    } finally {
      setIsIssuing(false);
    }
  }, [token, passportId]);

  const copyRoot = useCallback(async () => {
    if (!sth?.root_hash) return;
    try {
      await navigator.clipboard.writeText(sth.root_hash);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 浏览器不支持 clipboard API（如 http 非 localhost）—— 静默失败
    }
  }, [sth?.root_hash]);

  useEffect(() => {
    if (!isInitialized) return;
    if (!isAuthenticated) return;
    fetchLatest();
  }, [isInitialized, isAuthenticated, fetchLatest]);

  // ─── 未初始化 / 未登录 → 空 ──────────────────────────────────────────────
  if (!isInitialized || !isAuthenticated) {
    return null;
  }

  // ─── 加载中 ─────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="card-surface px-5 py-4">
        <p className="text-xs text-t-4">加载 STH...</p>
      </div>
    );
  }

  // ─── 主体 ───────────────────────────────────────────────────────────────
  return (
    <div className="card-surface px-5 py-4">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-wide text-t-4">
            Signed Tree Head（防篡改承诺）
          </p>
          <p className="mt-0.5 text-xs text-t-4">
            {passportId ? '护照级链' : '用户级链'}
          </p>
        </div>
        <button
          onClick={issueNow}
          disabled={isIssuing}
          className="btn-outline px-3 py-1 text-xs"
        >
          {isIssuing ? '签发中...' : '立即签发'}
        </button>
      </div>

      {errorMessage && (
        <p className="mb-3 rounded border border-status-red/40 bg-status-red-bg px-3 py-2 text-xs text-status-red">
          {errorMessage}
        </p>
      )}

      {sth ? (
        <dl className="space-y-2 text-xs">
          <div className="grid grid-cols-[110px_1fr] gap-2">
            <dt className="text-t-4">Tree Size</dt>
            <dd className="font-mono text-t-1">{sth.tree_size}</dd>
          </div>
          <div className="grid grid-cols-[110px_1fr] gap-2">
            <dt className="text-t-4">Signed At</dt>
            <dd className="font-mono text-t-2">
              {new Date(sth.signed_at).toLocaleString()}
            </dd>
          </div>
          <div className="grid grid-cols-[110px_1fr] gap-2">
            <dt className="text-t-4">Root Hash</dt>
            <dd className="flex flex-col gap-1">
              <code className="break-all text-t-2">{sth.root_hash}</code>
              <button
                onClick={copyRoot}
                className="self-start text-2xs text-brand hover:text-brand-light"
              >
                {copied ? '已复制 ✓' : '复制'}
              </button>
            </dd>
          </div>
          <div className="grid grid-cols-[110px_1fr] gap-2">
            <dt className="text-t-4">Signature</dt>
            <dd className="font-mono text-t-3" title={sth.signature}>
              {sth.signature.slice(0, 12)}...{sth.signature.slice(-8)}
            </dd>
          </div>
        </dl>
      ) : (
        <p className="text-xs text-t-4">
          尚无 STH（链上事件不足或周期签发未触发）
        </p>
      )}
    </div>
  );
}
