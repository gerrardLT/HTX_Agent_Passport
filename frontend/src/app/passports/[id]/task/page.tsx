'use client';

import { useParams } from 'next/navigation';
import { TaskComposer } from '@/components/TaskComposer';
import { useAuth } from '@/hooks/useAuth';

/**
 * 任务编排器页面。
 * 路由: /passports/[id]/task
 * 用户在此页面输入自然语言任务并选择执行模式。
 */
export default function TaskPage() {
  const params = useParams();
  const passportId = params.id as string;
  const { isAuthenticated, isInitialized } = useAuth();

  if (!isInitialized) {
    return null;
  }

  if (!isAuthenticated) {
    return (
      <main className="mx-auto max-w-2xl px-4 py-12">
        <div className="card-surface rounded-lg p-8 text-center">
          <p className="text-sm text-t-2">请先登录后再使用任务编排器</p>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-12">
      <div className="mb-8">
        <h1 className="text-xl font-semibold text-t-1">任务编排器</h1>
        <p className="mt-1 text-sm text-t-2">
          用自然语言描述你的交易意图，AI 代理将在策略边界内为你规划和执行
        </p>
      </div>

      <div className="card-surface rounded-lg p-6">
        <TaskComposer passportId={passportId} />
      </div>
    </main>
  );
}
