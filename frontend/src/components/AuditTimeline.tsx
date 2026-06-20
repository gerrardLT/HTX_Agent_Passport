'use client';

import { useState } from 'react';

// ─── Types ───────────────────────────────────────────────────────────────────

/** 审计事件（与后端 audit_events 表对应） */
export interface AuditEvent {
  id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  event_json: Record<string, unknown>;
  event_hash: string;
  previous_event_hash: string | null;
  trace_id: string | null;
  created_at: string;
}

/** 事件分组类别 */
type EventGroup = 'request' | 'plan' | 'policy' | 'approval' | 'execution' | 'reputation';

interface GroupConfig {
  label: string;
  match: (eventType: string) => boolean;
  color: {
    border: string;
    bg: string;
    text: string;
    dot: string;
  };
}

// ─── 分组配置 ─────────────────────────────────────────────────────────────────

const GROUP_CONFIGS: Record<EventGroup, GroupConfig> = {
  request: {
    label: '请求',
    match: (t) => t === 'ACTION_REQUESTED',
    color: {
      border: 'border-brand/40',
      bg: 'bg-brand-bg',
      text: 'text-brand-light',
      dot: 'bg-brand',
    },
  },
  plan: {
    label: '计划',
    match: (t) => t.startsWith('MODEL_CALL_') || t === 'PLAN_SCHEMA_VALIDATED',
    color: {
      border: 'border-brand-light/40',
      bg: 'bg-[rgba(139,124,246,.08)]',
      text: 'text-brand-light',
      dot: 'bg-brand-light',
    },
  },
  policy: {
    label: '策略',
    match: (t) => t === 'POLICY_CHECK_COMPLETED',
    color: {
      border: 'border-status-yellow/40',
      bg: 'bg-status-yellow-bg',
      text: 'text-status-yellow',
      dot: 'bg-status-yellow',
    },
  },
  approval: {
    label: '审批',
    match: (t) => t.startsWith('APPROVAL_'),
    color: {
      border: 'border-brand/40',
      bg: 'bg-brand-bg',
      text: 'text-brand',
      dot: 'bg-brand',
    },
  },
  execution: {
    label: '执行',
    match: (t) => t.startsWith('EXECUTION_'),
    color: {
      border: 'border-status-green/40',
      bg: 'bg-status-green-bg',
      text: 'text-status-green',
      dot: 'bg-status-green',
    },
  },
  reputation: {
    label: '声誉',
    match: (t) => t === 'REPUTATION_UPDATED',
    color: {
      border: 'border-border-hover',
      bg: 'bg-status-gray-bg',
      text: 'text-t-2',
      dot: 'bg-status-gray',
    },
  },
};

/** 执行失败事件使用红色覆盖 */
const EXECUTION_FAIL_COLOR = {
  border: 'border-status-red/40',
  bg: 'bg-status-red-bg',
  text: 'text-status-red',
  dot: 'bg-status-red',
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function classifyEvent(eventType: string): EventGroup {
  const order: EventGroup[] = ['request', 'plan', 'policy', 'approval', 'execution', 'reputation'];
  for (const group of order) {
    if (GROUP_CONFIGS[group].match(eventType)) return group;
  }
  // 默认归入请求组
  return 'request';
}

function getEventColor(event: AuditEvent) {
  const group = classifyEvent(event.event_type);
  const config = GROUP_CONFIGS[group];
  // 执行失败使用红色
  if (group === 'execution' && event.event_type.includes('FAILED')) {
    return EXECUTION_FAIL_COLOR;
  }
  return config.color;
}

function formatTime(isoStr: string): string {
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    });
  } catch {
    return isoStr;
  }
}

// ─── 子组件 ──────────────────────────────────────────────────────────────────

interface EventCardProps {
  event: AuditEvent;
}

function EventCard({ event }: EventCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const color = getEventColor(event);
  const group = classifyEvent(event.event_type);
  const groupConfig = GROUP_CONFIGS[group];

  // 提取 reason_codes（如果存在于 event_json 中）
  const reasonCodes =
    event.event_json?.data &&
    typeof event.event_json.data === 'object' &&
    'reason_codes' in (event.event_json.data as Record<string, unknown>)
      ? ((event.event_json.data as Record<string, unknown>).reason_codes as string[])
      : null;

  const handleCopyJson = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(event.event_json, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // 静默失败
    }
  };

  return (
    <div className="relative pl-8">
      {/* 时间线圆点 */}
      <div className={`absolute left-0 top-3 h-3 w-3 rounded-full ${color.dot} ring-2 ring-bg`} />

      {/* 卡片 */}
      <div
        className={`rounded-lg border ${color.border} ${color.bg} px-4 py-3 transition-colors`}
      >
        {/* 头部信息 */}
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className={`text-xs font-medium ${color.text}`}>
                {groupConfig.label}
              </span>
              <span className="font-mono text-xs text-t-2">
                {event.event_type}
              </span>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-t-3">
              <span>
                执行者: <span className="text-t-2">{event.actor_type}</span>
              </span>
              <span>{formatTime(event.created_at)}</span>
            </div>
          </div>
          <button
            onClick={() => setExpanded(!expanded)}
            className="shrink-0 rounded-xs px-2 py-1 text-xs text-t-3 transition-colors hover:bg-surface-2 hover:text-t-1"
          >
            {expanded ? '收起' : '展开'}
          </button>
        </div>

        {/* 被拒路径：显示 reason_codes */}
        {reasonCodes && reasonCodes.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {reasonCodes.map((code, i) => (
              <span
                key={i}
                className="inline-flex items-center rounded border border-status-red/30 bg-status-red-bg px-1.5 py-0.5 text-xs font-mono text-status-red"
              >
                {code}
              </span>
            ))}
          </div>
        )}

        {/* 展开详情 */}
        {expanded && (
          <div className="mt-3 space-y-2 border-t border-border/50 pt-3">
            {/* event_hash */}
            <div>
              <p className="text-xs text-t-4">event_hash</p>
              <p className="mt-0.5 break-all font-mono text-xs text-t-2">
                {event.event_hash}
              </p>
            </div>
            {/* previous_event_hash */}
            <div>
              <p className="text-xs text-t-4">previous_event_hash</p>
              <p className="mt-0.5 break-all font-mono text-xs text-t-2">
                {event.previous_event_hash ?? 'GENESIS'}
              </p>
            </div>
            {/* event_json */}
            <div>
              <div className="flex items-center justify-between">
                <p className="text-xs text-t-4">event_json</p>
                <button
                  onClick={handleCopyJson}
                  className="rounded-xs px-2 py-0.5 text-xs text-t-3 transition-colors hover:bg-surface-2 hover:text-t-1"
                >
                  {copied ? '✓ 已复制' : '复制 JSON'}
                </button>
              </div>
              <pre className="mt-1 max-h-48 overflow-auto rounded-xs bg-surface-2/60 p-2 text-xs text-t-2">
                {JSON.stringify(event.event_json, null, 2)}
              </pre>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── 主组件 ──────────────────────────────────────────────────────────────────

interface AuditTimelineProps {
  events: AuditEvent[];
}

/**
 * 审计时间线组件。
 *
 * - 按 created_at 升序渲染事件
 * - 按 6 类分组显示（请求 → 计划 → 策略 → 审批 → 执行 → 声誉）
 * - 每个事件卡片显示 event_type、actor_type、created_at
 * - 展开详情显示 event_hash、previous_event_hash、event_json
 * - 一键复制 JSON（navigator.clipboard.writeText）
 * - 被拒路径清晰显示 reason_codes
 *
 * Validates: Requirements 12
 */
export function AuditTimeline({ events }: AuditTimelineProps) {
  // 按 created_at 升序排列
  const sorted = [...events].sort(
    (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );

  if (sorted.length === 0) {
    return (
      <div className="card-surface p-8 text-center">
        <p className="text-sm text-t-3">暂无审计事件</p>
      </div>
    );
  }

  return (
    <div className="relative space-y-4">
      {/* 时间线竖线 */}
      <div className="absolute bottom-0 left-[5px] top-0 w-0.5 bg-border" />

      {sorted.map((event) => (
        <EventCard key={event.id} event={event} />
      ))}
    </div>
  );
}
