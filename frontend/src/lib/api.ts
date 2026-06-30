/**
 * 后端 API 客户端基础结构。
 *
 * 本文件为任务 1 的脚手架占位：仅提供一个最小化 fetch 包装，
 * 业务接口（登录、凭证、护照、操作、审批、审计）由后续任务（16-18）补齐。
 */

/**
 * 取得后端基址。
 *
 * 策略（零 localhost 泄漏）：
 * - 浏览器端：始终返回 ''（空字符串 = 同域相对路径），由 nginx 转发 /api → backend。
 * - 服务端（SSR / standalone server.js）：返回 Docker 内网地址 http://backend:8000，
 *   确保服务端 fetch 能通（Next.js standalone 容器与 backend 容器同处 Docker 网络）。
 * - 显式 NEXT_PUBLIC_API_BASE_URL 配置时优先使用（本地开发 .env.local → http://localhost:8000）。
 */
export function getApiBaseUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_API_BASE_URL;
  if (explicit) return explicit;
  // 浏览器端：同域相对路径（前端 path 已带 /api 前缀）
  if (typeof window !== 'undefined') return '';
  // 服务端：Docker 内网后端地址（compose service name）
  return 'http://backend:8000';
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
