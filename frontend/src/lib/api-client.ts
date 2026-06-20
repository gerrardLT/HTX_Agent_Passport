/**
 * Typed API Client - 后端所有端点的类型安全封装。
 *
 * 基于 apiFetch 通用工具，为每个后端路由提供 typed 方法，
 * 消除组件内散落的 raw fetch 调用，统一错误处理。
 */

import { apiFetch } from './api';
import type {
  ApprovalSubmitRequest,
  ApprovalSubmitResponse,
  AuditEventListResponse,
  ConsistencyProofResponse,
  Credential,
  CredentialCreateRequest,
  CredentialListResponse,
  CredentialValidateResponse,
  DemoLoginResponse,
  InclusionProofResponse,
  Passport,
  PassportCreateRequest,
  PassportListResponse,
  PassportTemplatesResponse,
  STHResponse,
} from './types';

// ---------------------------------------------------------------------------
// Auth API
// ---------------------------------------------------------------------------
export const authApi = {
  /** POST /api/auth/demo-login */
  demoLogin: (wallet?: string) =>
    apiFetch<DemoLoginResponse>('/api/auth/demo-login', {
      method: 'POST',
      body: wallet ? { wallet } : undefined,
    }),
};

// ---------------------------------------------------------------------------
// Credential API
// ---------------------------------------------------------------------------
export const credentialApi = {
  /** GET /api/credentials */
  list: (token: string) =>
    apiFetch<CredentialListResponse>('/api/credentials', { token }),

  /** POST /api/credentials/htx */
  create: (token: string, data: CredentialCreateRequest) =>
    apiFetch<Credential>('/api/credentials/htx', {
      method: 'POST',
      token,
      body: data,
    }),

  /** POST /api/credentials/{id}/validate */
  validate: (token: string, id: string) =>
    apiFetch<CredentialValidateResponse>(`/api/credentials/${id}/validate`, {
      method: 'POST',
      token,
    }),

  /** DELETE /api/credentials/{id} */
  remove: (token: string, id: string) =>
    apiFetch<{ id: string; state: string }>(`/api/credentials/${id}`, {
      method: 'DELETE',
      token,
    }),
};

// ---------------------------------------------------------------------------
// Passport API
// ---------------------------------------------------------------------------
export const passportApi = {
  /** GET /api/passports */
  list: (token: string) =>
    apiFetch<PassportListResponse>('/api/passports', { token }),

  /** GET /api/passports/{id} */
  get: (token: string, id: string) =>
    apiFetch<Passport>(`/api/passports/${id}`, { token }),

  /** POST /api/passports */
  create: (token: string, data: PassportCreateRequest) =>
    apiFetch<Passport>('/api/passports', {
      method: 'POST',
      token,
      body: data,
    }),

  /** PATCH /api/passports/{id}/policy */
  updatePolicy: (token: string, id: string, policy: Record<string, unknown>) =>
    apiFetch<Passport>(`/api/passports/${id}/policy`, {
      method: 'PATCH',
      token,
      body: { policy },
    }),

  /** POST /api/passports/{id}/pause */
  pause: (token: string, id: string) =>
    apiFetch<Passport>(`/api/passports/${id}/pause`, {
      method: 'POST',
      token,
    }),

  /** POST /api/passports/{id}/resume */
  resume: (token: string, id: string) =>
    apiFetch<Passport>(`/api/passports/${id}/resume`, {
      method: 'POST',
      token,
    }),

  /** POST /api/passports/{id}/revoke */
  revoke: (token: string, id: string) =>
    apiFetch<Passport>(`/api/passports/${id}/revoke`, {
      method: 'POST',
      token,
    }),

  /** GET /api/passports/templates */
  templates: (token: string) =>
    apiFetch<PassportTemplatesResponse>('/api/passports/templates', { token }),
};

// ---------------------------------------------------------------------------
// Action API
// ---------------------------------------------------------------------------
export const actionApi = {
  /** GET /api/actions/{id} - 轮询 action 状态 */
  get: (token: string, id: string) =>
    apiFetch<import('./types').ActionDetail>(`/api/actions/${id}`, { token }),

  /** POST /api/passports/{passportId}/actions - 创建 action */
  create: (
    token: string,
    passportId: string,
    data: { task: string; execution_mode: string },
  ) =>
    apiFetch<{ action_id: string; state: string; trace_id: string }>(
      `/api/passports/${passportId}/actions`,
      { method: 'POST', token, body: data },
    ),

  /** GET /api/actions/{id}/audit - 获取 action 的审计事件 */
  audit: (token: string, id: string) =>
    apiFetch<AuditEventListResponse>(`/api/actions/${id}/audit`, { token }),
};

// ---------------------------------------------------------------------------
// Approval API
// ---------------------------------------------------------------------------
export const approvalApi = {
  /** POST /api/actions/{actionId}/approve */
  submit: (token: string, actionId: string, data: ApprovalSubmitRequest) =>
    apiFetch<ApprovalSubmitResponse>(`/api/actions/${actionId}/approve`, {
      method: 'POST',
      token,
      body: data,
    }),
};

// ---------------------------------------------------------------------------
// Scenario API (Demo)
// ---------------------------------------------------------------------------
export const scenarioApi = {
  /** POST /api/scenarios/{name} - 运行预设场景 */
  run: (token: string, name: string, passportId: string) =>
    apiFetch<{ action_id: string; scenario: string }>(`/api/scenarios/${name}`, {
      method: 'POST',
      token,
      body: { passport_id: passportId },
    }),
};

// ---------------------------------------------------------------------------
// Audit API
// ---------------------------------------------------------------------------
export const auditApi = {
  /** GET /api/audit/events - 审计事件列表（支持过滤） */
  listEvents: (
    token: string,
    params?: { passport_id?: string; user_id?: string; limit?: number; offset?: number },
  ) => {
    const searchParams = new URLSearchParams();
    if (params?.passport_id) searchParams.set('passport_id', params.passport_id);
    if (params?.user_id) searchParams.set('user_id', params.user_id);
    if (params?.limit) searchParams.set('limit', String(params.limit));
    if (params?.offset) searchParams.set('offset', String(params.offset));
    const qs = searchParams.toString();
    return apiFetch<AuditEventListResponse>(
      `/api/audit/events${qs ? `?${qs}` : ''}`,
      { token },
    );
  },

  /** GET /api/audit/sth/latest - 最新 STH */
  latestSTH: (token: string, passportId?: string) => {
    const qs = passportId ? `?passport_id=${encodeURIComponent(passportId)}` : '';
    return apiFetch<STHResponse>(`/api/audit/sth/latest${qs}`, { token });
  },

  /** POST /api/audit/sth/issue - 手动触发 STH 签发 */
  issueSTH: (token: string, passportId?: string) => {
    const qs = passportId ? `?passport_id=${encodeURIComponent(passportId)}` : '';
    return apiFetch<STHResponse>(`/api/audit/sth/issue${qs}`, {
      method: 'POST',
      token,
    });
  },

  /** GET /api/audit/events/{eventId}/inclusion - inclusion proof */
  inclusionProof: (token: string, eventId: string) =>
    apiFetch<InclusionProofResponse>(
      `/api/audit/events/${eventId}/inclusion`,
      { token },
    ),

  /** GET /api/audit/sth/consistency - consistency proof */
  consistencyProof: (token: string, fromSize: number, toSize: number) =>
    apiFetch<ConsistencyProofResponse>(
      `/api/audit/sth/consistency?from_size=${fromSize}&to_size=${toSize}`,
      { token },
    ),
};
