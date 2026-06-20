'use client';

import { useState, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { useAuth } from '@/hooks/useAuth';
import { passportApi } from '@/lib/api-client';
import { PassportCard } from '@/components/PassportCard';
import type { PassportSummary } from '@/components/PassportCard';

export default function PassportsPage() {
  const router = useRouter();
  const { token, isAuthenticated, isInitialized } = useAuth();
  const [passports, setPassports] = useState<PassportSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (isInitialized && !isAuthenticated) router.push('/');
  }, [isInitialized, isAuthenticated, router]);

  const fetchPassports = useCallback(async () => {
    if (!token) return;
    try {
      const res = await passportApi.list(token);
      setPassports(res.passports);
    } catch { /* silent */ } finally { setIsLoading(false); }
  }, [token]);

  useEffect(() => { fetchPassports(); }, [fetchPassports]);

  if (!isInitialized || !isAuthenticated) return null;

  return (
    <main className="mx-auto max-w-content px-12 py-10">
      <div className="relative mb-10">
        <div className="pointer-events-none absolute -left-12 -top-10 right-0 h-[200px] bg-[radial-gradient(ellipse_60%_50%_at_30%_0%,rgba(94,106,210,.08),transparent)]" />
        <div className="flex items-center justify-between">
          <h1 className="page-heading">代理护照</h1>
          <Link href="/passports/new" className="btn-primary">
            创建护照
          </Link>
        </div>
      </div>

      {isLoading ? (
        <div className="py-12 text-center text-sm text-t-3">加载中...</div>
      ) : passports.length === 0 ? (
        <div className="card-surface p-8 text-center">
          <p className="text-sm text-t-3">暂无护照</p>
          <p className="mt-1 text-xs text-t-4">
            点击&ldquo;创建护照&rdquo;开始为您的 AI 代理签发能力凭证
          </p>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {passports.map((passport) => (
            <PassportCard key={passport.id} passport={passport} />
          ))}
        </div>
      )}
    </main>
  );
}
