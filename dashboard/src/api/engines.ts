// Adding an engine host (contracts/fleet-v1 engine-*.schema.json, `/v1/fleet/engines/*`; VIS-278,
// SRV-827, SRV-829, SRV-831, SRV-833): what the LAN advertises and what answers at a typed address,
// pairing by the code the engine host printed, the two other ways (a password once, a key line to
// paste), and after pairing the engine's mode, its models and a model's install with its progress.
//
// Every refusal of these routes carries a `code`, one sentence (`message`) and what to do about it
// (`fix`), with the facts it names: the page shows both, never a status number. A call that fails
// throws EngineError with that finding; a 401 of the controller's guard throws SignInNeeded. The
// calls go through `send`, so a test (or the mock) can answer them without HTTP.

import { apiBase } from './client';

/** What is wrong, in words: a refusal's body, or a 200 answer's `finding`. */
export interface EngineFinding {
  code: string;
  message: string;
  fix: string;
  detail?: string;
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
}

/** A refusal of an engine route: its finding and the HTTP status it came with. */
export class EngineError extends Error {
  constructor(
    public finding: EngineFinding,
    public status: number,
    public endpoint: string,
  ) {
    super(finding.message);
  }
}

/** The controller's guard answered 401: pairing needs the admin session, or a call from the
 *  controller's own machine. */
export class SignInNeeded extends Error {
  readonly status = 401;
  constructor(
    message: string,
    public endpoint: string,
  ) {
    super(message);
  }
}

/** One engine the LAN advertises. Nothing in it is proven: the code or the user proves the
 *  fingerprint. `node` names the fleet member that already pins it. */
export interface DiscoveredEngine {
  name: string;
  instance: string;
  version: string | null;
  address: string;
  addresses: string[];
  port: number;
  fingerprint: string | null;
  verified: false;
  node: string | null;
  alias: string | null;
}

export interface DiscoverView {
  contract_version: string;
  engines: DiscoveredEngine[];
  browsed_at: number | null;
  seconds: number | null;
  lan: 'ok' | 'denied' | 'failed' | 'not_run';
  finding: EngineFinding | null;
}

/** The engine's pairing window as its host shows it; never the code. */
export interface PairWindow {
  open: boolean;
  state: string;
  expires_in: number | null;
  tries_left: number | null;
  fingerprint: string | null;
}

export interface ProbeView {
  contract_version: string;
  address: string;
  port: number;
  reachable: boolean;
  engine: boolean;
  pairing: PairWindow | null;
  fingerprint: string | null;
  node: string | null;
  finding: EngineFinding | null;
}

export interface PairStart {
  contract_version: string;
  pairing: string;
  address: string;
  port: number;
  window: PairWindow;
  /** the fingerprint the code will prove */
  fingerprint: string | null;
  advertised: string | null;
  key_id: string;
  expires_in: number;
}

/** engine-ctl-v1's `status`, the part the page reads. */
export interface EngineStatus {
  engine_version: string | null;
  mode: string | null;
  engine: {
    state: 'healthy' | 'loading' | 'draining' | 'failed' | 'down';
    port: number;
    version: string | null;
    model: string | null;
    running_mode: 'engine' | 'chat' | null;
  };
}

/** What the first run does next: the bare engine preselected, then a model while none is served. */
export interface EngineWizard {
  mode_choice: 'engine';
  mode: string | null;
  needs_model: boolean;
  next: 'model' | 'chat' | null;
}

export interface EngineView {
  id: string;
  alias: string;
  address: string | null;
  ssh: { host: string | null; port: number; user: string | null; key_id: string | null };
  host_key: string | null;
  host_key_type: string | null;
  http: { port: number | null; token: { set: boolean; last4: string | null; sha256_6: string | null; rotated_at: number | null } };
  via: string | null;
  paired_at: number | null;
  engine_version: string | null;
  mode: string | null;
  status: EngineStatus | null;
  finding: EngineFinding | null;
  wizard?: EngineWizard;
}

export interface PairedView {
  contract_version: string;
  state: 'paired' | 'waiting';
  via: 'code' | 'password' | 'key' | null;
  engine: EngineView | null;
  node: { id: string; alias: string } | null;
  finding: EngineFinding | null;
}

/** A host key as an SSH first contact shows it, against what the fleet pins for that address. */
export interface HostKeyView {
  type: string;
  fingerprint: string;
  known: 'new' | 'same' | 'changed';
  stored: string | null;
}

export interface SshTargetBody {
  host: string;
  port?: number;
  user: string;
  engine_dir?: string;
  name?: string;
}

export interface ConnectPlan {
  contract_version: string;
  plan: string;
  host: string;
  port: number;
  user: string;
  engine_dir: string | null;
  host_key: HostKeyView;
  key_id: string;
  expires_in: number;
}

export interface ConnectKey {
  contract_version: string;
  plan: string;
  host: string;
  port: number;
  user: string;
  engine_dir: string;
  engine_dir_guessed: boolean;
  /** the authorized_keys line to paste */
  line: string;
  file: string;
  key_id: string;
  key_fingerprint: string;
  /** null with a finding while SSH does not answer there yet */
  host_key: HostKeyView | null;
  finding: EngineFinding | null;
  expires_in: number;
}

export interface ModeView {
  contract_version: string;
  id: string;
  mode: string;
  changed: boolean;
  previous: { mode: string | null; ext: string | null; source: string | null; inferred: boolean | null };
  job: Record<string, unknown> | null;
}

/** One model of the engine's Setup Guide (its tile). */
export interface EngineModel {
  id: string;
  name: string;
  family?: string;
  params?: string;
  pitch?: string;
  recommended?: boolean;
  best_speed?: { tok_s: number; task: string; profile: string } | null;
}

export interface ModelsView {
  contract_version: string;
  id: string;
  models: EngineModel[];
  errors: unknown[];
}

/** A Setup Guide job in a line: the step that runs, its detail (the download's GB so far), how
 *  many of its steps are done. */
export interface JobProgress {
  state: string | null;
  finished: boolean;
  step: string | null;
  label: string | null;
  detail: string | null;
  downloading: boolean;
  steps_done: number;
  steps: number;
  message: string | null;
}

export interface PullPlan {
  ok: boolean;
  errors: unknown[];
  warnings: unknown[];
  download_gb: number | null;
  on_disk: boolean | null;
  profile: string | null;
}

export interface PullView {
  contract_version: string;
  id: string;
  model: string;
  profile: string | null;
  plan: PullPlan;
  job: { id: string; model?: string } | null;
  progress: JobProgress | null;
}

export interface ProgressView {
  contract_version: string;
  id: string;
  job: { id: string; model?: string } | null;
  progress: JobProgress | null;
}

/** One call's answer: the status and the parsed body; status 0 when nothing answered. */
export interface Sent {
  status: number;
  body: unknown;
}
export type Send = (method: 'GET' | 'POST', path: string, body?: unknown) => Promise<Sent>;

const ROOT = '/v1/fleet/engines';

/** The page's own words for an answer that is not an engine refusal (no code of the catalog). */
export const LOCAL_FIX = {
  offline: 'Check that TandemApp still runs and that this device reaches it, then try again.',
  error: 'Try again. If it keeps failing, look at TandemApp’s log.',
} as const;

function fail(endpoint: string, r: Sent): never {
  const e = (r.body as { error?: Partial<EngineFinding> & { type?: string } } | null)?.error;
  if (e && typeof e.code === 'string' && typeof e.message === 'string' && typeof e.fix === 'string') {
    const { type: _type, ...finding } = e;
    throw new EngineError(finding as EngineFinding, r.status, endpoint);
  }
  if (r.status === 401) throw new SignInNeeded(e?.message ?? 'TandemApp’s admin password is needed for this.', endpoint);
  if (r.status === 0) throw new EngineError({ code: 'offline', message: 'TandemApp did not answer.', fix: LOCAL_FIX.offline }, 0, endpoint);
  throw new EngineError({ code: 'error', message: e?.message ? `TandemApp answered: ${e.message}` : `TandemApp answered with an error (${r.status}).`, fix: LOCAL_FIX.error }, r.status, endpoint);
}

export interface EnginesApi {
  list(): Promise<{ contract_version: string; engines: EngineView[] }>;
  engine(id: string): Promise<EngineView & { contract_version: string }>;
  /** the last search of the LAN, as it was */
  found(): Promise<DiscoverView>;
  /** a new search of the LAN (about two seconds) */
  discover(): Promise<DiscoverView>;
  probe(address: string, port?: number): Promise<ProbeView>;
  pairStart(b: { address: string; port?: number; fingerprint?: string; name?: string }): Promise<PairStart>;
  pairCode(pairing: string, code: string): Promise<PairedView>;
  pairAbandon(pairing: string): Promise<{ abandoned: boolean }>;
  passwordPlan(b: SshTargetBody): Promise<ConnectPlan>;
  passwordConfirm(plan: string, password: string, hostKey: string): Promise<PairedView>;
  keyNew(b: SshTargetBody): Promise<ConnectKey>;
  keyCheck(plan: string, hostKey?: string): Promise<PairedView>;
  /** the engine host's own mode: bare engine (the default) or its chat mode */
  setMode(id: string, mode?: 'engine' | 'chat'): Promise<ModeView>;
  models(id: string): Promise<ModelsView>;
  pull(id: string, b: { model: string; profile?: string; dry_run?: boolean }): Promise<PullView>;
  progress(id: string, job?: string): Promise<ProgressView>;
}

export function enginesApi(send: Send): EnginesApi {
  const call = async <T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> => {
    const r = await send(method, ROOT + path, body);
    if (r.status < 200 || r.status >= 300) fail(ROOT + path, r);
    return r.body as T;
  };
  const one = (id: string) => `/${encodeURIComponent(id)}`;
  return {
    list: () => call('GET', ''),
    engine: (id) => call('GET', one(id)),
    found: () => call('GET', '/discover'),
    discover: () => call('POST', '/discover', {}),
    probe: (address, port) => call('POST', '/probe', port == null ? { address } : { address, port }),
    pairStart: (b) => call('POST', '/pair/start', b),
    pairCode: (pairing, code) => call('POST', '/pair/code', { pairing, code }),
    pairAbandon: (pairing) => call('POST', '/pair/abandon', { pairing }),
    passwordPlan: (b) => call('POST', '/connect/password/plan', b),
    passwordConfirm: (plan, password, host_key) => call('POST', '/connect/password/confirm', { plan, password, host_key }),
    keyNew: (b) => call('POST', '/connect/key/new', b),
    keyCheck: (plan, host_key) => call('POST', '/connect/key/check', host_key ? { plan, host_key } : { plan }),
    setMode: (id, mode) => call('POST', `${one(id)}/mode`, mode ? { mode } : {}),
    models: (id) => call('GET', `${one(id)}/models`),
    pull: (id, b) => call('POST', `${one(id)}/models/pull`, b),
    progress: (id, job) => call('GET', `${one(id)}/models/progress${job ? `?job=${encodeURIComponent(job)}` : ''}`),
  };
}

/** Same-origin fetch. A 401 is read here, not by the shared client: the page shows its own
 *  sign-in for it, and an engine's refused token (also a 401) is a finding, not a session's end. */
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

export const engines: EnginesApi = enginesApi(http);
