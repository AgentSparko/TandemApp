// Adding an engine host in the mock (`/v1/fleet/engines/*`, contracts/fleet-v1 engine-*.schema.json;
// VIS-278, SRV-827, SRV-829, SRV-831, SRV-833), as the controller answers it
// (proxy/engineonboard.py): the engines the LAN advertises, an address typed by hand, pairing by
// code, a password once, a key line to paste, and after pairing the engine's mode, its models and
// a model's install as a job on the clock. Every refusal is a code of mock/enginecodes.ts with the
// controller's own sentence and fix. No answer holds a code, a password, a key or a token.
//
// The engine hosts (MOCK_ENGINES). Advertised on the LAN:
//   192.0.2.80  spark-desk   pairing window open, code 482913, 5 tries, 5 minutes; no model yet:
//               the whole walk. SSH: spark / spark-pass, the engine in ~/TandemLLM
//   192.0.2.81  spark-rack   no code showing: the first read says so, the next finds a window open
//               (as after `tandem pair` there); the same code
//   192.0.2.82  spark-attic  an engine too old to pair by code: the other ways take it
//               (spark / spark-pass)
//   192.0.2.84  spark-lab    advertises another fingerprint than its window shows: refused, both shown
// By address only:
//   gpu-box.tailnet.example  a tailnet engine, window open, the same code; it runs in chat mode
//               and serves a model already (the mode is set, no model step); its engine is in
//               /opt/tandem, so the other ways need that folder (gpu / gpu-pass)
//   192.0.2.85  SSH only: this fleet pinned another host key for it before (host_key_changed)
//   192.0.2.86  SSH only: its SSH server answers from the second try on (paste a key: no
//               fingerprint at first)
//   192.0.2.87  SSH only: an engine without `tandem remote` (engine_too_old)
//   192.0.2.90  nothing answers; 192.0.2.91 answers, not an engine; 192.0.2.92 refuses this
//               machine's address; 192.0.2.93 takes no more pairing tries for 30 s
// A window that ended (expired, five wrong codes, used) says so once; the next read finds a new
// one, as after `tandem pair`. A pasted key is taken 8 s after its line was made.
//
// The models (the Setup Guide's tiles): qwen3.8-27b (18.4 GB to download), qwen3.8-flash-next
// (already on the disk: no download) and nemotron-3.5-bolt-74b-a9b (its plan has an error: not
// enough free disk). A job's steps follow the clock; the download moves at 1.6 GB a second.
//
// `fail(route, code)` makes a route answer one code until `fail(route, null)`: the refusals no
// device here produces by itself (a key the host could not add, a token refused, the Setup Guide
// locked, ...). `failJob` makes the next install stop in its download. mock/server.ts sets both
// from `POST /__mock/fleet {engineFail: "<route>:<code>" | null, engineJob: "fail" | "ok"}`.

import type { ConnectKey, ConnectPlan, DiscoverView, DiscoveredEngine, EngineFinding, EngineModel, EngineView, EngineWizard, HostKeyView, JobProgress, ModeView, ModelsView, PairStart, PairWindow, PairedView, ProbeView, ProgressView, PullView } from '../src/api/engines.ts';
import type { FleetNode } from '../src/api/fleet.ts';
import { ENGINE_CODES, finding, refusal, type Facts, type Reply } from './enginecodes.ts';

const CONTRACT = '1.0';
const WINDOW_S = 300;
const TRIES = 5;
const PAIR_TTL_S = 600;
const PLAN_TTL_S = 600;
const KEY_TTL_S = 3600;
const TRIES_PER_MIN = 10;
/** how long the mock's user takes to paste the key line on the engine host */
export const KEY_PASTE_S = 8;
const DEFAULT_DIR = 'TandemLLM';
const KEY_OPTS = 'no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding';

const FP_RE = /^SHA256:[A-Za-z0-9+/]{43}$/;
const HOST_RE = /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*\.?$/;
const IP4_RE = /^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$/;
const USER_RE = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$/;
const DIR_RE = /^(?:~|~\/[A-Za-z0-9._/+@-]{0,1000}|\/[A-Za-z0-9._/+@-]{0,1023})$/;
const PENDING_RE = /^[0-9a-f]{32}$/;
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const PROFILE_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/;
const JOB_RE = /^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$/;

/** a fingerprint of the right shape from a word (43 base64 characters) */
const fp = (word: string): string => `SHA256:${(word + 'MockHostKeyOfAnEngineHostForThePagesOnly0123456789').slice(0, 43)}`;

/** n hex digits from a text (FNV-1a, chained): ids that are the same for the same input */
function hex(text: string, n: number): string {
  let out = '';
  for (let round = 0; out.length < n; round++) {
    let h = 0x811c9dc5 ^ round;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 0x01000193);
    out += (h >>> 0).toString(16).padStart(8, '0');
  }
  return out.slice(0, n);
}

export const MOCK_ENGINES = {
  code: '482913',
  desk: '192.0.2.80',
  rack: '192.0.2.81',
  attic: '192.0.2.82',
  lab: '192.0.2.84',
  tailnet: 'gpu-box.tailnet.example',
  changed: '192.0.2.85',
  sleepy: '192.0.2.86',
  ancient: '192.0.2.87',
  silent: '192.0.2.90',
  other: '192.0.2.91',
  refusing: '192.0.2.92',
  limited: '192.0.2.93',
  user: 'spark',
  password: 'spark-pass',
} as const;

/** the routes `fail` knows */
export const FAIL_ROUTES = ['discover', 'probe', 'pair-start', 'pair-code', 'password-plan', 'password-confirm', 'key-new', 'key-check', 'engine', 'mode', 'models', 'pull', 'progress'] as const;
export type FailRoute = (typeof FAIL_ROUTES)[number];

interface Device {
  name: string;
  address: string;
  port: number;
  version: string;
  fp: string;
  /** what its advertisement says; undefined: it does not advertise */
  advert?: { fp: string | null };
  http: 'engine' | 'silent' | 'other' | 'refusing' | 'limited';
  /** pairing by code: a window open at once, none until `tandem pair`, or an engine too old */
  code: 'open' | 'closed' | 'old';
  ssh: 'yes' | 'no' | 'late';
  /** the fleet pinned this key for its address before */
  pinned?: string;
  remote: boolean;
  user: string;
  password: string;
  dir: string;
  mode: 'engine' | 'chat';
  inferred: boolean;
  model: string | null;
  // what the mock's clock and the calls did to it
  win?: { at: number; tries: number; ended: 'expired' | 'spent' | 'paired' | null; told: boolean };
  asked?: boolean;
  scans?: number;
}

const device = (o: Partial<Device> & Pick<Device, 'name' | 'address'>): Device => ({
  port: 8000, version: '0.16.0', fp: fp(o.name.replace(/[^A-Za-z0-9]/g, '')), http: 'engine', code: 'open', ssh: 'yes', remote: true,
  user: MOCK_ENGINES.user, password: MOCK_ENGINES.password, dir: `/home/${MOCK_ENGINES.user}/${DEFAULT_DIR}`, mode: 'engine', inferred: true, model: null, ...o,
});

function devices(): Device[] {
  const d = device;
  /** a device that answers the LAN's search with what its advertisement says */
  const adv = (o: Partial<Device> & Pick<Device, 'name' | 'address'>): Device => {
    const x = device(o);
    return { ...x, advert: o.advert ?? { fp: x.fp } };
  };
  return [
    adv({ name: 'spark-desk', address: MOCK_ENGINES.desk }),
    adv({ name: 'spark-rack', address: MOCK_ENGINES.rack, code: 'closed' }),
    adv({ name: 'spark-attic', address: MOCK_ENGINES.attic, version: '0.15.4', code: 'old', advert: { fp: null } }),
    adv({ name: 'spark-lab', address: MOCK_ENGINES.lab, advert: { fp: fp('AnotherKeyThanTheWindowShows') } }),
    d({ name: 'gpu-box', address: MOCK_ENGINES.tailnet, user: 'gpu', password: 'gpu-pass', dir: '/opt/tandem', mode: 'chat', inferred: false, model: 'Qwen3.8-Flash-Next' }),
    d({ name: 'spark-moved', address: MOCK_ENGINES.changed, http: 'silent', pinned: fp('TheKeyThisFleetPinnedBefore') }),
    d({ name: 'spark-sleepy', address: MOCK_ENGINES.sleepy, http: 'silent', ssh: 'late' }),
    d({ name: 'spark-ancient', address: MOCK_ENGINES.ancient, http: 'silent', version: '0.14.2', remote: false }),
    d({ name: 'not-an-engine', address: MOCK_ENGINES.other, http: 'other', ssh: 'no' }),
    d({ name: 'spark-closed', address: MOCK_ENGINES.refusing, http: 'refusing', ssh: 'no' }),
    d({ name: 'spark-busy', address: MOCK_ENGINES.limited, http: 'limited', ssh: 'no' }),
  ];
}

interface ModelFacts {
  tile: EngineModel;
  profile: string;
  download_gb: number;
  on_disk: boolean;
  errors: string[];
}

const MODELS: ModelFacts[] = [
  {
    tile: { id: 'qwen3.8-27b', name: 'Qwen3.8-27B', family: 'Qwen3.8', params: '27B', pitch: 'A 27-billion-parameter model that reads images too, fast on one DGX Spark.', recommended: true, best_speed: { tok_s: 61.4, task: 'code', profile: 'exl3-4.5' } },
    profile: 'exl3-4.5', download_gb: 18.4, on_disk: false, errors: [],
  },
  {
    tile: { id: 'qwen3.8-flash-next', name: 'Qwen3.8-Flash-Next', family: 'Qwen3.8', params: '9B', pitch: 'The small, quick one: short answers and tool calls at a high rate.', recommended: false, best_speed: { tok_s: 148.2, task: 'chat', profile: 'exl3-3.05' } },
    profile: 'exl3-3.05', download_gb: 0, on_disk: true, errors: [],
  },
  {
    tile: { id: 'nemotron-3.5-bolt-74b-a9b', name: 'Nemotron 3.5 Bolt', family: 'Nemotron 3.5', params: '74B', pitch: 'A large mixture-of-experts model; 9 billion parameters work on each token.', recommended: false, best_speed: null },
    profile: 'nvfp4', download_gb: 46.2, on_disk: false, errors: ['not enough free disk for the weights: 46.2 GB needed, 31.0 GB free'],
  },
];

/** server/setup/apply.py STEPS, without the rollback: [id, label, seconds] */
const STEPS: [string, string, number][] = [
  ['check', 'Check the selection, the disk and the box', 1.5],
  ['unload_assistant', 'Unload Wren', 0],
  ['fetch', 'Download the weights', 0],
  ['preflight', 'Check the new setup before the restart', 1.5],
  ['write_config', 'Write the configuration (run/setup.env)', 1],
  ['switch', 'Restart the engine with the new setup', 5],
  ['smoke', 'Check that the new engine answers', 2.5],
];
const QUEUED_S = 0.5;
const GB_PER_S = 1.6;

interface Job {
  id: string;
  host: string;
  model: ModelFacts;
  profile: string;
  at: number;
  /** the job stops in its download at this share of it */
  failAt: number | null;
}

interface Host {
  id: string;
  alias: string;
  dev: Device;
  via: 'code' | 'ssh';
  key_id: string;
  ssh_port: number;
  user: string;
  paired_at: number;
}

interface Pending {
  kind: 'pair' | 'password' | 'key';
  until: number;
  at: number;
  address: string;
  port: number;
  user?: string;
  engine_dir?: string | null;
  name?: string | null;
  expect?: string | null;
  hk?: HostKeyView;
  key_id: string;
}

/** what the mock needs of the fleet it lives in */
export interface FleetHooks {
  nodes(now: number): FleetNode[];
  admitEngineHost(o: { id: string; alias: string; address: string; version: string; via: 'code' | 'ssh'; host_key: string }, now: number): FleetNode;
}

class Refused extends Error {
  constructor(public reply: Reply) {
    super('refused');
  }
}
function no(code: string, facts: Facts = {}, detail?: string | null, status?: number): never {
  throw new Refused(refusal(code, facts, detail, status));
}

export class MockEngineHosts {
  private devs = devices();
  private hosts = new Map<string, Host>();
  private pending = new Map<string, Pending>();
  private jobs: Job[] = [];
  private tries: number[] = [];
  private fails = new Map<FailRoute, string>();
  private found: Omit<DiscoverView, 'contract_version'> = { engines: [], browsed_at: null, seconds: null, lan: 'not_run', finding: null };
  private seq = 0;
  /** what macOS answers the search of the LAN (mock/server.ts: `?lan=deny`) */
  lanMode: 'allow' | 'deny' = 'allow';
  /** the next install stops in its download */
  failJob = false;

  constructor(private fleet: FleetHooks) {}

  /** the engine hosts paired so far: each one a target of the settings interface's engine scope */
  paired(): { id: string; alias: string }[] {
    return [...this.hosts.values()].map((h) => ({ id: h.id, alias: h.alias }));
  }

  /** A route answers `code` until it is cleared with null. */
  fail(route: FailRoute, code: string | null): void {
    if (code === null) this.fails.delete(route);
    else if (!ENGINE_CODES[code]) throw new Error(`no engine code ${code}`);
    else this.fails.set(route, code);
  }

  // ---- the routes ------------------------------------------------------------------------------
  /** `parts` after /v1/fleet: ['engines', ...]; `nowMs` the mock's clock */
  handle(method: string, parts: string[], q: URLSearchParams, body: unknown, nowMs = Date.now()): Reply {
    const now = nowMs / 1000;
    const sub = parts.slice(1).map((p) => decodeURIComponent(p));
    try {
      const ok = (b: object): Reply => ({ status: 200, body: { contract_version: CONTRACT, ...b } });
      if (method === 'GET' && sub.length === 0) return ok({ engines: [...this.hosts.values()].map((h) => this.view(h, null, null)) });
      if (sub.length === 1 && sub[0] === 'discover' && (method === 'GET' || method === 'POST')) return ok(method === 'POST' ? this.discover(body, now) : this.found);
      if (method === 'POST' && sub.join('/') === 'probe') return ok(this.probe(body, now));
      if (method === 'POST' && sub.join('/') === 'pair/start') return ok(this.pairStart(body, now));
      if (method === 'POST' && sub.join('/') === 'pair/code') return ok(this.pairCode(body, now));
      if (method === 'POST' && sub.join('/') === 'pair/abandon') return ok(this.pairAbandon(body));
      if (method === 'POST' && sub.join('/') === 'connect/password/plan') return ok(this.passwordPlan(body, now));
      if (method === 'POST' && sub.join('/') === 'connect/password/confirm') return ok(this.passwordConfirm(body, now));
      if (method === 'POST' && sub.join('/') === 'connect/key/new') return ok(this.keyNew(body, now));
      if (method === 'POST' && sub.join('/') === 'connect/key/check') return ok(this.keyCheck(body, now));
      if (method === 'GET' && sub.length === 1) return ok(this.engineView(sub[0], now));
      if (method === 'POST' && sub.length === 2 && sub[1] === 'mode') return ok(this.setMode(sub[0], body, now));
      if (method === 'GET' && sub.length === 2 && sub[1] === 'models') return ok(this.models(sub[0], now));
      if (method === 'POST' && sub.length === 3 && sub[1] === 'models' && sub[2] === 'pull') return ok(this.pull(sub[0], body, now));
      if (method === 'GET' && sub.length === 3 && sub[1] === 'models' && sub[2] === 'progress') return ok(this.progress(sub[0], q.get('job'), now));
      return refusal('not_found', {}, null, 404);
    } catch (e) {
      if (e instanceof Refused) return e.reply;
      throw e;
    }
  }

  // ---- small things ----------------------------------------------------------------------------
  private fields(body: unknown, allowed: string[], required: string[] = []): Record<string, unknown> {
    if (typeof body !== 'object' || body === null || Array.isArray(body)) no('bad_request', {}, 'the body is a JSON object');
    const b = body as Record<string, unknown>;
    const extra = Object.keys(b).filter((k) => !allowed.includes(k)).sort();
    if (extra.length) no('bad_request', {}, `unknown field '${extra[0]}'`);
    for (const k of required) if (b[k] == null) no('bad_request', {}, `${k} is missing`);
    return b;
  }
  private address(v: unknown, what = 'address'): string {
    if (typeof v !== 'string' || !v || v.startsWith('-') || v.length > 253) no('bad_request', {}, `${what} is a host name or an IP address`);
    const s = (v as string).trim().replace(/^\[|\]$/g, '');
    if (IP4_RE.test(s) || (/^[0-9a-fA-F:]+$/.test(s) && s.includes(':'))) return s.toLowerCase();
    if (!HOST_RE.test(s)) no('bad_request', {}, `${what} is a host name or an IP address`);
    return s.replace(/\.$/, '').toLowerCase();
  }
  private port(v: unknown, dflt: number): number {
    if (v == null) return dflt;
    if (typeof v !== 'number' || !Number.isInteger(v) || v < 1 || v > 65535) no('bad_request', {}, 'port is a whole number from 1 to 65535');
    return v as number;
  }
  private name(v: unknown): string | null {
    if (v == null) return null;
    if (typeof v !== 'string' || v.length > 64) no('bad_request', {}, 'name is a text of 64 characters at most');
    return (v as string).trim() || null;
  }
  private fpOf(v: unknown, what = 'fingerprint'): string | null {
    if (v == null) return null;
    if (typeof v !== 'string' || !FP_RE.test(v)) no('bad_request', {}, `${what} is an SSH SHA256 fingerprint (SHA256:...)`);
    return v as string;
  }
  private put(p: Omit<Pending, 'until' | 'at'>, ttl: number, now: number): string {
    for (const [k, v] of this.pending) if (v.until <= now) this.pending.delete(k);
    const id = hex(`${p.kind}:${p.address}:${++this.seq}:${now}`, 32);
    this.pending.set(id, { ...p, at: now, until: now + ttl });
    return id;
  }
  private get(id: unknown, kind: Pending['kind'], now: number): Pending {
    if (typeof id !== 'string' || !PENDING_RE.test(id)) no('bad_request', {}, `${kind === 'pair' ? 'pairing' : 'plan'} is the id the first call answered`);
    const item = this.pending.get(id as string);
    if (!item || item.kind !== kind || item.until <= now) {
      this.pending.delete(id as string);
      no('not_found');
    }
    return item as Pending;
  }
  private rate(now: number): void {
    this.tries = this.tries.filter((t) => now - t <= 60);
    if (this.tries.length >= TRIES_PER_MIN) no('too_many');
    this.tries.push(now);
  }
  /** a route's switch (`fail`): the code with the facts this call knows */
  private forced(route: FailRoute, f: Facts & { detail?: string } = {}): void {
    const code = this.fails.get(route);
    if (!code) return;
    const sample: Facts = { address: MOCK_ENGINES.desk, port: 8000, host: MOCK_ENGINES.desk, user: MOCK_ENGINES.user, dir: `~/${DEFAULT_DIR}`, tries_left: 3, retry_after: 30, expected: fp('TheFingerprintShownForIt'), offered: fp('TheFingerprintItOffersNow'), pinned: fp('TheKeyThisFleetPinnedBefore'), model: 'qwen3.8-27b', node: 'Desk' };
    const { detail, ...facts } = f;
    const text = `${ENGINE_CODES[code][1]} ${ENGINE_CODES[code][2]}`;
    const named = Object.fromEntries(Object.entries({ ...sample, ...facts }).filter(([k]) => text.includes(`{${k}}`) || FACT_OF[code]?.includes(k)));
    no(code, named as Facts, text.includes('{detail}') ? (detail ?? DETAIL_OF[code] ?? 'the other side gave no reason') : null);
  }
  private byHttp(address: string, port: number): Device | undefined {
    return this.devs.find((d) => d.address === address && d.port === port);
  }
  private bySsh(host: string, port: number): Device | undefined {
    return port === 22 ? this.devs.find((d) => d.address === host) : undefined;
  }
  private hostOf(d: Device): Host | undefined {
    return [...this.hosts.values()].find((h) => h.dev === d);
  }

  // ---- finding the engine (SRV-827) --------------------------------------------------------------
  private discover(body: unknown, now: number): Omit<DiscoverView, 'contract_version'> {
    const b = this.fields(body ?? {}, ['seconds', 'interface']);
    const secs = b.seconds ?? 2;
    if (typeof secs !== 'number' || secs < 0.2 || secs > 5) no('bad_request', {}, 'seconds is from 0.2 to 5');
    this.forced('discover');
    if (this.lanMode === 'deny') this.found = { engines: [], browsed_at: Math.round(now * 1000) / 1000, seconds: secs as number, lan: 'denied', finding: finding('lan_denied') };
    else {
      const engines: DiscoveredEngine[] = this.devs.filter((d) => d.advert).map((d) => {
        const h = d.advert!.fp ? [...this.hosts.values()].find((x) => x.dev.fp === d.advert!.fp) : undefined;
        return { name: d.name, instance: d.name, version: d.version, address: d.address, addresses: [d.address], port: d.port, fingerprint: d.advert!.fp, verified: false as const, node: h?.id ?? null, alias: h?.alias ?? null };
      });
      this.found = { engines, browsed_at: Math.round(now * 1000) / 1000, seconds: secs as number, lan: 'ok', finding: null };
    }
    return this.found;
  }

  /** the device's pairing window now; what `GET /v1/engine/pair` answers (never the code) */
  private window(d: Device, now: number): PairWindow {
    let w = d.win;
    if (w && !w.ended && now >= w.at + WINDOW_S) w.ended = 'expired';
    if (!w && d.code === 'closed' && !d.asked) {
      d.asked = true;
      return { open: false, state: 'closed', expires_in: null, tries_left: null, fingerprint: d.fp };
    }
    if (!w || (w.ended && w.told)) w = d.win = { at: now, tries: TRIES, ended: null, told: false };
    if (w.ended) {
      w.told = true;
      return { open: false, state: w.ended, expires_in: null, tries_left: null, fingerprint: d.fp };
    }
    return { open: true, state: 'open', expires_in: Math.ceil(w.at + WINDOW_S - now), tries_left: w.tries, fingerprint: d.fp };
  }

  /** what answers at an address: the window, or the refusal that says why there is none */
  private read(address: string, port: number, now: number): { d: Device; w: PairWindow } {
    const d = this.byHttp(address, port);
    if (!d || d.http === 'silent') return no('unreachable', { address, port });
    if (d.http === 'other') return no('not_an_engine', { address, port }, 'HTTP 404, not JSON');
    if (d.http === 'refusing') return no('not_allowed', { address, port });
    if (d.http === 'limited') return no('rate_limited', { address, port, retry_after: 30 });
    if (d.code === 'old') return no('pairing_unsupported', { address, port });
    return { d, w: this.window(d, now) };
  }

  private probe(body: unknown, now: number): Omit<ProbeView, 'contract_version'> {
    const b = this.fields(body, ['address', 'port'], ['address']);
    const address = this.address(b.address);
    const port = this.port(b.port, 8000);
    this.forced('probe', { address, port });
    const out: Omit<ProbeView, 'contract_version'> = { address, port, reachable: false, engine: false, pairing: null, fingerprint: null, node: null, finding: null };
    let got: { d: Device; w: PairWindow };
    try {
      got = this.read(address, port, now);
    } catch (e) {
      if (!(e instanceof Refused)) throw e;
      const f = (e.reply.body as { error: EngineFinding & { type?: string } }).error;
      const { type: _t, ...rest } = f;
      out.reachable = f.code !== 'unreachable';
      out.engine = ['pairing_unsupported', 'no_window', 'rate_limited'].includes(f.code);
      out.finding = rest;
      return out;
    }
    const { d, w } = got;
    Object.assign(out, { reachable: true, engine: true, fingerprint: w.fingerprint, pairing: w, node: this.hostOf(d)?.id ?? null });
    if (!w.open) out.finding = finding(['expired', 'spent', 'paired', 'confirming'].includes(w.state) ? 'code_spent' : 'no_window', { state: w.state, address, port });
    return out;
  }

  // ---- pairing by code (SRV-829) -----------------------------------------------------------------
  private pairStart(body: unknown, now: number): Omit<PairStart, 'contract_version'> {
    const b = this.fields(body, ['address', 'port', 'fingerprint', 'name'], ['address']);
    const address = this.address(b.address);
    const port = this.port(b.port, 8000);
    const advertised = this.fpOf(b.fingerprint);
    const name = this.name(b.name);
    this.forced('pair-start', { address, port });
    const { d, w } = this.read(address, port, now);
    if (!w.open) no(['expired', 'spent', 'paired', 'confirming'].includes(w.state) ? 'code_spent' : 'no_window', { state: w.state, address, port });
    if (advertised && w.fingerprint && advertised !== w.fingerprint) no('host_key_mismatch', { expected: advertised, offered: w.fingerprint, address, port }, 'the advertisement and the pairing window disagree');
    const key_id = `tandemapp-${hex(`key:${address}:${this.seq}:${now}`, 10)}`;
    const pairing = this.put({ kind: 'pair', address, port, expect: w.fingerprint ?? advertised, name: name ?? d.name, key_id }, PAIR_TTL_S, now);
    return { pairing, address, port, window: w, fingerprint: w.fingerprint ?? advertised, advertised, key_id, expires_in: PAIR_TTL_S };
  }

  private pairCode(body: unknown, now: number): Omit<PairedView, 'contract_version'> {
    const b = this.fields(body, ['pairing', 'code'], ['pairing', 'code']);
    const item = this.get(b.pairing, 'pair', now);
    if (typeof b.code !== 'string' || !/^[0-9]{6}$/.test(b.code)) no('bad_code');
    this.rate(now);
    const { address, port } = item;
    this.forced('pair-code', { address, port, expected: item.expect ?? undefined });
    const d = this.byHttp(address, port)!;
    const w = d.win;
    if (w && !w.ended && now >= w.at + WINDOW_S) w.ended = 'expired';
    if (!w || w.ended) {
      if (w) w.told = true;
      no('code_spent', { state: 'closed', address, port });
    }
    if (b.code !== MOCK_ENGINES.code) {
      w!.tries -= 1;
      if (w!.tries > 0) no('wrong_code', { tries_left: w!.tries, address, port });
      w!.ended = 'spent';
      w!.told = true;
      no('code_spent', { state: 'closed', address, port });
    }
    w!.ended = 'paired';
    this.pending.delete(b.pairing as string);
    return this.keep(d, { via: 'code', method: 'code', address, ssh_port: 22, user: d.user, key_id: item.key_id, name: item.name ?? null }, now);
  }

  private pairAbandon(body: unknown): { abandoned: boolean } {
    const b = this.fields(body, ['pairing'], ['pairing']);
    if (typeof b.pairing !== 'string' || !PENDING_RE.test(b.pairing)) no('bad_request', {}, 'pairing is the id the first call answered');
    return { abandoned: this.pending.delete(b.pairing as string) };
  }

  // ---- the other ways (SRV-831) ------------------------------------------------------------------
  private target(b: Record<string, unknown>): { host: string; port: number; user: string } {
    const host = this.address(b.host, 'host');
    const port = this.port(b.port, 22);
    if (typeof b.user !== 'string' || !USER_RE.test(b.user)) no('bad_request', {}, 'user is a login name');
    return { host, port, user: b.user as string };
  }
  private engineDir(v: unknown, absolute: boolean): string | null {
    if (v == null) return null;
    if (typeof v !== 'string' || !DIR_RE.test(v) || `${v}/`.includes('/../') || (absolute && !v.startsWith('/')))
      no('bad_request', {}, `engine_dir is the engine's install folder, ${absolute ? 'an absolute path' : 'an absolute path or ~/...'} of letters, digits and . _ / + @ -`);
    return (v as string).replace(/\/+$/, '') || '/';
  }
  /** the host key the host offers, against what the fleet pins for that address */
  private scan(host: string, port: number): HostKeyView {
    const d = this.bySsh(host, port);
    if (d) d.scans = (d.scans ?? 0) + 1;
    if (!d || d.ssh === 'no' || (d.ssh === 'late' && d.scans! < 2)) return no('ssh_unreachable', { host, port }, 'no SSH server answered');
    const stored = d.pinned ?? [...this.hosts.values()].find((h) => h.dev.address === host && h.ssh_port === port)?.dev.fp ?? null;
    return { type: 'ssh-ed25519', fingerprint: d.fp, known: stored === null ? 'new' : stored === d.fp ? 'same' : 'changed', stored };
  }
  private confirmed(item: Pending, given: string | null, hk: HostKeyView): void {
    if (hk.known === 'changed') no('host_key_changed', { pinned: hk.stored ?? undefined, offered: hk.fingerprint, host: item.address, port: item.port });
    if (given === null) no('host_key_unconfirmed', { offered: hk.fingerprint, host: item.address });
    if (given !== hk.fingerprint) no('host_key_mismatch', { expected: given ?? undefined, offered: hk.fingerprint, host: item.address }, 'the host key the host offers now is not the one confirmed');
  }
  /** the engine's folder there as its account sees it: ~/x is /home/<user>/x */
  private dirOf(dir: string | null | undefined, user: string): string {
    const d = dir ?? `~/${DEFAULT_DIR}`;
    return d === '~' ? `/home/${user}` : d.startsWith('~/') ? `/home/${user}/${d.slice(2)}` : d;
  }
  /** over the new login: the token rotated, `status` read; then the host is kept */
  private finishSsh(item: Pending, hk: HostKeyView, method: 'password' | 'key', now: number): Omit<PairedView, 'contract_version'> {
    const d = this.bySsh(item.address, item.port)!;
    if (!d.remote) no('engine_too_old', { host: item.address, port: item.port, user: item.user }, "unknown verb 'token'");
    void hk;
    return this.keep(d, { via: 'ssh', method, address: item.address, ssh_port: item.port, user: item.user!, key_id: item.key_id, name: item.name ?? null }, now);
  }

  private passwordPlan(body: unknown, now: number): Omit<ConnectPlan, 'contract_version'> {
    const b = this.fields(body, ['host', 'port', 'user', 'engine_dir', 'name'], ['host', 'user']);
    const { host, port, user } = this.target(b);
    const engine_dir = this.engineDir(b.engine_dir, false);
    const name = this.name(b.name);
    this.forced('password-plan', { host, port, user });
    const hk = this.scan(host, port);
    if (hk.known === 'changed') no('host_key_changed', { pinned: hk.stored ?? undefined, offered: hk.fingerprint, host, port });
    const key_id = `tandemapp-${hex(`key:${host}:${this.seq}:${now}`, 10)}`;
    const plan = this.put({ kind: 'password', address: host, port, user, engine_dir, name, hk, key_id }, PLAN_TTL_S, now);
    return { plan, host, port, user, engine_dir, host_key: hk, key_id, expires_in: PLAN_TTL_S };
  }

  private passwordConfirm(body: unknown, now: number): Omit<PairedView, 'contract_version'> {
    const b = this.fields(body, ['plan', 'password', 'host_key'], ['plan', 'password']);
    if (typeof b.password !== 'string' || !b.password || b.password.length > 1024 || b.password.includes('\n')) no('bad_request', {}, 'password is one line of text');
    const item = this.get(b.plan, 'password', now);
    const hk = item.hk!;
    this.confirmed(item, this.fpOf(b.host_key, 'host_key'), hk);
    this.rate(now);
    const { address: host, port, user } = item;
    this.forced('password-confirm', { host, port, user, dir: item.engine_dir ?? `~/${DEFAULT_DIR}` });
    const d = this.bySsh(host, port)!;
    if (user !== d.user || b.password !== d.password) no('auth_failed', { host, user });
    if (this.dirOf(item.engine_dir, user!) !== d.dir) no('not_installed', { dir: item.engine_dir ?? `~/${DEFAULT_DIR}`, host });
    const out = this.finishSsh(item, hk, 'password', now);
    this.pending.delete(b.plan as string);
    return out;
  }

  private keyNew(body: unknown, now: number): Omit<ConnectKey, 'contract_version'> {
    const b = this.fields(body, ['host', 'port', 'user', 'engine_dir', 'from', 'name'], ['host', 'user']);
    const { host, port, user } = this.target(b);
    const given = this.engineDir(b.engine_dir, true);
    const engine_dir = given ?? `/home/${user}/${DEFAULT_DIR}`;
    const name = this.name(b.name);
    this.forced('key-new', { host, port, user });
    const key_id = `tandemapp-${hex(`key:${host}:${this.seq}:${now}`, 10)}`;
    let hk: HostKeyView | null = null;
    let found: EngineFinding | null = null;
    try {
      hk = this.scan(host, port);
    } catch (e) {
      if (!(e instanceof Refused)) throw e;
      const { type: _t, ...rest } = (e.reply.body as { error: EngineFinding & { type?: string } }).error;
      found = rest;
    }
    if (hk && hk.known === 'changed') no('host_key_changed', { pinned: hk.stored ?? undefined, offered: hk.fingerprint, host, port });
    const plan = this.put({ kind: 'key', address: host, port, user, engine_dir, name, key_id }, KEY_TTL_S, now);
    const blob = `AAAAC3NzaC1lZDI1NTE5AAAAI${hex(`pub:${key_id}`, 43)}`;
    return {
      plan, host, port, user, engine_dir, engine_dir_guessed: given === null,
      line: `command="/bin/bash ${engine_dir}/bin/tandem remote --key-id ${key_id}",${KEY_OPTS} ssh-ed25519 ${blob} ${key_id}`,
      file: '~/.ssh/authorized_keys', key_id, key_fingerprint: fp(`Key${hex(key_id, 20)}`), host_key: hk, finding: found, expires_in: KEY_TTL_S,
    };
  }

  private keyCheck(body: unknown, now: number): Omit<PairedView, 'contract_version'> {
    const b = this.fields(body, ['plan', 'host_key'], ['plan']);
    const item = this.get(b.plan, 'key', now);
    const given = this.fpOf(b.host_key, 'host_key');
    const { address: host, port, user } = item;
    this.forced('key-check', { host, port, user, dir: item.engine_dir ?? undefined });
    const hk = this.scan(host, port);
    this.confirmed(item, given, hk);
    if (now < item.at + KEY_PASTE_S) return { state: 'waiting', via: 'key', engine: null, node: null, finding: finding('key_not_installed', { user, host }) };
    const d = this.bySsh(host, port)!;
    if (item.engine_dir !== d.dir) no('not_installed', { dir: item.engine_dir ?? undefined, host }, 'the key ran no tandem remote there');
    const out = this.finishSsh(item, hk, 'key', now);
    this.pending.delete(b.plan as string);
    return out;
  }

  // ---- a paired host -----------------------------------------------------------------------------
  private keep(d: Device, o: { via: 'code' | 'ssh'; method: string; address: string; ssh_port: number; user: string; key_id: string; name: string | null }, now: number): Omit<PairedView, 'contract_version'> {
    let h = this.hostOf(d);
    if (!h) {
      const taken = new Set(this.fleet.nodes(now).map((n) => n.alias.toLowerCase()));
      const base = o.name ?? o.address;
      let alias = base;
      for (let i = 2; taken.has(alias.toLowerCase()); i++) alias = `${base} ${i}`;
      h = { id: `eh-${hex(d.fp, 10)}`, alias, dev: d, via: o.via, key_id: o.key_id, ssh_port: o.ssh_port, user: o.user, paired_at: Math.round(now * 1000) / 1000 };
      this.hosts.set(h.id, h);
    } else Object.assign(h, { via: o.via, key_id: o.key_id, ssh_port: o.ssh_port, user: o.user, paired_at: Math.round(now * 1000) / 1000 });
    const node = this.fleet.admitEngineHost({ id: h.id, alias: h.alias, address: d.address, version: d.version, via: o.via, host_key: d.fp }, now);
    const status = this.status(h, now);
    return { state: 'paired', via: o.method as PairedView['via'], engine: { ...this.view(h, status, null), wizard: this.wizard(status) }, node, finding: null };
  }

  private host(id: string, now: number): Host {
    const h = this.hosts.get(id);
    if (h) return h;
    const n = this.fleet.nodes(now).find((x) => x.id === id);
    if (n) return no('not_ssh', { node: n.alias });
    return no('not_found');
  }

  /** engine-ctl-v1's `status` answer, as `tandem remote status` gives it */
  private status(h: Host, now: number): NonNullable<EngineView['status']> & Record<string, unknown> {
    const d = h.dev;
    const job = this.running(h.id, now);
    const switching = !!job && ['switch', 'smoke'].includes(this.summary(job, now).step ?? '');
    const state: 'loading' | 'healthy' = switching ? 'loading' : 'healthy';
    // the whole answer of the verb (more than the page's type names), so it is built apart
    const doc = {
      ctl: 'engine-ctl-v1', version: '1.0', engine_version: d.version, verb: 'status', ok: true,
      engine: { state, pid: 41822, port: d.port, health_code: switching ? 503 : 200, engine_api: '1.9', version: d.version, model: d.model, running_mode: d.mode, stopped_on_purpose: false },
      mode: d.mode, profile: d.model ? (MODELS.find((m) => m.tile.name === d.model)?.profile ?? null) : null, engine_api: '1.9', install: true,
      overlay: { current: 1, good: 1, versions: 1, file: true }, hook: false, job: null, http: { token: true, allow_from: null },
    };
    return doc;
  }
  private wizard(status: EngineView['status']): EngineWizard {
    const needs = status !== null && !status.engine.model;
    return { mode_choice: 'engine', mode: status?.mode ?? null, needs_model: needs, next: status === null ? null : needs ? 'model' : 'chat' };
  }
  private view(h: Host, status: EngineView['status'], found: EngineFinding | null): EngineView {
    const d = h.dev;
    return {
      id: h.id, alias: h.alias, address: d.address, ssh: { host: d.address, port: h.ssh_port, user: h.user, key_id: h.key_id }, host_key: d.fp, host_key_type: 'ssh-ed25519',
      http: { port: d.port, token: { set: true, last4: hex(`tok:${h.id}`, 4), sha256_6: hex(`sha:${h.id}`, 6), rotated_at: Math.floor(h.paired_at) } },
      via: h.via, paired_at: h.paired_at, engine_version: status?.engine_version ?? null, mode: status?.mode ?? null, status, finding: found,
    };
  }

  private engineView(id: string, now: number): EngineView {
    const h = this.host(id, now);
    const code = this.fails.get('engine');
    if (code) {
      // a status that cannot be read is the view's finding, in a 200 (not a refusal)
      let found: EngineFinding | null = null;
      try {
        this.forced('engine', { host: h.dev.address, port: h.ssh_port, user: h.user });
      } catch (e) {
        if (!(e instanceof Refused)) throw e;
        const { type: _t, ...rest } = (e.reply.body as { error: EngineFinding & { type?: string } }).error;
        found = rest;
      }
      return { ...this.view(h, null, found), wizard: this.wizard(null) };
    }
    const status = this.status(h, now);
    return { ...this.view(h, status, null), wizard: this.wizard(status) };
  }

  // ---- after pairing (SRV-833) -------------------------------------------------------------------
  private setMode(id: string, body: unknown, now: number): Omit<ModeView, 'contract_version'> {
    const b = this.fields(body ?? {}, ['mode', 'when', 'ext']);
    const mode = b.mode ?? 'engine';
    if (mode !== 'engine' && mode !== 'chat') no('bad_request', {}, 'mode is engine (bare engine, the default) or chat');
    const when = b.when ?? 'idle';
    if (when !== 'now' && when !== 'idle') no('bad_request', {}, 'when is now or idle');
    if (b.ext != null && (mode !== 'chat' || typeof b.ext !== 'string' || !/^[A-Za-z_][A-Za-z0-9_.]{0,127}$/.test(b.ext))) no('bad_request', {}, 'ext is the extension module chat mode runs (chat only)');
    const h = this.host(id, now);
    this.forced('mode', { host: h.dev.address, port: h.ssh_port, user: h.user });
    const d = h.dev;
    if (this.running(id, now)) no('busy', { host: d.address }, 'a setup job runs');
    const previous = { mode: d.mode as string | null, ext: d.mode === 'chat' ? 'tandemapp.engine_ext' : null, source: d.inferred ? null : 'overlay', inferred: d.inferred };
    if (d.mode === mode && !d.inferred) return { id, mode: mode as string, changed: false, previous, job: null };
    d.mode = mode as Device['mode'];
    d.inferred = false;
    const stamp = new Date(now * 1000).toISOString();
    return {
      id, mode: mode as string, changed: true, previous,
      job: { id: `${stamp.slice(0, 19).replace(/[-:]/g, '')}-${hex(`mode:${id}:${now}`, 6)}`, kind: 'mode', state: 'waiting', phase: 'waiting', when: { mode: when }, created_at: stamp, ended_at: null, message: null, waiting_for: when === 'idle' ? 'the engine to be idle' : null },
    };
  }

  private models(id: string, now: number): Omit<ModelsView, 'contract_version'> {
    const h = this.host(id, now);
    this.forced('models', { address: h.dev.address, port: h.dev.port, host: h.dev.address });
    return { id, models: MODELS.map((m) => m.tile), errors: [] };
  }

  private running(id: string, now: number): Job | null {
    const j = [...this.jobs].reverse().find((x) => x.host === id);
    return j && !this.summary(j, now).finished ? j : null;
  }

  /** the job at `now`: its steps on the clock (the Setup Guide's job document) */
  private doc(j: Job, now: number): { job: Record<string, unknown>; steps: { id: string; label: string; state: string; detail: string | null }[]; state: string; message: string | null } {
    const t = now - j.at - QUEUED_S;
    const fetchS = j.model.on_disk ? 0 : j.model.download_gb / GB_PER_S;
    let at = 0;
    let failed = false;
    const steps = STEPS.map(([id, label, s]) => {
      const len = id === 'fetch' ? fetchS : s;
      const from = at;
      at += len;
      if (failed) return { id, label, state: 'pending', detail: null as string | null };
      if (len === 0) return { id, label, state: t >= from ? 'skipped' : 'pending', detail: t >= from ? (id === 'fetch' ? 'already on the disk' : 'not loaded') : null };
      if (id === 'fetch') {
        const share = Math.min(1, Math.max(0, (t - from) / len));
        const gb = (Math.min(share, j.failAt ?? 1) * j.model.download_gb).toFixed(1);
        if (j.failAt !== null && share >= j.failAt) {
          failed = true;
          return { id, label, state: 'failed', detail: `${gb} of ${j.model.download_gb} GB` };
        }
        if (t >= from && t < at) return { id, label, state: 'running', detail: `${gb} of ${j.model.download_gb} GB` };
      }
      return { id, label, state: t < from ? 'pending' : t < at ? 'running' : 'done', detail: null as string | null };
    });
    const done = !failed && t >= at;
    const state = t < 0 ? 'queued' : failed ? 'failed' : done ? 'done' : 'running';
    const message = failed ? 'The download stopped: the connection to the model’s repository was lost. Nothing was changed.' : null;
    return {
      steps, state, message,
      job: { id: j.id, kind: 'apply', state, created_at: j.at, ended_at: failed || done ? j.at + QUEUED_S + at : null, profile: j.profile, steps: [...steps.map((s) => ({ ...s, started_at: null, ended_at: null })), { id: 'rollback', label: 'Roll back', state: 'pending', started_at: null, ended_at: null, detail: null }], message, changes: [], rolled_back: false, failed_step: failed ? 'fetch' : null, url: '<this box>', model: j.model.tile.id, summary: {}, retry_of: null },
    };
  }

  private summary(j: Job, now: number): JobProgress {
    const { steps, state, message } = this.doc(j, now);
    const cur = steps.find((s) => s.state === 'running') ?? null;
    if (state === 'done' && j.model.tile.name !== this.hosts.get(j.host)?.dev.model) {
      const d = this.hosts.get(j.host)?.dev;
      if (d) d.model = j.model.tile.name;
    }
    return { state, finished: ['done', 'failed', 'rolled_back', 'cancelled'].includes(state), step: cur?.id ?? null, label: cur?.label ?? null, detail: cur?.detail ?? null, downloading: cur?.id === 'fetch', steps_done: steps.filter((s) => s.state === 'done' || s.state === 'skipped').length, steps: steps.length, message };
  }

  private pull(id: string, body: unknown, now: number): Omit<PullView, 'contract_version'> {
    const b = this.fields(body, ['model', 'profile', 'dry_run'], ['model']);
    if (typeof b.model !== 'string' || !MODEL_RE.test(b.model)) no('bad_request', {}, "model is a model id of the engine's Setup Guide");
    if (b.profile != null && (typeof b.profile !== 'string' || !PROFILE_RE.test(b.profile))) no('bad_request', {}, 'profile is a weights profile id of that model');
    if (b.dry_run != null && typeof b.dry_run !== 'boolean') no('bad_request', {}, 'dry_run is true or false');
    const h = this.host(id, now);
    const model = b.model as string;
    this.forced('pull', { address: h.dev.address, port: h.dev.port, host: h.dev.address, model });
    const m = MODELS.find((x) => x.tile.id === model);
    if (!m) return no('unknown_model', { model }, `no model '${model}'`);
    const profile = (b.profile as string | undefined) ?? m.profile;
    const plan = { ok: m.errors.length === 0, errors: m.errors, warnings: [], download_gb: m.download_gb, on_disk: m.on_disk, profile };
    if (!plan.ok) no('setup_refused', { model }, m.errors.join(' '));
    const out: Omit<PullView, 'contract_version'> = { id, model, profile, plan, job: null, progress: null };
    if (b.dry_run) return out;
    if (this.running(id, now)) no('busy', {}, 'a job runs; one setup job at a time');
    const stamp = new Date(now * 1000).toISOString().slice(0, 19).replace(/[-:]/g, '');
    const j: Job = { id: `${stamp}-${hex(`job:${id}:${now}:${this.jobs.length}`, 6)}`, host: id, model: m, profile, at: now, failAt: this.failJob && !m.on_disk ? 0.4 : null };
    this.failJob = false;
    this.jobs.push(j);
    return { ...out, job: this.doc(j, now).job as PullView['job'], progress: this.summary(j, now) };
  }

  private progress(id: string, jid: string | null, now: number): Omit<ProgressView, 'contract_version'> {
    const h = this.host(id, now);
    this.forced('progress', { address: h.dev.address, port: h.dev.port, host: h.dev.address });
    let j: Job | null;
    if (jid !== null) {
      if (!JOB_RE.test(jid)) no('bad_request', {}, 'job is a Setup Guide job id');
      j = this.jobs.find((x) => x.id === jid && x.host === id) ?? null;
      if (!j) return no('setup_refused', {}, 'no job', 404);
    } else j = this.running(id, now);
    return { id, job: j ? (this.doc(j, now).job as ProgressView['job']) : null, progress: j ? this.summary(j, now) : null };
  }
}

/** the facts a forced code carries besides those its sentence names */
const FACT_OF: Record<string, string[]> = {
  wrong_code: ['tries_left'],
  rate_limited: ['retry_after'],
  host_key_mismatch: ['expected', 'offered'],
  host_key_changed: ['pinned', 'offered', 'host'],
  host_key_unconfirmed: ['offered', 'host'],
  auth_failed: ['host', 'user'],
  not_installed: ['dir', 'host'],
  unknown_model: ['model'],
};
/** the other side's own words for a forced code whose sentence holds them */
const DETAIL_OF: Record<string, string> = {
  bad_request: 'address is a host name or an IP address',
  lan_failed: 'PermissionError',
  key_refused: '/home/spark/.ssh is writable by group or others',
  engine_failed: 'the engine could not write its key file',
  ssh_failed: 'Connection closed by remote host',
  ctl_failed: 'the control command ended with exit status 70',
  busy: 'a restart runs',
  setup_refused: 'the selection asks for more memory than this machine has',
};
