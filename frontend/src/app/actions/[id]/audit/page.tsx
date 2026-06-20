'use client';

import { useParams } from 'next/navigation';
import { useState, useEffect } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { actionApi } from '@/lib/api-client';
import { AuditTimeline, type AuditEvent } from '@/components/AuditTimeline';
import { STHViewer } from '@/components/STHViewer';

interface ObservabilityData {
  decision: { route_type: string | null; capability_envelope: string | null; model_selection: string | null; };
  execution: { tool_calls: number; total_latency_ms: number; execution_mode: string | null; };
  quality: { policy_reject_count: number; planner_retry_count: number; total_events: number; };
  trace: { trace_id: string | null; event_count: number; chain_start: string | null; chain_end: string | null; };
}

function deriveObservability(events: AuditEvent[], traceId: string | null): ObservabilityData {
  const policyEvents = events.filter((e) => e.event_type === 'POLICY_CHECK_COMPLETED');
  const rejectCount = policyEvents.filter((e) => {
    const data = e.event_json?.data as Record<string, unknown> | undefined;
    return data?.verdict === 'REJECT';
  }).length;
  const modelEvents = events.filter((e) => e.event_type.startsWith('MODEL_CALL_'));
  const retryCount = Math.max(0, modelEvents.length - 1);
  const executionEvents = events.filter((e) => e.event_type.startsWith('EXECUTION_'));
  const totalLatency = executionEvents.reduce((sum, e) => {
    const data = e.event_json?.data as Record<string, unknown> | undefined;
    return sum + (typeof data?.latency_ms === 'number' ? data.latency_ms : 0);
  }, 0);
  const requestEvent = events.find((e) => e.event_type === 'ACTION_REQUESTED');
  const requestData = requestEvent?.event_json?.data as Record<string, unknown> | undefined;
  const routeType = requestData?.route_type as string | null ?? null;
  const execMode = (executionEvents[0]?.event_json?.data as Record<string, unknown> | undefined)?.mode as string | null ?? null;

  return {
    decision: { route_type: routeType, capability_envelope: modelEvents.length > 0 ? '已加载' : '未使用', model_selection: modelEvents.length > 0 ? 'B.AI Planner' : '规则路由' },
    execution: { tool_calls: executionEvents.length, total_latency_ms: totalLatency, execution_mode: execMode },
    quality: { policy_reject_count: rejectCount, planner_retry_count: retryCount, total_events: events.length },
    trace: { trace_id: traceId, event_count: events.length, chain_start: events.length > 0 ? events[0].created_at : null, chain_end: events.length > 0 ? events[events.length - 1].created_at : null },
  };
}

function ObservabilityPanel({ data }: { data: ObservabilityData }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {[
        { title: '决策日志', items: [['路由决策', data.decision.route_type], ['能力包', data.decision.capability_envelope], ['模型选择', data.decision.model_selection]] },
        { title: '执行日志', items: [['工具调用次数', data.execution.tool_calls], ['总耗时', `${data.execution.total_latency_ms} ms`], ['执行模式', data.execution.execution_mode]] },
        { title: '质量信号', items: [['策略拦截次数', data.quality.policy_reject_count], ['Planner 重试', data.quality.planner_retry_count], ['事件总数', data.quality.total_events]] },
        { title: 'Trace 链路', items: [['trace_id', data.trace.trace_id], ['事件数', data.trace.event_count], ['时间跨度', data.trace.chain_start && data.trace.chain_end ? `${Math.round((new Date(data.trace.chain_end).getTime() - new Date(data.trace.chain_start).getTime()) / 1000)}s` : null]] },
      ].map((panel) => (
        <div key={panel.title} className="card-surface p-4">
          <h3 className="text-xs font-medium text-t-3">{panel.title}</h3>
          <dl className="mt-2 space-y-1 text-xs">
            {panel.items.map(([label, value]) => (
              <div key={label as string} className="flex justify-between">
                <dt className="text-t-4">{label as string}</dt>
                <dd className="max-w-[140px] truncate font-mono text-t-2" title={String(value ?? '')}>{value != null ? String(value) : '—'}</dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

export default function AuditReplayPage() {
  const params = useParams();
  const actionId = params.id as string;
  const { token, isAuthenticated, isInitialized } = useAuth();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [traceId, setTraceId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isInitialized) return;
    if (!isAuthenticated || !token || !actionId) { setIsLoading(false); return; }
    let cancelled = false;
    async function fetchAudit() {
      try {
        const data = await actionApi.audit(token!, actionId);
        if (cancelled) return;
        setEvents(data.events ?? []);
        setTraceId(data.trace_id ?? null);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : '获取审计事件失败');
      } finally { if (!cancelled) setIsLoading(false); }
    }
    fetchAudit();
    return () => { cancelled = true; };
  }, [actionId, token, isAuthenticated, isInitialized]);

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
          <span className="text-sm text-t-3">加载审计事件...</span>
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

  const observability = deriveObservability(events, traceId);

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <div className="mb-6">
        <h1 className="text-[20px] font-semibold text-t-1">审计重放</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-t-3">
          <span>Action: <span className="font-mono text-t-2">{actionId.slice(0, 8)}...</span></span>
          {traceId && <span>Trace: <span className="font-mono text-t-2">{traceId}</span></span>}
          <span>事件数: <span className="font-mono text-t-1">{events.length}</span></span>
        </div>
      </div>

      <section className="mb-8">
        <h2 className="mb-3 text-sm font-medium text-t-2">可观测性</h2>
        <ObservabilityPanel data={observability} />
      </section>

      <section className="mb-8">
        <h2 className="mb-3 text-sm font-medium text-t-2">审计承诺（STH）</h2>
        <STHViewer />
      </section>

      <section>
        <h2 className="mb-4 text-sm font-medium text-t-2">事件时间线</h2>
        <AuditTimeline events={events} />
      </section>
    </main>
  );
}
