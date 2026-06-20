/**
 * 前端共享类型定义。
 *
 * 与 backend 的 Pydantic schema 镜像（手工维护，避免引入 OpenAPI 生成器）。
 */

// ---------------------------------------------------------------------------
// 基础枚举类型
// ---------------------------------------------------------------------------

/** 环境徽章对应后端运行模式。 */
export type EnvironmentMode = 'DEMO' | 'SIMULATION' | 'REAL_READ' | 'REAL_TRADE';

/** Passport 状态机（与 backend `agent_passports.state` 一致）。 */
export type PassportState =
  | 'DRAFT'
  | 'ACTIVE'
  | 'PAUSED'
  | 'REVOKED'
  | 'EXPIRED'
  | 'DELETED';

/** Credential 状态机（与 backend `api_credentials.state` 一致）。 */
export type CredentialState =
  | 'CREATED'
  | 'VALIDATING'
  | 'READ_ONLY'
  | 'TRADE_ENABLED'
  | 'INVALID'
  | 'REVOKED'
  | 'DELETED';

/** Action 状态机（与 backend `agent_actions.state` 一致）。 */
export type ActionState =
  | 'REQUESTED'
  | 'PLANNING'
  | 'PLAN_VALIDATED'
  | 'PLAN_INVALID'
  | 'RISK_CHECKING'
  | 'APPROVAL_REQUIRED'
  | 'AUTO_APPROVED'
  | 'APPROVED'
  | 'AUTO_REJECTED'
  | 'REJECTED_BY_USER'
  | 'EXECUTING'
  | 'EXECUTED'
  | 'EXECUTION_FAILED'
  | 'EXPIRED'
  | 'FAILED'
  | 'CANCELLED';

/** Policy Engine 三态裁决。 */
export type PolicyVerdict = 'ALLOW' | 'REQUIRE_APPROVAL' | 'REJECT';

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/** Demo 登录响应。 */
export interface DemoLoginResponse {
  token: string;
  user: {
    id: string;
    wallet: string;
  };
}

// ---------------------------------------------------------------------------
// Credential（与 backend schemas/credential.py 镜像）
// ---------------------------------------------------------------------------

export interface CredentialPermissions {
  read: boolean;
  trade: boolean;
  withdraw: boolean;
}

export interface Credential {
  id: string;
  provider: string;
  label: string;
  state: CredentialState;
  permissions: CredentialPermissions;
  created_at: string;
  last_validated_at: string | null;
  deleted_at: string | null;
}

export interface CredentialListResponse {
  credentials: Credential[];
}

export interface CredentialValidateResponse {
  id: string;
  state: CredentialState;
  permissions: CredentialPermissions;
}

export interface CredentialCreateRequest {
  label: string;
  access_key: string;
  secret_key: string;
}

// ---------------------------------------------------------------------------
// Passport（与 backend schemas/passport.py 镜像）
// ---------------------------------------------------------------------------

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
    expires_after_seconds: number;
  };
  blocked_actions: string[];
}

export interface Passport {
  id: string;
  name: string;
  agent_type: string;
  state: PassportState;
  version: number;
  policy: PolicyDSLv0;
  reputation_score: number;
  api_credential_id: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
}

export interface PassportListResponse {
  passports: Passport[];
}

export interface PassportCreateRequest {
  name: string;
  agent_type: string;
  api_credential_id?: string;
  policy?: PolicyDSLv0;
  template_name?: string;
  overrides?: Record<string, unknown>;
}

export interface TemplateInfo {
  name: string;
  description: string;
  policy: PolicyDSLv0;
}

export interface PassportTemplatesResponse {
  templates: TemplateInfo[];
}

// ---------------------------------------------------------------------------
// Action / Approval（与 backend schemas/approval.py + action hook 镜像）
// ---------------------------------------------------------------------------

export interface ActionPlanStep {
  type: string;
  symbol?: string;
  side?: string;
  order_type?: string;
  amount?: number;
  amount_unit?: string;
  max_notional_usdt?: number;
  rationale?: string;
}

export interface ActionPlanSummary {
  intent_summary?: string;
  actions?: ActionPlanStep[];
  risk_notes?: string[];
}

export interface ActionDetail {
  id: string;
  passport_id: string;
  trace_id: string;
  natural_language_request: string;
  state: ActionState;
  execution_mode: string;
  risk_verdict: PolicyVerdict | null;
  risk_score: number | null;
  reason_codes: string[] | null;
  normalized_action_json: ActionPlanSummary | null;
  created_at: string;
  updated_at: string;
}

export interface ApprovalSubmitRequest {
  approved: boolean;
  typed_confirmation: string;
  signature?: string;
}

export interface ApprovalSubmitResponse {
  action_id: string;
  state: ActionState;
}

// ---------------------------------------------------------------------------
// Audit（与 backend schemas/audit.py 镜像）
// ---------------------------------------------------------------------------

export interface AuditEvent {
  id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  event_json: Record<string, unknown>;
  event_hash: string;
  previous_event_hash: string | null;
  trace_id: string | null;
  created_at: string;
}

export interface AuditEventListResponse {
  events: AuditEvent[];
  count: number;
  /** Action audit endpoint returns trace_id */
  trace_id?: string | null;
  /** Action audit endpoint returns action_id */
  action_id?: string;
}

export interface STHResponse {
  id: string;
  user_id: string;
  passport_id: string | null;
  tree_size: number;
  root_hash: string;
  signature: string;
  signed_at: string;
}

export interface InclusionProofResponse {
  event_id: string;
  leaf_index: number;
  leaf_hash: string;
  proof: string[];
  tree_size: number;
  root_hash: string;
}

export interface ConsistencyProofResponse {
  from_size: number;
  to_size: number;
  proof: string[];
}

// ---------------------------------------------------------------------------
// Error
// ---------------------------------------------------------------------------

/** API 错误响应包络。 */
export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    status: number;
    trace_id?: string;
    details?: Record<string, unknown>;
  };
}
