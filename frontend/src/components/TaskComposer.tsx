'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { actionApi } from '@/lib/api-client';
import { useAuth } from '@/hooks/useAuth';

type ExecutionMode = 'simulation' | 'real_read' | 'real_trade';

interface TaskComposerProps {
  passportId: string;
}

/**
 * 任务编排器组件。
 * - 自然语言任务输入框
 * - execution_mode 下拉选择（simulation / real_read / real_trade）
 * - 提交后 POST /api/passports/{passport_id}/actions → 跳转到 /actions/{action_id}
 */
export function TaskComposer({ passportId }: TaskComposerProps) {
  const router = useRouter();
  const { token } = useAuth();
  const [task, setTask] = useState('');
  const [mode, setMode] = useState<ExecutionMode>('simulation');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!task.trim() || !token) return;

    setIsSubmitting(true);
    setError(null);

    try {
      const res = await actionApi.create(token, passportId, {
        task: task.trim(),
        execution_mode: mode,
      });
      router.push(`/actions/${res.action_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交任务失败');
      setIsSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* 自然语言任务输入 */}
      <div>
        <label htmlFor="task-input" className="block text-sm font-medium text-t-2">
          任务描述
        </label>
        <textarea
          id="task-input"
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="用自然语言描述你的交易意图，例如：查看 BTC/USDT 行情并准备 10 USDT 限价买入"
          rows={4}
          className="input-field mt-1 w-full px-4 py-3 text-sm"
          disabled={isSubmitting}
        />
      </div>

      {/* execution_mode 选择 */}
      <div>
        <label htmlFor="exec-mode" className="block text-sm font-medium text-t-2">
          执行模式
        </label>
        <select
          id="exec-mode"
          value={mode}
          onChange={(e) => setMode(e.target.value as ExecutionMode)}
          className="input-field mt-1 w-full px-4 py-2.5 text-sm"
          disabled={isSubmitting}
        >
          <option value="simulation">模拟执行 (Simulation)</option>
          <option value="real_read">真实只读 (Real Read)</option>
          <option value="real_trade">真实交易 (Real Trade)</option>
        </select>
        <p className="mt-1 text-xs text-t-3">
          {mode === 'simulation' && '使用模拟数据执行，不调用真实 HTX API'}
          {mode === 'real_read' && '调用真实 HTX 行情 API，写操作仍走模拟'}
          {mode === 'real_trade' && '⚠️ 真实交易模式，需环境变量启用'}
        </p>
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="rounded-lg border border-status-red/40 bg-status-red-bg px-4 py-3 text-sm text-status-red">
          {error}
        </div>
      )}

      {/* 提交按钮 */}
      <button
        type="submit"
        disabled={isSubmitting || !task.trim()}
        className="btn-outline w-full rounded-lg px-4 py-2.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
      >
        {isSubmitting ? (
          <span className="inline-flex items-center gap-2">
            <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
            提交中...
          </span>
        ) : (
          '提交任务'
        )}
      </button>
    </form>
  );
}
