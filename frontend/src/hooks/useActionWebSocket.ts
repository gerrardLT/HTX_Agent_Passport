'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { getApiBaseUrl } from '@/lib/api';
import type { ActionDetail, ActionState } from '@/lib/types';

// Re-export for backward compatibility
export type { ActionDetail };

export interface UseActionWebSocketReturn {
  action: ActionDetail | null;
  isConnected: boolean;
  error: string | null;
}

/**
 * WebSocket hook：订阅 action 状态变更的实时推送。
 *
 * - 建立 WebSocket 连接到 `ws://host/ws/actions/{action_id}?token=JWT`
 * - 自动重连（指数退避，最多 5 次）
 * - 状态更新回调
 * - 兼容 `useActionPolling` 的 API（可无缝替换）
 */
export function useActionWebSocket(
  actionId: string | null,
  onStateChange?: (action: ActionDetail) => void,
): UseActionWebSocketReturn {
  const { token } = useAuth();
  const [action, setAction] = useState<ActionDetail | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCountRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);

  const MAX_RECONNECTS = 5;

  const connect = useCallback(() => {
    if (!actionId || !token) return;

    // 构造 WebSocket URL
    const apiBase = getApiBaseUrl();
    const wsProtocol = apiBase.startsWith('https') ? 'wss' : 'ws';
    const wsHost = apiBase.replace(/^https?:\/\//, '');
    const wsUrl = `${wsProtocol}://${wsHost}/ws/actions/${actionId}?token=${encodeURIComponent(token)}`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!isMountedRef.current) return;
        setIsConnected(true);
        setError(null);
        reconnectCountRef.current = 0;
      };

      ws.onmessage = (event) => {
        if (!isMountedRef.current) return;

        try {
          const data = JSON.parse(event.data);

          // 心跳消息忽略
          if (data.type === 'ping') return;

          // 连接确认消息
          if (data.type === 'connected') {
            return;
          }

          // Action 状态更新
          if (data.action_id || data.state) {
            const updatedAction = data as ActionDetail;
            setAction(updatedAction);
            onStateChange?.(updatedAction);
          }
        } catch {
          // 非 JSON 消息忽略
        }
      };

      ws.onclose = (event) => {
        if (!isMountedRef.current) return;
        setIsConnected(false);

        // 正常关闭不重连
        if (event.code === 1000 || event.code === 1001) return;

        // 认证失败不重连
        if (event.code === 4001 || event.code === 4002) {
          setError(`WebSocket auth failed: ${event.reason}`);
          return;
        }

        // 指数退避重连
        if (reconnectCountRef.current < MAX_RECONNECTS) {
          const delay = Math.min(1000 * Math.pow(2, reconnectCountRef.current), 30000);
          reconnectCountRef.current += 1;
          reconnectTimerRef.current = setTimeout(() => {
            if (isMountedRef.current) {
              connect();
            }
          }, delay);
        } else {
          setError('WebSocket reconnect limit reached');
        }
      };

      ws.onerror = () => {
        if (!isMountedRef.current) return;
        setError('WebSocket connection error');
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create WebSocket');
    }
  }, [actionId, token, onStateChange]);

  useEffect(() => {
    isMountedRef.current = true;

    if (!actionId || !token) return;

    connect();

    return () => {
      isMountedRef.current = false;

      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      if (wsRef.current) {
        wsRef.current.close(1000, 'component unmount');
        wsRef.current = null;
      }
    };
  }, [actionId, token, connect]);

  return { action, isConnected, error };
}
