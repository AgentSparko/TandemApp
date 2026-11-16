// The hooks and the decision providers in the mock (VIS-282): the registry both the settings
// interface's hooks scope (mock/settings.ts) and the two admin APIs read and write, as the
// controller has it (proxy/hookstore.py, hookapi.py, providerstore.py, providerapi.py).
//
//   /v1/fleet/hooks                 list, stages, create (201, an http hook's secret once), get,
//                                   patch, delete, enable, disable, mode, test, calls, secret
//   /v1/fleet/decision-providers    list, create (201), get (?health=1), patch, delete, enable,
//                                   disable, mode, test, health (?force=1), calls, report
//
// A hook's receiver does not exist here: each hook carries the answer it gives (`answer`), how it
// behaves (`behave`) and how long it takes (`base_ms`). `request()` walks one made-up request
// through the stages as the runner would: the hooks in scope by role and priority, a deny or a
// closed failure ends the path, five failures in a row open the breaker for 30 s. `ask()` puts one
// of the conductor's questions to the providers. Both write the journals the pages read.
//
// What it holds: nothing until `seed(true)` (three hooks: `audit` and `export` at
// request.finished, `guard` at request.received; two providers: `classes`, `own`); `seed('demo')`
// adds a transform, a router, a guard in shadow whose breaker is open, a decision.ask adapter, an
// A/B provider and a hook provider, with an hour of traffic. `control()` (POST /__mock/hooks) sets
// what no click produces:
//   {fail: "<route>:<error type>" | null}  a route answers that refusal until cleared (hooks:
//                                          list, stages, create, get, patch, delete, test, calls,
//                                          secret; providers: p-list, p-create, p-get, p-patch,
//                                          p-delete, p-test, p-health, p-calls, p-report)
//   {trust: true | false}                  the caller has pairing's rights (the full payload, a
//                                          new endpoint for a full hook, a secret's rotation)
//   {race: "<hook or provider id>"}        it changes on another screen before the next PATCH (409 conflict)
//   {behave: {"<hook id>": "ok" | "timeout" | "error" | "invalid"}}   how that hook's receiver answers
//   {provider: {"<id>": "ok" | "timeout" | "unreachable" | "http_401" | "unknown_label" | "down"}}
//   {requests: N}                          N made-up requests pass the hooks now
//   {asks: N}                              N questions are put to the providers now
//   {replay: true | false}                 recorded answers replace every provider

import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { Answer, Arm, Breaker, Bucket, Grant, ProviderGrant, Health, Hook, HookCall, HookMetrics, HookScope, Outcome, Probe, Provider, ProviderCall, ProviderKind, Role, StageTable } from '../src/api/hooks.ts';

export type Reply = { status: number; body: unknown };

const HOOKS_V1 = (f: string): string => fileURLToPath(new URL(`../../contracts/hooks-v1/${f}`, import.meta.url));
/** the stage table as GET /v1/fleet/hooks/stages answers it: stages.json and each stage's sample event (SRV-849) */
export const STAGES: StageTable = JSON.parse(readFileSync(HOOKS_V1('stages.json'), 'utf8'));
STAGES.samples = Object.fromEntries(
  Object.keys(STAGES.stages).map((st) => {
    const full = HOOKS_V1(`examples/${st}.full.json`);
    const event = JSON.parse(readFileSync(HOOKS_V1(`examples/${st}.meta.json`), 'utf8')).event as Record<string, unknown>;
    return [st, { event, content: existsSync(full) ? JSON.parse(readFileSync(full, 'utf8')).event.content : null }];
  }),
);
export const QUESTIONS: Record<string, string> = {
  classify: 'the router: which class a new user message is',
  packages: 'the router and the library: which package fits the turn',
  deliverable: 'the router: what the turn is to deliver',
  memory_intent: 'the memory notes: does the message ask to remember, forget or recall',
  memory_gate: 'the memory writer: is there anything worth a note in the session',
  judge: 'the tester’s questions after a turn: a failure in a tool output, a build’s end',
};
const OPTIONS: Record<string, string[]> = { classify: ['code_edit', 'chat', 'research'], packages: ['core', 'none'], deliverable: ['answer', 'file'], memory_intent: ['yes', 'no'], memory_gate: ['yes', 'no'], judge: ['clean', 'failure', 'unclear'] };
export const BUILTIN_NAMES = ['deny', 'fallback', 'noop', 'route', 'set'];
const SCOPE_KEYS = ['models', 'backends', 'profiles', 'users', 'sessions', 'client_kinds'] as const;
const HOOK_FIELDS = ['name', 'kind', 'stages', 'role', 'priority', 'mode', 'scope', 'policy', 'timeout_ms', 'payload', 'endpoint', 'target', 'options', 'enabled'];
const PROVIDER_FIELDS = ['name', 'kind', 'endpoint', 'hook_id', 'target', 'model', 'timeout_ms', 'questions', 'mode', 'ab_percent', 'sticky', 'enabled', 'api_key'];
const ID_RE = /^[a-z0-9][a-z0-9._-]{0,47}$/;
const PROVIDER_TIMEOUT_MAX = 5000;
const JOURNAL = 500;
const BREAKER_FAILS = 5;
const BREAKER_OPEN_MS = 30_000;
const PROBE_TTL_MS = 10_000;
const FAILURES: Outcome[] = ['timeout', 'error', 'invalid'];

/** every refusal of the two APIs with its HTTP status */
export const HOOK_ERRORS: Record<string, number> = { bad_request: 400, unauthorized: 401, trust_required: 403, not_found: 404, exists: 409, conflict: 409, confirm_needed: 409, no_secret: 409, question_taken: 409, invalid: 422, kind_unavailable: 422, unavailable: 503 };
const SENTENCE: Record<string, string> = {
  unauthorized: 'a bearer token or the dashboard session is needed',
  trust_required: 'the full payload and a hook’s secret take pairing’s rights: set an admin password (App settings) to do this from another machine, or ask from this one',
  conflict: 'the hook changed meanwhile; read it again',
  unavailable: 'the config database is not open',
  not_found: 'no such hook',
  exists: 'a hook with that id exists',
  no_secret: 'only an http hook has a secret',
  question_taken: 'the provider ‘classes’ serves classify on: one provider a question on or ab (any number in shadow)',
  kind_unavailable: 'kind ‘mcp’ is not available in this version: it arrives with a later version (a plugin’s hooks are kind plugin). Use http, builtin or plugin.',
  invalid: 'name: 1 to 80 characters',
  bad_request: 'the body must be a JSON object',
  p_trust: 'a provider the questions’ text goes to, and each new place it goes, take pairing’s rights: set an admin password (App settings) to do this from another machine, or ask from this one',
};

class Refuse extends Error {
  constructor(
    public status: number,
    public type: string,
    message: string,
    public facts: Record<string, unknown> = {},
  ) {
    super(message);
  }
}
function no(type: string, message: string, field?: string, extra: Record<string, unknown> = {}): never {
  throw new Refuse(HOOK_ERRORS[type] ?? 400, type, message, { ...(field ? { field } : {}), ...extra });
}
const bad = (message: string, field?: string): never => no('invalid', message, field);
const copy = <T>(v: T): T => JSON.parse(JSON.stringify(v ?? null)) as T;
const sha = (v: string): string => createHash('sha256').update(v).digest('hex');
/** a deterministic 0..1 from a text: the mock's dice */
const dice = (text: string): number => parseInt(sha(text).slice(0, 8), 16) / 0xffffffff;
const rank = (ds: number[], p: number): number => ds[Math.max(0, Math.ceil(p * ds.length) - 1)];
const round1 = (n: number): number => Math.round(n * 10) / 10;

export interface MockHook {
  name: string;
  kind: 'http' | 'builtin';
  /** where its payload goes, and whether that is on the LAN */
  host: string | null;
  lan: boolean;
  /** the switches the settings interface holds too: enabled, mode, priority, scope, policy, timeout_ms, payload */
  vals: Record<string, unknown>;
  secret: string | null;
  stages: string[];
  role: Role;
  endpoint: string | null;
  target: string | null;
  options: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  updated_by: string | null;
  version: number;
  grant: Grant | null;
  rotated_at: number | null;
  previous_until: number | null;
  /** the receiver's stand-in: what it answers, how, and in about how many ms */
  answer: Record<string, unknown> | null;
  behave: 'ok' | 'timeout' | 'error' | 'invalid';
  base_ms: number;
  journal: HookCall[];
  durations: number[];
  calls: number;
  failures: number;
  timeouts: number;
  skipped: number;
  outcomes: Record<string, number>;
  fails: number;
  open_until: number;
  opened: number;
}

export interface MockProvider {
  name: string;
  kind: ProviderKind;
  /** the switches the settings interface holds too: enabled, mode, ab_percent, sticky, questions */
  vals: Record<string, unknown>;
  /** '*' (its own) or a question id -> the key */
  keys: Record<string, string>;
  key_at: Record<string, number>;
  endpoint: string | null;
  hook_id: string | null;
  model: string;
  timeout_ms: number;
  created_at: number;
  updated_at: number;
  updated_by: string | null;
  version: number;
  /** the service's stand-in: how it answers, and how often it agrees with the built-in decider */
  behave: 'ok' | 'timeout' | 'unreachable' | 'http_401' | 'unknown_label' | 'down';
  agree: number;
  base_ms: number;
  fails: number;
  open_until: number;
  opened: number;
  probed: Record<string, number>;
  /** SEC-16: where its questions' text may go (a systemone-http provider); null before a grant */
  grant: ProviderGrant | null;
}

function hostOf(url: string | null): string | null {
  try {
    return url ? new URL(url).hostname.toLowerCase() : null;
  } catch {
    return null;
  }
}
/** on the LAN: loopback, a private or tailnet address, a bare name, or a .lan, .local,
 *  .home.arpa or .internal name (proxy/hookcall.py; a public name does not resolve here) */
export function hostIsLan(host: string | null): boolean {
  const h = (host ?? '').replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!h) return false;
  if (/^\d+\.\d+\.\d+\.\d+$/.test(h)) {
    const [a, b] = h.split('.').map(Number);
    return a === 10 || a === 127 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254);
  }
  return h === 'localhost' || h === '::1' || !h.includes('.') || /\.(lan|local|home\.arpa|internal)$/.test(h);
}
function urlError(url: unknown): string | null {
  if (typeof url !== 'string' || !url || url.length > 2048) return 'an http or https URL of at most 2048 characters';
  if (/\s/.test(url)) return 'no spaces in the URL';
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return 'the URL does not parse';
  }
  if ((u.protocol !== 'http:' && u.protocol !== 'https:') || !u.hostname) return 'an http or https URL with a host';
  if (u.username || u.password) return 'no user or password in the URL: the call is signed instead';
  return u.hash ? 'no #fragment in the URL' : null;
}

function hook(now: number, o: Partial<MockHook> & Pick<MockHook, 'name' | 'kind' | 'stages' | 'role'> & { vals?: Record<string, unknown> }): MockHook {
  const host = o.kind === 'http' ? hostOf(o.endpoint ?? null) : null;
  return {
    host, lan: o.kind === 'http' ? hostIsLan(host) : true, secret: null, endpoint: null, target: null, options: {}, created_at: now - 86_400_000, updated_at: now - 86_400_000, updated_by: 'admin', version: 1, grant: null,
    rotated_at: null, previous_until: null, answer: null, behave: 'ok', base_ms: o.kind === 'http' ? 14 : 0.2, journal: [], durations: [], calls: 0, failures: 0, timeouts: 0, skipped: 0, outcomes: {}, fails: 0, open_until: 0, opened: 0,
    ...o,
    vals: { enabled: true, mode: 'on', priority: 100, scope: {}, policy: null, timeout_ms: null, payload: 'meta', ...(o.vals ?? {}) },
  };
}
function provider(now: number, o: Partial<MockProvider> & Pick<MockProvider, 'name' | 'kind'> & { vals?: Record<string, unknown> }): MockProvider {
  return {
    keys: {}, key_at: {}, endpoint: null, hook_id: null, model: 'auto', timeout_ms: 400, created_at: now - 86_400_000, updated_at: now - 86_400_000, updated_by: 'admin', version: 1, behave: 'ok', agree: 0.92, base_ms: 21, fails: 0, open_until: 0, opened: 0, probed: {}, grant: null,
    ...o,
    vals: { enabled: true, mode: 'shadow', ab_percent: 0, sticky: 'session', questions: {}, ...(o.vals ?? {}) },
  };
}

function sampleHooks(now: number): Record<string, MockHook> {
  return {
    audit: hook(now, { name: 'Audit log', kind: 'http', endpoint: 'http://audit.lan:9300/hook', role: 'observer', stages: ['request.finished'], vals: { priority: 50 }, secret: 'hks_mock-audit-0001', base_ms: 9 }),
    export: hook(now, { name: 'Export to the archive', kind: 'http', endpoint: 'https://archive.example.com/hooks/export', role: 'observer', stages: ['response.after_engine', 'request.finished'], vals: { mode: 'shadow', priority: 200, policy: 'open', timeout_ms: 500, payload: 'redacted' }, secret: 'hks_mock-export-0002', base_ms: 48 }),
    guard: hook(now, { name: 'No big model for guests', kind: 'builtin', target: 'deny', options: { reason: 'ask the admin for this model' }, role: 'guard', stages: ['request.received'], vals: { priority: 10, scope: { models: ['big-*'], users: ['guest*'] }, policy: 'closed' } }),
  };
}
function sampleProviders(now: number): Record<string, MockProvider> {
  return {
    classes: provider(now, { name: 'Classes', kind: 'systemone-http', endpoint: 'http://decider.lan:8720', vals: { mode: 'on', questions: { classify: {}, judge: { model: 'judge-v2' } } }, keys: { '*': 'sk-mock-classes-0003' }, key_at: { '*': now - 86_400_000 } }),
    own: provider(now, { name: 'Own decider', kind: 'builtin', vals: { questions: { memory_gate: {} } }, base_ms: 17 }),
  };
}
function demoHooks(now: number): Record<string, MockHook> {
  return {
    'temperature-cap': hook(now, { name: 'Temperature cap', kind: 'http', endpoint: 'http://hooks.lan:9300/cap', role: 'transform', stages: ['request.before_engine'], vals: { priority: 10, scope: { models: ['example-*', 'qwen*'] } }, secret: 'hks_mock-cap-0004', answer: { action: 'modify', patch: { temperature: 0.3 }, reason: 'capped' }, base_ms: 12 }),
    'vision-route': hook(now, { name: 'Vision goes to the cloud', kind: 'builtin', target: 'route', options: { backend: 'cloud' }, role: 'decider', stages: ['route.select'], vals: { priority: 20, scope: { models: ['vision-*'] } } }),
    'pii-guard': hook(now, { name: 'Personal data check', kind: 'http', endpoint: 'http://hooks.lan:9300/pii', role: 'guard', stages: ['request.before_engine'], vals: { mode: 'shadow', priority: 30, payload: 'redacted', timeout_ms: 200 }, secret: 'hks_mock-pii-0005', answer: { action: 'deny', reason: 'the message holds a phone number' }, base_ms: 86 }),
    'classes-adapter': hook(now, { name: 'Classifier adapter', kind: 'http', endpoint: 'http://hooks.lan:9300/classify', role: 'decider', stages: ['decision.ask'], vals: { priority: 100, payload: 'redacted' }, secret: 'hks_mock-adapter-0006', answer: { action: 'decide', decision: { answer: { choice: 'core', confidence: 0.91 } } }, base_ms: 31 }),
  };
}
function demoProviders(now: number): Record<string, MockProvider> {
  return {
    intent: provider(now, { name: 'Memory intent', kind: 'systemone-http', endpoint: 'http://intent.lan:8721', vals: { mode: 'ab', ab_percent: 30, questions: { memory_intent: { timeout_ms: 250 } } }, agree: 0.88, base_ms: 34 }),
    adapter: provider(now, { name: 'Packages through a hook', kind: 'hook', hook_id: 'classes-adapter', vals: { questions: { packages: {} } }, agree: 0.81, base_ms: 31 }),
  };
}

const globMatch = (pattern: string, text: string): boolean => new RegExp(`^${pattern.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`).test(text);
const ROLES: Role[] = ['transform', 'guard', 'decider', 'observer'];

export class MockHooks {
  hooks: Record<string, MockHook> = {};
  providers: Record<string, MockProvider> = {};
  /** the caller has pairing's rights (this machine, or the admin) */
  trust = true;
  /** recorded answers replace every provider */
  replay = false;
  /** the clock of the last request, for what the settings scope writes */
  now: number;
  private fails = new Map<string, string>();
  private racing = new Set<string>();
  private ring: ProviderCall[] = [];
  private seq = 0;

  constructor(now = Date.now()) {
    this.now = now;
  }

  get plugged(): boolean {
    return Object.keys(this.hooks).length + Object.keys(this.providers).length > 0;
  }

  /** The registry holds nothing, the samples, or the samples and the demo with its traffic. */
  seed(level: boolean | 'demo'): void {
    const now = this.now;
    this.ring = [];
    this.hooks = level ? sampleHooks(now) : {};
    this.providers = level ? sampleProviders(now) : {};
    if (level !== 'demo') return;
    Object.assign(this.hooks, demoHooks(now));
    Object.assign(this.providers, demoProviders(now));
    const models = ['example-model', 'example-model', 'qwen-next', 'vision-1', 'big-1'];
    for (let i = 60; i > 0; i--) {
      // the personal data check's receiver goes away for the last few requests: its breaker opens
      this.hooks['pii-guard'].behave = i <= 7 ? 'timeout' : 'ok';
      this.request(now - i * 55_000 - 4000, { model: models[i % models.length], user: i % 5 === 4 ? 'guest-7' : 'owner', error: i % 17 === 0 });
    }
    const qs = Object.keys(QUESTIONS);
    for (let i = 240; i > 0; i--) this.ask(now - i * 340_000, { question: qs[i % qs.length], session: `s-${i % 23}` });
    this.providers.intent.behave = 'timeout';
    for (let i = 3; i > 0; i--) this.ask(now - i * 9000, { question: 'memory_intent', session: `s-${i + 40}` });
    this.providers.intent.behave = 'ok';
  }

  control(b: { fail?: string | null; trust?: boolean; race?: string; behave?: Record<string, MockHook['behave']>; provider?: Record<string, MockProvider['behave']>; requests?: number; asks?: number; replay?: boolean }): Record<string, unknown> {
    if (b.fail === null) this.fails.clear();
    else if (typeof b.fail === 'string') {
      const [route, type] = b.fail.split(':');
      if (!(type in HOOK_ERRORS)) throw new Error(`no hooks error ${type}`);
      this.fails.set(route, type);
    }
    if (typeof b.trust === 'boolean') this.trust = b.trust;
    if (typeof b.race === 'string') this.racing.add(b.race);
    for (const [id, how] of Object.entries(b.behave ?? {})) if (this.hooks[id]) this.hooks[id].behave = how;
    for (const [id, how] of Object.entries(b.provider ?? {})) if (this.providers[id]) this.providers[id].behave = how;
    for (let i = 0; i < (b.requests ?? 0); i++) this.request(this.now + i);
    for (let i = 0; i < (b.asks ?? 0); i++) this.ask(this.now + i, { question: Object.keys(QUESTIONS)[i % 6], session: `s-${i}` });
    if (typeof b.replay === 'boolean') this.replay = b.replay;
    return { fails: Object.fromEntries(this.fails), trust: this.trust, hooks: Object.keys(this.hooks), providers: Object.keys(this.providers), replay: this.replay };
  }

  private forced(route: string): void {
    const type = this.fails.get(route);
    if (!type) return;
    if (type === 'confirm_needed') no(type, 'the full payload (prompts and answers as they are) would go to archive.example.com, outside the LAN: send the same request again with this confirm to grant it', 'payload', { confirm: 'c'.repeat(32), destination: 'archive.example.com', lan: false });
    no(type, SENTENCE[type] ?? type, type === 'invalid' ? 'name' : type === 'kind_unavailable' ? 'kind' : type === 'question_taken' ? 'questions' : undefined);
  }

  /** What the settings scope wrote for a hook: its version moves, and a full payload is a grant. */
  touched(id: string, who = 'admin'): void {
    const h = this.hooks[id];
    if (!h) return;
    h.version++;
    h.updated_at = this.now;
    h.updated_by = who;
    if (h.vals.payload !== 'full') h.grant = null;
    else if (!h.grant || h.grant.destination !== h.host) h.grant = { level: 'full', by: who, at: this.now, destination: h.host, lan: h.lan, confirmed_by: h.lan ? null : who, confirmed_at: h.lan ? null : this.now };
  }
  providerTouched(id: string, keys: string[] = [], who = 'admin'): void {
    const p = this.providers[id];
    if (!p) return;
    p.version++;
    p.updated_at = this.now;
    p.updated_by = who;
    for (const q of keys) p.key_at[q] = this.now;
  }

  // ---- a hook's call -----------------------------------------------------------------------------
  private policy(h: MockHook, stage: string): string {
    const s = STAGES.stages[stage];
    if (h.vals.policy) return h.vals.policy as string;
    if (!s.sync) return 'drop';
    return h.role === 'guard' ? 'closed' : s.on_failure;
  }
  private timeout(h: MockHook, stage: string): number {
    const b = STAGES.stages[stage].budget_ms;
    const own = h.vals.timeout_ms as number | null;
    return b == null ? (own ?? STAGES.async_timeout_ms) : Math.min(own ?? b, b);
  }
  private breaker(h: Pick<MockHook, 'fails' | 'open_until' | 'opened'>, now: number): Breaker {
    const state = h.open_until === 0 ? 'closed' : now < h.open_until ? 'open' : 'half_open';
    return { state, consecutive_failures: h.fails, open_for_s: state === 'open' ? round1((h.open_until - now) / 1000) : 0, opened: h.opened };
  }
  private metrics(h: MockHook, now: number): HookMetrics {
    const ds = [...h.durations].sort((a, b) => a - b);
    return { calls: h.calls, failures: h.failures, timeouts: h.timeouts, skipped: h.skipped, outcomes: { ...h.outcomes }, p50_ms: ds.length ? round1(rank(ds, 0.5)) : null, p95_ms: ds.length ? round1(rank(ds, 0.95)) : null, breaker: this.breaker(h, now) };
  }
  /** what the hook's code answers: a builtin from its options, a webhook what its receiver is set to */
  private answerOf(h: MockHook): Record<string, unknown> {
    if (h.kind === 'http') return copy(h.answer ?? { action: 'continue' });
    const o = h.options;
    switch (h.target) {
      case 'set':
        return o.set && typeof o.set === 'object' && Object.keys(o.set).length ? { action: 'modify', patch: copy(o.set) } : { action: 'continue' };
      case 'deny':
        return { action: 'deny', reason: String(o.reason || 'refused by a hook').slice(0, 300) };
      case 'route': {
        const d = Object.fromEntries(['backend', 'model', 'profile'].filter((k) => typeof o[k] === 'string').map((k) => [k, o[k]]));
        return Object.keys(d).length ? { action: 'decide', decision: d } : { action: 'continue' };
      }
      case 'fallback':
        return typeof o.backend === 'string' ? { action: 'decide', decision: { fallback: { backend: o.backend, ...(typeof o.model === 'string' ? { model: o.model } : {}) } } } : { action: 'continue' };
      default:
        return { action: 'continue' };
    }
  }
  /** One call of a hook: its row in the journal, its figures and its breaker. */
  private call(h: MockHook, stage: string, mode: 'on' | 'shadow' | 'test', ts: number, request: string | null): HookCall {
    const count = mode !== 'test';
    const id = Object.keys(this.hooks).find((k) => this.hooks[k] === h)!;
    let entry: HookCall;
    if (count && h.open_until && ts < h.open_until) {
      entry = { ts, stage, request, mode, outcome: 'breaker_open', action: null, duration_ms: 0, breaker: 'open' };
      h.skipped++;
      h.outcomes.breaker_open = (h.outcomes.breaker_open ?? 0) + 1;
    } else {
      const limit = this.timeout(h, stage);
      const wobble = 0.8 + 0.6 * dice(`${id}|${stage}|${request}|${ts}`);
      let outcome: Outcome = 'ok';
      let result: Record<string, unknown> | null = null;
      let error: string | undefined;
      let took = round1(h.base_ms * wobble);
      if (h.behave === 'timeout' || took > limit) {
        outcome = 'timeout';
        took = limit;
        error = `no answer within ${limit} ms`;
      } else if (h.behave === 'error') {
        outcome = 'error';
        error = 'the hook answered HTTP 500';
      } else {
        result = this.answerOf(h);
        const action = String(result.action ?? 'continue');
        if (h.behave === 'invalid' || !STAGES.roles[h.role].includes(action) || !STAGES.stages[stage].actions.includes(action)) {
          outcome = 'invalid';
          error = `a ${h.role} at ${stage} may not answer ${h.behave === 'invalid' ? 'that' : action}`;
          result = null;
        }
      }
      entry = { ts, stage, request, mode, outcome, action: result ? String(result.action ?? 'continue') : null, duration_ms: took, breaker: this.breaker(h, ts).state };
      if (result) {
        if (typeof result.reason === 'string') entry.reason = result.reason;
        if (result.labels) entry.labels = result.labels as Record<string, unknown>;
        if (result.decision != null) entry.decision = copy(result.decision) as Record<string, unknown>;
        if (result.patch != null) entry.fields = Object.keys(result.patch as object).sort();
        if (mode === 'shadow' && entry.action !== 'continue') entry.diff = entry.action === 'modify' ? diffOf(result.patch as Record<string, unknown>) : entry.action === 'deny' ? { deny: true } : { decision: copy(result.decision) as Record<string, unknown> };
      }
      if (error) entry.error = error;
      if (count) {
        h.calls++;
        h.durations.push(took);
        if (h.durations.length > 512) h.durations.shift();
        const failed = FAILURES.includes(outcome);
        if (failed) h.failures++;
        if (outcome === 'timeout') h.timeouts++;
        const k = outcome === 'ok' ? (entry.action ?? 'continue') : outcome;
        h.outcomes[k] = (h.outcomes[k] ?? 0) + 1;
        // the breaker: five in a row open it; a probe after 30 s closes it or opens it again
        if (failed) {
          h.fails++;
          if (h.fails >= BREAKER_FAILS || h.open_until) {
            h.open_until = ts + BREAKER_OPEN_MS;
            h.opened++;
          }
        } else {
          h.fails = 0;
          h.open_until = 0;
        }
      }
    }
    h.journal.push(entry);
    if (h.journal.length > JOURNAL) h.journal.shift();
    return entry;
  }

  private inScope(h: MockHook, ctx: Record<string, string | null>): boolean {
    const s = (h.vals.scope ?? {}) as HookScope;
    const one = (k: (typeof SCOPE_KEYS)[number], v: string | null): boolean => !s[k]?.length || (v != null && s[k]!.some((p) => globMatch(p, v)));
    return one('models', ctx.model) && one('backends', ctx.backend) && one('profiles', ctx.profile) && one('users', ctx.user) && one('sessions', ctx.session) && one('client_kinds', ctx.client_kind);
  }

  /** One made-up request through the stages, as the runner walks them. Returns its id. */
  request(now = this.now, o: { model?: string; user?: string; session?: string; error?: boolean; stream?: boolean } = {}): string {
    const id = `chatcmpl-px-${sha(`request-${++this.seq}`).slice(0, 24)}`;
    const ctx = { model: o.model ?? 'example-model', backend: 'local', profile: null, user: o.user ?? 'owner', session: o.session ?? 's-1', client_kind: 'unknown' };
    const path = ['request.received', 'route.select', 'request.before_engine', ...(o.error ? ['request.error'] : []), ...(o.stream ? ['stream.chunk'] : []), 'response.after_engine', 'request.finished'];
    let t = now;
    let ended = false;
    for (const stage of path) {
      if (ended && stage !== 'request.finished') continue;
      const here = Object.entries(this.hooks)
        .filter(([, h]) => h.stages.includes(stage) && h.vals.enabled && h.vals.mode !== 'off' && this.inScope(h, ctx))
        .sort(([a, x], [b, y]) => ROLES.indexOf(x.role) - ROLES.indexOf(y.role) || (x.vals.priority as number) - (y.vals.priority as number) || (a < b ? -1 : 1));
      for (const [, h] of here) {
        const mode = h.vals.mode === 'shadow' ? 'shadow' : 'on';
        const c = this.call(h, stage, mode, t, id);
        t += Math.max(1, Math.round(c.duration_ms));
        if (mode !== 'on' || h.role === 'observer') continue;
        const failed = FAILURES.includes(c.outcome) || c.outcome === 'breaker_open';
        if ((c.outcome === 'ok' && c.action === 'deny') || (failed && this.policy(h, stage) === 'closed')) {
          ended = true;
          break;
        }
      }
    }
    return id;
  }

  private view(id: string, now: number): Hook {
    const h = this.hooks[id];
    const v = h.vals;
    return {
      id, name: h.name, kind: h.kind, stages: [...h.stages], role: h.role, priority: v.priority as number, mode: v.mode as Hook['mode'], scope: copy(v.scope ?? {}) as HookScope, policy: (v.policy ?? null) as Hook['policy'], timeout_ms: (v.timeout_ms ?? null) as number | null,
      payload: v.payload as Hook['payload'], endpoint: h.endpoint, target: h.target, options: copy(h.options), enabled: !!v.enabled, created_at: h.created_at, updated_at: h.updated_at, updated_by: h.updated_by, version: h.version, grant: copy(h.grant),
      policy_effective: Object.fromEntries(h.stages.map((s) => [s, this.policy(h, s)])),
      secret: h.kind === 'http' ? { set: !!h.secret, last4: h.secret ? h.secret.slice(-4) : null, sha256_6: h.secret ? sha(h.secret).slice(0, 6) : null, rotated_at: h.rotated_at, previous_until: h.previous_until && h.previous_until > now ? h.previous_until : null } : null,
      destination: { host: h.host, lan: h.lan },
      metrics: this.metrics(h, now),
    };
  }

  private need(id: string): MockHook {
    return this.hooks[id] ?? no('not_found', `no hook '${id}'`);
  }

  /** A hook document that may be stored, or the refusal (proxy/hookstore.py `_check`). */
  private check(d: Record<string, unknown>): void {
    if (typeof d.name !== 'string' || !d.name.trim() || d.name.trim().length > 80) bad('name: 1 to 80 characters', 'name');
    if (!STAGES.kinds.includes(d.kind as never)) bad(`kind: one of ${STAGES.kinds.join(', ')}`, 'kind');
    if (!STAGES.kinds_available.includes(d.kind as never)) no('kind_unavailable', `kind '${String(d.kind)}' is not available in this version: it arrives with ${d.kind === 'inproc' ? 'the in-process chain' : 'a later version (a plugin’s hooks are kind plugin)'}. Use http, builtin or plugin.`, 'kind');
    if (!ROLES.includes(d.role as Role)) bad(`role: one of ${ROLES.join(', ')}`, 'role');
    const st = d.stages;
    if (!Array.isArray(st) || !st.length || new Set(st).size !== st.length || !st.every((x) => typeof x === 'string')) bad('stages: a list of distinct stage names', 'stages');
    for (const x of st as string[]) {
      if (!STAGES.stages[x]) bad(`stages: no stage '${x}'`, 'stages');
      if (!STAGES.stages[x].roles.includes(d.role as Role)) bad(`stages: a ${String(d.role)} cannot run at ${x} (it takes ${STAGES.stages[x].roles.join(', ')})`, 'stages');
    }
    if (!Number.isInteger(d.priority) || Math.abs(d.priority as number) > 100000) bad('priority: an integer -100000..100000', 'priority');
    if (!STAGES.modes.includes(d.mode as never)) bad(`mode: one of ${STAGES.modes.join(', ')}`, 'mode');
    if (d.policy != null && !STAGES.policies.includes(d.policy as never)) bad('policy: open, closed or null (the stage’s default)', 'policy');
    if (d.timeout_ms != null && (!Number.isInteger(d.timeout_ms) || (d.timeout_ms as number) < 1 || (d.timeout_ms as number) > STAGES.max_timeout_ms)) bad(`timeout_ms: 1..${STAGES.max_timeout_ms} or null (the stage’s budget)`, 'timeout_ms');
    if (!STAGES.payloads.includes(d.payload as never)) bad(`payload: one of ${STAGES.payloads.join(', ')}`, 'payload');
    const sc = d.scope as Record<string, unknown>;
    if (!sc || typeof sc !== 'object' || Array.isArray(sc) || Object.keys(sc).some((k) => !SCOPE_KEYS.includes(k as never))) bad(`scope: an object of ${SCOPE_KEYS.join(', ')}`, 'scope');
    for (const [k, v] of Object.entries(sc)) if (!Array.isArray(v) || v.length > 64 || !v.every((x) => typeof x === 'string' && x.length >= 1 && x.length <= 128)) bad(`scope.${k}: a list of at most 64 patterns`, `scope.${k}`);
    if (!d.options || typeof d.options !== 'object' || Array.isArray(d.options) || JSON.stringify(d.options).length > 16384) bad('options: an object of at most 16 KiB', 'options');
    if (typeof d.enabled !== 'boolean') bad('enabled: true or false', 'enabled');
    if (d.kind === 'http') {
      const why = urlError(d.endpoint);
      if (why) bad(`endpoint: ${why}`, 'endpoint');
      if (d.target != null) bad('target: only for a builtin or plugin hook', 'target');
    } else if (d.kind === 'plugin') {
      // the mock installs no plugin: a target is refused as the registry refuses an unknown one
      if (d.endpoint != null) bad('endpoint: only for an http hook', 'endpoint');
      if (typeof d.target !== 'string' || !/^[a-z0-9][a-z0-9-]{0,47}\/[a-z0-9][a-z0-9_-]{0,47}$/.test(d.target)) bad('target: <plugin>/<hook>, a hook a plugin declares', 'target');
      bad(`target: no installed plugin declares the hook ${String(d.target)}`, 'target');
    } else {
      if (d.endpoint != null) bad('endpoint: only for an http hook', 'endpoint');
      if (!BUILTIN_NAMES.includes(d.target as string)) bad(`target: one of the builtins (${BUILTIN_NAMES.join(', ')})`, 'target');
    }
  }

  private confirmToken(id: string, host: string | null, version: number): string {
    return sha(`${id}\0${host}\0${version}`).slice(0, 32);
  }
  /** SEC-16: host -> on the LAN, of every place a systemone-http provider's text goes */
  private destinations(d: Record<string, unknown>): Record<string, boolean> {
    if (d.kind !== 'systemone-http') return {};
    const out: Record<string, boolean> = {};
    const urls = [d.endpoint, ...Object.values((d.questions ?? {}) as Record<string, { endpoint?: string }>).map((q) => q?.endpoint)];
    for (const u of urls) {
      const h = typeof u === 'string' && u ? hostOf(u) : null;
      if (h && !(h in out)) out[h] = hostIsLan(h);
    }
    return out;
  }
  private providerNeedsGrant(old: MockProvider | null, d: Record<string, unknown>): boolean {
    const have = old?.grant?.destinations ?? (old ? this.destinations({ kind: old.kind, endpoint: old.endpoint, questions: old.vals.questions }) : {});
    return Object.keys(this.destinations(d)).some((h) => !(h in have));
  }
  private providerGrant(id: string, old: MockProvider | null, d: Record<string, unknown>, who: string, confirm: unknown): ProviderGrant | null {
    const dests = this.destinations(d);
    if (!Object.keys(dests).length) return null;
    const g = old?.grant ?? null;
    if (g && Object.keys(dests).every((h) => h in g.destinations)) return { ...g, destinations: Object.fromEntries(Object.keys(dests).map((h) => [h, g.destinations[h]])) };
    const sure = g?.confirmed_by ? Object.keys(g.destinations) : [];
    const away = Object.keys(dests).filter((h) => !dests[h] && !sure.includes(h)).sort();
    if (away.length) {
      const want = this.confirmToken(id, away.join(','), old?.version ?? 0);
      if (confirm !== want) no('confirm_needed', `the questions’ text would go to ${away.join(', ')}, outside the LAN: send the same request again with this confirm to grant it`, 'endpoint', { confirm: want, destination: away.join(', '), lan: false });
    }
    const outside = Object.values(dests).some((lan) => !lan);
    const confirmed = outside && (away.length > 0 || !!g?.confirmed_by);
    return { by: who, at: this.now, destinations: dests, confirmed_by: confirmed ? (away.length ? who : g!.confirmed_by) : null, confirmed_at: confirmed ? (away.length ? this.now : g!.confirmed_at) : null };
  }
  /** the grant a document carries; a destination outside the LAN wants its confirm first */
  private grant(id: string, old: MockHook | null, payload: unknown, host: string | null, lan: boolean, who: string, confirm: unknown): Grant | null {
    if (payload !== 'full') return null;
    if (old && old.vals.payload === 'full' && old.grant && old.grant.destination === host) return old.grant;
    if (!lan) {
      const want = this.confirmToken(id, host, old?.version ?? 0);
      if (confirm !== want) no('confirm_needed', `the full payload (prompts and answers as they are) would go to ${host}, outside the LAN: send the same request again with this confirm to grant it`, 'payload', { confirm: want, destination: host, lan: false });
    }
    return { level: 'full', by: who, at: this.now, destination: host, lan, confirmed_by: lan ? null : who, confirmed_at: lan ? null : this.now };
  }

  private freeId(name: string, taken: Record<string, unknown>, word: string): string {
    let base = name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^[-._]+|[-._]+$/g, '').slice(0, 40) || word;
    if (!ID_RE.test(base)) base = word;
    let id = base;
    for (let n = 2; taken[id]; n++) id = `${base}-${n}`;
    return id;
  }

  private createHook(doc: Record<string, unknown>, who: string): { hook: Hook; secret?: string } {
    const unknown = Object.keys(doc).filter((k) => !HOOK_FIELDS.includes(k) && k !== 'id' && k !== 'confirm').sort();
    if (unknown.length) bad(`unknown field '${unknown[0]}'`, unknown[0]);
    const d: Record<string, unknown> = { name: doc.name, kind: doc.kind ?? 'http', stages: doc.stages, role: doc.role, priority: doc.priority ?? 100, mode: doc.mode ?? 'on', scope: doc.scope ?? {}, policy: doc.policy ?? null, timeout_ms: doc.timeout_ms ?? null, payload: doc.payload ?? 'meta', endpoint: doc.endpoint ?? null, target: doc.target ?? null, options: doc.options ?? {}, enabled: doc.enabled ?? true };
    this.check(d);
    let id = doc.id as string | undefined;
    if (id == null) id = this.freeId(d.name as string, { ...this.hooks, stages: 1, calls: 1 }, 'hook');
    else if (typeof id !== 'string' || !ID_RE.test(id)) bad('id: [a-z0-9][a-z0-9._-]{0,47}', 'id');
    else if (id === 'stages' || id === 'calls') bad(`id: '${id}' is a route of the hooks' API; choose another`, 'id');
    else if (this.hooks[id]) no('exists', `a hook '${id}' exists`, 'id');
    const host = d.kind === 'http' ? hostOf(d.endpoint as string) : null;
    const lan = d.kind === 'http' ? hostIsLan(host) : true;
    const grant = this.grant(id!, null, d.payload, host, lan, who, doc.confirm);
    const secret = d.kind === 'http' ? `hks_${sha(`secret-${id}-${++this.seq}`).slice(0, 43)}` : null;
    this.hooks[id!] = hook(this.now, {
      name: (d.name as string).trim(), kind: d.kind as 'http' | 'builtin', stages: d.stages as string[], role: d.role as Role, endpoint: d.endpoint as string | null, target: d.target as string | null, options: copy(d.options) as Record<string, unknown>, secret, grant,
      created_at: this.now, updated_at: this.now, updated_by: who, rotated_at: secret ? this.now : null,
      vals: { enabled: d.enabled, mode: d.mode, priority: d.priority, scope: copy(d.scope), policy: d.policy, timeout_ms: d.timeout_ms, payload: d.payload },
    });
    return { hook: this.view(id!, this.now), ...(secret ? { secret } : {}) };
  }

  private updateHook(id: string, patch: Record<string, unknown>, who: string): Hook {
    const unknown = Object.keys(patch).filter((k) => !HOOK_FIELDS.includes(k) && k !== 'confirm').sort();
    if (unknown.length) bad(`unknown field '${unknown[0]}'`, unknown[0]);
    if ('kind' in patch) bad('kind: a hook’s kind is fixed; make a new hook', 'kind');
    const h = this.need(id);
    const { confirm, ...p } = patch;
    const now: Record<string, unknown> = { name: h.name, kind: h.kind, stages: h.stages, role: h.role, endpoint: h.endpoint, target: h.target, options: h.options, ...h.vals };
    const next = { ...now, ...p };
    if ('scope' in p && p.scope === null) next.scope = {};
    if ('options' in p && p.options === null) next.options = {};
    this.check(next);
    const host = h.kind === 'http' ? hostOf(next.endpoint as string) : null;
    const lan = h.kind === 'http' ? hostIsLan(host) : true;
    const grant = this.grant(id, h, next.payload, host, lan, who, confirm);
    const changed = Object.keys(p).filter((k) => JSON.stringify(p[k]) !== JSON.stringify(now[k]));
    if (!changed.length && grant === h.grant) return this.view(id, this.now);
    if (this.racing.delete(id)) {
      // another screen got there first: its change stands, this one is refused
      h.version++;
      h.updated_by = 'another screen';
      h.updated_at = this.now;
      no('conflict', 'the hook changed meanwhile; read it again');
    }
    Object.assign(h, { name: (next.name as string).trim(), stages: [...(next.stages as string[])], role: next.role, endpoint: next.endpoint, target: next.target, options: copy(next.options), host, lan, grant, version: h.version + 1, updated_at: this.now, updated_by: who });
    for (const k of ['enabled', 'mode', 'priority', 'scope', 'policy', 'timeout_ms', 'payload']) h.vals[k] = copy(next[k]);
    return this.view(id, this.now);
  }

  private needsTrust(method: string, sub: string[], body: Record<string, unknown>): boolean {
    if (sub.length === 2 && sub[1] === 'secret') return true;
    if (method === 'POST' && !sub.length) return body.payload === 'full';
    if (method === 'PATCH' && sub.length === 1) return body.payload === 'full' || ('endpoint' in body && this.hooks[sub[0]]?.vals.payload === 'full');
    return false;
  }

  private hookRoute(method: string, sub: string[], q: URLSearchParams, b: Record<string, unknown>, now: number, who: string): [number, unknown] {
    // SRV-849: the writes are there, pairing's rights are not: 403, as the settings interface answers
    if (this.needsTrust(method, sub, b) && !this.trust) no('trust_required', SENTENCE.trust_required);
    if (!sub.length) {
      if (method === 'GET') {
        this.forced('list');
        return [200, { contract_version: STAGES.version, hooks: Object.keys(this.hooks).sort().map((id) => this.view(id, now)), kinds_available: STAGES.kinds_available, builtins: BUILTIN_NAMES }];
      }
      if (method === 'POST') {
        this.forced('create');
        return [201, this.createHook(b, who)];
      }
    } else if (sub.length === 1 && sub[0] === 'stages' && method === 'GET') {
      this.forced('stages');
      return [200, STAGES];
    } else if (sub.length === 1 && sub[0] === 'calls' && method === 'GET') {
      // SRV-849: one request's trace, every hook's journal read for it
      this.forced('trace');
      const rid = q.get('request') ?? '';
      if (!rid || rid.length > 200) no('bad_request', "request: the id of a request (its X-Request-Id, or a journal row's `request`)");
      const order = Object.keys(STAGES.stages);
      const calls = Object.entries(this.hooks).flatMap(([id, h]) => h.journal.filter((c) => c.request === rid).map((c) => ({ ...copy(c), hook: id, name: h.name, prio: h.vals.priority as number })));
      calls.sort((a, b) => a.ts - b.ts || order.indexOf(a.stage) - order.indexOf(b.stage) || a.prio - b.prio || (a.hook < b.hook ? -1 : 1));
      return [200, { request: rid, calls: calls.map(({ prio: _p, ...c }) => c) }];
    } else if (sub.length === 1) {
      const id = sub[0];
      if (method === 'GET') {
        this.forced('get');
        this.need(id);
        return [200, { hook: this.view(id, now) }];
      }
      if (method === 'PATCH') {
        this.forced('patch');
        return [200, { hook: this.updateHook(id, b, who) }];
      }
      if (method === 'DELETE') {
        this.forced('delete');
        this.need(id);
        delete this.hooks[id];
        return [200, { deleted: id }];
      }
    } else if (sub.length === 2) {
      const [id, act] = sub;
      if ((act === 'enable' || act === 'disable') && method === 'POST') return [200, { hook: this.updateHook(id, { enabled: act === 'enable' }, who) }];
      if (act === 'mode' && (method === 'PUT' || method === 'POST')) {
        if (!('mode' in b)) bad('mode: off, shadow or on', 'mode');
        return [200, { hook: this.updateHook(id, { mode: b.mode }, who) }];
      }
      if (act === 'test' && method === 'POST') {
        this.forced('test');
        const h = this.need(id);
        if (b.event != null && (typeof b.event !== 'object' || Array.isArray(b.event))) bad('event: an object', 'event');
        if (b.stage != null && !STAGES.stages[b.stage as string]) bad(`stage: no stage '${String(b.stage)}'`, 'stage');
        const stage = (b.stage as string) ?? h.stages[0];
        if (!h.stages.includes(stage)) bad(`the hook does not run at ${stage}`, 'stage');
        const c = this.call(h, stage, 'test', now, `test-${sha(`test-${++this.seq}`).slice(0, 12)}`);
        const result = c.outcome === 'ok' ? this.answerOf(h) : null;
        // the event it was sent: the stage's sample with the typed one over it, its content only above meta
        const { content: typed, ...over } = (b.event ?? {}) as Record<string, unknown>;
        const content = typed !== undefined ? typed : STAGES.samples![stage].content;
        const event = { ...copy(STAGES.samples![stage].event), ...copy(over), ...(h.vals.payload !== 'meta' && content != null ? { content: copy(content) } : {}) };
        return [200, { hook: id, stage, outcome: c.outcome, duration_ms: c.duration_ms, result, error: c.error ?? null, event, ...(result?.action === 'modify' ? { diff: diffOf(result.patch as Record<string, unknown>) } : {}) }];
      }
      if (act === 'calls' && method === 'GET') {
        this.forced('calls');
        const h = this.need(id);
        const limit = Number(q.get('limit') ?? 100);
        if (!Number.isFinite(limit)) no('bad_request', 'limit: a number 1..500');
        return [200, { hook: id, calls: copy(h.journal.slice(-Math.max(1, Math.min(500, limit))).reverse()), metrics: this.metrics(h, now) }];
      }
      if (act === 'secret' && method === 'POST') {
        this.forced('secret');
        const h = this.need(id);
        const grace = b.grace_s ?? 0;
        if (!Number.isInteger(grace) || (grace as number) < 0 || (grace as number) > 86400) bad('grace_s: 0..86400', 'grace_s');
        if (h.kind !== 'http') no('no_secret', 'only an http hook has a secret');
        h.secret = `hks_${sha(`secret-${id}-${++this.seq}`).slice(0, 43)}`;
        h.rotated_at = now;
        h.previous_until = grace ? now + (grace as number) * 1000 : null;
        h.version++;
        h.updated_at = now;
        h.updated_by = who;
        return [200, { hook: this.view(id, now), secret: h.secret }];
      }
    }
    return no('not_found', `no route ${method} /v1/fleet/hooks/${sub.join('/')}`);
  }

  // ---- the providers -----------------------------------------------------------------------------
  private hint(p: MockProvider, q: string) {
    const k = p.keys[q];
    return k ? { key_set: true, last4: k.slice(-4), sha256_6: sha(k).slice(0, 6), set_at: p.key_at[q] ?? p.created_at } : { key_set: false, last4: null, sha256_6: null, set_at: null };
  }
  private endpointOf(p: MockProvider, spec: { endpoint?: string } | null): string {
    return p.kind === 'systemone-http' ? (spec?.endpoint ?? p.endpoint ?? '') : p.kind === 'hook' ? `hook:${p.hook_id}` : 'builtin';
  }
  private targets(p: MockProvider): [string, string[]][] {
    const by: Record<string, string[]> = {};
    const qs = p.vals.questions as Record<string, { endpoint?: string }>;
    for (const q of Object.keys(qs).sort()) (by[this.endpointOf(p, qs[q])] ??= []).push(q);
    return Object.entries(by).sort(([a], [b]) => (a < b ? -1 : 1));
  }
  private needProvider(id: string): MockProvider {
    return this.providers[id] ?? no('not_found', `no decision provider '${id}'`);
  }
  /** `cached` (the list's view, SRV-851): nothing is probed, each endpoint's last probe as it is or null */
  private healthOf(id: string, now: number, force: boolean, cached = false): Health {
    const p = this.needProvider(id);
    const endpoints = this.targets(p).map(([endpoint, questions]) => {
      if (!cached && (force || p.probed[endpoint] == null || now - p.probed[endpoint] >= PROBE_TTL_MS)) p.probed[endpoint] = now;
      const up = p.kind === 'hook' ? !!this.hooks[p.hook_id ?? '']?.vals.enabled && this.hooks[p.hook_id ?? '']?.vals.mode === 'on' : p.behave !== 'down' && p.behave !== 'unreachable';
      const probe: Probe | null = p.probed[endpoint] == null ? null : { up, ms: up ? round1(p.base_ms * 0.6) : null, error: up ? null : p.kind === 'hook' ? 'hook_off' : 'unreachable', age_s: round1((now - p.probed[endpoint]) / 1000) };
      return { endpoint, questions, probe, breaker: this.breaker(p, now) };
    });
    const last = [...this.ring].reverse().find((r) => r.provider === id && r.answer !== null);
    const last_call = last ? { ts_ms: last.ts_ms, question: last.question, error: last.answer!.error, ms: last.answer!.ms } : null;
    const status = !p.vals.enabled ? 'off' : endpoints.some((e) => e.probe && !e.probe.up) ? 'down' : endpoints.some((e) => e.breaker.state !== 'closed') || (last_call?.error && now - last_call.ts_ms < 60_000) ? 'failing' : endpoints.some((e) => !e.probe) ? 'unknown' : 'ok';
    return { provider: id, kind: p.kind, status, endpoints, last_call };
  }
  private providerView(id: string, now: number, health: boolean | 'cached' = false): Provider {
    const p = this.providers[id];
    const v = p.vals;
    const questions = Object.fromEntries(
      Object.entries(v.questions as Record<string, { model?: string; endpoint?: string; timeout_ms?: number }>)
        .sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([q, s]) => [q, { model: s.model ?? null, endpoint: s.endpoint ?? null, timeout_ms: s.timeout_ms ?? null, ...(p.kind === 'systemone-http' ? { key: this.hint(p, q) } : {}) }]),
    );
    return {
      id, name: p.name, kind: p.kind, endpoint: p.endpoint, hook_id: p.hook_id, target: null, model: p.model, timeout_ms: p.timeout_ms, questions, mode: v.mode as Provider['mode'], ab_percent: v.ab_percent as number, sticky: v.sticky as Provider['sticky'], enabled: !!v.enabled,
      key: p.kind === 'systemone-http' ? this.hint(p, '*') : null, grant: p.grant ? copy(p.grant) : null, created_at: p.created_at, updated_at: p.updated_at, updated_by: p.updated_by, version: p.version,
      breakers: Object.fromEntries(this.targets(p).map(([ep]) => [ep, this.breaker(p, now)])),
      ...(health ? { health: this.healthOf(id, now, false, health === 'cached') } : {}),
    };
  }

  private checkProvider(d: Record<string, unknown>): void {
    if (typeof d.name !== 'string' || !d.name.trim() || d.name.trim().length > 80) bad('name: 1 to 80 characters', 'name');
    if (!['builtin', 'systemone-http', 'hook', 'plugin'].includes(d.kind as string)) bad('kind: one of builtin, systemone-http, hook, plugin', 'kind');
    if (d.kind === 'systemone-http') {
      const why = urlError(d.endpoint);
      if (why) bad(`endpoint: ${why}`, 'endpoint');
    } else if (d.endpoint != null) bad('endpoint: only for a systemone-http provider (a hook has its own)', 'endpoint');
    if (d.kind === 'hook') {
      const h = typeof d.hook_id === 'string' ? this.hooks[d.hook_id] : undefined;
      if (!h) bad('hook_id: the id of a hook in /v1/fleet/hooks', 'hook_id');
      if (!h!.stages.includes('decision.ask') || h!.role !== 'decider') bad('hook_id: the hook must be a decider at the stage decision.ask', 'hook_id');
    } else if (d.hook_id != null) bad('hook_id: only for a hook provider', 'hook_id');
    if (d.kind === 'plugin') {
      // the mock installs no plugin: a target is refused as the store refuses an unknown one
      if (typeof d.target !== 'string' || !/^[a-z0-9][a-z0-9-]{0,47}\/[a-z0-9][a-z0-9_-]{0,47}$/.test(d.target)) bad('target: <plugin>/<provider>, a provider a plugin declares', 'target');
      bad(`target: no installed plugin declares the provider ${String(d.target)}`, 'target');
    } else if (d.target != null) bad('target: only for a plugin provider', 'target');
    if (typeof d.model !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(d.model)) bad('model: a model id or auto', 'model');
    if (!Number.isInteger(d.timeout_ms) || (d.timeout_ms as number) < 1 || (d.timeout_ms as number) > PROVIDER_TIMEOUT_MAX) bad(`timeout_ms: 1..${PROVIDER_TIMEOUT_MAX}`, 'timeout_ms');
    if (!['on', 'shadow', 'ab'].includes(d.mode as string)) bad('mode: one of on, shadow, ab', 'mode');
    if (!Number.isInteger(d.ab_percent) || (d.ab_percent as number) < 0 || (d.ab_percent as number) > 100) bad('ab_percent: 0..100, the share of sessions the provider answers in ab', 'ab_percent');
    if (!['session', 'user', 'request'].includes(d.sticky as string)) bad('sticky: one of session, user, request', 'sticky');
    if (typeof d.enabled !== 'boolean') bad('enabled: true or false', 'enabled');
    const qs = d.questions as Record<string, Record<string, unknown>>;
    const ids = Object.keys(QUESTIONS).join(', ');
    if (!qs || typeof qs !== 'object' || Array.isArray(qs) || !Object.keys(qs).length) bad(`questions: an object of the question ids it answers (${ids})`, 'questions');
    const key = (k: unknown, f: string): void => {
      if (d.kind !== 'systemone-http') bad(`${f}: only a systemone-http provider sends a key (a hook signs with its own secret)`, f);
      if (typeof k !== 'string' || !k || k.length > 4096 || /\s/.test(k)) bad(`${f}: 1..4096 characters, no spaces`, f);
    };
    for (const [q, s] of Object.entries(qs)) {
      const f = `questions.${q}`;
      if (!(q in QUESTIONS)) bad(`questions: no question '${q}' (one of ${ids})`, f);
      if (!s || typeof s !== 'object' || Object.keys(s).some((k) => !['model', 'endpoint', 'timeout_ms', 'api_key'].includes(k))) bad(`${f}: an object of model, endpoint, timeout_ms, api_key`, f);
      if (s.endpoint != null) {
        if (d.kind !== 'systemone-http') bad(`${f}.endpoint: only for a systemone-http provider`, `${f}.endpoint`);
        const why = urlError(s.endpoint);
        if (why) bad(`${f}.endpoint: ${why}`, `${f}.endpoint`);
      }
      if (s.timeout_ms != null && (!Number.isInteger(s.timeout_ms) || (s.timeout_ms as number) < 1 || (s.timeout_ms as number) > PROVIDER_TIMEOUT_MAX)) bad(`${f}.timeout_ms: 1..${PROVIDER_TIMEOUT_MAX}`, `${f}.timeout_ms`);
      if (s.api_key != null) key(s.api_key, `${f}.api_key`);
    }
    if (d.api_key != null) key(d.api_key, 'api_key');
  }
  /** 409 when another enabled provider serves one of these questions on or ab */
  private taken(d: Record<string, unknown>, self: string): void {
    if (!d.enabled || d.mode === 'shadow') return;
    for (const [id, p] of Object.entries(this.providers)) {
      if (id === self || !p.vals.enabled || p.vals.mode === 'shadow') continue;
      const both = Object.keys(p.vals.questions as object).filter((q) => q in (d.questions as object)).sort();
      if (both.length) no('question_taken', `the provider '${id}' serves ${both.join(', ')} ${String(p.vals.mode)}: one provider a question on or ab (any number in shadow)`, 'questions');
    }
  }
  /** the mapping without keys, and the keys a write gives */
  private split(qs: Record<string, Record<string, unknown>>): [Record<string, Record<string, unknown>>, Record<string, string | null>] {
    const mapping: Record<string, Record<string, unknown>> = {};
    const keys: Record<string, string | null> = {};
    for (const [q, s] of Object.entries(qs)) {
      const { api_key, ...rest } = s ?? {};
      if (s && 'api_key' in s) keys[q] = api_key as string | null;
      mapping[q] = Object.fromEntries(Object.entries(rest).filter(([, v]) => v != null));
    }
    return [mapping, keys];
  }
  private putKeys(p: MockProvider, keys: Record<string, string | null>): void {
    for (const [q, k] of Object.entries(keys)) {
      if (typeof k === 'string') {
        p.keys[q] = k;
        p.key_at[q] = this.now;
      } else {
        delete p.keys[q];
        delete p.key_at[q];
      }
    }
  }

  private createProvider(doc: Record<string, unknown>, who: string): Provider {
    const unknown = Object.keys(doc).filter((k) => !PROVIDER_FIELDS.includes(k) && k !== 'id' && k !== 'confirm').sort();
    if (unknown.length) bad(`unknown field '${unknown[0]}'`, unknown[0]);
    const d: Record<string, unknown> = { name: doc.name, kind: doc.kind ?? 'systemone-http', endpoint: doc.endpoint ?? null, hook_id: doc.hook_id ?? null, target: doc.target ?? null, model: doc.model ?? 'auto', timeout_ms: doc.timeout_ms ?? 400, questions: doc.questions, mode: doc.mode ?? 'shadow', ab_percent: doc.ab_percent ?? 0, sticky: doc.sticky ?? 'session', enabled: doc.enabled ?? true, api_key: doc.api_key ?? null };
    this.checkProvider(d);
    let id = doc.id as string | undefined;
    if (id == null) id = this.freeId(d.name as string, this.providers, 'provider');
    else if (typeof id !== 'string' || !ID_RE.test(id)) bad('id: [a-z0-9][a-z0-9._-]{0,47}', 'id');
    if (this.providers[id!]) no('exists', `a decision provider '${id}' exists`, 'id');
    const [mapping, keys] = this.split(d.questions as Record<string, Record<string, unknown>>);
    this.taken({ ...d, questions: mapping }, id!);
    const grant = this.providerGrant(id!, null, { ...d, questions: mapping }, who, doc.confirm);
    const p = provider(this.now, { name: (d.name as string).trim(), kind: d.kind as ProviderKind, endpoint: d.endpoint as string | null, hook_id: d.hook_id as string | null, model: d.model as string, timeout_ms: d.timeout_ms as number, created_at: this.now, updated_at: this.now, updated_by: who, vals: { enabled: d.enabled, mode: d.mode, ab_percent: d.ab_percent, sticky: d.sticky, questions: mapping } });
    p.grant = grant;
    this.putKeys(p, { ...keys, ...(d.api_key != null ? { '*': d.api_key as string } : {}) });
    this.providers[id!] = p;
    return this.providerView(id!, this.now);
  }

  private updateProvider(id: string, patch: Record<string, unknown>, who: string): Provider {
    const confirm = patch.confirm;
    patch = Object.fromEntries(Object.entries(patch).filter(([k]) => k !== 'confirm'));
    const unknown = Object.keys(patch).filter((k) => !PROVIDER_FIELDS.includes(k)).sort();
    if (unknown.length) bad(`unknown field '${unknown[0]}'`, unknown[0]);
    if ('kind' in patch) bad('kind: a provider’s kind is fixed; make a new provider', 'kind');
    const p = this.needProvider(id);
    const now: Record<string, unknown> = { name: p.name, kind: p.kind, endpoint: p.endpoint, hook_id: p.hook_id, model: p.model, timeout_ms: p.timeout_ms, ...p.vals };
    const next: Record<string, unknown> = { ...now, ...patch, api_key: patch.api_key ?? null };
    this.checkProvider(next);
    const [mapping, keys] = this.split(next.questions as Record<string, Record<string, unknown>>);
    this.taken({ ...next, questions: mapping }, id);
    const grant = this.providerGrant(id, p, { ...next, questions: mapping }, who, confirm);
    if (this.racing.delete(id)) {
      p.version++;
      p.updated_by = 'another screen';
      p.updated_at = this.now;
      no('conflict', 'the provider changed meanwhile; read it again');
    }
    Object.assign(p, { name: (next.name as string).trim(), endpoint: next.endpoint, hook_id: next.hook_id, model: next.model, timeout_ms: next.timeout_ms, version: p.version + 1, updated_at: this.now, updated_by: who, grant });
    Object.assign(p.vals, { enabled: next.enabled, mode: next.mode, ab_percent: next.ab_percent, sticky: next.sticky, questions: mapping });
    this.putKeys(p, { ...keys, ...('api_key' in patch ? { '*': patch.api_key as string | null } : {}) });
    for (const q of Object.keys(p.keys)) if (q !== '*' && !(q in mapping)) delete p.keys[q];
    return this.providerView(id, this.now);
  }

  /** What a provider says to one question, or why it says nothing. */
  private reply(p: MockProvider, question: string, seed: string, builtin: string): Answer {
    const spec = (p.vals.questions as Record<string, { model?: string; timeout_ms?: number }>)[question] ?? {};
    const took = round1(p.base_ms * (0.8 + 0.6 * dice(`${seed}|ms`)));
    const model = spec.model ?? (p.model === 'auto' ? `${p.kind === 'builtin' ? 'decider' : 'my-decider'}` : p.model);
    const failed = (error: string, ms = took): Answer => ({ label: null, confidence: 0, probs: {}, model: null, ms, error });
    if (p.kind === 'hook') {
      const h = this.hooks[p.hook_id ?? ''];
      if (!h || !h.vals.enabled || h.vals.mode !== 'on') return failed('hook_off', 0);
    }
    if (p.behave === 'timeout') return failed('timeout', spec.timeout_ms ?? p.timeout_ms);
    if (p.behave === 'unreachable' || p.behave === 'down') return failed(p.behave === 'down' ? 'provider_down' : 'unreachable', 0);
    if (p.behave === 'http_401') return failed('http_401');
    if (p.behave === 'unknown_label') return failed('unknown_label');
    const opts = OPTIONS[question] ?? ['yes', 'no'];
    const label = dice(`${seed}|agree`) < p.agree ? builtin : (opts.find((o) => o !== builtin) ?? builtin);
    const confidence = Math.round((0.72 + 0.27 * dice(`${seed}|conf`)) * 10000) / 10000;
    const rest = Math.round(((1 - confidence) / Math.max(1, opts.length - 1)) * 10000) / 10000;
    return { label, confidence, probs: Object.fromEntries(opts.map((o) => [o, o === label ? confidence : rest])), model, ms: took, error: null };
  }

  /** One of the conductor's questions, put to every provider that answers or watches it. */
  ask(now = this.now, o: { question?: string; session?: string } = {}): void {
    const question = o.question ?? 'classify';
    const session = o.session ?? 's-1';
    const n = ++this.seq;
    const opts = OPTIONS[question] ?? ['yes', 'no'];
    const builtin = opts[Math.floor(dice(`builtin|${question}|${session}|${n}`) * 2.2) % opts.length];
    const decider = { label: builtin, ms: round1(15 + 8 * dice(`builtin-ms|${n}`)), error: null };
    for (const [id, p] of Object.entries(this.providers)) {
      const v = p.vals;
      if (!v.enabled || !(question in (v.questions as object))) continue;
      const mode = v.mode as 'on' | 'shadow' | 'ab';
      const arm = mode === 'ab' ? (dice(`${id}|${v.sticky === 'request' ? n : session}`) * 100 < (v.ab_percent as number) ? 'b' : 'a') : null;
      const row: ProviderCall = { provider: id, kind: p.kind, question, mode, arm, used: 'builtin', label: builtin, answer: null, endpoint: null, builtin: { decider }, agree: null, fallback: null, key: sha(`${question}|${session}|${n}`).slice(0, 24), limits: null, request: null, session, ts_ms: now };
      if (arm !== 'a') {
        const spec = (v.questions as Record<string, { endpoint?: string }>)[question];
        row.endpoint = this.endpointOf(p, spec);
        const open = p.open_until && now < p.open_until;
        const a = this.replay ? { label: null, confidence: 0, probs: {}, model: null, ms: 0, error: 'replay_miss' } : open ? { label: null, confidence: 0, probs: {}, model: null, ms: 0, error: 'breaker_open' } : this.reply(p, question, `${id}|${n}`, builtin);
        row.answer = a;
        // a hook provider's question is a call of its hook: the hook's journal has it too
        if (p.kind === 'hook' && !open && !this.replay && a.error !== 'hook_off' && this.hooks[p.hook_id ?? '']) this.call(this.hooks[p.hook_id ?? ''], 'decision.ask', 'on', now, null);
        if (!open && !this.replay) {
          if (a.error) {
            p.fails++;
            if (p.fails >= BREAKER_FAILS || p.open_until) {
              p.open_until = now + BREAKER_OPEN_MS;
              p.opened++;
            }
          } else {
            p.fails = 0;
            p.open_until = 0;
          }
        }
        if (mode === 'shadow') row.agree = a.error ? null : a.label === builtin;
        else if (a.error) row.fallback = a.error;
        else {
          row.used = 'provider';
          row.label = a.label;
        }
      }
      this.ring.push(row);
    }
    if (this.ring.length > 4000) this.ring.splice(0, this.ring.length - 4000);
  }

  private report(now: number, hours: number, pid: string | null) {
    const since = Math.round(now - hours * 3_600_000);
    const out: Record<string, Record<string, Partial<Record<Arm, Bucket & { _ms?: number[] }>>>> = {};
    for (const r of this.ring) {
      if (r.ts_ms < since || (pid && r.provider !== pid)) continue;
      const arm: Arm = r.mode === 'ab' ? (`ab_${r.arm ?? 'b'}` as Arm) : r.mode;
      const b = (((out[r.provider] ??= {})[r.question] ??= {})[arm] ??= { calls: 0, answered: 0, failures: 0, fallbacks: 0, fallback_reasons: {}, compared: 0, agreed: 0, agreement_pct: null, p50_ms: null, p95_ms: null, labels: {}, _ms: [] });
      b.calls++;
      if (r.answer) {
        if (r.answer.error) b.failures++;
        else {
          b.answered++;
          b._ms!.push(r.answer.ms);
        }
      } else b._ms!.push((r.builtin as { decider: { ms: number } }).decider.ms);
      if (r.fallback) {
        b.fallbacks++;
        b.fallback_reasons[r.fallback] = (b.fallback_reasons[r.fallback] ?? 0) + 1;
      }
      if (r.agree !== null) {
        b.compared++;
        if (r.agree) b.agreed++;
      }
      const lab = r.mode !== 'shadow' ? r.label : (r.answer?.label ?? null);
      if (lab !== null) b.labels[lab] = (b.labels[lab] ?? 0) + 1;
    }
    for (const qs of Object.values(out))
      for (const arms of Object.values(qs))
        for (const b of Object.values(arms)) {
          const ds = b._ms!.sort((x, y) => x - y);
          delete b._ms;
          b.p50_ms = ds.length ? round1(rank(ds, 0.5)) : null;
          b.p95_ms = ds.length ? round1(rank(ds, 0.95)) : null;
          b.agreement_pct = b.compared ? round1((100 * b.agreed) / b.compared) : null;
        }
    return { contract_version: '1.0', since_ms: since, hours, source: 'memory', providers: out };
  }

  private providerRoute(method: string, sub: string[], q: URLSearchParams, b: Record<string, unknown>, now: number, who: string): [number, unknown] {
    const flag = (k: string): boolean => ['1', 'true', 'yes'].includes((q.get(k) ?? '').toLowerCase());
    if (!sub.length) {
      if (method === 'GET') {
        this.forced('p-list');
        return [200, { contract_version: '1.0', providers: Object.keys(this.providers).sort().map((id) => this.providerView(id, now, 'cached')), questions: Object.fromEntries(Object.entries(QUESTIONS).map(([k, what]) => [k, { what, asked_in: 'controller', engine: 'follow-up (docs)' }])), kinds: ['builtin', 'systemone-http', 'hook'], modes: ['on', 'shadow', 'ab'], sticky: ['session', 'user', 'request'], replay: this.replay }];
      }
      if (method === 'POST') {
        this.forced('p-create');
        if (!this.trust && this.providerNeedsGrant(null, { ...b, kind: b.kind ?? 'systemone-http' })) no('trust_required', SENTENCE.p_trust);
        return [201, { provider: this.createProvider(b, who) }];
      }
    } else if (sub.length === 1 && sub[0] === 'report' && method === 'GET') {
      this.forced('p-report');
      const hours = Number(q.get('hours') ?? 24);
      if (!Number.isFinite(hours) || hours < 0.01 || hours > 720) no('bad_request', 'hours: a number 0.01..720');
      const pid = q.get('provider');
      if (pid !== null) this.needProvider(pid);
      return [200, this.report(now, hours, pid)];
    } else if (sub.length === 1) {
      const id = sub[0];
      if (method === 'GET') {
        this.forced('p-get');
        this.needProvider(id);
        return [200, { provider: this.providerView(id, now, flag('health')) }];
      }
      if (method === 'PATCH') {
        this.forced('p-patch');
        const old = this.needProvider(id);
        if (!this.trust && ('endpoint' in b || 'questions' in b) && this.providerNeedsGrant(old, { kind: old.kind, endpoint: 'endpoint' in b ? b.endpoint : old.endpoint, questions: 'questions' in b ? b.questions : old.vals.questions })) no('trust_required', SENTENCE.p_trust);
        return [200, { provider: this.updateProvider(id, b, who) }];
      }
      if (method === 'DELETE') {
        this.forced('p-delete');
        this.needProvider(id);
        delete this.providers[id];
        return [200, { deleted: id }];
      }
    } else if (sub.length === 2) {
      const [id, act] = sub;
      if ((act === 'enable' || act === 'disable') && method === 'POST') return [200, { provider: this.updateProvider(id, { enabled: act === 'enable' }, who) }];
      if (act === 'mode' && (method === 'PUT' || method === 'POST')) {
        if (!('mode' in b) || Object.keys(b).some((k) => !['mode', 'ab_percent', 'sticky'].includes(k))) bad('{mode: on|shadow|ab, ab_percent?, sticky?}', 'mode');
        return [200, { provider: this.updateProvider(id, b, who) }];
      }
      if (act === 'test' && method === 'POST') {
        this.forced('p-test');
        const p = this.needProvider(id);
        if (Object.keys(b).some((k) => k !== 'question' && k !== 'text')) bad('{question?, text?}');
        const qs = Object.keys(p.vals.questions as object).sort();
        const question = (b.question as string) ?? qs[0];
        if (!qs.includes(question)) bad(`question: the provider does not answer '${question}'`, 'question');
        if (b.text != null && (typeof b.text !== 'string' || !b.text || b.text.length > 8000)) bad('text: 1..8000 characters', 'text');
        const a = this.reply(p, question, `test|${id}|${question}|${String(b.text ?? '')}`, (OPTIONS[question] ?? ['yes'])[0]);
        const spec = (p.vals.questions as Record<string, { endpoint?: string }>)[question];
        return [200, { provider: id, question, ok: a.error === null, endpoint: this.endpointOf(p, spec), ms: a.ms, answer: a.error === null ? a : null, error: a.error }];
      }
      if (act === 'health' && method === 'GET') {
        this.forced('p-health');
        return [200, this.healthOf(id, now, flag('force'))];
      }
      if (act === 'calls' && method === 'GET') {
        this.forced('p-calls');
        this.needProvider(id);
        const limit = Number(q.get('limit') ?? 100);
        if (!Number.isFinite(limit)) no('bad_request', 'limit: a number 1..500');
        return [200, { provider: id, calls: copy(this.ring.filter((r) => r.provider === id).reverse().slice(0, Math.max(1, Math.min(500, limit)))) }];
      }
    }
    return no('not_found', `no route ${method} /v1/fleet/decision-providers/${sub.join('/')}`);
  }

  /** One request of either API. `parts`: the path after /v1/fleet (`hooks`, …). */
  handle(method: string, parts: string[], q: URLSearchParams, body: unknown, nowMs = Date.now(), who = 'admin'): Reply {
    this.now = nowMs;
    const sub = parts.slice(1).filter(Boolean).map((p) => decodeURIComponent(p));
    try {
      if (body !== null && body !== undefined && (typeof body !== 'object' || Array.isArray(body))) no('bad_request', 'the body must be a JSON object');
      const b = (body ?? {}) as Record<string, unknown>;
      const [status, out] = parts[0] === 'hooks' ? this.hookRoute(method, sub, q, b, nowMs, who) : this.providerRoute(method, sub, q, b, nowMs, who);
      return { status, body: out };
    } catch (e) {
      if (e instanceof Refuse) return { status: e.status, body: { error: { type: e.type, code: e.type, message: e.message, ...e.facts } } };
      throw e;
    }
  }
}

/** the request's body a patch is laid over: what a diff calls replaced, and what added */
const BODY_KEYS = ['model', 'temperature', 'max_tokens', 'messages', 'tools'];
function diffOf(patch: Record<string, unknown>): Record<string, { op: 'add' | 'replace' | 'remove'; bytes?: number }> {
  const out: Record<string, { op: 'add' | 'replace' | 'remove'; bytes?: number }> = {};
  for (const [k, v] of Object.entries(patch ?? {})) out[k] = v === null ? { op: 'remove' } : { op: BODY_KEYS.includes(k) ? 'replace' : 'add', bytes: Buffer.byteLength(JSON.stringify(v)) };
  return out;
}
