'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { actionApi } from '@/lib/api-client';
import { useAuth } from '@/hooks/useAuth';
import type { ActionState, ActionDetail, ActionPlanSummary, ActionPlanStep, PolicyVerdict } from '@/lib/types';

// Re-export types for backward compatibility
export type { ActionDetail, ActionPlanSummary, ActionPlanStep };

/** 终态集合：到达这些状态后停止轮询 */
const TERMINAL_STATES: Set<ActionState> = new Set([
  'EXECUTED',
  'EXECUTION_FAILED',
  'AUTO_REJECTED',
  'REJECTED_BY_USER',
  'PLAN_INVALID',
  'EXPIRED',
  'FAILED',
  'CANCELLED',
]);

export interface UseActionPollingReturn {
  action: ActionDetail | null;
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
}

/**
 * 轮询 action 状态的 hook。
 * - 每 2 秒轮询 GET /api/actions/{action_id}
 * - 终态时自动停止轮询
 * - 首次请求 < 2 秒内返回
 */
export function useActionPolling(actionId: string | null): UseActionPollingReturn {
  const { token } = useAuth();
  const [action, setAction] = useState<ActionDetail | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const isMountedRef = useRef(true);

  const fetchAction = useCallback(async () => {
    if (!actionId || !token) return;
    try {
      const data = await actionApi.get(token, actionId);
      if (!isMountedRef.current) return;
      setAction(data);
      setError(null);
      setIsLoading(false);

      // 终态时停止轮询
      if (TERMINAL_STATES.has(data.state)) {
        if (intervalRef.current) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
      }
    } catch (err) {
      if (!isMountedRef.current) return;
      setError(err instanceof Error ? err.message : '获取操作状态失败');
      setIsLoading(false);
    }
  }, [actionId, token]);

  useEffect(() => {
    isMountedRef.current = true;

    if (!actionId || !token) {
      setIsLoading(false);
      return;
    }

    // 立即首次请求
    setIsLoading(true);
    fetchAction();

    // 每 2 秒轮询
    intervalRef.current = setInterval(fetchAction, 2000);

    return () => {
      isMountedRef.current = false;
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [actionId, token, fetchAction]);

  return { action, isLoading, error, refetch: fetchAction };
}
