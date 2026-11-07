// Typed client for the Argus API. Same-origin: the session cookie is sent by the
// browser (Vite proxies /api and /auth in development; the API serves /app in prod).

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      ...(init.body ? { "content-type": "application/json" } : {}),
      ...init.headers,
    },
  })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  const body = text ? safeJson(text) : null
  if (!res.ok) {
    const detail =
      (body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : null) ?? `${res.status} ${res.statusText}`
    throw new ApiError(res.status, detail)
  }
  return body as T
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

const json = (data: unknown) => JSON.stringify(data)

// --- types (mirror src/argus/api/app.py) ---------------------------------------------

export type Role = "viewer" | "member" | "admin" | "owner"
export type RunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "superseded"

export interface Me {
  actor: { type: "user" | "api_key"; id: string; github_login: string | null }
  tenant: { login: string; role: Role }
  tenants: { login: string; role: Role }[]
}

export interface Citation {
  citation: string
  path: string
  lines: [number, number]
  url: string
}

export interface RunResult {
  review?: string
  answer?: string
  comment_id?: string
  citations?: Citation[]
  unverified_citations?: string[]
  index_commit?: string | null
  model?: string
  prompt_version?: string
  steps?: number
  steps_replayed?: number
  cache_hit?: boolean
  superseded_by?: string
  approval?: string
  guardrails?: {
    injection_flagged?: boolean
    rules?: string[]
    sources?: string[]
    output_redactions?: Record<string, number>
    echoes_removed?: number
  }
  [key: string]: unknown
}

export interface Run {
  id: string
  agent_type: "pr_review" | "qa" | string
  repo: string | null
  pr_number: number | null
  question: string | null
  status: RunStatus
  result: RunResult | null
  error: string | null
  steps: number
  tokens: { input: number; output: number }
  created_at: string
  finished_at: string | null
}

export interface RunPage {
  runs: Run[]
  total: number
  limit: number
  offset: number
}

export interface Overview {
  days: number
  totals: {
    runs: number
    succeeded: number
    failed: number
    active: number
    waiting: number
    superseded: number
    reviews: number
    questions: number
    flagged: number
    tokens: number
    p50_s: number | null
    p95_s: number | null
  }
  success_rate: number | null
  pending_approvals: number
  daily: { day: string; succeeded: number; failed: number; other: number }[]
  top_repos: { full_name: string; runs: number; succeeded: number }[]
}

export interface Repo {
  id: string
  full_name: string
  private: boolean
  indexed_commit: string | null
  chunks: number
  last_full_index: string | null
}

export interface Usage {
  budget: { daily_tokens: number; alert_pct: number } | null
  daily: { day: string; used: number; reserved: number; cost_usd: number }[]
  models: {
    model: string
    calls: number
    input_tokens: number
    output_tokens: number
    cached_tokens: number
    cost_usd: number
    errors: number
  }[]
}

export interface Approval {
  id: string
  run_id: string
  tool: string
  repo: string | null
  status: "pending" | "approved" | "rejected"
  preview: { repo?: string; pr?: number; body?: string }
  requested_at: string
  decided_by: string | null
}

export interface ApiKey {
  id: string
  name: string
  prefix: string
  role: Role
  created_at: string
  last_used_at: string | null
  revoked: boolean
}

export interface Member {
  user_id: string
  github_login: string
  name: string | null
  role: Role
  source: "github" | "manual"
  last_login_at: string | null
}

export interface AuditEntry {
  id: number
  at: string
  actor: string
  action: string
  target: string | null
  decision: "allow" | "deny" | "info"
  details: Record<string, unknown> | null
  hash: string
}

export interface OnlineEvals {
  judge_by_prompt: { prompt_version: string; samples: number; mean_score: number }[]
  feedback: { up: number; down: number }
}

export interface AuthConfig {
  github: boolean
  dev_login: boolean
}

// --- endpoints ---------------------------------------------------------------------

export interface RunFilters {
  limit?: number
  offset?: number
  status?: string
  agent?: string
  q?: string
}

function qs(params: Record<string, string | number | undefined>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ""
}

export const api = {
  authConfig: () => request<AuthConfig>("/auth/config"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<Me>("/api/me"),
  overview: (days: number) => request<Overview>(`/api/overview${qs({ days })}`),
  runs: (f: RunFilters) => request<RunPage>(`/api/runs${qs({ ...f })}`),
  run: (id: string) => request<Run>(`/api/runs/${id}`),
  feedback: (id: string, rating: "up" | "down") =>
    request<{ rating: string }>(`/api/runs/${id}/feedback`, {
      method: "POST",
      body: json({ rating }),
    }),
  repos: () => request<{ repos: Repo[] }>("/api/repos"),
  ask: (repo: string, question: string) =>
    request<{ run_id: string; created: boolean }>("/api/qa", {
      method: "POST",
      body: json({ repo, question }),
    }),
  approvals: (status: string) =>
    request<{ approvals: Approval[] }>(`/api/approvals${qs({ status })}`),
  decide: (id: string, decision: "approve" | "reject", reason?: string) =>
    request<{ run_id: string }>(`/api/approvals/${id}`, {
      method: "POST",
      body: json({ decision, reason: reason || null }),
    }),
  usage: (days: number) => request<Usage>(`/api/usage${qs({ days })}`),
  setBudget: (daily_tokens: number, alert_pct: number) =>
    request<{ daily_tokens: number }>("/api/budget", {
      method: "PUT",
      body: json({ daily_tokens, alert_pct }),
    }),
  onlineEvals: (days: number) => request<OnlineEvals>(`/api/evals/online${qs({ days })}`),
  keys: () => request<{ keys: ApiKey[] }>("/api/keys"),
  createKey: (name: string, role: Role, repos: string[] | null) =>
    request<{ id: string; token: string; prefix: string }>("/api/keys", {
      method: "POST",
      body: json({ name, role, repos }),
    }),
  revokeKey: (id: string) => request<void>(`/api/keys/${id}`, { method: "DELETE" }),
  members: () => request<{ members: Member[] }>("/api/members"),
  setRole: (userId: string, role: Role) =>
    request<{ role: Role }>(`/api/members/${userId}`, { method: "PUT", body: json({ role }) }),
  audit: (limit: number) =>
    request<{ chain_intact: boolean; first_broken_id: number | null; entries: AuditEntry[] }>(
      `/api/audit${qs({ limit })}`,
    ),
  settings: () =>
    request<{ require_approval_for: string[]; write_tools: string[] }>("/api/settings"),
  setSettings: (require_approval_for: string[]) =>
    request<{ require_approval_for: string[] }>("/api/settings", {
      method: "PUT",
      body: json({ require_approval_for }),
    }),
}

export function setTenantCookie(login: string) {
  document.cookie = `argus_tenant=${encodeURIComponent(login)}; path=/; samesite=lax`
}
