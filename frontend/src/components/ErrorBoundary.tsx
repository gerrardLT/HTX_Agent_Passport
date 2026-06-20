'use client';

import React from 'react';

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

/**
 * 全局 Error Boundary - 捕获子组件渲染错误，展示友好的降级 UI。
 * 防止整个页面白屏，提供"重试"按钮恢复。
 */
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  ErrorBoundaryState
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[ErrorBoundary] Caught:', error, info.componentStack);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-[50vh] flex-col items-center justify-center gap-4 p-8">
          <div className="text-4xl">⚠️</div>
          <h2 className="text-xl font-semibold text-t-1">页面出了点问题</h2>
          <p className="max-w-md text-center text-sm text-t-2">
            {this.state.error?.message || '发生了未知错误'}
          </p>
          <button
            onClick={this.handleRetry}
            className="btn-outline mt-2 rounded-md px-4 py-2 text-sm font-medium"
          >
            重试
          </button>
          <a
            href="/"
            className="text-xs text-t-3 underline hover:text-t-2"
          >
            返回首页
          </a>
        </div>
      );
    }

    return this.props.children;
  }
}
