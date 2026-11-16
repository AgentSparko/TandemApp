// The hooks' and the decision providers' admin APIs (contracts/hooks-v1 `/v1/fleet/hooks/*`,
// contracts/conductor-v1 `/v1/fleet/decision-providers/*`; VIS-282). What the settings interface
// does not hold lives here: adding and removing, where and when a hook runs, its endpoint, its
// secret (answered once, when it is made or rotated), the playground's test call, the journal and
// the report. Every switch (on, mode, order, scope, policy, time limit, payload level, a
// provider's mode, share, questions and keys) goes through the settings interface instead, so it
// is planned, versioned and audited like every other setting.
//
// A refusal throws HookError with the contract's `type`, its sentence and its facts (`field`, and
// for a full payload outside the LAN the `confirm` to send the same request again with). The calls
// go through `send`, so a test (or the mock) can answer them without HTTP.

import { apiBase } from './client';
import type { Sent } from './engines';

export type Role = 'transform' | 'guard' | 'decider' | 'observer';
export type HookKind = 'http' | 'builtin' | 'plugin' | 'mcp' | 'inproc';
export type HookMode = 'off' | 'shadow' | 'on';
export type Payload = 'meta' | 'redacted' | 'full';
export type Policy = 'open' | 'closed';
export type BreakerState = 'closed' | 'open' | 'half_open';
export type Outcome = 'ok' | 'timeout' | 'error' | 'invalid' | 'cap' | 'breaker_open' | 'redaction_failed' | 'refused' | 'dropped';

/** the requests a hook runs on: lists of patterns; an absent or empty list matches everything */
export type HookScope = Partial<Record<'models' | 'backends' | 'profiles' | 'users' | 'sessions' | 'client_kinds', string[]>>;
export const SCOPE_KEYS = ['models', 'backends', 'profiles', 'users', 'sessions', 'client_kinds'] as const;

export interface Breaker {
  state: BreakerState;
  consecutive_failures: number;
  open_for_s: number;
  opened: number;
}

export interface HookMetrics {
  calls: number;
  failures: number;
  timeouts: number;
  skipped: number;
  outcomes: Record<string, number>;
  p50_ms: number | null;
  p95_ms: number | null;
  breaker: Breaker;
}

export interface Grant {
  level: 'full';
  by: string;
  at: number;
  destination: string | null;
  lan: boolean;
  confirmed_by: string | null;
  confirmed_at: number | null;
}

/** what is shown of a hook's secret: never the secret */
export interface SecretHint {
  set: boolean;
  last4: string | null;
  sha256_6: string | null;
  rotated_at: number | null;
  /** the previous secret still signs beside the new one until then */
  previous_until: number | null;
}

export interface Hook {
  id: string;
  name: string;
  kind: HookKind;
  stages: string[];
  role: Role;
  priority: number;
  mode: HookMode;
  scope: HookScope;
  /** null: the stage's default */
  policy: Policy | null;
  /** null: the stage's budget */
  timeout_ms: number | null;
  payload: Payload;
  endpoint: string | null;
  target: string | null;
  options: Record<string, unknown>;
  enabled: boolean;
  created_at: number;
  updated_at: number;
  updated_by: string | null;
  version: number;
  grant: Grant | null;
  /** stage -> the policy that holds there */
  policy_effective: Record<string, string>;
  secret: SecretHint | null;
  destination: { host: string | null; lan: boolean };
  metrics: HookMetrics;
}

export interface StageSpec {
  when: string;
  may: string[];
  actions: string[];
  roles: Role[];
  /** shared by the stage's sync hooks per request; null: off the request's path */
  budget_ms: number | null;
  sync: boolean;
  on_failure: 'open' | 'closed' | 'drop';
  /** the fields a patch may touch */
  fields: string[];
  /** of those, the ones only a hook with the full payload may change */
  content: string[];
  decision: string[];
  /** it counts toward the cap before the first token */
  first_token: boolean;
  /** the controller calls it in this version */
  wired: boolean;
}

export interface StageTable {
  contract: string;
  version: string;
  cap_ms: number;
  async_timeout_ms: number;
  max_timeout_ms: number;
  kinds: HookKind[];
  kinds_available: HookKind[];
  payloads: Payload[];
  modes: HookMode[];
  policies: Policy[];
  roles: Record<Role, string[]>;
  /** in the order a request passes them */
  stages: Record<string, StageSpec>;
  /** GET /v1/fleet/hooks/stages: each stage's sample event, the playground's default (stages.json has none) */
  samples?: Record<string, StageSample>;
}

/** a stage's sample: the event a hook at payload meta gets, and the text a redacted or full one gets with it */
export interface StageSample {
  event: Record<string, unknown>;
  content: unknown;
}

export interface HookList {
  contract_version: string;
  hooks: Hook[];
  kinds_available: HookKind[];
  builtins: string[];
}

export interface DiffEntry {
  op: 'add' | 'replace' | 'remove';
  bytes?: number;
}
/** a shadow call's diff: field -> {op, bytes}, or {deny: true}, or {decision}; never a value */
export type CallDiff = Record<string, DiffEntry | boolean | Record<string, unknown>>;

export interface HookCall {
  ts: number;
  stage: string;
  request: string | null;
  mode: 'on' | 'shadow' | 'test';
  outcome: Outcome;
  action: string | null;
  duration_ms: number;
  breaker: BreakerState;
  reason?: string;
  labels?: Record<string, unknown>;
  decision?: Record<string, unknown>;
  fields?: string[];
  error?: string;
  diff?: CallDiff;
}

export interface HookCalls {
  hook: string;
  calls: HookCall[];
  metrics: HookMetrics;
}

/** GET /v1/fleet/hooks/calls?request=: one request's calls across hooks and stages, in the order
 *  the request met them; each names its hook (its name now, its id when it is gone) */
export interface HookTraceCall extends HookCall {
  hook: string;
  name: string;
}

export interface HookTrace {
  request: string;
  calls: HookTraceCall[];
}

export interface HookTest {
  hook: string;
  stage: string;
  outcome: Outcome;
  duration_ms: number;
  result: Record<string, unknown> | null;
  error: string | null;
  /** the event the hook was sent, at its payload level; null when none was */
  event: Record<string, unknown> | null;
  diff?: Record<string, DiffEntry>;
}

/** POST /v1/fleet/hooks: name, role and stages always; endpoint for http, target for builtin */
export interface NewHook {
  name: string;
  kind: HookKind;
  stages: string[];
  role: Role;
  priority?: number;
  mode?: HookMode;
  scope?: HookScope;
  policy?: Policy | null;
  timeout_ms?: number | null;
  payload?: Payload;
  endpoint?: string | null;
  target?: string | null;
  options?: Record<string, unknown>;
  enabled?: boolean;
  /** the token a 409 confirm_needed answered */
  confirm?: string;
}

/** what the hooks page changes on the API itself; the switches are settings keys */
export type HookPatch = Partial<Pick<NewHook, 'name' | 'stages' | 'role' | 'endpoint' | 'target' | 'options' | 'confirm'>>;

export interface HookFacts {
  field?: string;
  code?: string;
  /** a full payload outside the LAN: the same request again with this grants it */
  confirm?: string;
  destination?: string;
  lan?: boolean;
}

/** A refusal of a hooks or decision-providers route. */
export class HookError extends Error {
  constructor(
    public type: string,
    message: string,
    public status: number,
    public endpoint: string,
    public facts: HookFacts = {},
  ) {
    super(message);
  }
}

export const asHookError = (e: unknown): HookError => (e instanceof HookError ? e : new HookError('error', (e as Error)?.message ?? String(e), 0, ''));

// ---- decision providers ---------------------------------------------------------------------------

export type ProviderKind = 'builtin' | 'systemone-http' | 'hook';
export type ProviderMode = 'on' | 'shadow' | 'ab';
export type Sticky = 'session' | 'user' | 'request';
/** unknown: an endpoint was not probed yet (the list's view probes nothing) */
export type HealthStatus = 'ok' | 'failing' | 'down' | 'off' | 'unknown';

/** what is shown of an API key: never the key */
export interface KeyHint {
  key_set: boolean;
  last4: string | null;
  sha256_6: string | null;
  set_at: number | null;
}

export interface QuestionMap {
  model: string | null;
  endpoint: string | null;
  timeout_ms: number | null;
  key?: KeyHint | null;
}

export interface Probe {
  up: boolean;
  ms?: number | null;
  error?: string | null;
  detail?: Record<string, unknown> | null;
  limits?: Record<string, unknown>;
  age_s: number;
}

export interface Health {
  provider: string;
  kind: ProviderKind;
  status: HealthStatus;
  /** `probe` null: not probed since the provider last changed (the list's view) */
  endpoints: { endpoint: string; questions: string[]; probe: Probe | null; breaker: Breaker }[];
  last_call: { ts_ms: number; question: string; error: string | null; ms: number | null } | null;
}

export interface Provider {
  id: string;
  name: string;
  kind: ProviderKind;
  endpoint: string | null;
  hook_id: string | null;
  /** kind plugin: `<plugin>/<provider>` (SRV-859) */
  target?: string | null;
  model: string;
  timeout_ms: number;
  questions: Record<string, QuestionMap>;
  mode: ProviderMode;
  ab_percent: number;
  sticky: Sticky;
  enabled: boolean;
  key: KeyHint | null;
  created_at: number;
  updated_at: number;
  updated_by: string | null;
  version: number;
  breakers: Record<string, Breaker>;
  /** in the list: from the last probes, nothing probed; with ?health=1: probed */
  health?: Health;
}

export interface ProviderList {
  contract_version: string;
  providers: Provider[];
  /** question id -> what it is */
  questions: Record<string, { what: string }>;
  kinds: ProviderKind[];
  modes: ProviderMode[];
  sticky: Sticky[];
  /** recorded answers replace every provider (TANDEM_DECISION_REPLAY) */
  replay: boolean;
}

export interface Answer {
  label: string | null;
  confidence: number;
  probs: Record<string, number>;
  model: string | null;
  ms: number;
  error: string | null;
}

export interface ProviderCall {
  provider: string;
  kind: string;
  question: string;
  mode: ProviderMode;
  arm: 'a' | 'b' | null;
  used: 'provider' | 'builtin';
  label: string | null;
  answer: Answer | null;
  endpoint: string | null;
  builtin: Record<string, unknown> | null;
  agree: boolean | null;
  /** why the built-in answer was used instead */
  fallback: string | null;
  key: string | null;
  limits: Record<string, unknown> | null;
  request: string | null;
  session: string | null;
  ts_ms: number;
}

export interface ProviderTest {
  provider: string;
  question: string;
  ok: boolean;
  endpoint: string | null;
  ms: number;
  answer: Answer | null;
  error: string | null;
}

export type Arm = 'on' | 'shadow' | 'ab_a' | 'ab_b';
export const ARMS: Arm[] = ['on', 'shadow', 'ab_b', 'ab_a'];

export interface Bucket {
  calls: number;
  answered: number;
  failures: number;
  fallbacks: number;
  /** each reason the built-in answered instead, counted (SRV-851) */
  fallback_reasons: Record<string, number>;
  compared: number;
  agreed: number;
  agreement_pct: number | null;
  p50_ms: number | null;
  p95_ms: number | null;
  labels: Record<string, number>;
}

export interface Report {
  contract_version: string;
  since_ms: number;
  hours: number;
  /** memory: the controller runs no conductor journal, so only this process's calls */
  source: 'journal' | 'memory';
  /** provider -> question -> arm */
  providers: Record<string, Record<string, Partial<Record<Arm, Bucket>>>>;
}

export interface NewProvider {
  name: string;
  kind: ProviderKind;
  endpoint?: string | null;
  hook_id?: string | null;
  model?: string;
  timeout_ms?: number;
  /** question id -> its mapping; `api_key` here is that question's own key */
  questions: Record<string, { model?: string; endpoint?: string; timeout_ms?: number; api_key?: string }>;
  mode?: ProviderMode;
  ab_percent?: number;
  sticky?: Sticky;
  enabled?: boolean;
  api_key?: string | null;
}

/** what the providers page changes on the API itself; the switches and keys are settings keys */
export type ProviderPatch = Partial<Pick<NewProvider, 'name' | 'endpoint' | 'hook_id' | 'model' | 'timeout_ms'>>;

// ---- the clients ----------------------------------------------------------------------------------

export type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE';
export type HookSend = (method: Method, path: string, body?: unknown) => Promise<Sent>;

export interface HooksApi {
  list(): Promise<HookList>;
  stages(): Promise<StageTable>;
  get(id: string): Promise<Hook>;
  /** the answer holds an http hook's secret, once */
  create(doc: NewHook): Promise<{ hook: Hook; secret?: string }>;
  patch(id: string, p: HookPatch): Promise<Hook>;
  remove(id: string): Promise<void>;
  /** the hook once against the stage's sample event; nothing counted, nothing applied */
  test(id: string, stage?: string, event?: Record<string, unknown>): Promise<HookTest>;
  calls(id: string, limit?: number): Promise<HookCalls>;
  /** one request's calls across every hook and stage, from the journals */
  trace(request: string): Promise<HookTrace>;
  /** a new secret, answered once; the previous one signs beside it for `grace_s` */
  rotate(id: string, grace_s?: number): Promise<{ hook: Hook; secret: string }>;
}

export interface ProvidersApi {
  list(): Promise<ProviderList>;
  get(id: string): Promise<Provider>;
  create(doc: NewProvider): Promise<Provider>;
  patch(id: string, p: ProviderPatch): Promise<Provider>;
  remove(id: string): Promise<void>;
  test(id: string, question?: string, text?: string): Promise<ProviderTest>;
  /** `force`: probe now, not the answer kept for 10 s */
  health(id: string, force?: boolean): Promise<Health>;
  calls(id: string, limit?: number): Promise<{ provider: string; calls: ProviderCall[] }>;
  report(hours?: number, provider?: string): Promise<Report>;
}

const HOOKS = '/v1/fleet/hooks';
const PROVIDERS = '/v1/fleet/decision-providers';

function fail(endpoint: string, status: number, body: unknown): never {
  const e = (body as { error?: HookFacts & { type?: string; message?: string } } | null)?.error;
  if (e && typeof e.type === 'string') {
    const { type, message, ...facts } = e;
    throw new HookError(type, message ?? type, status, endpoint, facts);
  }
  if (status === 401) throw new HookError('unauthorized', 'TandemApp’s admin password is needed for this.', 401, endpoint);
  if (status === 0) throw new HookError('offline', 'TandemApp did not answer.', 0, endpoint);
  throw new HookError('error', `TandemApp answered with an error (${status}).`, status, endpoint);
}

const caller =
  (send: HookSend, root: string) =>
  async <T>(method: Method, path: string, body?: unknown): Promise<T> => {
    const r = await send(method, root + path, body);
    if (r.status < 200 || r.status >= 300) fail(root + path, r.status, r.body);
    return r.body as T;
  };
const seg = (id: string): string => `/${encodeURIComponent(id)}`;

export function hooksApi(send: HookSend): HooksApi {
  const call = caller(send, HOOKS);
  return {
    list: () => call('GET', ''),
    stages: () => call('GET', '/stages'),
    get: async (id) => (await call<{ hook: Hook }>('GET', seg(id))).hook,
    create: (doc) => call('POST', '', doc),
    patch: async (id, p) => (await call<{ hook: Hook }>('PATCH', seg(id), p)).hook,
    remove: async (id) => void (await call('DELETE', seg(id))),
    test: (id, stage, event) => call('POST', `${seg(id)}/test`, { ...(stage ? { stage } : {}), ...(event ? { event } : {}) }),
    calls: (id, limit = 200) => call('GET', `${seg(id)}/calls?limit=${limit}`),
    trace: (request) => call('GET', `/calls?request=${encodeURIComponent(request)}`),
    rotate: (id, grace_s = 0) => call('POST', `${seg(id)}/secret`, { grace_s }),
  };
}

export function providersApi(send: HookSend): ProvidersApi {
  const call = caller(send, PROVIDERS);
  return {
    list: () => call('GET', ''),
    get: async (id) => (await call<{ provider: Provider }>('GET', seg(id))).provider,
    create: async (doc) => (await call<{ provider: Provider }>('POST', '', doc)).provider,
    patch: async (id, p) => (await call<{ provider: Provider }>('PATCH', seg(id), p)).provider,
    remove: async (id) => void (await call('DELETE', seg(id))),
    test: (id, question, text) => call('POST', `${seg(id)}/test`, { ...(question ? { question } : {}), ...(text ? { text } : {}) }),
    health: (id, force = false) => call('GET', `${seg(id)}/health${force ? '?force=1' : ''}`),
    calls: (id, limit = 200) => call('GET', `${seg(id)}/calls?limit=${limit}`),
    report: (hours = 24, provider) => call('GET', `/report?hours=${hours}${provider ? `&provider=${encodeURIComponent(provider)}` : ''}`),
  };
}

/** Same-origin fetch, a JSON body on every write. A 401 is read here: the page shows its own
 *  sign-in for it. */
const http: HookSend = async (method, path, body) => {
  let res: Response;
  try {
    res = await fetch(apiBase() + path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    });
  } catch {
    return { status: 0, body: null };
  }
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    /* not JSON: the status says the rest */
  }
  return { status: res.status, body: parsed };
};

export const hooks: HooksApi = hooksApi(http);
export const providers: ProvidersApi = providersApi(http);
