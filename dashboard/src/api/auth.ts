// The controller's accounts, sessions and API tokens as the page reads and writes them (SRV-875):
// /v1/auth/* (proxy/authapi.py, proxy/tokenapi.py), the `api` settings scope's exposure switches
// through the settings interface's plan and apply, and the controller's own health findings. The
// calls go through `send`, so a test (or the mock) answers them without HTTP. A refusal throws
// AuthError with the controller's code, its words and its facts (`permission`: the right that is
// missing).

import { apiBase } from './client';

export type Role = 'viewer' | 'operator' | 'admin' | 'owner';
export const ROLES: Role[] = ['viewer', 'operator', 'admin', 'owner'];

export interface Me {
  authenticated: boolean;
  account: { id: string; username: string; display_name: string | null; role: Role; must_change: boolean } | null;
  principal: { kind: string; name: string } | null;
  permissions: string[];
  setup_required: boolean;
  local_mode: boolean;
  expires_at: string | null;
}

export interface User {
  id: string;
  username: string;
  display_name: string | null;
  role: Role;
  enabled: boolean;
  provider: string;
  has_password: boolean;
  must_change: boolean;
  /** milliseconds */
  created_at: number;
  last_login_at: number | null;
}
/** an account with the one-time code that stands for its password, in this answer only */
export interface UserCode {
  user: User;
  code: string;
  code_valid_s: number;
}

export interface Session {
  id: string;
  account_id: string;
  username: string;
  created_at: string | null;
  last_seen_at: string | null;
  idle_until: string | null;
  expires_at: string | null;
  address: string | null;
  agent: string | null;
  current: boolean;
}

export const LIMIT_KEYS = ['requests_per_minute', 'tokens_per_minute', 'concurrent', 'cost_per_day', 'cost_per_month'] as const;
export type LimitKey = (typeof LIMIT_KEYS)[number];
export type Limits = Partial<Record<LimitKey, number>>;

export interface Token {
  id: string;
  kind: 'user' | 'service';
  label: string;
  owner: string | null;
  owner_id: string | null;
  /** all the page ever sees of the token again: its id and last 4 characters */
  hint: string | null;
  scopes: string[];
  models: string[];
  limits: Limits;
  memory_user: string | null;
  source: string;
  needs_review: boolean;
  state: 'active' | 'expired' | 'revoked';
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}
/** a token's row with the token itself, in the answer that made it only */
export type TokenOnce = Token & { token: string };

export interface NewToken {
  label: string;
  scopes: string[];
  models?: string[];
  days?: number | 'never';
  limits?: Limits;
}

export interface AuditRow {
  at: string | null;
  who: string;
  action: string;
  object: string;
  object_id: string | null;
  fields: string[];
  detail: Record<string, unknown>;
}

export type Protocol = 'openai' | 'anthropic';
export type Exposure = 'this-machine' | 'network';
export interface ExposureView {
  /** null: this controller does not say (an older one) */
  values: Record<Protocol, Exposure | null>;
  listen: string | null;
  /** an owner may change the switches here */
  writable: boolean;
}
export interface Finding {
  code: string;
  severity: string;
  sentence: string;
  fix: string;
  facts?: Record<string, unknown>;
}

export class AuthError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
    public facts: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export interface Sent {
  status: number;
  body: unknown;
}
export type Send = (method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown) => Promise<Sent>;

function fail(r: Sent): never {
  const e = (r.body as { error?: Record<string, unknown> } | null)?.error;
  if (e) {
    const { type, code, message, ...facts } = e;
    throw new AuthError(String(code ?? type ?? 'error'), String(message ?? ''), r.status, facts);
  }
  throw new AuthError(r.status === 0 ? 'offline' : r.status === 401 ? 'sign_in_required' : 'error', '', r.status);
}

export interface AuthApi {
  /** null: this TandemApp runs without accounts */
  me(): Promise<Me | null>;
  password(current: string, password: string): Promise<{ csrf: string }>;
  signOut(): Promise<void>;
  sessions(user?: string): Promise<Session[]>;
  endSession(id: string): Promise<void>;
  /** every session of an account: one's own without `user` */
  endSessions(user?: string): Promise<void>;
  users(): Promise<User[]>;
  addUser(body: { username: string; role: Role; display_name?: string }): Promise<UserCode>;
  patchUser(id: string, body: { role?: Role; enabled?: boolean }): Promise<User>;
  resetUser(id: string): Promise<UserCode>;
  removeUser(id: string): Promise<void>;
  tokens(all?: boolean): Promise<Token[]>;
  createToken(body: NewToken): Promise<TokenOnce>;
  rotateToken(id: string, grace_s: number): Promise<TokenOnce>;
  revokeToken(id: string): Promise<Token>;
  confirmToken(id: string): Promise<Token>;
  audit(limit?: number): Promise<AuditRow[]>;
  exposure(): Promise<ExposureView>;
  /** plans one key of the `api` scope and applies the plan */
  setApi(key: string, value: string): Promise<void>;
  findings(): Promise<Finding[]>;
}

const ROOT = '/v1/auth';
const one = (id: string): string => `/${encodeURIComponent(id)}`;

export function authApi(send: Send): AuthApi {
  const call = async <T>(method: Parameters<Send>[0], path: string, body?: unknown): Promise<T> => {
    const r = await send(method, path, body);
    if (r.status < 200 || r.status >= 300) fail(r);
    return r.body as T;
  };
  return {
    me: async () => {
      const r = await send('GET', `${ROOT}/session`);
      if (r.status === 404) return null;
      if (r.status !== 200) fail(r);
      return r.body as Me;
    },
    password: (current, password) => call('PUT', `${ROOT}/password`, { current, password }),
    signOut: () => call('DELETE', `${ROOT}/session`),
    sessions: async (user) => (await call<{ sessions: Session[] }>('GET', `${ROOT}/sessions${user ? `?user=${encodeURIComponent(user)}` : ''}`)).sessions,
    endSession: (id) => call('DELETE', `${ROOT}/sessions${one(id)}`),
    endSessions: (user) => call('DELETE', `${ROOT}/sessions${user ? `?user=${encodeURIComponent(user)}` : ''}`),
    users: async () => (await call<{ users: User[] }>('GET', `${ROOT}/users`)).users,
    addUser: (body) => call('POST', `${ROOT}/users`, body),
    patchUser: async (id, body) => (await call<{ user: User }>('PATCH', `${ROOT}/users${one(id)}`, body)).user,
    resetUser: (id) => call('POST', `${ROOT}/users${one(id)}/reset`, {}),
    removeUser: (id) => call('DELETE', `${ROOT}/users${one(id)}`),
    tokens: async (all) => (await call<{ tokens: Token[] }>('GET', `${ROOT}/tokens${all ? '?all=1' : ''}`)).tokens,
    createToken: (body) => call('POST', `${ROOT}/tokens`, body),
    rotateToken: (id, grace_s) => call('POST', `${ROOT}/tokens${one(id)}/rotate`, { grace_s }),
    revokeToken: (id) => call('DELETE', `${ROOT}/tokens${one(id)}`),
    confirmToken: (id) => call('PATCH', `${ROOT}/tokens${one(id)}`, { needs_review: false }),
    audit: async (limit = 200) => (await call<{ rows: AuditRow[] }>('GET', `${ROOT}/audit?limit=${limit}`)).rows,
    exposure: async () => {
      const v = await call<{ writable: boolean; values: { key: string; value: unknown }[] }>('GET', '/v1/settings/api');
      const of = (k: string) => v.values.find((x) => x.key === k)?.value;
      const ex = (k: string) => (of(k) === 'network' || of(k) === 'this-machine' ? (of(k) as Exposure) : null);
      return { values: { openai: ex('openai.exposure'), anthropic: ex('anthropic.exposure') }, listen: typeof of('listen') === 'string' ? (of('listen') as string) : null, writable: v.writable };
    },
    setApi: async (key, value) => {
      const changes = [{ scope: 'api', set: { [key]: value } }];
      const plan = await call<{ changed: boolean; confirm: string | null }>('POST', '/v1/settings/plan', { changes });
      if (plan.changed && plan.confirm) await call('POST', '/v1/settings/apply', { changes, confirm: plan.confirm });
    },
    findings: async () => (await call<{ findings: Finding[] }>('GET', '/v1/fleet/health/controller')).findings,
  };
}

/** Same-origin fetch with a JSON body on every write. The page's fetch wrapper (lib/csrf.ts) adds
 *  the session's header and tells the shell about a 401. */
const http: Send = async (method, path, body) => {
  let res: Response;
  try {
    res = await fetch(apiBase() + path, { method, credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) });
  } catch {
    return { status: 0, body: null };
  }
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    /* no body (204), or not JSON: the status says the rest */
  }
  return { status: res.status, body: parsed };
};

export const auth: AuthApi = authApi(http);
