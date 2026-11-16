// The health page in the mock (`/v1/fleet/health`, contracts/fleet-v1 health*.schema.json; SRV-835),
// as the controller answers it (proxy/fleethealth.py): the controller's own checks, each engine
// host's checks in their order, the pairing codes refused lately, and the fixes. Every finding is
// a code of mock/enginecodes.ts with the controller's own sentence and fix, its severity and its
// action from the same table the contract has (engine-codes.json `findings` and `actions`;
// tests/health-mock.test.ts holds them equal). No answer holds a token, a key or a pairing code.
//
// What is checked is a simulation: each engine host has a few conditions (how SSH answers, the
// engine's state, its versions, a model, how its HTTP port answers, the token), the controller has
// its own (Chat, the sandbox runtime, the memory cgroup, the free disk, the backends that are
// down, the plugins that do not run), and the checks read them as the controller's do. A fix
// changes a condition, on the mock's clock: Start loads for 12 s and then serves, Rotate stores a
// new token, an upgrade's plan carries a confirm and its apply ends after 8 s (14 s when it waits
// for idle), Restart brings chatd back, Check again probes the backends that are down.
//
// The scenarios (`?health=<name>` on the page URL, or POST /__mock/health {"scenario"}):
//   mixed     the default: a stopped engine (Start), a refused token (Rotate), an engine too old
//             (Upgrade), no sandbox socket, little disk, a backend down (a check again brings it
//             back), a plugin that gave up, a wrong pairing code
//   ok        everything works
//   first     the first run: no engine yet (the default while the fleet is empty)
//   all       every engine host finding on a host of its own, the pairing refusals, a controller
//             whose chatd is down, with no sandbox image, no memory cgroup, almost no disk, two
//             backends down and four plugin problems
//   silent    chatd runs but does not answer, the container runtime does not either
//   external  Chat runs on another machine, which does not answer
//   cluster   chatd's sandboxes are pods, and its cluster check failed (sandbox_unavailable)
// The engine hosts paired through Add engine (mock/enginehosts.ts) are checked too, in every
// scenario. `fail(action, code)` makes a fix answer one refusal until it is cleared; `touch()`
// forgets the kept answers after a test changed a condition by hand.

import type { ActionId, CheckName, HealthAction, HealthCheck, HealthFinding, HealthGuide, HealthState, HealthTarget, Severity, TokenHint, UpgradePlan } from '../src/api/health.ts';
import { instanceOf } from '../src/lib/health.ts';
import { ENGINE_CODES, filled, refusal, type Facts, type Reply } from './enginecodes.ts';

const CONTRACT = '1.0';
export const CONTROLLER = 'controller';
/** a target's answer is kept this long; `fresh=1` checks again unless the last check is younger */
export const CACHE_S = 15;
export const MIN_GAP_S = 3;
export const FIXES_PER_MIN = 6;
const DISK_WARN_GB = 5;
const DISK_ERROR_GB = 1;
/** the engine API and the control this TandemApp is made for */
export const API_SPEAKS = '1.9';
export const CTL_SPEAKS = '1.1';
export const START_S = 12;
export const UPGRADE_S = 8;
export const UPGRADE_IDLE_S = 14;
const LOAD_S = 6;

const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
const CONFIRM_RE = /^[0-9a-f]{16}$/;

export type Rights = 'dashboard' | 'setup' | 'fleet_trust' | 'conductor_write';

/** action -> [the button's label, it changes something, it needs a confirm, the rights it needs] */
export const HEALTH_ACTIONS: Record<ActionId, readonly [string, boolean, boolean, Rights]> = {
  retry: ['Check again', false, false, 'dashboard'],
  start: ['Start', true, false, 'setup'],
  rotate_token: ['Rotate the token', true, false, 'fleet_trust'],
  upgrade: ['Upgrade', true, true, 'fleet_trust'],
  restart_chatd: ['Restart chatd', true, false, 'conductor_write'],
  re_pair: ['How to pair again', false, false, 'dashboard'],
  pick_model: ['Pick a model', false, false, 'dashboard'],
  add_engine: ['Add engine', false, false, 'dashboard'],
};

/** code -> [severity, action]: what the health page shows of each finding it makes */
export const HEALTH_FINDINGS: Record<string, readonly [Severity, ActionId | null]> = {
  // an engine host
  ssh_unreachable: ['error', 'retry'],
  host_key_changed: ['error', 're_pair'],
  not_paired: ['error', 're_pair'],
  ctl_failed: ['error', 'retry'],
  engine_down: ['error', 'start'],
  engine_loading: ['info', null],
  engine_too_old: ['warning', 'upgrade'],
  engine_api_unsupported: ['error', null],
  no_model: ['warning', 'pick_model'],
  http_blocked: ['error', 'retry'],
  not_allowed: ['error', null],
  not_an_engine: ['error', 'retry'],
  token_missing: ['error', 'rotate_token'],
  token_refused: ['error', 'rotate_token'],
  // a pairing code the engine refused: no target, so no fix (the page links to Add engine)
  wrong_code: ['warning', null],
  code_spent: ['warning', null],
  no_window: ['warning', null],
  host_key_mismatch: ['error', null],
  rate_limited: ['warning', null],
  // the controller
  no_engine: ['info', 'add_engine'],
  chatd_down: ['error', 'restart_chatd'],
  chatd_silent: ['error', 'restart_chatd'],
  chat_unreachable: ['warning', null],
  sandbox_off: ['warning', null],
  sandbox_unreachable: ['warning', null],
  sandbox_no_image: ['warning', null],
  sandbox_unavailable: ['warning', null],
  no_memory_cgroup: ['warning', null],
  disk_low: ['warning', null],
  backend_down: ['warning', 'retry'],
  // the plugin host
  plugin_failed: ['warning', null],
  plugin_no_sandbox: ['warning', null],
  plugin_tampered: ['error', null],
  plugin_untrusted: ['warning', null],
  auth_setup_pending: ['error', null],
  auth_owner_no_password: ['info', null],
  local_mode_proxy_seen: ['error', null],
  legacy_env_set: ['warning', null],
  legacy_env_invalid: ['error', null],
  legacy_users_from_unreadable: ['warning', null],
  bootstrap_secret_left: ['info', null],
};

export const actionView = (a: ActionId | null): HealthAction | null => (a === null ? null : { id: a, label: HEALTH_ACTIONS[a][0], changes: HEALTH_ACTIONS[a][1], confirm: HEALTH_ACTIONS[a][2] });

/** The rights a fix needs (the mock's guard, mock/server.ts): a read's for a check again and the
 *  guides, the Setup Guide's for Start, pairing's for the token and an upgrade, the conductor's
 *  writes for chatd. */
export function rightsOf(body: unknown): Rights {
  const a = body && typeof body === 'object' ? (body as { action?: unknown }).action : null;
  return typeof a === 'string' && a in HEALTH_ACTIONS ? HEALTH_ACTIONS[a as ActionId][3] : 'dashboard';
}

/** One finding as the controller words it: the table's sentence and fix filled from the facts,
 *  its severity and its action. */
export function healthFinding(code: string, facts: Facts = {}, detail?: string | null, o: { severity?: Severity; action?: ActionId | null } = {}): HealthFinding {
  const f = filled(code, facts, detail);
  const [sev, act] = HEALTH_FINDINGS[code] ?? (['error', null] as const);
  return { code, severity: o.severity ?? sev, sentence: f.message, fix: f.fix, action: actionView('action' in o ? (o.action ?? null) : act), facts: f.facts, ...(f.detail ? { detail: f.detail } : {}) };
}

const stateOf = (found: HealthFinding[]): HealthState => (found.some((f) => f.severity === 'error') ? 'error' : found.some((f) => f.severity === 'warning') ? 'warning' : 'ok');
const check = (name: CheckName, ok: boolean | null, code: string | null = null): HealthCheck => ({ check: name, ok, code });
const ver = (text: string | null): [number, number] | null => {
  const m = /^(\d+)\.(\d+)$/.exec(text ?? '');
  return m ? [Number(m[1]), Number(m[2])] : null;
};
const older = (a: [number, number], b: [number, number]): boolean => a[0] < b[0] || (a[0] === b[0] && a[1] < b[1]);

/** a fingerprint of the right shape from a word (43 base64 characters) */
const fp = (word: string): string => `SHA256:${(word.replace(/[^A-Za-z0-9]/g, '') + 'MockHostKeyOfAnEngineHostForTheHealthPage0123456789').slice(0, 43)}`;

/** n hex digits from a text (FNV-1a, chained): the same for the same input */
function hex(text: string, n: number): string {
  let out = '';
  for (let round = 0; out.length < n; round++) {
    let h = 0x811c9dc5 ^ round;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 0x01000193);
    out += (h >>> 0).toString(16).padStart(8, '0');
  }
  return out.slice(0, n);
}

// ---- what is simulated -----------------------------------------------------------------------------

/** One engine host and the conditions its checks read. */
export interface HostSim {
  id: string;
  alias: string;
  address: string;
  user: string;
  sshPort: number;
  httpPort: number;
  /** the SSH host key this fleet pinned; `offered`: what the host shows now when it changed */
  hostKey: string;
  offered?: string;
  /** how `tandem remote status` over SSH ends */
  ssh: 'ok' | 'unreachable' | 'key_changed' | 'not_paired' | 'too_old' | 'ctl_failed';
  /** SSH answers again from the check after this many failed ones (a check again that clears) */
  sshBackAfter?: number;
  engine: 'healthy' | 'loading' | 'draining' | 'down' | 'failed';
  stoppedOnPurpose: boolean;
  api: string;
  ctl: string;
  version: string;
  mode: string;
  model: string | null;
  /** the engine's port from here: it answers, nothing does, it refuses this address, something else answers */
  http: 'ok' | 'silent' | 'refusing' | 'other';
  token: 'ok' | 'missing' | 'refused';
  /** the next Start ends with an error */
  startFails?: boolean;
  // what the mock's clock and the fixes did to it
  readyAt?: number | null;
  upgrade?: { at: number; version: string } | null;
  tokenSeq?: number;
  failed?: number;
  /** a host paired through Add engine: its conditions are the device's, read each time */
  paired?: boolean;
}

export interface PluginProblem {
  code: 'plugin_failed' | 'plugin_no_sandbox' | 'plugin_tampered' | 'plugin_untrusted';
  plugin: string;
  detail: string;
}

/** This controller and the conditions its checks read. */
export interface ControllerSim {
  chat: 'own' | 'external' | 'off';
  chatd: 'ok' | 'down' | 'silent';
  /** why chatd is down, when the watcher knows */
  chatdWhy: string | null;
  chatLog: string;
  chatUrl: string;
  chatReachable: boolean;
  /** as chatd reports it: `unavailable` is chatd's no (a cluster check that failed, with `sandboxWhy`) */
  sandbox: 'ok' | 'off' | 'unreachable' | 'no_image' | 'unavailable';
  sandboxWhy?: string;
  /** chatd's sandbox backend: a container socket, or pods in the cluster (no socket here) */
  backend?: 'docker' | 'kubernetes';
  socket: string;
  image: string;
  linux: boolean;
  /** true, or why the kernel does not cap a container's memory */
  cgroup: true | string;
  freeGb: number;
  statePath: string;
  backendsDown: { label: string; error: string; back: boolean }[];
  plugins: PluginProblem[];
  /** the sign-in's findings (SEC-26, SRV-865), with the accounts on */
  signIn?: { code: string; facts: Facts }[];
  /** the next Restart does not bring chatd back */
  restartFails?: boolean;
}

export interface PairingSim {
  address: string;
  port: number;
  at: number;
  code: string;
  facts: Facts;
}

export const HEALTH_SCENARIOS = ['mixed', 'ok', 'first', 'all', 'silent', 'external', 'cluster'] as const;
export type HealthScenario = (typeof HEALTH_SCENARIOS)[number];
export const isHealthScenario = (s: unknown): s is HealthScenario => (HEALTH_SCENARIOS as readonly unknown[]).includes(s);

const host = (o: Partial<HostSim> & Pick<HostSim, 'id' | 'alias' | 'address'>): HostSim => ({
  user: 'spark', sshPort: 22, httpPort: 8000, hostKey: fp(o.alias), ssh: 'ok', engine: 'healthy', stoppedOnPurpose: false, api: API_SPEAKS, ctl: CTL_SPEAKS, version: '0.16.0', mode: 'engine',
  model: 'Qwen3.8-27B', http: 'ok', token: 'ok', ...o,
});

const controller = (o: Partial<ControllerSim> = {}): ControllerSim => ({
  chat: 'own', chatd: 'ok', chatdWhy: null, chatLog: '/home/tandem/.local/state/tandemapp/chatd.log', chatUrl: 'http://127.0.0.1:8016', chatReachable: true, sandbox: 'ok',
  socket: '/run/user/1000/podman/podman.sock', image: 'tandem-sandbox:1', linux: true, cgroup: true, freeGb: 212.4, statePath: '/home/tandem/.local/state/tandemapp', backendsDown: [], plugins: [], ...o,
});

interface Sim {
  hosts: HostSim[];
  controller: ControllerSim;
  pairings: PairingSim[];
}

function build(name: HealthScenario, t0: number): Sim {
  const desk = () => host({ id: 'spark-desk', alias: 'spark-desk', address: '192.0.2.80' });
  switch (name) {
    case 'ok':
      return { hosts: [desk(), host({ id: 'spark-rack', alias: 'spark-rack', address: '192.0.2.81', model: 'Qwen3.8-Flash-Next' })], controller: controller(), pairings: [] };
    case 'first':
      return { hosts: [], controller: controller({ chat: 'off' }), pairings: [] };
    case 'silent':
      return { hosts: [desk()], controller: controller({ chatd: 'silent', sandbox: 'unreachable' }), pairings: [] };
    case 'external':
      return { hosts: [desk()], controller: controller({ chat: 'external', chatUrl: 'http://192.0.2.60:8016', chatReachable: false }), pairings: [] };
    case 'cluster':
      return {
        hosts: [desk()],
        controller: controller({
          backend: 'kubernetes', sandbox: 'unavailable', sandboxWhy: "the runtimeClass 'gvisor' does not exist in this cluster",
          // every finding of the sign-in, so each one is drawn (one install never has them all)
          signIn: [
            { code: 'auth_setup_pending', facts: {} },
            { code: 'auth_owner_no_password', facts: {} },
            { code: 'local_mode_proxy_seen', facts: {} },
            { code: 'legacy_env_set', facts: { variable: 'TANDEM_ADMIN_TOKEN', file: '/data/app.env' } },
            { code: 'legacy_env_invalid', facts: { variable: 'TANDEM_API_KEYS', file: 'the environment' } },
            { code: 'legacy_users_from_unreadable', facts: { variable: 'TANDEM_API_USERS_FROM', file: '/data/app.env' } },
            { code: 'bootstrap_secret_left', facts: { path: '/etc/tandemapp/setup/code' } },
          ],
        }),
        pairings: [],
      };
    case 'all':
      return {
        hosts: [
          host({ id: 'spark-far', alias: 'spark-far', address: '192.0.2.101', ssh: 'unreachable', http: 'silent' }),
          host({ id: 'spark-serving', alias: 'spark-serving', address: '192.0.2.102', ssh: 'unreachable' }),
          host({ id: 'spark-moved', alias: 'spark-moved', address: '192.0.2.103', ssh: 'key_changed', hostKey: fp('TheKeyThisFleetPinnedBefore'), offered: fp('AnotherKeyAnswersThereNow') }),
          host({ id: 'spark-reset', alias: 'spark-reset', address: '192.0.2.104', ssh: 'not_paired' }),
          host({ id: 'spark-odd', alias: 'spark-odd', address: '192.0.2.105', ssh: 'ctl_failed' }),
          host({ id: 'spark-stopped', alias: 'spark-stopped', address: '192.0.2.106', engine: 'down', stoppedOnPurpose: true }),
          host({ id: 'spark-loading', alias: 'spark-loading', address: '192.0.2.107', engine: 'loading', readyAt: t0 + 3600 }),
          host({ id: 'spark-old', alias: 'spark-old', address: '192.0.2.108', api: '1.7', ctl: '1.0', version: '0.15.2' }),
          host({ id: 'spark-next', alias: 'spark-next', address: '192.0.2.109', api: '2.0', version: '0.20.0' }),
          host({ id: 'spark-empty', alias: 'spark-empty', address: '192.0.2.110', model: null }),
          host({ id: 'spark-walled', alias: 'spark-walled', address: '192.0.2.111', http: 'silent' }),
          host({ id: 'spark-closed', alias: 'spark-closed', address: '192.0.2.112', http: 'refusing' }),
          host({ id: 'spark-other', alias: 'spark-other', address: '192.0.2.113', http: 'other' }),
          host({ id: 'spark-notoken', alias: 'spark-notoken', address: '192.0.2.114', token: 'missing' }),
          host({ id: 'gpu-box', alias: 'gpu-box', address: 'gpu-box.tailnet.example', user: 'gpu', token: 'refused', model: 'Qwen3.8-Flash-Next' }),
        ],
        controller: controller({
          chatd: 'down', chatdWhy: 'the watcher gave up for this hour', sandbox: 'no_image', cgroup: 'the memory controller is not delegated to user 1000', freeGb: 0.6,
          backendsDown: [
            { label: 'DeepSeek', error: 'HTTP 401 from /v1/models: the key is not valid', back: false },
            { label: 'rack-gpu', error: 'connection refused at 192.0.2.44:8001', back: false },
          ],
          plugins: [
            { code: 'plugin_failed', plugin: 'web-notes', detail: 'it exited five times within an hour (exit 1)' },
            { code: 'plugin_no_sandbox', plugin: 'pdf-reader', detail: 'no container runtime answers' },
            { code: 'plugin_tampered', plugin: 'calendar', detail: 'tool.py is not the file that was installed' },
            { code: 'plugin_untrusted', plugin: 'shell-tools', detail: 'it asks for engine control, and no grant allows it' },
          ],
        }),
        pairings: [
          { address: '192.0.2.84', port: 8000, at: t0 - 95, code: 'wrong_code', facts: { tries_left: 3 } },
          { address: '192.0.2.81', port: 8000, at: t0 - 210, code: 'code_spent', facts: {} },
          { address: '192.0.2.82', port: 8000, at: t0 - 260, code: 'no_window', facts: {} },
          { address: '192.0.2.88', port: 8000, at: t0 - 340, code: 'host_key_mismatch', facts: { expected: fp('TheFingerprintShownForIt'), offered: fp('TheFingerprintItOffersNow') } },
          { address: '192.0.2.93', port: 8000, at: t0 - 20, code: 'rate_limited', facts: { retry_after: 30 } },
        ],
      };
    default:
      return {
        hosts: [
          desk(),
          host({ id: 'spark-rack', alias: 'spark-rack', address: '192.0.2.81', engine: 'down', stoppedOnPurpose: true, model: 'Qwen3.8-Flash-Next' }),
          host({ id: 'gpu-box', alias: 'gpu-box', address: 'gpu-box.tailnet.example', user: 'gpu', token: 'refused', model: 'Qwen3.8-Flash-Next' }),
          host({ id: 'spark-attic', alias: 'spark-attic', address: '192.0.2.82', api: '1.7', ctl: '1.0', version: '0.15.2' }),
        ],
        controller: controller({
          sandbox: 'off', socket: '/var/run/docker.sock', freeGb: 3.2,
          backendsDown: [{ label: 'DeepSeek', error: 'timed out after 5 s', back: true }],
          plugins: [{ code: 'plugin_failed', plugin: 'web-notes', detail: 'it exited five times within an hour (exit 1)' }],
        }),
        pairings: [{ address: '192.0.2.84', port: 8000, at: t0 - 130, code: 'wrong_code', facts: { tries_left: 3 } }],
      };
  }
}

/** what the mock needs of the fleet it lives in */
export interface HealthHooks {
  /** the fleet's members that are no engine host of this page (Sparks, backends): any of them, and
   *  the first run is over */
  members(now: number): number;
  /** the engine hosts paired through Add engine */
  paired(now: number): { id: string; alias: string; address: string; user: string; sshPort: number; httpPort: number; hostKey: string; version: string; mode: string; model: string | null; state: 'healthy' | 'loading'; token: TokenHint }[];
}

type TargetBody = Omit<HealthTarget, 'state' | 'checked_at' | 'age_s' | 'cached' | 'limited'>;

class Refused extends Error {
  constructor(public reply: Reply) {
    super('refused');
  }
}
function no(code: string, facts: Facts = {}, detail?: string | null, status?: number): never {
  throw new Refused(refusal(code, facts, detail, status));
}

export class MockHealth {
  scenario: HealthScenario;
  hosts: HostSim[] = [];
  controller: ControllerSim = controller();
  refused: PairingSim[] = [];
  /** the release an upgrade goes to when the call names none (the controller's TANDEM_ENGINE_REF) */
  engineRef: string | null = null;
  private cache = new Map<string, { at: number; view: HealthTarget }>();
  private fixes = new Map<string, number[]>();
  private fails = new Map<ActionId, string>();
  private seq = 0;
  /** each target's latest fix job: it ends as `end` at `at` (the mock's clock), unless failed by hand */
  private jobs = new Map<string, { job: Record<string, unknown>; at: number; end: string; message: string | null }>();

  constructor(
    private fleet: HealthHooks,
    scenario: HealthScenario,
    private t0: number,
  ) {
    this.scenario = scenario;
    this.load(scenario);
  }

  /** Another scenario, from its start. */
  load(name: HealthScenario): void {
    const s = build(name, this.t0);
    this.scenario = name;
    this.hosts = s.hosts;
    this.controller = s.controller;
    this.refused = s.pairings;
    this.cache.clear();
    this.fixes.clear();
    this.fails.clear();
    this.jobs.clear();
  }

  /** A fix's job that has not ended fails later, with the host's words (a test's or the control's). */
  failJob(target: string, message: string): void {
    const j = this.jobs.get(target);
    if (j) Object.assign(j, { at: 0, end: 'failed', message });
  }

  /** A simulated engine host, to change one of its conditions. */
  host(id: string): HostSim {
    const h = this.hosts.find((x) => x.id === id);
    if (!h) throw new Error(`no simulated engine host ${id}`);
    this.cache.delete(id);
    return h;
  }

  /** Adds a simulated engine host. */
  add(o: Partial<HostSim> & Pick<HostSim, 'id' | 'alias' | 'address'>): HostSim {
    const h = host(o);
    this.hosts.push(h);
    return h;
  }

  /** The kept answers go: the next read checks again (after a condition was changed by hand). */
  touch(): void {
    this.cache.clear();
  }

  /** A fix answers `code` until it is cleared with null. */
  fail(action: ActionId, code: string | null): void {
    if (code === null) this.fails.delete(action);
    else if (!ENGINE_CODES[code]) throw new Error(`no engine code ${code}`);
    else this.fails.set(action, code);
  }

  /** What no click produces (mock/server.ts, POST /__mock/health). */
  control(b: Record<string, unknown>): Record<string, unknown> {
    if (b.scenario != null) {
      if (!isHealthScenario(b.scenario)) throw new Error(`scenario is one of ${HEALTH_SCENARIOS.join(', ')}`);
      this.load(b.scenario);
    }
    if (b.reset) this.load(this.scenario);
    if ('ref' in b) this.engineRef = typeof b.ref === 'string' && b.ref ? b.ref : null;
    if (b.fail != null) {
      const [action, code] = String(b.fail).split(':');
      if (!(action in HEALTH_ACTIONS)) throw new Error(`fail is <action>:<code>, an action of ${Object.keys(HEALTH_ACTIONS).join(', ')}`);
      this.fail(action as ActionId, code || null);
    }
    if (b.controller && typeof b.controller === 'object') {
      Object.assign(this.controller, b.controller);
      this.cache.delete(CONTROLLER);
    }
    if (b.host && typeof b.host === 'object') {
      const { id, ...patch } = b.host as Partial<HostSim> & { id?: string };
      Object.assign(this.host(String(id)), patch);
    }
    return { scenario: this.scenario, hosts: this.hosts.map((h) => h.id), ref: this.engineRef, fail: Object.fromEntries(this.fails) };
  }

  // ---- the routes ------------------------------------------------------------------------------
  /** `parts` after /v1/fleet: ['health', ...]; `nowMs` the mock's clock */
  handle(method: string, parts: string[], q: URLSearchParams, body: unknown, nowMs = Date.now()): Reply {
    const now = nowMs / 1000;
    const sub = parts.slice(1).map((p) => decodeURIComponent(p));
    const fresh = ['1', 'true', 'yes'].includes(q.get('fresh') ?? '');
    try {
      const view = q.get('view');
      if (method === 'GET' && sub.length === 0 && view !== null && view !== 'state') no('bad_request', {}, 'view is state, or none for everything');
      if (method === 'GET' && sub.length === 0) return { status: 200, body: view === 'state' ? this.stateView(now) : this.all(fresh, now) };
      if (method === 'GET' && sub.length === 3 && sub[1] === 'jobs') return { status: 200, body: this.readJob(sub[0], sub[2], now) };
      if (method === 'GET' && sub.length === 1) return { status: 200, body: { contract_version: CONTRACT, ...this.target(sub[0], now, { fresh }) } };
      if (method === 'POST' && sub.length === 2 && sub[1] === 'fix') return { status: 200, body: this.fix(sub[0], body, now) };
      return refusal('not_found', {}, null, 404);
    } catch (e) {
      if (e instanceof Refused) return e.reply;
      throw e;
    }
  }

  // ---- the views -------------------------------------------------------------------------------
  /** the simulated hosts, then the ones paired through Add engine */
  private rows(now: number): HostSim[] {
    const have = new Set(this.hosts.map((h) => h.id));
    const paired = this.fleet
      .paired(now)
      .filter((p) => !have.has(p.id))
      .map((p) => host({ id: p.id, alias: p.alias, address: p.address, user: p.user, sshPort: p.sshPort, httpPort: p.httpPort, hostKey: p.hostKey, version: p.version, mode: p.mode, model: p.model, engine: p.state, paired: true }));
    return [...this.hosts, ...paired];
  }

  private row(id: string, now: number): HostSim {
    const h = this.rows(now).find((x) => x.id === id);
    if (!h) no('not_found', {}, null, 404);
    return h as HostSim;
  }

  private setup(now: number): { needs_setup: boolean; engines: number; nodes: number; backends: number; first_page: 'add_engine' | null } {
    const engines = this.rows(now).length;
    const nodes = this.fleet.members(now);
    const needs = engines === 0 && nodes === 0;
    return { needs_setup: needs, engines, nodes, backends: 0, first_page: needs ? 'add_engine' : null };
  }

  private pairings(now: number): { address: string; port: number; at: number; finding: HealthFinding }[] {
    return this.refused.filter((p) => now - p.at <= 600).map((p) => ({ address: p.address, port: p.port, at: p.at, finding: healthFinding(p.code, { address: p.address, port: p.port, ...p.facts }) }));
  }

  private all(fresh: boolean, now: number): Record<string, unknown> {
    const ids = this.rows(now).map((h) => h.id);
    for (const k of [...this.cache.keys()]) if (k !== CONTROLLER && !ids.includes(k)) this.cache.delete(k);
    const ctl = this.target(CONTROLLER, now, { fresh });
    const engines = ids.map((id) => this.target(id, now, { fresh }));
    const pairings = this.pairings(now);
    const every: HealthState[] = [ctl.state, ...engines.map((e) => e.state), ...pairings.map((p) => stateOf([p.finding]))];
    return {
      contract_version: CONTRACT, checked_at: now, state: every.includes('error') ? 'error' : every.includes('warning') ? 'warning' : 'ok', setup: this.setup(now), controller: ctl, engines, pairings,
      actions: Object.fromEntries((Object.keys(HEALTH_ACTIONS) as ActionId[]).map((k) => [k, actionView(k)])),
    };
  }

  /** The menu's badge: the kept answers' state and counts; nothing is checked. */
  private stateView(now: number): Record<string, unknown> {
    const ids = this.rows(now).map((h) => h.id);
    const kept = [...this.cache.entries()].filter(([k]) => k === CONTROLLER || ids.includes(k)).map(([, c]) => c);
    const pairings = this.pairings(now);
    const found = [...kept.flatMap((c) => c.view.findings), ...pairings.map((p) => p.finding)];
    const count = (s: Severity) => found.filter((f) => f.severity === s).length;
    const oldest = kept.length ? Math.min(...kept.map((c) => c.at)) : null;
    return {
      contract_version: CONTRACT, state: kept.length || pairings.length ? stateOf(found) : 'unknown', counts: { error: count('error'), warning: count('warning'), info: count('info') },
      targets: { total: ids.length + 1, checked: kept.length }, pairings: pairings.length, checked_at: oldest, age_s: oldest === null ? null : Math.round((now - oldest) * 10) / 10,
    };
  }

  /** A fix's job read again: the target's latest, as it stands on the clock. */
  private readJob(id: string, jid: string, now: number): Record<string, unknown> {
    const alias = id === CONTROLLER ? 'TandemApp' : this.row(id, now).alias;
    const j = this.jobs.get(id);
    if (!j || j.job.id !== jid) no('job_not_found', { node: alias, job: jid }, null, 404);
    const rec = j as NonNullable<typeof j>;
    const live = ['waiting', 'acting', 'running'].includes(String(rec.job.state));
    if (live && now >= rec.at) Object.assign(rec.job, { state: rec.end, phase: 'end', ended_at: new Date(now * 1000).toISOString(), ...(rec.message ? { message: rec.message } : {}) });
    const ended = !['waiting', 'acting', 'running'].includes(String(rec.job.state));
    return { contract_version: CONTRACT, id, job: { ...rec.job }, ended };
  }

  /** One target's view: the kept one while it is young (CACHE_S; MIN_GAP_S with `fresh`), else
   *  checked now. `force` (after a fix) always checks. */
  private target(id: string, now: number, o: { fresh?: boolean; force?: boolean } = {}): HealthTarget {
    const row = id === CONTROLLER ? null : this.row(id, now);
    const c = this.cache.get(id);
    if (!o.force && c) {
      const age = now - c.at;
      if (age < (o.fresh ? MIN_GAP_S : CACHE_S)) return { ...c.view, age_s: Math.round(age * 10) / 10, cached: true, limited: !!o.fresh };
    }
    const body = row ? this.checkEngine(row, now) : this.checkController(now);
    const view: HealthTarget = { ...body, state: stateOf(body.findings), checked_at: now, age_s: 0, cached: false, limited: false };
    this.cache.set(id, { at: now, view });
    return { ...view };
  }

  // ---- an engine host --------------------------------------------------------------------------
  /** the clock moves a host on: a start that served, an upgrade that ended */
  private advance(h: HostSim, now: number): void {
    if (h.upgrade && now >= h.upgrade.at) {
      Object.assign(h, { api: API_SPEAKS, ctl: CTL_SPEAKS, version: h.upgrade.version, engine: 'loading', readyAt: h.upgrade.at + LOAD_S, upgrade: null });
    }
    if (h.engine === 'loading' && h.readyAt != null && now >= h.readyAt) Object.assign(h, { engine: 'healthy', readyAt: null });
  }

  private checkEngine(h: HostSim, now: number): TargetBody {
    this.advance(h, now);
    const found: HealthFinding[] = [];
    const checks: HealthCheck[] = [];
    if (h.ssh === 'unreachable' && h.sshBackAfter != null) {
      h.failed = (h.failed ?? 0) + 1;
      if (h.failed > h.sshBackAfter) h.ssh = 'ok';
    }
    const who = { host: h.address, port: h.sshPort, user: h.user };
    let ssh: HealthFinding | null = null;
    if (h.ssh === 'unreachable') ssh = healthFinding('ssh_unreachable', who, `ssh: connect to host ${h.address} port ${h.sshPort}: Connection timed out`);
    else if (h.ssh === 'key_changed') ssh = healthFinding('host_key_changed', { ...who, pinned: h.hostKey, offered: h.offered ?? fp(`${h.alias}OffersNow`) });
    else if (h.ssh === 'not_paired') ssh = healthFinding('not_paired', who, 'Permission denied (publickey)');
    else if (h.ssh === 'too_old') ssh = healthFinding('engine_too_old', who, 'the engine has no `tandem remote`');
    else if (h.ssh === 'ctl_failed') ssh = healthFinding('ctl_failed', who, 'status: the answer is not JSON');
    const code = ssh?.code ?? null;
    const up = ssh === null;
    checks.push(check('ssh', up, code));
    checks.push(check('host_key', up ? true : code === 'host_key_changed' ? false : null, code === 'host_key_changed' ? code : null));
    const state = up ? h.engine : null;
    if (ssh) {
      found.push(ssh);
      checks.push(check('engine', null), check('version', null), check('model', null));
    } else {
      if (state === 'down' || state === 'failed') found.push(healthFinding('engine_down', { state, host: h.address }, state === 'failed' ? 'it ended with an error; its log says why' : h.stoppedOnPurpose ? 'it was stopped on purpose' : null));
      else if (state === 'loading' || state === 'draining') found.push(healthFinding('engine_loading', { state, host: h.address }));
      checks.push(check('engine', state === 'healthy', found.find((f) => f.code === 'engine_down' || f.code === 'engine_loading')?.code ?? null));
      const v = this.versions(h);
      if (v) found.push(v);
      checks.push(check('version', v === null, v?.code ?? null));
      if (state === 'healthy' && !h.model) {
        found.push(healthFinding('no_model', { host: h.address }));
        checks.push(check('model', false, 'no_model'));
      } else checks.push(check('model', state === 'healthy' ? true : null));
    }
    if (ssh || state === 'healthy' || state === 'loading' || state === 'draining') {
      // never the token to a host whose SSH key changed or that refuses this key
      const trusted = code !== 'host_key_changed' && code !== 'not_paired';
      let [hf, tok, answered] = this.http(h, state === 'healthy', trusted);
      if (ssh) {
        if (!answered) {
          ssh.detail = [ssh.detail, `its HTTP port ${h.httpPort} does not answer either`].filter(Boolean).join('; ');
          hf = null;
        } else if (hf === null && tok === null && trusted) ssh.severity = 'warning'; // it serves with the token; only the control is cut off (never for a key changed or refused: no token went there)
      }
      if (hf) found.push(hf);
      if (tok) found.push(tok);
      checks.push(check('http', hf ? false : answered ? true : null, hf?.code ?? null));
      checks.push(check('token', answered && !hf && trusted ? tok === null : null, tok?.code ?? null));
    } else checks.push(check('http', null), check('token', null));
    const seq = h.tokenSeq ?? 0;
    const token: TokenHint = h.token === 'missing' ? { set: false, last4: null, sha256_6: null, rotated_at: null } : { set: true, last4: hex(`tok:${h.id}:${seq}`, 4), sha256_6: hex(`sha:${h.id}:${seq}`, 6), rotated_at: Math.floor(this.t0) + seq };
    return {
      id: h.id, kind: 'engine', alias: h.alias, address: h.address, ssh: { host: h.address, port: h.sshPort, user: h.user }, http: { port: h.httpPort, token }, host_key: h.hostKey,
      engine_version: up ? h.version : null, engine_api: up ? h.api : null, mode: up ? h.mode : null, engine_state: state, model: up && state === 'healthy' ? h.model : null, findings: found, checks,
    };
  }

  private versions(h: HostSim): HealthFinding | null {
    const [wantApi, wantCtl] = [ver(API_SPEAKS)!, ver(CTL_SPEAKS)!];
    const [api, ctl] = [ver(h.api), ver(h.ctl)];
    if (!api || api[0] !== wantApi[0]) {
      if (api && api[0] < wantApi[0]) return healthFinding('engine_too_old', { host: h.address }, `engine API ${h.api}; TandemApp speaks ${API_SPEAKS}`);
      return healthFinding('engine_api_unsupported', { host: h.address, engine_api: h.api, speaks: API_SPEAKS });
    }
    if (ctl && ctl[0] !== wantCtl[0]) return healthFinding('engine_api_unsupported', { host: h.address, engine_api: h.api, speaks: API_SPEAKS }, `its control speaks ${h.ctl}, TandemApp ${CTL_SPEAKS}`);
    const old: string[] = [];
    if (older(api, wantApi)) old.push(`engine API ${h.api}, TandemApp is made for ${API_SPEAKS}`);
    if (ctl && older(ctl, wantCtl)) old.push(`control ${h.ctl}, TandemApp is made for ${CTL_SPEAKS}`);
    return old.length ? healthFinding('engine_too_old', { host: h.address }, old.join('; ')) : null;
  }

  /** [the HTTP finding, the token finding, whether the port answered] */
  private http(h: HostSim, serving: boolean, trusted: boolean): [HealthFinding | null, HealthFinding | null, boolean] {
    const listens = h.http !== 'silent' && h.engine !== 'down' && h.engine !== 'failed';
    if (!listens) return [serving ? healthFinding('http_blocked', { port: h.httpPort, address: h.address, host: h.address }) : null, null, false];
    if (h.http === 'refusing') return [healthFinding('not_allowed', { address: h.address, port: h.httpPort }), null, true];
    if (h.http === 'other') return [healthFinding('not_an_engine', { address: h.address, port: h.httpPort }, '/health answered HTTP 404'), null, true];
    if (!trusted) return [null, null, true];
    if (h.token === 'missing') return [null, healthFinding('token_missing', { host: h.address }), true];
    if (h.token === 'refused') return [null, healthFinding('token_refused', { host: h.address }), true];
    return [null, null, true];
  }

  // ---- the controller --------------------------------------------------------------------------
  private checkController(now: number): TargetBody {
    const c = this.controller;
    const found: HealthFinding[] = [];
    const checks: HealthCheck[] = [];
    const own = c.chat === 'own';
    let sock: string | null = null;
    if (c.chat === 'off') checks.push(check('chatd', null), check('sandbox', null));
    else if (own) {
      if (c.chatd === 'down') found.push(healthFinding('chatd_down', { path: c.chatLog }, c.chatdWhy));
      else if (c.chatd === 'silent') found.push(healthFinding('chatd_silent', { path: c.chatLog }));
      checks.push(check('chatd', c.chatd === 'ok', c.chatd === 'ok' ? null : `chatd_${c.chatd}`));
      const pods = c.backend === 'kubernetes';
      sock = pods ? null : c.socket;
      const sb =
        c.sandbox === 'unavailable'
          ? healthFinding('sandbox_unavailable', {}, c.sandboxWhy ?? 'chatd says its sandboxes are not ready')
          : pods
            ? null
            : c.sandbox === 'off'
              ? healthFinding('sandbox_off', { path: c.socket })
              : c.sandbox === 'unreachable'
                ? healthFinding('sandbox_unreachable', { path: c.socket }, 'this user may not open the socket')
                : c.sandbox === 'no_image'
                  ? healthFinding('sandbox_no_image', { image: c.image })
                  : null;
      if (sb) found.push(sb);
      checks.push(check('sandbox', sb === null, sb?.code ?? null));
    } else {
      const u = new URL(c.chatUrl);
      if (!c.chatReachable) found.push(healthFinding('chat_unreachable', { address: `${u.hostname}:${u.port}` }));
      checks.push(check('chatd', c.chatReachable, c.chatReachable ? null : 'chat_unreachable'), check('sandbox', null));
    }
    // the memory cgroup, where an own chatd's sandboxes run: a socket has to be there
    if (own && c.linux && c.backend !== 'kubernetes' && c.sandbox !== 'off') {
      if (c.cgroup !== true) found.push(healthFinding('no_memory_cgroup', {}, c.cgroup));
      checks.push(check('memory_cgroup', c.cgroup === true, c.cgroup === true ? null : 'no_memory_cgroup'));
    } else checks.push(check('memory_cgroup', null));
    const low = c.freeGb < DISK_WARN_GB;
    if (low) found.push(healthFinding('disk_low', { path: c.statePath, free_gb: Math.round(c.freeGb * 10) / 10, need_gb: DISK_WARN_GB }, null, { severity: c.freeGb < DISK_ERROR_GB ? 'error' : 'warning' }));
    checks.push(check('disk', !low, low ? 'disk_low' : null));
    for (const b of c.backendsDown) found.push(healthFinding('backend_down', { backend: b.label }, b.error.slice(0, 200)));
    checks.push(check('backends', !c.backendsDown.length, c.backendsDown.length ? 'backend_down' : null));
    for (const p of c.plugins) found.push(healthFinding(p.code, { plugin: p.plugin }, p.detail.slice(0, 300)));
    for (const f of c.signIn ?? []) found.push(healthFinding(f.code, f.facts));
    const rank = (code: string) => (['error', 'warning', 'info'] as Severity[]).indexOf(HEALTH_FINDINGS[code]?.[0] ?? 'error');
    const worst = [...c.plugins].sort((a, b) => rank(a.code) - rank(b.code))[0];
    checks.push(check('plugins', !c.plugins.length, worst?.code ?? null));
    const setup = this.setup(now);
    if (setup.needs_setup) found.push(healthFinding('no_engine'));
    checks.push(check('engines', !setup.needs_setup, setup.needs_setup ? 'no_engine' : null));
    return { id: CONTROLLER, kind: 'controller', alias: 'TandemApp', address: null, chat: c.chat, sandbox_backend: own ? (c.backend ?? 'docker') : null, sandbox_socket: sock, findings: found, checks };
  }

  // ---- the fixes -------------------------------------------------------------------------------
  private fields(body: unknown): Record<string, unknown> {
    if (typeof body !== 'object' || body === null || Array.isArray(body)) no('bad_request', {}, 'the body is a JSON object');
    const b = body as Record<string, unknown>;
    const extra = Object.keys(b)
      .filter((k) => !['action', 'ref', 'confirm', 'when', 'instance'].includes(k))
      .sort();
    if (extra.length) no('bad_request', {}, `unknown field '${extra[0]}'`);
    if (b.action == null) no('bad_request', {}, 'action is missing');
    return b;
  }

  private rate(id: string, alias: string, now: number): void {
    const q = (this.fixes.get(id) ?? []).filter((t) => now - t <= 60);
    if (q.length >= FIXES_PER_MIN) {
      this.fixes.set(id, q);
      no('too_many_fixes', { node: alias, retry_after: Math.max(1, Math.ceil(60 - (now - q[0]))) });
    }
    q.push(now);
    this.fixes.set(id, q);
  }

  private fix(id: string, body: unknown, now: number): Record<string, unknown> {
    const b = this.fields(body);
    if (typeof b.action !== 'string' || !(b.action in HEALTH_ACTIONS)) no('bad_request', {}, `action is one of ${Object.keys(HEALTH_ACTIONS).join(', ')}`);
    const action = b.action as ActionId;
    const when = b.when ?? 'idle';
    if (when !== 'now' && when !== 'idle') no('bad_request', {}, 'when is now or idle');
    const inst = b.instance;
    if (inst !== undefined && (typeof inst !== 'string' || !inst.length || inst.length > 200)) no('bad_request', {}, 'instance is the backend or plugin a finding names');
    const row = id === CONTROLLER ? null : this.row(id, now);
    const alias = row ? row.alias : 'TandemApp';
    const before = this.target(id, now);
    const offered = [...new Set([...before.findings.flatMap((f) => (f.action ? [f.action.id] : [])), 'retry' as ActionId])].sort();
    if (!offered.includes(action)) no('not_offered', { node: alias }, `it offers ${offered.join(', ')}`);
    this.rate(id, alias, now);
    const forced = this.fails.get(action);
    if (forced) no(forced, { node: alias, host: row?.address, port: row?.sshPort, user: row?.user, address: row?.address }, ENGINE_CODES[forced][1].includes('{detail}') ? 'the other side gave no reason' : null);
    // what the fix is for: each finding that offers it, one instance each (narrowed to the one named)
    const forKeys = before.findings.filter((f) => f.action?.id === action && (inst === undefined || instanceOf(f) === inst)).map((f) => [f.code, instanceOf(f)] as const);
    const isFor = (f: HealthFinding): boolean => forKeys.some(([c, i]) => c === f.code && i === instanceOf(f));
    const out: Record<string, unknown> = { contract_version: CONTRACT, id, action, done: true, needs_confirm: false, job: null, result: null, guide: null };
    if (action === 'retry') {
      // the backends the poll found down are probed now
      if (!row) this.controller.backendsDown = this.controller.backendsDown.filter((x) => !x.back);
    } else if (action === 're_pair' || action === 'pick_model' || action === 'add_engine') out.guide = this.guide(action, row, before);
    else if (action === 'restart_chatd') {
      const ok = !this.controller.restartFails;
      if (ok) Object.assign(this.controller, { chatd: 'ok', chatdWhy: null });
      out.job = { id: `chat-${++this.seq}`, action: 'restart', state: ok ? 'done' : 'failed', started_at: now, ended_at: now, rc: ok ? 0 : 1, message: ok ? null : 'chatd ended at once: its port 8016 is in use' };
      out.done = ok;
    } else {
      const h = row as HostSim;
      this.overSsh(h);
      if (action === 'start') {
        const fails = !!h.startFails;
        Object.assign(h, fails ? { engine: 'failed', stoppedOnPurpose: false } : { engine: 'loading', stoppedOnPurpose: false, readyAt: now + START_S });
        out.job = this.job('start', now, fails ? 'failed' : 'acting', 'now', fails ? 'the engine ended while it loaded the model' : null);
      } else if (action === 'rotate_token') {
        h.tokenSeq = (h.tokenSeq ?? 0) + 1;
        h.token = 'ok';
        out.result = { rotated: true, stored: true, hint: { set: true, last4: hex(`tok:${h.id}:${h.tokenSeq}`, 4), sha256_6: hex(`sha:${h.id}:${h.tokenSeq}`, 6), rotated_at: Math.floor(now) } };
      } else Object.assign(out, this.upgrade(h, b, when as 'now' | 'idle', now));
    }
    if (out.job && typeof out.job === 'object') {
      const j = out.job as Record<string, unknown>;
      const h = row as HostSim | null;
      const at = action === 'start' ? (h?.readyAt ?? now) : action === 'upgrade' ? (h?.upgrade?.at ?? now) : now;
      this.jobs.set(id, { job: { ...j }, at, end: 'done', message: null });
    }
    const after = this.target(id, now, { force: true });
    const still = after.findings.filter(isFor);
    const instances = forKeys.map(([code, instance]) => ({ code, instance, cleared: !still.some((f) => f.code === code && instanceOf(f) === instance) }));
    return { ...out, target: after, finding: still[0] ?? null, cleared: !still.length, instances };
  }

  /** a fix that goes over SSH needs SSH: else the control's own refusal */
  private overSsh(h: HostSim): void {
    const who = { host: h.address, port: h.sshPort, user: h.user };
    if (h.ssh === 'unreachable') no('ssh_unreachable', who, `ssh: connect to host ${h.address} port ${h.sshPort}: Connection timed out`);
    if (h.ssh === 'key_changed') no('host_key_changed', { ...who, pinned: h.hostKey, offered: h.offered ?? fp(`${h.alias}OffersNow`) });
    if (h.ssh === 'not_paired') no('not_paired', who);
  }

  private job(kind: string, now: number, state: string, mode: 'now' | 'idle', message: string | null = null): Record<string, unknown> {
    const stamp = new Date(now * 1000).toISOString();
    const ended = state === 'done' || state === 'failed';
    return {
      id: `${stamp.slice(0, 19).replace(/[-:]/g, '')}-${hex(`${kind}:${now}:${++this.seq}`, 6)}`, kind, state, phase: ended ? 'end' : state === 'waiting' ? 'wait' : kind, when: { mode },
      created_at: stamp, ended_at: ended ? stamp : null, message, waiting_for: state === 'waiting' ? 'the engine to be idle' : null,
    };
  }

  private upgrade(h: HostSim, b: Record<string, unknown>, when: 'now' | 'idle', now: number): Record<string, unknown> {
    const ref = b.ref || this.engineRef;
    if (typeof ref !== 'string' || !REF_RE.test(ref)) no('bad_request', {}, 'ref is the engine release to upgrade to (a tag such as v0.16.0), or set TANDEM_ENGINE_REF');
    const named = { ref, ref_source: b.ref ? 'asked' : 'default', default_ref: this.engineRef };
    const to = (ref as string).replace(/^v/, '');
    const confirm = hex(`plan:${h.id}:${ref}`, 16);
    const plan: UpgradePlan & Record<string, unknown> = {
      from: { commit: hex(`from:${h.id}`, 40), version: h.version, engine_api: h.api, ctl: h.ctl },
      to: { source: 'ref', ref: ref as string, commit: hex(`to:${ref}`, 40), version: to, engine_api: API_SPEAKS, ctl: CTL_SPEAKS },
      venv: 'synced', keeps: ['run/local.env', 'run/setup.env', 'the weights'],
      steps: ['Fetch the release', 'Install it beside the one that runs', 'Restart the engine with it', 'Check that it answers'], rollback: 'the release that runs now',
      warnings: h.model ? [`${h.model} is loaded again after the restart: some minutes without answers`] : [], confirm,
    };
    if (b.confirm == null) return { done: false, needs_confirm: true, result: { ...named, plan } };
    if (typeof b.confirm !== 'string' || !CONFIRM_RE.test(b.confirm)) no('bad_request', {}, 'confirm is the plan’s confirm token');
    if (b.confirm !== confirm) no('ctl_failed', { host: h.address }, 'the confirm token is not this plan’s');
    h.upgrade = { at: now + (when === 'idle' ? UPGRADE_IDLE_S : UPGRADE_S), version: to };
    return { job: { ...this.job('upgrade', now, when === 'idle' ? 'waiting' : 'acting', when), upgrade: { source: 'ref', ref } }, result: { ...named, plan } };
  }

  private guide(action: ActionId, row: HostSim | null, view: HealthTarget): HealthGuide {
    if (action === 're_pair') {
      const address = row?.address ?? view.address;
      const hk = view.findings.find((f) => f.code === 'host_key_changed')?.facts ?? {};
      return {
        open: 'add_engine', engine: row?.id ?? null, address, pinned: hk.pinned ?? null, offered: hk.offered ?? null,
        steps: [
          'On the engine host, run `tandem pair`: it prints a code, the SSH host key\'s fingerprint and the address.',
          'Compare that fingerprint with the one shown here; if they differ, do not pair.',
          `Open Add engine, pick the engine or type ${address}, and type the code.`,
        ],
      };
    }
    if (action === 'pick_model') return { open: 'model', engine: row?.id ?? null, address: null, pinned: null, offered: null, steps: ['Open Add engine\'s model step for this engine and pick a model; the page shows its download.'] };
    return {
      open: 'add_engine', engine: null, address: null, pinned: null, offered: null,
      steps: ['Install the engine on its host with its one command; it prints a code, the SSH host key\'s fingerprint and the address.', 'Open Add engine, pick the engine or type its address, and type the code.'],
    };
  }
}
