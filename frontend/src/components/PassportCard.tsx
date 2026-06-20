'use client';

import Link from 'next/link';
import type { PassportState } from '@/lib/types';

export interface PassportSummary {
  id: string;
  name: string;
  state: PassportState;
  agent_type: string;
  reputation_score: number;
}

const STATE_STYLES: Record<PassportState, { label: string; className: string }> = {
  DRAFT: { label: '草稿', className: 'bg-status-gray-bg text-status-gray' },
  ACTIVE: { label: '活跃', className: 'bg-status-green-bg text-status-green' },
  PAUSED: { label: '暂停', className: 'bg-status-yellow-bg text-status-yellow' },
  REVOKED: { label: '已撤销', className: 'bg-status-red-bg text-status-red' },
  EXPIRED: { label: '已过期', className: 'bg-status-gray-bg text-status-gray' },
  DELETED: { label: '已删除', className: 'bg-status-gray-bg text-status-gray' },
};

const ICON_COLORS: Record<PassportState, string> = {
  ACTIVE: 'from-[rgba(76,183,130,.2)] to-[rgba(76,183,130,.05)] text-status-green',
  PAUSED: 'from-brand-border to-brand-bg text-brand',
  DRAFT: 'from-[rgba(232,184,76,.2)] to-[rgba(232,184,76,.05)] text-status-yellow',
  REVOKED: 'from-[rgba(232,93,93,.2)] to-[rgba(232,93,93,.05)] text-status-red',
  EXPIRED: 'from-[rgba(107,111,118,.2)] to-[rgba(107,111,118,.05)] text-status-gray',
  DELETED: 'from-[rgba(107,111,118,.2)] to-[rgba(107,111,118,.05)] text-status-gray',
};

interface PassportCardProps {
  passport: PassportSummary;
}

/**
 * 护照卡片组件 — Linear 风格。
 */
export function PassportCard({ passport }: PassportCardProps) {
  const stateConfig = STATE_STYLES[passport.state] ?? STATE_STYLES.DRAFT;
  const iconColor = ICON_COLORS[passport.state] ?? ICON_COLORS.DRAFT;
  const barColor =
    passport.reputation_score >= 70
      ? 'bg-status-green'
      : passport.reputation_score >= 40
        ? 'bg-status-yellow'
        : 'bg-status-red';

  return (
    <Link
      href={`/passports/${passport.id}`}
      className="card-surface card-surface-hover block overflow-hidden p-5"
    >
      <div className="flex items-start justify-between">
        <div className="flex items-start gap-3">
          <div className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-gradient-to-br ${iconColor}`}>
            <span className="text-sm">◈</span>
          </div>
          <div>
            <h3 className="text-sm font-medium tracking-tight text-t-1">{passport.name}</h3>
            <p className="mt-0.5 text-2xs text-t-4">{passport.agent_type}</p>
          </div>
        </div>
        <span className={`badge-dot ${stateConfig.className}`}>
          {stateConfig.label}
        </span>
      </div>

      <div className="mt-4">
        <div className="flex items-center justify-between text-xs text-t-4">
          <span>声誉分</span>
          <span className="font-mono tabular-nums text-t-2">{passport.reputation_score}/100</span>
        </div>
        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-white/[.06]">
          <div
            className={`h-full rounded-full transition-all ${barColor}`}
            style={{ width: `${Math.min(100, Math.max(0, passport.reputation_score))}%` }}
          />
        </div>
      </div>
    </Link>
  );
}
