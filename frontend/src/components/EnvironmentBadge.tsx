'use client';

import type { EnvironmentMode } from '@/lib/types';

const MODE_CONFIG: Record<EnvironmentMode, { label: string; className: string }> = {
  DEMO: {
    label: 'SANDBOX',
    className: 'bg-env-demo/20 text-env-demo border-env-demo/40',
  },
  SIMULATION: {
    label: 'SIMULATION',
    className: 'bg-env-simulation/20 text-env-simulation border-env-simulation/40',
  },
  REAL_READ: {
    label: 'REAL READ',
    className: 'bg-env-real-read/20 text-env-real-read border-env-real-read/40',
  },
  REAL_TRADE: {
    label: 'REAL TRADE',
    className: 'bg-env-real-trade/20 text-env-real-trade border-env-real-trade/40',
  },
};

function getEnvironmentMode(): EnvironmentMode {
  const demoMode = process.env.NEXT_PUBLIC_DEMO_MODE;
  const executionMode = process.env.NEXT_PUBLIC_EXECUTION_MODE;

  if (executionMode === 'REAL_TRADE') return 'REAL_TRADE';
  if (executionMode === 'REAL_READ') return 'REAL_READ';
  if (executionMode === 'SIMULATION') return 'SIMULATION';

  return 'DEMO';
}

/**
 * 环境徽章组件。
 * 根据 NEXT_PUBLIC_EXECUTION_MODE 显示当前运行模式。
 * 4 种模式：SANDBOX（紫色）/ SIMULATION（蓝色）/ REAL_READ（绿色）/ REAL_TRADE（橙色）
 */
export function EnvironmentBadge() {
  const mode = getEnvironmentMode();
  const config = MODE_CONFIG[mode];

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${config.className}`}
    >
      {config.label}
    </span>
  );
}
