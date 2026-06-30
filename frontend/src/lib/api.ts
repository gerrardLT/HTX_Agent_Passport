/**
 * 后端 API 客户端基础结构。
 *
 * 本文件为任务 1 的脚手架占位：仅提供一个最小化 fetch 包装，
 * 业务接口（登录、凭证、护照、操作、审批、审计）由后续任务（16-18）补齐。
 */

/**
 * 取得后端基址。
 *
 * 生产环境：始终返回空字符串（同域相对路径）。
 * 前端所有接口 path 已自带 /api 前缀（如 /api/passports），
 * 浏览器发出 /api/passports → 由 nginx 转发到后端。
 *
 * 本地开发：.env.local 设置 NEXT_PUBLIC_API_BASE_URL=http://localhost:8000，
 * 该值会被 Next.js 在构建/开发时内联，优先级高于此硬编码空串。
 */
export function getApiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL || '';
}

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly traceId?: string;
  readonly details?: unknown;

  constructor(args: {
    status: number;
    message: string;
    code?: string;
    traceId?: string;
    details?: unknown;
  }) {
    super(args.message);
    this.name = 'ApiError';
    this.status = args.status;
    this.code = args.code;
    this.traceId = args.traceId;
    this.details = args.details;
  }
}

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** 请求体；非 GET/HEAD 自动 JSON 序列化。 */
  body?: unknown;
  /** Bearer Token（演示账号 JWT），调用方可不传。 */
  token?: string;
}

/**
 * 通用 JSON 请求工具。后续任务可在其上构建 typed clients
 * （例如 ``api.passports.create(...)``）。
 */
export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { body, token, headers, ...rest } = options;

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(headers as Record<string, string> | undefined),
  };

  let payload: BodyInit | undefined;
  if (body !== undefined) {
    finalHeaders['Content-Type'] ??= 'application/json';
    payload = JSON.stringify(body);
  }
  if (token) {
    finalHeaders['Authorization'] = `Bearer ${token}`;
  }

  const url = `${getApiBaseUrl()}${path.startsWith('/') ? path : `/${path}`}`;
  const response = await fetch(url, {
    ...rest,
    headers: finalHeaders,
    body: payload,
  });

  // 204 No Content
  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  const data = text ? safeJsonParse(text) : null;

  if (!response.ok) {
    const errorEnvelope =
      data && typeof data === 'object' && 'error' in data
        ? (data as { error: { code?: string; message?: string; trace_id?: string; details?: unknown } }).error
        : undefined;
    throw new ApiError({
      status: response.status,
      message: errorEnvelope?.message ?? `Request failed with status ${response.status}`,
      code: errorEnvelope?.code,
      traceId: errorEnvelope?.trace_id,
      details: errorEnvelope?.details,
    });
  }

  return data as T;
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
