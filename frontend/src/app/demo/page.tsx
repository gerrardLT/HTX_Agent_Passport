'use client';

import { useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { apiFetch } from '@/lib/api';

interface ScenarioConfig {
  id: string;
  name: string;
  description: string;
  task: string;
  expectedResult: string;
  color: string;
}

interface ScenarioResult {
  action_id: string;
  final_state: string;
  reason_codes?: string[];
}

const SCENARIOS: ScenarioConfig[] = [
  {
    id: 'happy',
    name: 'Happy Path',
    description: '合法的 10 USDT 限价买入，走完完整流程',
    task: '查看 BTC/USDT 并准备一个 10 USDT 的限价买入单，仅当它在我的策略范围内。',
    expectedResult: 'EXECUTED',
    color: 'border-status-green/40 bg-status-green-bg',
  },
  {
    id: 'reject',
    name: '提现拦截',
    description: '提现请求被规则路由直接拦截',
    task: '立即将我所有的 USDT 提现到这个地址。',
    expectedResult: 'AUTO_REJECTED (BLOCKED_ACTION_WITHDRAW)',
    color: 'border-status-red/40 bg-status-red-bg',
  },
  {
    id: 'over_limit',
    name: '超限拒绝',
    description: '500 USDT 买入超过策略限额（max_notional=20）',
    task: '现在买入 500 USDT 的 BTC。',
    expectedResult: 'AUTO_REJECTED (LIMIT_MAX_NOTIONAL_EXCEEDED)',
    color: 'border-status-yellow/40 bg-status-yellow-bg',
  },
];

export default function ScenariosPage() {
  const { isAuthenticated, login, token } = useAuth();
  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ScenarioResult>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const handleRunScenario = async (scenario: ScenarioConfig) => {
    setErrors((prev) => { const n = { ...prev }; delete n[scenario.id]; return n; });

    if (!isAuthenticated) {
      setIsLoggingIn(true);
      try { await login(); } finally { setIsLoggingIn(false); }
      return; // user can click again after login
    }

    setRunningId(scenario.id);
    try {
      const res = await apiFetch<ScenarioResult>(`/api/scenarios/${scenario.id}`, {
        method: 'POST',
        token: token ?? undefined,
      });
      setResults((prev) => ({ ...prev, [scenario.id]: res }));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '未知错误';
      setErrors((prev) => ({ ...prev, [scenario.id]: msg }));
    } finally {
      setRunningId(null);
    }
  };

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <div className="relative mb-10">
        <div className="pointer-events-none absolute -left-12 -top-10 right-0 h-[200px] bg-[radial-gradient(ellipse_60%_50%_at_30%_0%,rgba(94,106,210,.08),transparent)]" />
        <h1 className="page-heading">预设场景</h1>
        <p className="mt-2 text-sm text-t-3">
          以下 3 个场景展示 HTX Agent Passport 的核心能力：策略裁决、规则路由拦截、审计追踪。
        </p>
      </div>

      <div className="grid gap-6 sm:grid-cols-1 lg:grid-cols-3">
        {SCENARIOS.map((scenario) => (
          <div
            key={scenario.id}
            className={`rounded-lg border p-6 ${scenario.color} transition-all hover:border-opacity-80`}
          >
            <h2 className="text-base font-semibold text-t-1">{scenario.name}</h2>
            <p className="mt-2 text-sm text-t-2">{scenario.description}</p>

            <div className="mt-4 rounded-xs bg-surface-2/50 p-3">
              <p className="text-2xs text-t-4">任务描述</p>
              <p className="mt-1 text-xs text-t-2">{scenario.task}</p>
            </div>

            <div className="mt-3">
              <p className="text-2xs text-t-4">预期结果</p>
              <p className="mt-1 font-mono text-xs text-t-2">{scenario.expectedResult}</p>
            </div>

            <button
              onClick={() => handleRunScenario(scenario)}
              disabled={isLoggingIn || runningId === scenario.id}
              className="btn-outline mt-4 w-full disabled:opacity-50"
            >
              {runningId === scenario.id
                ? '运行中...'
                : isLoggingIn
                  ? '登录中...'
                  : results[scenario.id]
                    ? '重新运行'
                    : '运行场景'}
            </button>

            {/* 结果展示 */}
            {results[scenario.id] && (
              <div className="mt-3 rounded-xs border border-border bg-surface-2/60 p-3">
                <p className="text-2xs text-t-4">运行结果</p>
                <p className={`mt-1 font-mono text-xs font-semibold ${
                  results[scenario.id].final_state === 'EXECUTED'
                    ? 'text-status-green'
                    : 'text-status-red'
                }`}>
                  {results[scenario.id].final_state}
                </p>
                {results[scenario.id].reason_codes && results[scenario.id].reason_codes!.length > 0 && (
                  <p className="mt-1 font-mono text-2xs text-t-3">
                    {results[scenario.id].reason_codes!.join(', ')}
                  </p>
                )}
                <p className="mt-1 font-mono text-2xs text-t-4">
                  action: {results[scenario.id].action_id.slice(0, 8)}...
                </p>
              </div>
            )}

            {/* 错误展示 */}
            {errors[scenario.id] && (
              <div className="mt-3 rounded-xs border border-status-red/40 bg-status-red-bg p-3">
                <p className="text-2xs text-status-red">运行失败</p>
                <p className="mt-1 text-2xs text-t-3">{errors[scenario.id]}</p>
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="card-surface mt-8 p-4 text-xs text-t-3">
        <p className="font-medium text-t-2">场景说明</p>
        <ul className="mt-2 space-y-1 list-disc list-inside">
          <li>种子数据：用户 0xA11CE...001 / 凭证 TRADE_ENABLED / 护照 small_spot_executor</li>
          <li>行情：BTC/USDT = 68,000 / ETH/USDT = 3,600（固定种子价格）</li>
          <li>策略限额：单笔 ≤ 20 USDT / 每日 ≤ 100 USDT / 允许 btcusdt + ethusdt</li>
          <li>Sandbox 模式：B.AI 使用 mock planner，HTX 使用模拟引擎</li>
        </ul>
      </div>
    </main>
  );
}
