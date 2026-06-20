'use client';

import { useState } from 'react';

export interface CredentialFormData {
  label: string;
  access_key: string;
  secret_key: string;
}

interface CredentialFormProps {
  onSubmit: (data: CredentialFormData) => Promise<void>;
  isLoading?: boolean;
}

/**
 * 凭证添加表单组件。
 * 收集 label + access_key + secret_key，提交后 secret_key 不再显示。
 */
export function CredentialForm({ onSubmit, isLoading }: CredentialFormProps) {
  const [label, setLabel] = useState('');
  const [accessKey, setAccessKey] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    const trimmedLabel = label.trim();
    const trimmedAccessKey = accessKey.trim();
    const trimmedSecretKey = secretKey.trim();

    if (!trimmedLabel || !trimmedAccessKey || !trimmedSecretKey) {
      setError('所有字段均为必填');
      return;
    }

    if (trimmedLabel.length > 200) {
      setError('标签长度不能超过 200 个字符');
      return;
    }

    if (trimmedAccessKey.length > 512) {
      setError('Access Key 长度不能超过 512 个字符');
      return;
    }

    if (trimmedSecretKey.length > 512) {
      setError('Secret Key 长度不能超过 512 个字符');
      return;
    }

    try {
      await onSubmit({ label: trimmedLabel, access_key: trimmedAccessKey, secret_key: trimmedSecretKey });
      setLabel('');
      setAccessKey('');
      setSecretKey('');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '添加凭证失败');
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label htmlFor="cred-label" className="block text-sm text-t-2">
          标签
        </label>
        <input
          id="cred-label"
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="例如：我的 HTX 主账户"
          maxLength={200}
          className="input-field mt-1 w-full px-3 py-2 text-sm"
        />
      </div>

      <div>
        <label htmlFor="cred-access-key" className="block text-sm text-t-2">
          Access Key
        </label>
        <input
          id="cred-access-key"
          type="text"
          value={accessKey}
          onChange={(e) => setAccessKey(e.target.value)}
          placeholder="HTX API Access Key"
          maxLength={512}
          className="input-field mt-1 w-full px-3 py-2 text-sm font-mono"
        />
      </div>

      <div>
        <label htmlFor="cred-secret-key" className="block text-sm text-t-2">
          Secret Key
        </label>
        <input
          id="cred-secret-key"
          type="password"
          value={secretKey}
          onChange={(e) => setSecretKey(e.target.value)}
          placeholder="HTX API Secret Key（提交后不再显示）"
          maxLength={512}
          className="input-field mt-1 w-full px-3 py-2 text-sm font-mono"
        />
      </div>

      {error && (
        <p className="text-sm text-status-red">{error}</p>
      )}

      <button
        type="submit"
        disabled={isLoading}
        className="btn-outline rounded-md px-4 py-2 text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {isLoading ? '添加中...' : '添加凭证'}
      </button>
    </form>
  );
}
