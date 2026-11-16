// The settings interface (contracts/settings-v1, `/v1/settings/*`; VIS-280, SRV-845): five scopes
// (app, backends, conductor, engine, hooks) that answer the same questions with the same shapes, a
// change set across them with its plan, its confirm and its result, the engine restarts a change
// scheduled, and the audit rows. The page plans and applies through the change-set routes only: one
// part a scope and target, a restore as a part too.
//
// A refusal throws SettingsError with the contract's `type`, its sentence and its facts (`key`,
// `variable`, `plan`, `errors`). The calls go through `send`, so a test (or the mock) can answer
// them without HTTP.

import { apiBase } from './client';
import type { Send } from './engines';

export type Scope = 'app' | 'backends' | 'conductor' | 'engine' | 'hooks';
export const SCOPES: Scope[] = ['app', 'backends', 'conductor', 'engine', 'hooks'];
/** none: applied as it is; app: this controller restarts; hot: the running service reads it at
 *  once; engine: the engine restarts (scheduled, waits for idle) */
export type RestartClass = 'none' | 'app' | 'hot' | 'engine';
export type KeyType = 'string' | 'integer' | 'number' | 'boolean' | 'enum' | 'list' | 'object' | 'document' | 'path' | 'module' | 'cidrs' | 'secret';

export interface KeySpec {
  key: string;
  type: KeyType;
  restart: RestartClass;
  /** it feeds an engine's disk-cache fingerprint: the change asks twice */
  fingerprint: boolean;
  secret: boolean;
  write_only: boolean;
  writable: boolean;
  nullable: boolean;
  default: unknown;
  values?: string[];
  why?: string;
  /** the environment variable that sets it instead */
  variable?: string;
  text?: string;
}

export interface KeyValue {
  key: string;
  /** a secret's value is always null: `set` says whether one is stored */
  value: unknown;
  set: boolean;
  source: string;
  variable?: string;
  hint?: string | null;
  label?: string;
  version?: string;
}

export interface Target {
  id: string;
  label: string;
  writable: boolean;
  why: string | null;
  transport?: 'ssh' | 'http';
}

export interface Overview {
  contract_version: string;
  scopes: { scope: Scope; order: number; targets: Target[]; why: string | null }[];
  order: Scope[];
  operations: string[];
}

export interface SchemaView {
  contract_version: string;
  scope: Scope;
  target: string;
  writable: boolean;
  why: string | null;
  keys: KeySpec[];
}

export interface ValuesView {
  contract_version: string;
  scope: Scope;
  target: string;
  writable: boolean;
  why: string | null;
  version: string | null;
  values: KeyValue[];
}

export interface Version {
  id: string;
  at: string | null;
  who: string | null;
  note: string;
  change_set: string | null;
  keys: string[];
  current: boolean;
  /** the engine's last version that started */
  good?: boolean;
  values?: Record<string, unknown>;
}

export interface VersionsView {
  contract_version: string;
  scope: Scope;
  target: string;
  current: string | null;
  versions: Version[];
}

export interface DiffRow {
  path: string;
  before: unknown;
  after: unknown;
}

export interface Change {
  key: string;
  from: unknown;
  to: unknown;
  was_set: boolean;
  is_set: boolean;
  secret: boolean;
  restart: RestartClass;
  fingerprint: boolean;
  /** a document's change, path by path (the first 50) */
  diff?: DiffRow[];
  diff_count?: number;
}

export interface PartPlan {
  scope: Scope;
  target: string;
  kind: 'set' | 'restore';
  version: string | null;
  changed: boolean;
  changes: Change[];
  restart: RestartClass;
  restart_needed: boolean;
  /** a part that restarts an engine: how long the restart itself takes. measured: this controller
   *  saw that engine's last restart; engine: the engine's own last start; default: not known yet */
  restart_estimate?: RestartEstimate | null;
  fingerprint_keys: string[];
  /** the keys that grant a hook the full payload to a destination outside the LAN */
  grant_keys: string[];
  warnings: string[];
}

export interface RestartEstimate {
  seconds: number;
  source: 'measured' | 'engine' | 'default';
}

export interface Plan {
  contract_version: string;
  change_set: string;
  parts: PartPlan[];
  changed: boolean;
  restart: RestartClass;
  restart_needed: boolean;
  /** `<scope>/<target>:<key>` */
  fingerprint_keys: string[];
  /** `<scope>/<target>:<key>`: a hook's full payload to a destination outside the LAN */
  grant_keys: string[];
  needs_confirm: boolean;
  confirm: string | null;
  confirm_fingerprint: string | null;
  /** the second confirm of a full payload outside the LAN (SEC-16) */
  confirm_grant: string | null;
}

export type RestartState = 'scheduled' | 'draining' | 'restarting' | 'warming' | 'done' | 'failed' | 'timeout' | 'cancelled';
export const RESTART_ENDED: RestartState[] = ['done', 'failed', 'timeout', 'cancelled'];

export interface Restart {
  contract_version?: string;
  id: string;
  host: string;
  alias: string;
  change_set: string;
  /** mode: the engine's mode switched (`tandem remote mode set`) */
  kind: 'settings' | 'restore' | 'mode';
  who: string;
  state: RestartState;
  message: string | null;
  when: string;
  deadline_s: number | null;
  deadline_at: string | null;
  /** the countdown while it waits */
  remaining_s: number | null;
  created_at: string | null;
  ended_at: string | null;
  /** the engine's own job */
  job: { id?: string | null; kind?: string | null; state?: string | null; phase?: string | null; message?: string | null; waiting_for?: unknown } | null;
  backends: string[];
  draining: boolean;
  warm: { ok: boolean; backends: Record<string, { ok: boolean; why: string | null }> } | null;
  /** after a failed start: the version the engine runs again, and whether it answers */
  restored: { version: number; healthy: boolean } | null;
  can_cancel: boolean;
}

export interface RestoreRef {
  scope: Scope;
  target: string;
  version: string;
}

export type PartState = 'applied' | 'scheduled' | 'unchanged' | 'failed' | 'not_run';

export interface PartResult {
  scope: Scope;
  target: string;
  kind: 'set' | 'restore';
  state: PartState;
  keys: string[];
  version_before: string | null;
  version_after: string | null;
  restore: RestoreRef[];
  restart: Restart | null;
  error: { type: string; message: string } | null;
}

export interface Result {
  contract_version: string;
  change_set: string;
  state: 'applied' | 'partial' | 'failed' | 'unchanged';
  replayed: boolean;
  parts: PartResult[];
  applied: string[];
  failed: string | null;
  not_run: string[];
}

export interface AuditRow {
  at: string;
  ts_ms: number;
  who: string;
  action: string;
  object: string;
  object_id: string | null;
  scope: string | null;
  target: string | null;
  /** names, never values */
  keys: string[];
  change_set: string | null;
  result: string | null;
}

/** One part of a change set: keys to set and unset, or one version to restore. */
export type PartChange = { scope: Scope; target?: string; set?: Record<string, unknown>; unset?: string[] } | { scope: Scope; target?: string; restore: string };

export interface ApplyOptions {
  confirm: string;
  confirm_fingerprint?: string;
  confirm_grant?: string;
  /** engine parts: `idle` (the default), `now`, or `at=<ISO time with a zone>` */
  when?: string;
  deadline_s?: number;
  note?: string;
}

export interface ErrorFacts {
  /** the part a plan's refusal is about */
  scope?: Scope;
  target?: string;
  key?: string;
  keys?: string[];
  variable?: string;
  /** the plan as it is now (428, 409 stale_plan) */
  plan?: Plan;
  errors?: string[];
  code?: string;
  pinned?: string;
  offered?: string;
}

/** A refusal of a settings route: the contract's error type, its sentence and its facts. */
export class SettingsError extends Error {
  constructor(
    public type: string,
    message: string,
    public status: number,
    public endpoint: string,
    public facts: ErrorFacts = {},
  ) {
    super(message);
  }
}

export interface SettingsApi {
  overview(): Promise<Overview>;
  schema(scope: Scope, target?: string): Promise<SchemaView>;
  values(scope: Scope, target?: string): Promise<ValuesView>;
  versions(scope: Scope, target?: string): Promise<VersionsView>;
  /** what would change: nothing is written */
  plan(changes: PartChange[]): Promise<Plan>;
  apply(changes: PartChange[], o: ApplyOptions): Promise<Result>;
  restarts(): Promise<{ restarts: Restart[] }>;
  restart(id: string): Promise<Restart>;
  cancel(id: string): Promise<Restart>;
  audit(o?: { change_set?: string | null; limit?: number }): Promise<{ rows: AuditRow[] }>;
}

const ROOT = '/v1/settings';

function fail(endpoint: string, status: number, body: unknown): never {
  const e = (body as { error?: ErrorFacts & { type?: string; message?: string } } | null)?.error;
  if (e && typeof e.type === 'string') {
    const { type, message, ...facts } = e;
    throw new SettingsError(type, message ?? type, status, endpoint, facts);
  }
  if (status === 401) throw new SettingsError('unauthorized', 'TandemApp’s admin password is needed for this.', 401, endpoint);
  if (status === 0) throw new SettingsError('offline', 'TandemApp did not answer.', 0, endpoint);
  throw new SettingsError('error', `TandemApp answered with an error (${status}).`, status, endpoint);
}

export function settingsApi(send: Send): SettingsApi {
  const call = async <T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> => {
    const r = await send(method, ROOT + path, body);
    if (r.status < 200 || r.status >= 300) fail(ROOT + path, r.status, r.body);
    return r.body as T;
  };
  const at = (target?: string) => (target ? `?target=${encodeURIComponent(target)}` : '');
  return {
    overview: () => call('GET', ''),
    schema: (scope, target) => call('GET', `/${scope}/schema${at(target)}`),
    values: (scope, target) => call('GET', `/${scope}${at(target)}`),
    versions: (scope, target) => call('GET', `/${scope}/versions${at(target)}`),
    plan: (changes) => call('POST', '/plan', { changes }),
    apply: (changes, o) => call('POST', '/apply', { changes, ...o }),
    restarts: () => call('GET', '/restarts'),
    restart: (id) => call('GET', `/restarts/${encodeURIComponent(id)}`),
    cancel: (id) => call('POST', `/restarts/${encodeURIComponent(id)}/cancel`, {}),
    audit: (o = {}) => {
      const q = new URLSearchParams();
      if (o.change_set) q.set('change_set', o.change_set);
      if (o.limit) q.set('limit', String(o.limit));
      const s = q.toString();
      return call('GET', `/audit${s ? `?${s}` : ''}`);
    },
  };
}

/** Same-origin fetch, a JSON body on every write (the controller takes nothing else). A 401 is
 *  read here: the page shows its own sign-in for it. */
const http: Send = async (method, path, body) => {
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

export const settings: SettingsApi = settingsApi(http);
