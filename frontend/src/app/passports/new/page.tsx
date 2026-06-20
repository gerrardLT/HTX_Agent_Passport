'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/hooks/useAuth';
import { credentialApi, passportApi } from '@/lib/api-client';
import type { Credential } from '@/lib/types';
import { PolicyEditor } from '@/components/PolicyEditor';
import type { PolicyDSLv0 } from '@/components/PolicyEditor';

/** 模板定义 */
const TEMPLATES: Record<string, { label: string; description: string; agent_type: string; policy: PolicyDSLv0 }> = {
  readonly_researcher: {
    label: '只读研究员',
    description: '仅允许读取行情和账户信息，不允许任何交易操作',
    agent_type: 'readonly_researcher',
    policy: {
      version: '0.1',
      capabilities: {
        read_market: true,
        read_account: true,
        place_order: false,
        cancel_order: false,
        withdraw: false,
      },
      limits: {
        allowed_symbols: ['btcusdt', 'ethusdt'],
        max_notional_usdt_per_order: 0,
        max_daily_notional_usdt: 0,
        max_orders_per_day: 0,
      },
      approval: {
        required_for_trade: true,
        required_for_policy_change: true,
        expires_after_seconds: 300,
      },
      blocked_actions: ['withdraw', 'borrow', 'margin', 'transfer_out', 'unknown_tool_call'],
    },
  },
  small_spot_executor: {
    label: '小额现货执行者',
    description: '允许小额现货交易，单笔最大 20 USDT，每日最大 100 USDT',
    agent_type: 'small_spot_executor',
    policy: {
      version: '0.1',
      capabilities: {
        read_market: true,
        read_account: true,
        place_order: true,
        cancel_order: true,
        withdraw: false,
      },
      limits: {
        allowed_symbols: ['btcusdt', 'ethusdt'],
        max_notional_usdt_per_order: 20,
        max_daily_notional_usdt: 100,
        max_orders_per_day: 10,
      },
      approval: {
        required_for_trade: true,
        required_for_policy_change: true,
        expires_after_seconds: 300,
      },
      blocked_actions: ['withdraw', 'borrow', 'margin', 'transfer_out', 'unknown_tool_call'],
    },
  },
  dao_treasury_guarded: {
    label: 'DAO 金库守卫',
    description: '严格限制的交易策略，需要审批，适合 DAO 资金管理',
    agent_type: 'dao_treasury_guarded',
    policy: {
      version: '0.1',
      capabilities: {
        read_market: true,
        read_account: true,
        place_order: true,
        cancel_order: true,
        withdraw: false,
      },
      limits: {
        allowed_symbols: ['btcusdt', 'ethusdt', 'solusdt'],
        max_notional_usdt_per_order: 50,
        max_daily_notional_usdt: 200,
        max_orders_per_day: 5,
      },
      approval: {
        required_for_trade: true,
        required_for_policy_change: true,
        expires_after_seconds: 600,
      },
      blocked_actions: ['withdraw', 'borrow', 'margin', 'transfer_out', 'unknown_tool_call'],
    },
  },
};

const STEPS = ['选择模板', '连接密钥', '编辑策略', '审查', '激活'];

/**
 * 护照创建向导页面。
 * 多步骤：选择模板 → 连接密钥 → 编辑策略 → 审查 → 激活
 */
export default function NewPassportPage() {
  const router = useRouter();
  const { token, isAuthenticated, isInitialized } = useAuth();

  const [step, setStep] = useState(0);
  const [selectedTemplate, setSelectedTemplate] = useState<string | null>(null);
  const [passportName, setPassportName] = useState('');
  const [selectedCredentialId, setSelectedCredentialId] = useState<string | null>(null);
  const [policy, setPolicy] = useState<PolicyDSLv0 | null>(null);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isInitialized && !isAuthenticated) {
      router.push('/');
    }
  }, [isInitialized, isAuthenticated, router]);

  const fetchCredentials = useCallback(async () => {
    if (!token) return;
    try {
      const res = await credentialApi.list(token);
      setCredentials(res.credentials.filter((c) => c.state === 'READ_ONLY' || c.state === 'TRADE_ENABLED'));
    } catch {
      // 静默处理
    }
  }, [token]);

  useEffect(() => {
    fetchCredentials();
  }, [fetchCredentials]);

  const handleSelectTemplate = (key: string) => {
    setSelectedTemplate(key);
    const template = TEMPLATES[key];
    setPolicy({ ...template.policy });
    setPassportName(`${template.label} - ${new Date().toLocaleDateString('zh-CN')}`);
  };

  const handleSubmit = async () => {
    if (!token || !policy || !selectedTemplate) return;
    setIsSubmitting(true);
    setError(null);

    try {
      const template = TEMPLATES[selectedTemplate];
      await passportApi.create(token, {
        name: passportName,
        agent_type: template.agent_type,
        api_credential_id: selectedCredentialId ?? undefined,
        policy,
      });
      router.push('/passports');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '创建护照失败');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (!isAuthenticated) return null;

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <h1 className="page-heading">创建代理护照</h1>

      {/* Step indicator */}
      <div className="mt-6 flex items-center gap-1">
        {STEPS.map((label, i) => (
          <div key={label} className="flex items-center">
            <div
              className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-medium ${
                i === step
                  ? 'bg-brand text-white'
                  : i < step
                    ? 'bg-status-green text-white'
                    : 'bg-surface-2 text-t-3'
              }`}
            >
              {i < step ? '✓' : i + 1}
            </div>
            <span
              className={`ml-1.5 text-xs ${
                i === step ? 'text-t-1' : 'text-t-4'
              }`}
            >
              {label}
            </span>
            {i < STEPS.length - 1 && (
              <div className="mx-2 h-px w-6 bg-border" />
            )}
          </div>
        ))}
      </div>

      {/* Step content */}
      <div className="mt-8">
        {/* Step 1: 选择模板 */}
        {step === 0 && (
          <div className="space-y-4">
            <p className="text-sm text-t-3">选择一个预设模板作为起点：</p>
            <div className="grid gap-4 sm:grid-cols-3">
              {Object.entries(TEMPLATES).map(([key, tmpl]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => handleSelectTemplate(key)}
                  className={`card-surface p-4 text-left transition-all ${
                    selectedTemplate === key
                      ? 'border-brand bg-brand-bg'
                      : 'hover:border-border-hover'
                  }`}
                >
                  <h3 className="text-sm font-medium text-t-1">{tmpl.label}</h3>
                  <p className="mt-1 text-xs text-t-3">{tmpl.description}</p>
                </button>
              ))}
            </div>
            <div className="mt-4">
              <label htmlFor="passport-name" className="block text-sm text-t-3">
                护照名称
              </label>
              <input
                id="passport-name"
                type="text"
                value={passportName}
                onChange={(e) => setPassportName(e.target.value)}
                placeholder="为您的护照命名"
                maxLength={200}
                className="input-field mt-1"
              />
            </div>
            <button
              onClick={() => setStep(1)}
              disabled={!selectedTemplate || !passportName.trim()}
              className="btn-primary mt-4 disabled:cursor-not-allowed"
            >
              下一步
            </button>
          </div>
        )}

        {/* Step 2: 连接密钥 */}
        {step === 1 && (
          <div className="space-y-4">
            <p className="text-sm text-t-3">
              选择一个已验证的 API 凭证，或跳过（护照将以 DRAFT 模式创建）：
            </p>
            {credentials.length === 0 ? (
              <div className="card-surface p-6 text-center">
                <p className="text-sm text-t-3">暂无可用凭证</p>
                <p className="mt-1 text-xs text-t-4">
                  您可以跳过此步骤，护照将以 DRAFT 模式创建
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {credentials.map((cred) => (
                  <button
                    key={cred.id}
                    type="button"
                    onClick={() => setSelectedCredentialId(cred.id)}
                    className={`card-surface w-full p-4 text-left transition-all ${
                      selectedCredentialId === cred.id
                        ? 'border-brand bg-brand-bg'
                        : 'hover:border-border-hover'
                    }`}
                  >
                    <span className="text-sm text-t-1">{cred.label}</span>
                    <span className="ml-2 text-xs text-t-3">({cred.state})</span>
                  </button>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => setStep(0)}
                className="btn-outline"
              >
                上一步
              </button>
              <button
                onClick={() => {
                  if (!selectedCredentialId) setSelectedCredentialId(null);
                  setStep(2);
                }}
                className="btn-primary"
              >
                {selectedCredentialId ? '下一步' : '跳过'}
              </button>
            </div>
          </div>
        )}

        {/* Step 3: 编辑策略 */}
        {step === 2 && policy && (
          <div className="space-y-4">
            <p className="text-sm text-t-3">
              编辑策略参数（可修改模板默认值）：
            </p>
            <div className="card-surface p-6">
              <PolicyEditor value={policy} onChange={setPolicy} />
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setStep(1)}
                className="btn-outline"
              >
                上一步
              </button>
              <button
                onClick={() => setStep(3)}
                className="btn-primary"
              >
                下一步
              </button>
            </div>
          </div>
        )}

        {/* Step 4: 审查 */}
        {step === 3 && policy && (
          <div className="space-y-4">
            <p className="text-sm text-t-3">审查护照配置：</p>
            <div className="card-surface p-6 space-y-4">
              <div>
                <p className="text-xs text-t-4">护照名称</p>
                <p className="mt-1 text-sm text-t-1">{passportName}</p>
              </div>
              <div>
                <p className="text-xs text-t-4">代理类型</p>
                <p className="mt-1 text-sm text-t-1">
                  {selectedTemplate ? TEMPLATES[selectedTemplate].agent_type : '-'}
                </p>
              </div>
              <div>
                <p className="text-xs text-t-4">关联凭证</p>
                <p className="mt-1 text-sm text-t-1">
                  {selectedCredentialId
                    ? credentials.find((c) => c.id === selectedCredentialId)?.label ?? selectedCredentialId
                    : '无（DRAFT 模式）'}
                </p>
              </div>
              <div>
                <p className="text-xs text-t-4">策略 JSON</p>
                <pre className="mt-1 max-h-64 overflow-auto rounded-xs bg-surface-2 p-3 font-mono text-xs text-t-2">
                  {JSON.stringify(policy, null, 2)}
                </pre>
              </div>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setStep(2)}
                className="btn-outline"
              >
                上一步
              </button>
              <button
                onClick={() => setStep(4)}
                className="btn-primary"
              >
                确认并激活
              </button>
            </div>
          </div>
        )}

        {/* Step 5: 激活 */}
        {step === 4 && (
          <div className="space-y-4">
            <p className="text-sm text-t-3">
              确认创建护照？提交后护照将立即生效。
            </p>
            {error && (
              <p className="text-sm text-status-red">{error}</p>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => setStep(3)}
                disabled={isSubmitting}
                className="btn-outline"
              >
                返回修改
              </button>
              <button
                onClick={handleSubmit}
                disabled={isSubmitting}
                className="rounded-xs bg-status-green px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-status-green/80 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isSubmitting ? '创建中...' : '创建护照'}
              </button>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
