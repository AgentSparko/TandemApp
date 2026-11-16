// The health page's routes (contracts/fleet-v1 health*.schema.json, `/v1/fleet/health`; SRV-835):
// what is wrong with this TandemApp and with each engine host it reaches over SSH, in words, and
// the fixes TandemApp can run itself.
//
//   GET  /v1/fleet/health[?fresh=1]        the worst state, the first run's setup flag, the
//                                          controller, every engine host, the pairing codes the
//                                          engines refused lately, and every button's label
//   GET  /v1/fleet/health/{id}[?fresh=1]   one of them (`controller`: this TandemApp)
//   POST /v1/fleet/health/{id}/fix         {action, ref?, confirm?, when?}: one fix, then that
//                                          target checked again
//
// A finding is a code of the engine codes table with its severity, one sentence, its fix, the
// facts it names and the action its button runs. A refusal of these routes has the shape of the
// engine routes' refusals (a code, a sentence, a fix), so a failed call throws the same
// EngineError, and a 401 of the controller's guard the same SignInNeeded (api/engines.ts). The
// calls go through `send`, so a test (or the mock) can answer them without HTTP.

import { fail, http, type Send } from './engines';

export type Severity = 'error' | 'warning' | 'info';
export type HealthState = 'ok' | 'warning' | 'error';
export type ActionId = 'retry' | 'start' | 'rotate_token' | 'upgrade' | 'restart_chatd' | 're_pair' | 'pick_model' | 'add_engine';

/** What a finding's button does: `changes` false is a read or a guide; `confirm` asks first. */
export interface HealthAction {
  id: ActionId;
  label: string;
  changes: boolean;
  confirm: boolean;
}

/** The facts a sentence names, as values. Never a secret. */
export interface HealthFacts {
  address?: string;
  port?: number;
  host?: string;
  user?: string;
  dir?: string;
  tries_left?: number;
  state?: string;
  retry_after?: number;
  expected?: string;
  offered?: string;
  pinned?: string;
  model?: string;
  job?: string;
  node?: string;
  alias?: string;
  path?: string;
  image?: string;
  free_gb?: number;
  need_gb?: number;
  backend?: string;
  plugin?: string;
  engine_api?: string;
  speaks?: string;
}

export interface HealthFinding {
  code: string;
  /** error: it does not work; warning: it works, worse; info: it will pass */
  severity: Severity;
  sentence: string;
  fix: string;
  action: HealthAction | null;
  facts: HealthFacts;
  /** the other side's own words, when it gave some */
  detail?: string;
}

export type CheckName = 'ssh' | 'host_key' | 'engine' | 'version' | 'model' | 'http' | 'token' | 'chatd' | 'sandbox' | 'memory_cgroup' | 'disk' | 'backends' | 'plugins' | 'engines';

/** One check that ran, in the order they ran; `ok` null: not run, an earlier one made it moot. */
export interface HealthCheck {
  check: CheckName;
  ok: boolean | null;
  code: string | null;
}

export interface TokenHint {
  set: boolean;
  last4: string | null;
  sha256_6: string | null;
  rotated_at: number | null;
}

/** An engine host, or this controller (`id` controller). */
export interface HealthTarget {
  id: string;
  kind: 'engine' | 'controller';
  alias: string;
  address: string | null;
  state: HealthState;
  checked_at: number;
  /** how old this answer is; `cached`: it is a kept one; `limited`: asked fresh too soon */
  age_s: number;
  cached: boolean;
  limited: boolean;
  findings: HealthFinding[];
  checks: HealthCheck[];
  ssh?: { host: string | null; port: number; user: string | null };
  http?: { port: number | null; token: TokenHint };
  host_key?: string | null;
  engine_version?: string | null;
  engine_api?: string | null;
  mode?: string | null;
  engine_state?: 'healthy' | 'loading' | 'draining' | 'failed' | 'down' | null;
  model?: string | null;
  chat?: 'own' | 'external' | 'off';
  /** the controller: the backend of chatd's sandboxes, as chatd reports it */
  sandbox_backend?: 'docker' | 'kubernetes' | null;
  sandbox_socket?: string | null;
}

/** A pairing code an engine host refused in the last ten minutes. */
export interface RefusedPairing {
  address: string;
  port: number;
  at: number;
  finding: HealthFinding;
}

export interface HealthSetup {
  /** nothing serves through this TandemApp yet: the first run */
  needs_setup: boolean;
  engines: number;
  nodes: number;
  backends: number;
  first_page: 'add_engine' | null;
}

export interface HealthView {
  contract_version: string;
  checked_at: number;
  state: HealthState;
  setup: HealthSetup;
  controller: HealthTarget;
  engines: HealthTarget[];
  pairings: RefusedPairing[];
  actions: Record<ActionId, HealthAction | null>;
}

/** What an action that changes nothing answers: where to go, and the steps. */
export interface HealthGuide {
  open: 'add_engine' | 'model';
  engine: string | null;
  address: string | null;
  pinned: string | null;
  offered: string | null;
  steps: string[];
}

/** One side of an upgrade's plan (engine-ctl-v1 upgrade-plan). */
export interface UpgradeSide {
  commit?: string;
  version?: string | null;
  engine_api?: string | null;
  ctl?: string | null;
  ref?: string;
  source?: 'ref' | 'archive';
}

/** An upgrade's plan as the engine host made it; `confirm` goes back with the apply. */
export interface UpgradePlan {
  from?: UpgradeSide;
  to?: UpgradeSide;
  venv?: 'kept' | 'synced';
  keeps?: string[];
  steps?: string[];
  rollback?: string;
  warnings?: string[];
  confirm?: string;
}

/** A job of the engine host or of chatd, the part the page reads. */
export interface FixJob {
  id?: string;
  kind?: string;
  action?: string;
  state?: string;
  phase?: string;
  when?: { mode?: string };
  message?: string | null;
  waiting_for?: string | null;
  rc?: number | null;
}

export interface FixResult {
  /** Rotate: the new token is stored, and its hint (never the token) */
  rotated?: boolean;
  stored?: boolean | null;
  hint?: Partial<TokenHint> | string | null;
  /** Upgrade: the release asked for and its plan */
  ref?: string;
  plan?: UpgradePlan | null;
}

export interface HealthFix {
  contract_version: string;
  id: string;
  action: ActionId;
  done: boolean;
  /** the answer is a plan: send the action again with the plan's `confirm` */
  needs_confirm: boolean;
  job: FixJob | null;
  result: FixResult | null;
  guide: HealthGuide | null;
  /** the target, checked again after the fix */
  target: HealthTarget;
  /** the finding the action was for, if it is still there */
  finding: HealthFinding | null;
  cleared: boolean;
  /** each instance the fix was for (a finding of its code, the backend or plugin it names where there are several) */
  instances?: { code: string; instance: string | null; cleared: boolean }[];
}

export interface FixBody {
  action: ActionId;
  ref?: string;
  confirm?: string;
  when?: 'now' | 'idle';
  /** the backend or plugin the finding names: `cleared` and `finding` are of that one */
  instance?: string;
}

export interface HealthApi {
  all(fresh?: boolean): Promise<HealthView>;
  target(id: string, fresh?: boolean): Promise<HealthTarget>;
  fix(id: string, body: FixBody): Promise<HealthFix>;
}

export const HEALTH_ROOT = '/v1/fleet/health';
/** the id of this TandemApp's own target */
export const CONTROLLER = 'controller';

export function healthApi(send: Send): HealthApi {
  const call = async <T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> => {
    const r = await send(method, HEALTH_ROOT + path, body);
    if (r.status < 200 || r.status >= 300) fail(HEALTH_ROOT + path, r);
    return r.body as T;
  };
  const one = (id: string) => `/${encodeURIComponent(id)}`;
  const q = (fresh?: boolean) => (fresh ? '?fresh=1' : '');
  return {
    all: (fresh) => call('GET', q(fresh)),
    target: (id, fresh) => call('GET', one(id) + q(fresh)),
    fix: (id, body) => call('POST', `${one(id)}/fix`, body),
  };
}

export const health: HealthApi = healthApi(http);
