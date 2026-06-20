'use client';

import { useState } from 'react';
import { approvalApi } from '@/lib/api-client';
import { useAuth } from '@/hooks/useAuth';
import type { ActionDetail } from '@/hooks/useActionPolling';

interface ApprovalModalProps {
  action: ActionDetail;
  onComplete: () => void;
}

/**
 * 审批弹窗组件。
 * - 显示操作摘要：symbol、side、order_type、amount、max_notional_usdt
 * - 显示 risk_notes 列表
 * - 显示 risk_score 进度条
 * - typed_confirmation 输入框（必须输入 "APPROVE"）
 * - 确认/拒绝按钮 → POST /api/actions/{action_id}/approve
 */
export function ApprovalModal({ action, onComplete }: ApprovalModalProps) {
  const { token } = useAuth();
  const [confirmation, setConfirmation] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const plan = action.normalized_action_json;
  const firstAction = plan?.actions?.[0];
  const riskNotes = plan?.risk_notes ?? [];
  const riskScore = action.risk_score ?? 0;

  const canApprove = confirmation === 'APPROVE';

  const handleApprove = async () => {
    if (!canApprove || !token) return;
    setIsSubmitting(true);
    setError(null);
    try {
      await approvalApi.submit(token, action.id, {
        approved: true,
        typed_confirmation: 'APPROVE',
      });
      onComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : '审批提交失败');
      setIsSubmitting(false);
    }
  };

  const handleReject = async () => {
    if (!token) return;
    setIsSubmitting(true);
    setError(null);
    try {
      await approvalApi.submit(token, action.id, {
        approved: false,
        typed_confirmation: '',
      });
      onComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : '拒绝提交失败');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="mx-4 w-full max-w-lg rounded-lg border border-border bg-surface-1 shadow-2xl">
        {/* 标题 */}
        <div className="border-b border-border px-6 py-4">
          <h2 className="text-base font-semibold text-t-1">操作审批</h2>
          <p className="mt-1 text-xs text-t-3">该操作需要您的确认才能执行</p>
        </div>

        {/* 操作摘要 */}
        <div className="space-y-4 px-6 py-4">
          {/* 意图摘要 */}
          {plan?.intent_summary && (
            <div className="rounded-xs border border-border bg-surface-2/50 px-4 py-3">
              <p className="text-sm text-t-2">{plan.intent_summary}</p>
            </div>
          )}

          {/* 操作详情 */}
          {firstAction && (
            <div className="grid grid-cols-2 gap-3 text-sm">
              {firstAction.symbol && (
                <div>
                  <span className="text-t-4">交易对</span>
                  <p className="font-mono text-t-1">{firstAction.symbol.toUpperCase()}</p>
                </div>
              )}
              {firstAction.side && (
                <div>
                  <span className="text-t-4">方向</span>
                  <p className={`font-medium ${firstAction.side === 'buy' ? 'text-status-green' : 'text-status-red'}`}>
                    {firstAction.side === 'buy' ? '买入' : '卖出'}
                  </p>
                </div>
              )}
              {firstAction.order_type && (
                <div>
                  <span className="text-t-4">订单类型</span>
                  <p className="text-t-1">{firstAction.order_type}</p>
                </div>
              )}
              {firstAction.amount != null && (
                <div>
                  <span className="text-t-4">数量</span>
                  <p className="font-mono text-t-1">
                    {firstAction.amount} {firstAction.amount_unit ?? ''}
                  </p>
                </div>
              )}
              {firstAction.max_notional_usdt != null && (
                <div className="col-span-2">
                  <span className="text-t-4">最大名义价值</span>
                  <p className="font-mono text-t-1">{firstAction.max_notional_usdt} USDT</p>
                </div>
              )}
            </div>
          )}

          {/* 匹配策略 */}
          {action.risk_verdict && (
            <div className="text-sm">
              <span className="text-t-4">策略裁决</span>
              <p className="font-medium text-status-yellow">{action.risk_verdict}</p>
            </div>
          )}

          {/* Risk Score 进度条 */}
          <div>
            <div className="flex items-center justify-between text-xs text-t-3">
              <span>风险评分</span>
              <span className="font-mono">{riskScore}/100</span>
            </div>
            <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-white/[.06]">
              <div
                className={`h-full rounded-full transition-all ${
                  riskScore >= 70 ? 'bg-status-red' : riskScore >= 40 ? 'bg-status-yellow' : 'bg-status-green'
                }`}
                style={{ width: `${Math.min(100, riskScore)}%` }}
              />
            </div>
          </div>

          {/* Risk Notes */}
          {riskNotes.length > 0 && (
            <div>
              <p className="text-xs font-medium text-t-3">风险提示</p>
              <ul className="mt-1 space-y-1">
                {riskNotes.map((note, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-status-yellow">
                    <span className="mt-0.5 shrink-0">⚠️</span>
                    <span>{note}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* typed_confirmation 输入 */}
          <div>
            <label htmlFor="typed-confirm" className="block text-xs font-medium text-t-3">
              输入 <span className="font-mono text-t-1">APPROVE</span> 以确认
            </label>
            <input
              id="typed-confirm"
              type="text"
              value={confirmation}
              onChange={(e) => setConfirmation(e.target.value)}
              placeholder="APPROVE"
              className="input-field mt-1 font-mono"
              disabled={isSubmitting}
              autoComplete="off"
            />
          </div>

          {/* 错误提示 */}
          {error && (
            <div className="rounded-lg border border-status-red/40 bg-status-red-bg px-4 py-2 text-xs text-status-red">
              {error}
            </div>
          )}
        </div>

        {/* 操作按钮 */}
        <div className="flex gap-3 border-t border-border px-6 py-4">
          <button
            onClick={handleReject}
            disabled={isSubmitting}
            className="flex-1 btn-outline disabled:opacity-50"
          >
            拒绝
          </button>
          <button
            onClick={handleApprove}
            disabled={!canApprove || isSubmitting}
            className="flex-1 rounded-xs bg-status-green px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-status-green/80 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSubmitting ? '提交中...' : '确认执行'}
          </button>
        </div>
      </div>
    </div>
  );
}
