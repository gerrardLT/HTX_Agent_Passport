'use client';

import { useState, useEffect } from 'react';

/** Policy DSL v0 类型定义 */
export interface PolicyDSLv0 {
  version: string;
  capabilities: {
    read_market: boolean;
    read_account: boolean;
    place_order: boolean;
    cancel_order: boolean;
    withdraw: boolean;
  };
  limits: {
    allowed_symbols: string[];
    max_notional_usdt_per_order: number;
    max_daily_notional_usdt: number;
    max_orders_per_day: number;
  };
  approval: {
    required_for_trade: boolean;
    required_for_policy_change: boolean;
    expires_after_seconds: number;
  };
  blocked_actions: string[];
}

const AVAILABLE_SYMBOLS = [
  'btcusdt', 'ethusdt', 'solusdt', 'dogeusdt', 'xrpusdt',
  'adausdt', 'dotusdt', 'avaxusdt', 'maticusdt', 'linkusdt',
];

const BLOCKED_ACTION_OPTIONS = [
  'withdraw', 'borrow', 'margin', 'transfer_out', 'unknown_tool_call',
];

interface PolicyEditorProps {
  value: PolicyDSLv0;
  onChange: (policy: PolicyDSLv0) => void;
}

/**
 * 策略编辑器组件。
 * 表单编辑 Policy DSL v0 的各字段：
 * - capabilities 开关（withdraw 锁定 false）
 * - limits 输入（allowed_symbols 多选、max_notional、max_daily、max_orders）
 * - approval 开关（required_for_trade、expires_after_seconds）
 * - blocked_actions 多选
 */
export function PolicyEditor({ value, onChange }: PolicyEditorProps) {
  const [policy, setPolicy] = useState<PolicyDSLv0>(value);

  useEffect(() => {
    setPolicy(value);
  }, [value]);

  const update = (partial: Partial<PolicyDSLv0>) => {
    const next = { ...policy, ...partial };
    // withdraw 始终锁定为 false
    next.capabilities = { ...next.capabilities, withdraw: false };
    setPolicy(next);
    onChange(next);
  };

  const toggleCapability = (key: keyof PolicyDSLv0['capabilities']) => {
    if (key === 'withdraw') return; // 锁定
    update({
      capabilities: { ...policy.capabilities, [key]: !policy.capabilities[key] },
    });
  };

  const toggleSymbol = (symbol: string) => {
    const current = policy.limits.allowed_symbols;
    const next = current.includes(symbol)
      ? current.filter((s) => s !== symbol)
      : [...current, symbol];
    update({ limits: { ...policy.limits, allowed_symbols: next } });
  };

  const toggleBlockedAction = (action: string) => {
    const current = policy.blocked_actions;
    const next = current.includes(action)
      ? current.filter((a) => a !== action)
      : [...current, action];
    update({ blocked_actions: next });
  };

  return (
    <div className="space-y-6">
      {/* Capabilities */}
      <section>
        <h4 className="text-sm font-medium text-t-2">能力 (Capabilities)</h4>
        <div className="mt-3 grid grid-cols-2 gap-3">
          {(Object.keys(policy.capabilities) as Array<keyof PolicyDSLv0['capabilities']>).map((cap) => (
            <label
              key={cap}
              className={`flex items-center gap-2 rounded-xs border px-3 py-2 text-sm ${
                cap === 'withdraw'
                  ? 'cursor-not-allowed border-border bg-surface-2/30 text-t-4'
                  : 'cursor-pointer border-border bg-surface-2/50 text-t-2 hover:border-border-hover'
              }`}
            >
              <input
                type="checkbox"
                checked={policy.capabilities[cap]}
                disabled={cap === 'withdraw'}
                onChange={() => toggleCapability(cap)}
                className="h-4 w-4 rounded border-border bg-surface-2 text-brand focus:ring-brand disabled:opacity-50"
              />
              <span>{cap}</span>
              {cap === 'withdraw' && (
                <span className="ml-auto text-xs text-status-red">锁定</span>
              )}
            </label>
          ))}
        </div>
      </section>

      {/* Limits */}
      <section>
        <h4 className="text-sm font-medium text-t-2">限额 (Limits)</h4>
        <div className="mt-3 space-y-4">
          {/* Allowed Symbols */}
          <div>
            <p className="text-xs text-t-3 mb-2">允许的交易对</p>
            <div className="flex flex-wrap gap-2">
              {AVAILABLE_SYMBOLS.map((symbol) => (
                <button
                  key={symbol}
                  type="button"
                  onClick={() => toggleSymbol(symbol)}
                  className={`rounded-xs border px-2.5 py-1 text-xs font-mono transition-colors ${
                    policy.limits.allowed_symbols.includes(symbol)
                      ? 'border-brand bg-brand-bg text-brand'
                      : 'border-border bg-surface-2 text-t-3 hover:border-border-hover'
                  }`}
                >
                  {symbol.toUpperCase()}
                </button>
              ))}
            </div>
          </div>

          {/* Numeric limits */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div>
              <label htmlFor="max-notional" className="block text-xs text-t-3">
                单笔最大名义值 (USDT)
              </label>
              <input
                id="max-notional"
                type="number"
                min={0}
                value={policy.limits.max_notional_usdt_per_order}
                onChange={(e) =>
                  update({
                    limits: { ...policy.limits, max_notional_usdt_per_order: Number(e.target.value) || 0 },
                  })
                }
                className="input-field mt-1 font-mono"
              />
            </div>
            <div>
              <label htmlFor="max-daily" className="block text-xs text-t-3">
                每日最大名义值 (USDT)
              </label>
              <input
                id="max-daily"
                type="number"
                min={0}
                value={policy.limits.max_daily_notional_usdt}
                onChange={(e) =>
                  update({
                    limits: { ...policy.limits, max_daily_notional_usdt: Number(e.target.value) || 0 },
                  })
                }
                className="input-field mt-1 font-mono"
              />
            </div>
            <div>
              <label htmlFor="max-orders" className="block text-xs text-t-3">
                每日最大订单数
              </label>
              <input
                id="max-orders"
                type="number"
                min={0}
                value={policy.limits.max_orders_per_day}
                onChange={(e) =>
                  update({
                    limits: { ...policy.limits, max_orders_per_day: Number(e.target.value) || 0 },
                  })
                }
                className="input-field mt-1 font-mono"
              />
            </div>
          </div>
        </div>
      </section>

      {/* Approval */}
      <section>
        <h4 className="text-sm font-medium text-t-2">审批 (Approval)</h4>
        <div className="mt-3 space-y-3">
          <label className="flex items-center gap-2 text-sm text-t-2">
            <input
              type="checkbox"
              checked={policy.approval.required_for_trade}
              onChange={() =>
                update({
                  approval: { ...policy.approval, required_for_trade: !policy.approval.required_for_trade },
                })
              }
              className="h-4 w-4 rounded border-border bg-surface-2 text-brand focus:ring-brand"
            />
            交易操作需要人工审批
          </label>
          <label className="flex items-center gap-2 text-sm text-t-2">
            <input
              type="checkbox"
              checked={policy.approval.required_for_policy_change}
              onChange={() =>
                update({
                  approval: { ...policy.approval, required_for_policy_change: !policy.approval.required_for_policy_change },
                })
              }
              className="h-4 w-4 rounded border-border bg-surface-2 text-brand focus:ring-brand"
            />
            策略变更需要人工审批
          </label>
          <div>
            <label htmlFor="expires-after" className="block text-xs text-t-3">
              审批过期时间 (秒，30-3600)
            </label>
            <input
              id="expires-after"
              type="number"
              min={30}
              max={3600}
              value={policy.approval.expires_after_seconds}
              onChange={(e) =>
                update({
                  approval: {
                    ...policy.approval,
                    expires_after_seconds: Math.min(3600, Math.max(30, Number(e.target.value) || 300)),
                  },
                })
              }
              className="input-field mt-1 w-40 font-mono"
            />
          </div>
        </div>
      </section>

      {/* Blocked Actions */}
      <section>
        <h4 className="text-sm font-medium text-t-2">阻断动作 (Blocked Actions)</h4>
        <div className="mt-3 flex flex-wrap gap-2">
          {BLOCKED_ACTION_OPTIONS.map((action) => (
            <button
              key={action}
              type="button"
              onClick={() => toggleBlockedAction(action)}
              className={`rounded-xs border px-2.5 py-1 text-xs transition-colors ${
                policy.blocked_actions.includes(action)
                  ? 'border-status-red bg-status-red-bg text-status-red'
                  : 'border-border bg-surface-2 text-t-3 hover:border-border-hover'
              }`}
            >
              {action}
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
