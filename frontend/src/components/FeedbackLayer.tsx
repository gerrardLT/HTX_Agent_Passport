'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import type { ActionDetail } from '@/hooks/useActionPolling';
import type { ActionState } from '@/lib/types';

interface FeedbackLayerProps {
  action: ActionDetail;
}

/** 加载 spinner 组件 */
function Spinner() {
  return (
    <svg className="h-5 w-5 animate-spin text-brand" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

/** 状态对应的反馈配置 */
interface FeedbackConfig {
  message: string;
  icon: string;
  color: string;
  showSpinner: boolean;
}

function getFeedbackConfig(state: ActionState): FeedbackConfig {
  switch (state) {
    case 'REQUESTED':
    case 'PLANNING':
      return {
        message: '正在调用 AI 规划器...',
        icon: '🤖',
        color: 'text-brand-light',
        showSpinner: true,
      };
    case 'PLAN_VALIDATED':
    case 'RISK_CHECKING':
      return {
        message: '操作计划已生成，正在进行策略检查...',
        icon: '🛡️',
        color: 'text-status-yellow',
        showSpinner: true,
      };
    case 'APPROVAL_REQUIRED':
      return {
        message: '该操作需要您的确认',
        icon: '✋',
        color: 'text-status-yellow',
        showSpinner: false,
      };
    case 'AUTO_APPROVED':
    case 'APPROVED':
    case 'EXECUTING':
      return {
        message: '正在执行...',
        icon: '⚡',
        color: 'text-brand-light',
        showSpinner: true,
      };
    case 'EXECUTED':
      return {
        message: '执行成功',
        icon: '✅',
        color: 'text-status-green',
        showSpinner: false,
      };
    case 'AUTO_REJECTED':
      return {
        message: '操作被策略引擎自动拒绝',
        icon: '🚫',
        color: 'text-status-red',
        showSpinner: false,
      };
    case 'REJECTED_BY_USER':
      return {
        message: '操作已被用户拒绝',
        icon: '❌',
        color: 'text-status-red',
        showSpinner: false,
      };
    case 'PLAN_INVALID':
      return {
        message: '操作计划无效',
        icon: '⚠️',
        color: 'text-status-red',
        showSpinner: false,
      };
    case 'EXECUTION_FAILED':
    case 'FAILED':
      return {
        message: '执行失败',
        icon: '💥',
        color: 'text-status-red',
        showSpinner: false,
      };
    case 'EXPIRED':
      return {
        message: '审批已过期',
        icon: '⏰',
        color: 'text-t-3',
        showSpinner: false,
      };
    case 'CANCELLED':
      return {
        message: '操作已取消',
        icon: '🚫',
        color: 'text-t-3',
        showSpinner: false,
      };
    default:
      return {
        message: '处理中...',
        icon: '⏳',
        color: 'text-t-2',
        showSpinner: true,
      };
  }
}

/**
 * 分层反馈组件。
 * 根据 action state 显示不同反馈：
 * - REQUESTED/PLANNING → "正在调用 AI 规划器..."
 * - PLAN_VALIDATED/RISK_CHECKING → "操作计划已生成，正在进行策略检查..."
 * - APPROVAL_REQUIRED → 提示需要审批（ApprovalModal 由父组件渲染）
 * - EXECUTING → "正在执行..."
 * - EXECUTED → 成功结果 + 审计链接
 * - AUTO_REJECTED/PLAN_INVALID → 错误信息 + reason_codes
 * - 任何步骤 > 5 秒显示 spinner
 */
export function FeedbackLayer({ action }: FeedbackLayerProps) {
  const config = getFeedbackConfig(action.state);
  const [showTimeoutSpinner, setShowTimeoutSpinner] = useState(false);
  const stateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevStateRef = useRef<ActionState>(action.state);

  // 任何步骤 > 5 秒显示加载指示器
  useEffect(() => {
    if (action.state !== prevStateRef.current) {
      prevStateRef.current = action.state;
      setShowTimeoutSpinner(false);
      if (stateTimerRef.current) {
        clearTimeout(stateTimerRef.current);
      }
    }

    if (config.showSpinner) {
      stateTimerRef.current = setTimeout(() => {
        setShowTimeoutSpinner(true);
      }, 5000);
    }

    return () => {
      if (stateTimerRef.current) {
        clearTimeout(stateTimerRef.current);
      }
    };
  }, [action.state, config.showSpinner]);

  return (
    <div className="space-y-4">
      {/* 主反馈区域 */}
      <div className="card-surface px-5 py-4">
        <div className="flex items-center gap-3">
          <span className="text-xl">{config.icon}</span>
          <div className="flex-1">
            <p className={`text-sm font-medium ${config.color}`}>{config.message}</p>
            {action.normalized_action_json?.intent_summary && (
              <p className="mt-1 text-xs text-t-3">
                {action.normalized_action_json.intent_summary}
              </p>
            )}
          </div>
          {(config.showSpinner || showTimeoutSpinner) && <Spinner />}
        </div>
      </div>

      {/* 超时提示 */}
      {showTimeoutSpinner && config.showSpinner && (
        <div className="card-surface px-4 py-2 text-xs text-t-3">
          处理时间较长，请耐心等待...
        </div>
      )}

      {/* 错误状态：显示 reason_codes */}
      {(action.state === 'AUTO_REJECTED' ||
        action.state === 'PLAN_INVALID' ||
        action.state === 'EXECUTION_FAILED' ||
        action.state === 'FAILED') &&
        action.reason_codes &&
        action.reason_codes.length > 0 && (
          <div className="rounded-lg border border-status-red/30 bg-status-red-bg px-5 py-3">
            <p className="text-xs font-medium text-status-red">拒绝原因</p>
            <ul className="mt-2 space-y-1">
              {action.reason_codes.map((code, i) => (
                <li key={i} className="flex items-center gap-2 text-xs text-status-red">
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-status-red" />
                  <span className="font-mono">{code}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

      {/* 成功状态：显示执行结果 + 审计链接 */}
      {action.state === 'EXECUTED' && (
        <div className="space-y-3">
          <div className="rounded-lg border border-status-green/30 bg-status-green-bg px-5 py-3">
            <p className="text-xs font-medium text-status-green">执行完成</p>
            <p className="mt-1 text-xs text-t-2">
              模式: <span className="font-mono">{action.execution_mode}</span>
            </p>
            {action.trace_id && (
              <p className="mt-1 text-xs text-t-3">
                Trace ID: <span className="font-mono">{action.trace_id}</span>
              </p>
            )}
          </div>
          <Link
            href={`/actions/${action.id}/audit`}
            className="btn-outline inline-flex items-center gap-2"
          >
            <span>📋</span>
            查看审计重放
          </Link>
        </div>
      )}
    </div>
  );
}
