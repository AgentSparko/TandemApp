// The settings interface in the mock (`/v1/settings/*`, contracts/settings-v1; VIS-280, SRV-845), as
// the controller answers it (proxy/settings.py, settingscopes.py, settingsengine.py): five scopes
// with the same six operations, a change set planned and applied with one confirm (a second one for
// a key that feeds an engine's disk-cache fingerprint, and one for a hook's full payload outside the
// LAN), versions, restore, the engine restarts a change scheduled and the audit rows. No answer
// holds a secret.
//
// A sixth scope, `plugins` (settings-v1 1.1), has one target an installed plugin: its keys come
// from its manifest (mock/plugins.ts holds the registry, with its own API).
//
// What it holds:
//   app        chat and conductor, on or off: the same switches as What runs here (mock/appsettings.ts)
//   backends   `engine` (TandemLLM; its url and enabled are set by TANDEM_BACKENDS: locked) and
//              `cloud` (a key stored); two routing rules, the last one the environment's; two users
//   conductor  settings, router, roles, packages, monitor, residency, deciders, profiles.default and
//              profiles.safe (locked). A router without `rules` is an invalid document
//   engine     `eh-desk` (spark-desk, over SSH: its mode as `mode` and `mode.ext` (QSE_MODE and
//              QSE_EXT underneath, the chat extension not installed yet), MAX_QUEUE, QSE_ALLOW_FROM
//              hot, QWEN38_PREFIX_CHUNK feeds the fingerprint, HF_TOKEN and PORT not managed) and
//              `http:bench` (HTTP only: read-only, its hot keys); plus every engine host paired in
//              this mock. The `empty` fleet starts with no engine target. Chat mode installs this
//              controller's wheel first when the host lacks the extension (a warning in the plan)
//   hooks      the hook registry and the decision providers (proxy/hooksettings.py), held in
//              mock/hooks.ts with their own two APIs: no key until `control({hooks: true})` fills
//              it with three hooks (`audit` http on the LAN, `export` http outside it, `guard`
//              builtin) and two providers (`classes` systemone-http with its key set, `own`
//              builtin). The mock fleet holds the demo set unless it is `empty`
//
// A restart follows the clock: scheduled (an `at=` time ahead), draining (the engine is busy for
// `busy_s`, 6 s by default; the gateway answers 503 with Retry-After meanwhile), restarting (4 s),
// warming (3 s), done. A plan's restart estimate is 180 s until a restart of that host ended,
// then the 7 s it took. `control()` (POST /__mock/settings) sets what no click produces:
//   {restart: "fail" | "timeout" | "ok"}   how the next restart ends
//   {busy_s: N}                            how long the engine stays busy after the drain starts
//   {fail: "<route>:<error type>" | null}  a route answers that refusal until cleared (routes:
//                                          overview, schema, values, versions, plan, apply,
//                                          restarts, cancel, audit; `apply-<scope>` makes that
//                                          scope's part fail inside an apply)
//   {busy: true | false}                   an apply answers 409 busy
//   {move: true}                           the settings move under every plan read so far (409 stale_plan)
//   {hooks: true | false}                  the hook registry holds its samples, or nothing
//   {trust: true | false}                  the caller has pairing's rights (the full payload)
//   {unreachable: "<engine target>" | null} that engine host does not answer over SSH
//   {wheel: false | true}                  this controller has no wheel of its own to install the
//                                          chat extension from (409 extension_missing)
//   plugins    one target an installed plugin (mock/plugins.ts): `enabled`, `settings.<property>`
//              from its settings schema, `secrets.<NAME>` write-only, `capabilities.<capability>`
//              and, for a manifest that asks the host, `trust`. Enabling and a grant take
//              pairing's rights ({trust: false}: 403 trust_required); prompts:full,
//              engine:control, a network host outside the LAN and trust host are the plan's
//              `grant_keys` and need `confirm_grant`

import { createHash } from 'node:crypto';
import { MockHooks, type MockHook, type MockProvider } from './hooks.ts';
import { MockPlugins, needsConfirm } from './plugins.ts';
import { validateField } from '../src/lib/plugins.ts';
import { schemaForm } from '../src/lib/schema-form.ts';
import type { MockAuth } from './auth.ts';
import type { AuditRow, Change, DiffRow, KeySpec, KeyType, KeyValue, PartPlan, PartResult, Plan, Restart, RestartClass, RestartEstimate, RestoreRef, Result, Scope, Target, Version } from '../src/api/settings.ts';

export type Reply = { status: number; body: unknown; headers?: Record<string, string> };

const CONTRACT = '1.2';
/** a controller without accounts; with them `auth` and `api` stand after `app` (settings-v1 1.2) */
const SCOPES: Scope[] = ['app', 'backends', 'conductor', 'engine', 'hooks', 'plugins'];
const WITH_ACCOUNTS: Scope[] = ['app', 'auth', 'api', 'backends', 'conductor', 'engine', 'hooks', 'plugins'];
const OPS = ['schema', 'get', 'plan', 'apply', 'versions', 'restore'];
const REPLAY_MS = 300_000;
const RESTART_MS = 4000;
const WARM_MS = 3000;

/** every error type with its HTTP status (contracts/settings-v1/scopes.json `errors`) */
export const SETTINGS_ERRORS: Record<string, number> = {
  bad_request: 400, target_required: 400, unknown_field: 400, not_found: 404, no_scope: 404, no_target: 404, no_version: 404, no_restart: 404,
  confirm_required: 428, fingerprint_confirm_required: 428, grant_confirm_required: 428, stale_plan: 409, busy: 409, read_only: 409, too_late: 409, ended: 409, engine_busy: 409,
  engine_refused: 409, host_key_changed: 409, no_http: 409, extension_missing: 409, unknown_key: 422, locked: 422, bad_value: 422, invalid_document: 422, restore_invalid: 422,
  store_refused: 422, engine_unreachable: 502, engine_failed: 502, engine_too_old: 502, engine_timeout: 504, unauthorized: 401, cross_site: 403,
  cross_origin: 403, trust_required: 403, json_only: 415, token_refused: 403, owner_required: 403, admin_required: 403, password_first: 409,
};
const SENTENCE: Record<string, string> = {
  busy: 'another change set is being applied: plan again when it is done',
  engine_unreachable: 'ssh: connect to host 192.0.2.80 port 22: Connection timed out',
  engine_timeout: 'tandem remote settings get: no answer in 30 s',
  engine_failed: 'tandem remote settings plan: exit 1',
  engine_too_old: 'tandem remote: unknown verb settings',
  engine_busy: 'a job runs on the engine host already (upgrade)',
  engine_refused: 'the engine refused: the overlay is locked by its Setup Guide',
  host_key_changed: 'the engine host offers another SSH host key than the pinned one: nothing ran',
  no_http: 'a hot change goes through the engine’s HTTP route, which this controller cannot reach',
  store_refused: 'the store refused the write',
  read_only: 'this server’s settings are read-only',
  unauthorized: 'a session or an admin token is required',
  cross_site: 'this request came from another site',
  cross_origin: 'this request came from another origin',
  json_only: 'the body must be JSON (Content-Type: application/json)',
};

/** the refusals about one key's value (proxy/settings.py KEY_ERRORS) */
const KEY_ERRORS = ['unknown_key', 'locked', 'bad_value', 'invalid_document', 'extension_missing'];

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
function no(type: string, message: string, facts: Record<string, unknown> = {}): never {
  throw new Refuse(SETTINGS_ERRORS[type] ?? 400, type, message, facts);
}

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`;
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${stable((v as Record<string, unknown>)[k])}`).join(',')}}`;
  return JSON.stringify(v) ?? 'null';
}
const digest = (v: unknown): string => createHash('sha256').update(stable(v)).digest('hex');
const copy = <T>(v: T): T => JSON.parse(JSON.stringify(v ?? null)) as T;
const iso = (ms: number | null): string | null => (ms == null ? null : new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'));
/** `A feeds`, `A, B feed`: the verb agrees with the number of keys (proxy/settings.py `feeds`) */
const feeds = (keys: string[]): string => `${keys.join(', ')} ${keys.length === 1 ? 'feeds' : 'feed'}`;
const RANK: RestartClass[] = ['none', 'app', 'hot', 'engine'];
const maxRestart = (xs: RestartClass[]): RestartClass => xs.reduce<RestartClass>((a, b) => (RANK.indexOf(b) > RANK.indexOf(a) ? b : a), 'none');

function spec(key: string, type: KeyType, o: Partial<KeySpec> & { locked?: string } = {}): KeySpec {
  const secret = !!o.secret;
  const out: KeySpec = { key, type: secret ? 'secret' : type, restart: o.restart ?? 'none', fingerprint: !!o.fingerprint, secret, write_only: secret, writable: o.writable ?? !o.locked, nullable: !!o.nullable, default: secret ? null : (o.default ?? null) };
  if (o.values) out.values = o.values;
  if (o.locked) {
    out.why = `set by ${o.locked}`;
    out.variable = o.locked;
  } else if (o.why) out.why = o.why;
  if (o.text) out.text = o.text;
  return out;
}

function change(key: string, from: unknown, to: unknown, o: { was_set: boolean; is_set: boolean; restart?: RestartClass; fingerprint?: boolean; secret?: boolean; diff?: DiffRow[] }): Change {
  const out: Change = { key, from: o.secret ? null : from, to: o.secret ? null : to, was_set: o.was_set, is_set: o.is_set, secret: !!o.secret, restart: o.restart ?? 'none', fingerprint: !!o.fingerprint };
  if (o.diff) {
    out.diff = o.diff.slice(0, 50);
    out.diff_count = o.diff.length;
  }
  return out;
}

/** what a scope plans: its changes and what it read (bound into the confirm, never shown) */
interface ScopePlan {
  changes: Change[];
  restart: RestartClass;
  fingerprint_keys: string[];
  /** a hook's full payload outside the LAN: the second confirm */
  grant_keys?: string[];
  warnings: string[];
  state: string;
  native: Record<string, unknown>;
  /** an engine restart's own length */
  estimate?: RestartEstimate | null;
}
const planOf = (changes: Change[], state: unknown, o: Partial<ScopePlan> = {}): ScopePlan => ({
  changes,
  restart: o.restart ?? maxRestart(changes.map((c) => c.restart)),
  fingerprint_keys: (o.fingerprint_keys ?? changes.filter((c) => c.fingerprint).map((c) => c.key)).sort(),
  grant_keys: o.grant_keys ?? [],
  warnings: o.warnings ?? [],
  state: digest(state),
  native: o.native ?? {},
  estimate: o.estimate ?? null,
});
/** when neither the controller nor the engine knows how long a restart takes (proxy/settingsengine.py) */
const DEFAULT_RESTART_S = 180;

interface Applied {
  version_before: string | null;
  version_after: string | null;
  restore: RestoreRef[];
  restart?: Restart;
}
interface Ctx {
  who: string;
  change_set: string;
  when: string | null;
  deadline_s: number | null;
  note: string | null;
  now: number;
}

interface ScopeImpl {
  targets(): Target[];
  schema(t: string): { writable: boolean; why: string | null; keys: KeySpec[] };
  get(t: string): { writable: boolean; why: string | null; version: string | null; values: KeyValue[] };
  plan(t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan;
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied;
  versions(t: string): { current: string | null; versions: Version[] };
  planRestore(t: string, version: string): ScopePlan;
}

const HERE: Target[] = [{ id: 'controller', label: 'this controller', writable: true, why: null }];
type Val = { value: unknown; set: boolean; source: string; extra?: Partial<KeyValue> };

/** A scope of flat keys whose versions are its values after each change (app, backends, hooks). */
abstract class KeyValueScope implements ScopeImpl {
  private kept: { n: number; doc: Record<string, unknown>; at: number; who: string | null; note: string; change_set: string | null; keys: string[] }[] = [];
  constructor(protected name: Scope) {}
  abstract spec(): Record<string, KeySpec>;
  abstract values(): Record<string, Val>;
  abstract check(sets: Record<string, unknown>, unsets: string[], spec: Record<string, KeySpec>): void;
  abstract write(sets: Record<string, unknown>, unsets: string[]): void;
  writable(): [boolean, string | null] {
    return [true, null];
  }
  targets(): Target[] {
    return HERE;
  }
  schema() {
    const [writable, why] = this.writable();
    const s = this.spec();
    return { writable, why, keys: Object.keys(s).sort().map((k) => s[k]) };
  }
  private snapshot(): Record<string, unknown> {
    const s = this.spec();
    return Object.fromEntries(Object.entries(this.values()).filter(([k]) => !s[k]?.secret).map(([k, v]) => [k, copy(v.value)]));
  }
  get() {
    const [writable, why] = this.writable();
    const vals = this.values();
    const last = this.kept[this.kept.length - 1];
    const values = Object.keys(vals).sort().map((k) => ({ key: k, value: this.spec()[k]?.secret ? null : vals[k].value, set: vals[k].set, source: vals[k].source, ...vals[k].extra }));
    return { writable, why, version: last && stable(last.doc) === stable(this.snapshot()) ? String(last.n) : null, values };
  }
  plan(_t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    const [ok, why] = this.writable();
    if (!ok) no('read_only', why ?? `the ${this.name} scope is read-only here`);
    const s = this.spec();
    const all = [...Object.keys(sets), ...unsets];
    const unknown = all.filter((k) => !s[k]).sort();
    if (unknown.length) no('unknown_key', `not a key of the ${this.name} scope: ${unknown.join(', ')}`, { keys: unknown });
    const locked = all.find((k) => !s[k].writable);
    if (locked) no('locked', `${locked} cannot be changed here: ${s[locked].why ?? 'read-only'}`, { key: locked, ...(s[locked].variable ? { variable: s[locked].variable } : {}) });
    this.check(sets, unsets, s);
    const vals = this.values();
    const changes: Change[] = [];
    for (const k of [...new Set(all)].sort()) {
      const cur = vals[k] ?? { value: null, set: false, source: '' };
      if (k in sets) {
        if (!s[k].secret && cur.set && stable(cur.value) === stable(sets[k])) continue;
        if (sets[k] === null && !cur.set) continue; // nothing on nothing is no change
        changes.push(change(k, cur.value, sets[k], { was_set: cur.set, is_set: sets[k] !== null, restart: s[k].restart, fingerprint: s[k].fingerprint, secret: s[k].secret }));
      } else {
        if (!cur.set) continue;
        changes.push(change(k, cur.value, s[k].default, { was_set: cur.set, is_set: false, restart: s[k].restart, fingerprint: s[k].fingerprint, secret: s[k].secret }));
      }
    }
    return planOf(changes, all.sort().map((k) => [k, s[k].secret ? vals[k]?.set : vals[k]?.value, vals[k]?.set, vals[k]?.source]), { native: { sets } });
  }
  private record(ctx: Ctx, note: string, keys: string[], who: string | null = ctx.who): number {
    const n = (this.kept[this.kept.length - 1]?.n ?? 0) + 1;
    this.kept.push({ n, doc: this.snapshot(), at: ctx.now, who, note, change_set: ctx.change_set, keys });
    return n;
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    const last = this.kept[this.kept.length - 1];
    const before = last && stable(last.doc) === stable(this.snapshot()) ? last.n : this.record(ctx, 'as found before a change', [], null);
    const keys = p.changes.map((c) => c.key);
    const all = p.native.sets as Record<string, unknown>;
    const sets = Object.fromEntries(keys.filter((k) => k in all).map((k) => [k, all[k]]));
    this.write(sets, keys.filter((k) => !(k in sets)));
    const after = this.record(ctx, ctx.note ?? (p.native.version != null ? `restore of version ${p.native.version}` : `change set ${ctx.change_set}`), keys);
    return { version_before: String(before), version_after: String(after), restore: [{ scope: this.name, target: t, version: String(before) }] };
  }
  versions() {
    const cur = this.get().version;
    const s = this.spec();
    return {
      current: cur,
      versions: [...this.kept].reverse().map((r) => ({ id: String(r.n), at: iso(r.at), who: r.who, note: r.note, change_set: r.change_set, keys: r.keys, current: String(r.n) === cur, values: Object.fromEntries(Object.entries(r.doc).filter(([k]) => !s[k]?.secret)) })),
    };
  }
  planRestore(t: string, version: string): ScopePlan {
    const got = /^\d+$/.test(version) ? this.kept.find((r) => r.n === Number(version)) : undefined;
    if (!got) no('no_version', `no version '${version}' of the ${this.name} scope`);
    const s = this.spec();
    const cur = this.snapshot();
    const sets: Record<string, unknown> = {};
    const warnings: string[] = [];
    const gone = Object.keys(got!.doc).filter((k) => !s[k]).sort();
    if (gone.length) warnings.push(`${gone.length} key(s) of version ${version} no longer exist and stay as they are: ${gone.join(', ')}`);
    const fixed: string[] = [];
    for (const [k, v] of Object.entries(got!.doc)) {
      if (!s[k] || s[k].secret || stable(cur[k]) === stable(v)) continue;
      if (!s[k].writable) fixed.push(k);
      else sets[k] = v;
    }
    if (fixed.length) warnings.push(`${fixed.join(', ')}: set elsewhere now (the environment), not restored`);
    if (Object.values(s).some((x) => x.secret)) warnings.push('secrets are not kept in versions: they stay as they are');
    const p = Object.keys(sets).length ? this.plan(t, sets, []) : planOf([], ['none', version, this.get().version]);
    p.warnings = [...warnings, ...p.warnings];
    p.native = { sets, version: Number(version) };
    return p;
  }
}

// ---- app ------------------------------------------------------------------------------------------
export interface PartSwitches {
  on: Record<'chat' | 'conductor', boolean>;
  switched: Record<'chat' | 'conductor', boolean>;
}

class AppScope extends KeyValueScope {
  constructor(private parts: PartSwitches) {
    super('app');
  }
  spec() {
    return {
      chat: spec('chat', 'enum', { values: ['on', 'off'], default: 'off', text: 'Chat: this controller’s own chatd, or the forward to another host’s' }),
      conductor: spec('conductor', 'enum', { values: ['on', 'off'], default: 'off', text: 'the conductor in front of the backends' }),
    };
  }
  values() {
    const one = (k: 'chat' | 'conductor'): Val => ({ value: this.parts.on[k] ? 'on' : 'off', set: this.parts.switched[k], source: this.parts.switched[k] ? 'page' : 'install' });
    return { chat: one('chat'), conductor: one('conductor') };
  }
  check(sets: Record<string, unknown>) {
    for (const [k, v] of Object.entries(sets)) if (v !== 'on' && v !== 'off') no('bad_value', `${k} is on or off`, { key: k });
  }
  write(sets: Record<string, unknown>, unsets: string[]) {
    for (const k of Object.keys(sets) as ('chat' | 'conductor')[]) {
      this.parts.on[k] = sets[k] === 'on';
      this.parts.switched[k] = true;
    }
    for (const k of unsets as ('chat' | 'conductor')[]) {
      this.parts.on[k] = false; // the install's choice
      this.parts.switched[k] = false;
    }
  }
}

// ---- auth, api (SRV-871, with accounts only) --------------------------------------------------------
interface AccountKey {
  type: KeyType;
  def: unknown;
  text: string;
  values?: string[];
  lo?: number;
  hi?: number;
  /** who writes it: an owner, but for the one key an admin switches */
  admin?: boolean;
  /** it lets other devices in: never from local mode */
  password?: boolean;
  restart?: RestartClass;
}
const days = (def: number, text: string, lo = 1, hi = 400): AccountKey => ({ type: 'integer', def, text, lo, hi });
/** proxy/settingscopes.py AuthScope.KEYS and ApiScope.KEYS */
export const ACCOUNT_KEYS: Record<'auth' | 'api', Record<string, AccountKey>> = {
  auth: {
    provider: { type: 'enum', def: 'local', values: ['local'], password: true, text: 'who answers who someone is: local (the accounts here); ldap, oidc and trusted-header come later' },
    local_mode: { type: 'enum', def: 'auto', values: ['auto', 'off'], text: 'auto: a bare install bound to this machine with no owner password asks nothing on this machine; off: never' },
    local_login: { type: 'enum', def: 'on', values: ['on', 'owners', 'hidden'], text: 'the page’s sign-in form: on, owners only, or hidden (a provider’s button alone)' },
    chat_min_role: { type: 'enum', def: 'operator', values: ['operator', 'viewer'], text: 'who may use the Chat tab' },
    'session.idle_days': days(30, 'a session ends after this many idle days'),
    'session.absolute_days': days(180, 'a session ends this many days after the sign-in'),
    'password.min_length': { type: 'integer', def: 12, lo: 12, hi: 256, text: 'a new password’s length at least' },
  },
  api: {
    'openai.exposure': { type: 'enum', def: 'this-machine', values: ['this-machine', 'network'], password: true, text: 'who may use the OpenAI API with a token: this machine, or any device (network)' },
    'anthropic.exposure': { type: 'enum', def: 'this-machine', values: ['this-machine', 'network'], password: true, text: 'who may use the Anthropic API with a token: this machine, or any device (network)' },
    listen: { type: 'string', def: '127.0.0.1', password: true, restart: 'app', text: 'the address TandemApp listens on (TANDEM_HOST in its env file), applied at the next restart' },
    'tokens.default_days': days(90, 'a new token’s days when none are asked', 1, 3650),
    'tokens.max_days': days(0, 'a token lives at most this many days (0: no cap)', 0, 3650),
    'tokens.max_days_external': days(30, 'the cap for an external provider’s accounts (later)', 0, 3650),
    'tokens.viewers_may_create': { type: 'enum', def: 'off', values: ['on', 'off'], admin: true, text: 'viewers may make inference tokens' },
  },
};

class AccountKeys extends KeyValueScope {
  private at = 0;
  constructor(
    private which: 'auth' | 'api',
    private auth: MockAuth,
  ) {
    super(which);
  }
  private get keys(): Record<string, AccountKey> {
    return ACCOUNT_KEYS[this.which];
  }
  private def(k: string): unknown {
    if (k === 'openai.exposure' && this.auth.hadLegacyKeys) return 'network';
    return k === 'listen' ? this.auth.bind : this.keys[k].def;
  }
  spec() {
    return Object.fromEntries(
      Object.entries(this.keys).map(([k, d]) => {
        const pod = k === 'listen' && this.auth.kubernetes;
        return [k, spec(k, d.type, { values: d.values, default: this.def(k), restart: d.restart ?? 'none', text: d.text, writable: !pod, ...(pod ? { why: 'the pod listens on every address; the chart’s ingress decides who reaches it' } : {}) })];
      }),
    );
  }
  values() {
    return Object.fromEntries(
      Object.keys(this.keys).map((k): [string, Val] => {
        const full = `${this.which}.${k}`;
        if (full in this.auth.settings) return [k, { value: this.auth.settings[full], set: true, source: 'page' }];
        return [k, { value: this.def(k), set: false, source: k === 'listen' ? 'install' : k === 'openai.exposure' && this.auth.hadLegacyKeys ? 'migration' : 'default' }];
      }),
    );
  }
  check(sets: Record<string, unknown>, unsets: string[]) {
    const c = this.auth.caller;
    const n = this.which;
    if (c === null || c.kind === 'token') no('token_refused', `the ${n} settings take a signed-in owner, never an API token`);
    for (const k of [...new Set([...Object.keys(sets), ...unsets])].sort()) {
      const d = this.keys[k];
      if (!this.auth.may(c, d.admin ? 'users' : 'owner')) no(d.admin ? 'admin_required' : 'owner_required', `${n}.${k} is changed by ${d.admin ? 'an admin' : 'an owner'}`, { key: k });
      if (d.password && c!.kind === 'local') no('password_first', `${n}.${k} lets other devices in: set the owner’s password first`, { key: k });
    }
    for (const [k, v] of Object.entries(sets)) {
      const d = this.keys[k];
      if (d.type === 'enum' && !d.values!.includes(v as string)) no('bad_value', `${n}.${k} is one of ${d.values!.join(', ')}`, { key: k });
      if (d.type === 'integer' && (typeof v !== 'number' || !Number.isInteger(v) || v < d.lo! || v > d.hi!)) no('bad_value', `${n}.${k} is a whole number from ${d.lo} to ${d.hi}`, { key: k });
      if (d.type === 'string' && (typeof v !== 'string' || !/^[A-Za-z0-9.:[\]-]{1,253}$/.test(v))) no('bad_value', 'api.listen is an address: 127.0.0.1, 0.0.0.0, ::, or a name', { key: k });
      if (k === 'listen' && this.auth.setupRequired()) no('password_first', 'api.listen lets other devices in: an owner with a password first', { key: k });
    }
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    this.at = ctx.now;
    return super.apply(t, p, ctx);
  }
  write(sets: Record<string, unknown>, unsets: string[]) {
    for (const k of [...Object.keys(sets), ...unsets].sort()) {
      const full = `${this.which}.${k}`;
      const from = this.values()[k].value;
      if (k in sets) this.auth.settings[full] = sets[k];
      else delete this.auth.settings[full];
      this.auth.wrote(this.at, full, from, k in sets ? sets[k] : this.def(k), k in sets);
    }
  }
}

// ---- backends -------------------------------------------------------------------------------------
type Field = { type: KeyType; nullable?: boolean; values?: string[]; default?: unknown; min?: number; max?: number };
const URL_RE = /^https?:\/\/[^\s/@]+(\/[^\s]*)?$/;
const FIELDS: Record<string, Field> = {
  label: { type: 'string', nullable: true },
  url: { type: 'string' },
  enabled: { type: 'boolean', default: true },
  models_mode: { type: 'enum', values: ['discover', 'list'], default: 'discover' },
  models: { type: 'list', default: [] },
  fallback: { type: 'list', default: [] },
  max_inflight: { type: 'integer', nullable: true, min: 1, max: 4096 },
  inject_usage: { type: 'boolean' },
  'timeouts.connect': { type: 'number', min: 0.1, max: 120 },
  'timeouts.first_byte': { type: 'number', min: 1, max: 3600 },
  'timeouts.idle': { type: 'number', min: 1, max: 3600 },
  'harness.mode': { type: 'enum', values: ['off', 'shadow', 'on'], default: 'off' },
  'harness.profile': { type: 'string', nullable: true },
  'host.mode': { type: 'enum', values: ['auto', 'same', 'collector', 'exporter', 'off'], default: 'auto' },
  'host.url': { type: 'string', nullable: true },
  'host.dcgm_url': { type: 'string', nullable: true },
};
interface Backend {
  id: string;
  f: Record<string, unknown>;
  locks: Record<string, string>;
  key: { set: boolean; hint: string | null; value?: string };
}
const backend = (id: string, f: Record<string, unknown>, locks: Record<string, string> = {}, key: Backend['key'] = { set: false, hint: null }): Backend => ({
  id, locks, key,
  f: { label: null, url: '', enabled: true, models_mode: 'discover', models: [], fallback: [], max_inflight: null, inject_usage: true, 'timeouts.connect': 5, 'timeouts.first_byte': 120, 'timeouts.idle': 300, 'harness.mode': 'off', 'harness.profile': null, 'host.mode': 'auto', 'host.url': null, 'host.dcgm_url': null, ...f },
});

class BackendsScope extends KeyValueScope {
  backends: Backend[] = [
    backend('engine', { label: 'TandemLLM engine', url: 'http://127.0.0.1:8000/v1' }, { url: 'TANDEM_BACKENDS', enabled: 'TANDEM_BACKENDS' }),
    backend('cloud', { label: 'Cloud', url: 'https://api.example.com/v1', models_mode: 'list', models: ['example-chat', 'example-reasoner'], fallback: ['engine'], max_inflight: 8 }, {}, { set: true, hint: 'a1b2c3' }),
  ];
  routes: { pattern: string; backend: string; enabled: boolean; variable?: string }[] = [
    { pattern: 'example-*', backend: 'cloud', enabled: true },
    { pattern: '*', backend: 'engine', enabled: true, variable: 'TANDEM_ROUTE_DEFAULT' },
  ];
  users = [
    { id: 1, label: 'laptop', enabled: true },
    { id: 2, label: 'ci', enabled: false },
  ];
  constructor() {
    super('backends');
  }
  spec() {
    const out: Record<string, KeySpec> = {};
    for (const b of this.backends) {
      for (const [f, d] of Object.entries(FIELDS)) out[`backends.${b.id}.${f}`] = spec(`backends.${b.id}.${f}`, d.type, { values: d.values, nullable: d.nullable, default: d.default, locked: b.locks[f] });
      out[`backends.${b.id}.key`] = spec(`backends.${b.id}.key`, 'string', { secret: true, nullable: true, locked: b.locks.key, text: 'the backend’s API key' });
    }
    const env = this.routes.filter((r) => r.variable).map((r) => r.variable);
    out.routes = spec('routes', 'list', { default: [], text: `the routing rules in order: a model id that matches a pattern goes to its backend${env.length ? `; ${env.join(', ')} keep their place and value` : ''}` });
    for (const u of this.users) out[`users.${u.id}.enabled`] = spec(`users.${u.id}.enabled`, 'boolean', { default: true, text: `the key of ${u.label}` });
    return out;
  }
  values() {
    const out: Record<string, Val> = {};
    for (const b of this.backends) {
      // a field that holds nothing is unset, as the database has it
      for (const f of Object.keys(FIELDS)) out[`backends.${b.id}.${f}`] = { value: copy(b.f[f]), set: b.f[f] != null, source: b.locks[f] ? 'env' : 'database', ...(b.locks[f] ? { extra: { variable: b.locks[f] } } : {}) };
      out[`backends.${b.id}.key`] = { value: null, set: b.key.set, source: b.key.set ? 'database' : 'unset', extra: { hint: b.key.hint } };
    }
    out.routes = { value: copy(this.routes), set: true, source: 'database' };
    for (const u of this.users) out[`users.${u.id}.enabled`] = { value: u.enabled, set: true, source: 'database', extra: { label: u.label } };
    return out;
  }
  check(sets: Record<string, unknown>, unsets: string[], s: Record<string, KeySpec>) {
    for (const k of unsets) {
      if (k === 'routes') no('bad_value', 'routes has no unset: set [] for none', { key: k });
      if (!s[k].nullable) no('bad_value', `${k} has no unset: set ${k.startsWith('users.') ? 'true or false' : 'a value'}`, { key: k });
    }
    for (const [k, v] of Object.entries(sets)) {
      const bad = (what: string): never => no('bad_value', `${k} ${what}`, { key: k });
      if (k === 'routes') {
        if (!Array.isArray(v)) bad('is a list of {pattern, backend, enabled}');
        const env = this.routes.filter((r) => r.variable);
        (v as Record<string, unknown>[]).forEach((r, i) => {
          if (!r || typeof r !== 'object' || typeof r.pattern !== 'string' || !r.pattern || typeof r.backend !== 'string' || typeof r.enabled !== 'boolean') no('bad_value', `routes[${i}] is {pattern, backend, enabled, variable?}`, { key: k });
          if (!this.backends.some((b) => b.id === r.backend)) no('bad_value', `routes: rule ${i + 1} names the backend '${r.backend}', which does not exist`, { key: k });
          if (r.variable != null && !env.some((e) => e.variable === r.variable)) no('bad_value', `routes[${i}]: no rule is set by ${r.variable}`, { key: k });
        });
        for (const e of env) if (!(v as { variable?: string }[]).some((r) => r.variable === e.variable)) no('bad_value', `routes: the rule ${e.variable} sets cannot be removed here`, { key: k, variable: e.variable });
        continue;
      }
      if (k.startsWith('users.')) {
        if (typeof v !== 'boolean') bad('is true or false');
        continue;
      }
      const [, bid, ...rest] = k.split('.');
      const f = rest.join('.');
      if (f === 'key') {
        if (v !== null && (typeof v !== 'string' || !v || v.length > 4096)) bad('is a key of at most 4096 characters');
        continue;
      }
      const d = FIELDS[f];
      if (v === null) {
        if (!d.nullable) bad('needs a value');
        continue;
      }
      if (d.type === 'boolean' && typeof v !== 'boolean') bad('is true or false');
      if (d.type === 'enum' && !d.values!.includes(v as string)) bad(`is one of ${d.values!.join(', ')}`);
      if ((d.type === 'integer' || d.type === 'number') && (typeof v !== 'number' || (d.type === 'integer' && !Number.isInteger(v)) || v < d.min! || v > d.max!)) bad(`is a ${d.type === 'integer' ? 'whole number' : 'number'} from ${d.min} to ${d.max}`);
      if (d.type === 'string' && typeof v !== 'string') bad('is text');
      if ((f === 'url' || f.endsWith('_url') || f === 'host.url') && typeof v === 'string' && !URL_RE.test(v)) bad('is an http or https URL');
      if (d.type === 'list' && (!Array.isArray(v) || v.some((x) => typeof x !== 'string' || !x))) bad('is a list of names');
      if (f === 'fallback') for (const x of v as string[]) if (x === bid || !this.backends.some((b) => b.id === x)) bad(`names '${x}', which is ${x === bid ? 'this backend itself' : 'no backend'}`);
    }
  }
  write(sets: Record<string, unknown>, unsets: string[]) {
    for (const [k, v] of [...Object.entries(sets), ...unsets.map((u) => [u, null] as [string, unknown])]) {
      if (k === 'routes') this.routes = copy(v) as typeof this.routes;
      else if (k.startsWith('users.')) this.users.find((u) => u.id === Number(k.split('.')[1]))!.enabled = v as boolean;
      else {
        const [, bid, ...rest] = k.split('.');
        const b = this.backends.find((x) => x.id === bid)!;
        if (rest.join('.') === 'key') b.key = v === null ? { set: false, hint: null } : { set: true, hint: digest(v).slice(0, 6), value: v as string };
        else b.f[rest.join('.')] = copy(v);
      }
    }
  }
}

// ---- hooks ----------------------------------------------------------------------------------------
// The hook registry's adapter (proxy/hooksettings.py): each hook's switches and each decision
// provider's, a provider's API keys write-only, an http hook's secret only as set. Setting a hook's
// payload to `full` is a grant (SEC-16): planned only with pairing's rights (`trust`), and a
// destination outside the LAN adds the second confirm (grant_keys, confirm_grant).
const HOOK_FIELDS: Record<string, { type: KeyType; values?: string[]; nullable?: boolean; default: unknown; text: string }> = {
  enabled: { type: 'boolean', default: true, text: 'called at all' },
  mode: { type: 'enum', values: ['off', 'shadow', 'on'], default: 'on', text: 'off: never called; shadow: called on a copy, nothing applied; on: as its role says' },
  priority: { type: 'integer', default: 100, text: 'the order among the hooks of a stage, low first; ties by id' },
  scope: { type: 'object', nullable: true, default: {}, text: 'the requests it runs on: lists of patterns under models, backends, profiles, users, sessions or client_kinds; unset: every request' },
  policy: { type: 'enum', values: ['open', 'closed'], nullable: true, default: null, text: 'when it fails: open (as if absent) or closed (the request is refused); unset: the stage’s default' },
  timeout_ms: { type: 'integer', nullable: true, default: null, text: 'its own time limit in milliseconds, at most the stage’s budget; unset: the stage’s budget' },
  payload: { type: 'enum', values: ['meta', 'redacted', 'full'], default: 'meta', text: 'what it sees: meta, redacted, or full (prompts and answers as they are: pairing’s rights, and a second confirm outside the LAN)' },
};
const QUESTIONS = ['classify', 'packages', 'deliverable', 'memory_intent', 'memory_gate', 'judge'];
const PROVIDER_FIELDS: Record<string, { type: KeyType; values?: string[]; default: unknown; text: string }> = {
  enabled: { type: 'boolean', default: true, text: 'asked at all' },
  mode: { type: 'enum', values: ['on', 'shadow', 'ab'], default: 'shadow', text: 'on: it answers its questions; shadow: asked beside the conductor, nothing it says is used; ab: it answers for a share' },
  ab_percent: { type: 'integer', default: 0, text: 'in ab, the share (0..100) it answers' },
  sticky: { type: 'enum', values: ['session', 'user', 'request'], default: 'session', text: 'in ab, what the share sticks to' },
  questions: { type: 'object', default: null, text: `the questions it answers (${QUESTIONS.join(', ')}), each {model?, endpoint?, timeout_ms?}` },
};
const SCOPE_KEYS = ['models', 'backends', 'profiles', 'users', 'sessions', 'client_kinds'];
const hint6 = (v: string): string => digest(v).slice(0, 6);

class HooksScope extends KeyValueScope {
  /** the registry the hooks' and the providers' own APIs read and write too (mock/hooks.ts) */
  constructor(private reg: MockHooks) {
    super('hooks');
  }
  private get hooks(): Record<string, MockHook> {
    return this.reg.hooks;
  }
  private get providers(): Record<string, MockProvider> {
    return this.reg.providers;
  }
  /** the caller has pairing's rights (this machine, or the admin) */
  get trust(): boolean {
    return this.reg.trust;
  }
  set trust(on: boolean) {
    this.reg.trust = on;
  }
  /** the registry holds the sample hooks and providers, or none */
  get plugged(): boolean {
    return this.reg.plugged;
  }
  set plugged(on: boolean) {
    this.reg.seed(on);
  }
  writable(): [boolean, string | null] {
    return [true, this.plugged ? null : 'no hook and no decision provider yet: they are added on the hooks’ API (/v1/fleet/hooks, /v1/fleet/decision-providers); their switches show here once there is one'];
  }
  /** key -> where it lands */
  private index(): Record<string, { kind: 'hook' | 'provider'; id: string; field: string; question?: string }> {
    const out: Record<string, { kind: 'hook' | 'provider'; id: string; field: string; question?: string }> = {};
    for (const [id, h] of Object.entries(this.hooks)) {
      for (const f of Object.keys(HOOK_FIELDS)) out[`hooks.${id}.${f}`] = { kind: 'hook', id, field: f };
      if (h.kind === 'http') out[`hooks.${id}.secret`] = { kind: 'hook', id, field: 'secret' };
    }
    for (const [id, p] of Object.entries(this.providers)) {
      for (const f of Object.keys(PROVIDER_FIELDS)) out[`providers.${id}.${f}`] = { kind: 'provider', id, field: f };
      if (p.kind === 'systemone-http') {
        out[`providers.${id}.api_key`] = { kind: 'provider', id, field: 'api_key', question: '*' };
        for (const q of Object.keys(p.vals.questions as object)) out[`providers.${id}.questions.${q}.api_key`] = { kind: 'provider', id, field: 'api_key', question: q };
      }
    }
    return out;
  }
  spec(): Record<string, KeySpec> {
    const out: Record<string, KeySpec> = {};
    for (const [k, w] of Object.entries(this.index())) {
      if (w.kind === 'hook') {
        if (w.field === 'secret') out[k] = spec(k, 'string', { secret: true, writable: false, why: 'made by the hooks’ API and answered there once: rotate it there', text: 'the secret that signs its calls' });
        else {
          const d = HOOK_FIELDS[w.field];
          out[k] = spec(k, d.type, { values: d.values, nullable: !!d.nullable, default: d.default, text: d.text });
        }
      } else if (w.field === 'api_key') out[k] = spec(k, 'string', { secret: true, nullable: true, text: w.question === '*' ? 'its API key, sent as the bearer' : `the API key for ${w.question} alone` });
      else {
        const d = PROVIDER_FIELDS[w.field];
        out[k] = spec(k, d.type, { values: d.values, default: d.default, text: d.text });
      }
    }
    return out;
  }
  values() {
    const out: Record<string, Val> = {};
    for (const [k, w] of Object.entries(this.index())) {
      if (w.kind === 'hook') {
        const h = this.hooks[w.id];
        const extra = { label: h.name };
        if (w.field === 'secret') out[k] = { value: null, set: !!h.secret, source: 'hooks', extra: { ...extra, hint: h.secret ? hint6(h.secret) : null } };
        else {
          const v = h.vals[w.field];
          const empty = v === null || (w.field === 'scope' && !Object.keys(v as object).length);
          out[k] = { value: empty ? null : copy(v), set: !empty, source: 'hooks', extra };
        }
      } else {
        const p = this.providers[w.id];
        const extra = { label: p.name };
        if (w.field === 'api_key') out[k] = { value: null, set: w.question! in p.keys, source: 'providers', extra: { ...extra, hint: w.question! in p.keys ? hint6(p.keys[w.question!]) : null } };
        else out[k] = { value: copy(p.vals[w.field]), set: true, source: 'providers', extra };
      }
    }
    return out;
  }
  check(sets: Record<string, unknown>, unsets: string[], s: Record<string, KeySpec>) {
    const idx = this.index();
    const bad = (k: string, why: string): never => no('bad_value', `${k}: ${why}`, { key: k });
    for (const k of unsets) if (!s[k].nullable) no('bad_value', `${k} has no unset: set a value`, { key: k });
    const next: Record<string, Record<string, unknown>> = {};
    for (const [k, v] of Object.entries(sets)) {
      const w = idx[k];
      if (s[k].secret) {
        if (v !== null && (typeof v !== 'string' || !v || /\s/.test(v))) bad(k, '1..4096 characters, no spaces');
        if (w.question && w.question !== '*') {
          const qs = (sets[`providers.${w.id}.questions`] ?? this.providers[w.id].vals.questions) as Record<string, unknown>;
          if (!(w.question in qs)) bad(k, `${w.id} does not answer ${w.question}`);
        }
        continue;
      }
      if (v === null && s[k].nullable) continue;
      if (s[k].values && !s[k].values!.includes(v as string)) bad(k, `one of ${s[k].values!.join(', ')}`);
      if (s[k].type === 'integer' && !Number.isInteger(v)) bad(k, 'a whole number');
      if (s[k].type === 'boolean' && typeof v !== 'boolean') bad(k, 'true or false');
      if (w.field === 'priority' && Math.abs(v as number) > 100000) bad(k, 'an integer -100000..100000');
      if (w.field === 'timeout_ms' && ((v as number) < 1 || (v as number) > 30000)) bad(k, '1..30000 or unset (the stage’s budget)');
      if (w.field === 'ab_percent' && ((v as number) < 0 || (v as number) > 100)) bad(k, '0..100, the share of sessions the provider answers in ab');
      if (w.field === 'scope' && (!v || typeof v !== 'object' || Array.isArray(v) || Object.keys(v).some((x) => !SCOPE_KEYS.includes(x)))) bad(k, `an object of ${SCOPE_KEYS.join(', ')}`);
      if (w.field === 'questions') {
        if (!v || typeof v !== 'object' || Array.isArray(v) || !Object.keys(v).length) bad(k, `an object of the question ids it answers (${QUESTIONS.join(', ')})`);
        for (const [q, spec_] of Object.entries(v as Record<string, unknown>)) {
          if (!QUESTIONS.includes(q)) bad(k, `no question '${q}' (one of ${QUESTIONS.join(', ')})`);
          if (spec_ && typeof spec_ === 'object' && 'api_key' in spec_) bad(k, `a question’s API key is a key of its own (providers.${w.id}.questions.${q}.api_key)`);
        }
      }
      if (w.kind === 'provider') (next[w.id] ??= {})[w.field] = v;
    }
    // one enabled provider a question on or ab, across the providers as they would be
    const serving: Record<string, string> = {};
    for (const id of Object.keys(this.providers).sort()) {
      const p = { ...this.providers[id].vals, ...(next[id] ?? {}) };
      if (!p.enabled || p.mode === 'shadow') continue;
      for (const q of Object.keys(p.questions as object)) {
        if (serving[q]) {
          const who = next[id] ? id : serving[q];
          no('bad_value', `providers.${who}: ${q} is served ${p.mode} by ${serving[q]} and ${id}; one provider a question on or ab (any number in shadow)`, { key: `providers.${who}.mode` });
        }
        serving[q] = id;
      }
    }
  }
  /** the grants the changed sets make: a hook's payload to full that is not full yet */
  grants(sets: Record<string, unknown>): { key: string; hook: string; destination: string | null; lan: boolean }[] {
    const out: { key: string; hook: string; destination: string | null; lan: boolean }[] = [];
    for (const [k, v] of Object.entries(sets)) {
      const m = /^hooks\.(.+)\.payload$/.exec(k);
      if (!m || v !== 'full' || this.hooks[m[1]].vals.payload === 'full') continue;
      const h = this.hooks[m[1]];
      out.push({ key: k, hook: m[1], destination: h.host, lan: h.lan });
    }
    return out;
  }
  plan(t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    const p = super.plan(t, sets, unsets);
    const changed = new Set(p.changes.map((c) => c.key));
    const grants = this.grants(Object.fromEntries(Object.entries(sets).filter(([k]) => changed.has(k))));
    if (!grants.length) return p;
    if (!this.trust) no('trust_required', `${grants[0].key}: the full payload takes pairing’s rights: set an admin password (App settings) to grant it from another machine, or grant it from this one`, { key: grants[0].key });
    for (const g of grants) p.warnings.push(`${g.key}: the hook ${g.hook} gets the full payload (prompts and answers as they are) at ${g.destination ?? 'this controller'}${g.lan ? '' : ', outside the LAN: apply asks a second time'}`);
    p.grant_keys = grants.filter((g) => !g.lan).map((g) => g.key).sort();
    p.state = digest([p.state, grants]);
    return p;
  }
  write(sets: Record<string, unknown>, unsets: string[]) {
    const idx = this.index();
    const hooks = new Set<string>();
    const providers = new Map<string, string[]>();
    for (const k of [...Object.keys(sets), ...unsets]) {
      const w = idx[k];
      const v = k in sets ? sets[k] : null;
      if (w.kind === 'hook') {
        this.hooks[w.id].vals[w.field] = w.field === 'scope' && v === null ? {} : copy(v);
        hooks.add(w.id);
        continue;
      }
      const keyed = providers.get(w.id) ?? [];
      providers.set(w.id, keyed);
      if (w.field === 'api_key') {
        const keys = this.providers[w.id].keys;
        if (typeof v === 'string') {
          keys[w.question!] = v;
          keyed.push(w.question!);
        } else delete keys[w.question!];
      } else {
        this.providers[w.id].vals[w.field] = copy(v);
        // a question no longer answered takes its key with it
        if (w.field === 'questions') for (const q of Object.keys(this.providers[w.id].keys)) if (q !== '*' && !(q in (v as object))) delete this.providers[w.id].keys[q];
      }
    }
    // the registry's own view moves with it: the version, who changed it, a full payload's grant
    for (const id of hooks) this.reg.touched(id);
    for (const [id, keyed] of providers) this.reg.providerTouched(id, keyed);
  }
}

// ---- conductor ------------------------------------------------------------------------------------
const LOCKED: Record<string, string> = { safe: 'it ships with TandemApp' };
const BUILT_IN: Record<string, Record<string, unknown>> = {
  settings: { mode: 'on', notes: true, max_hidden_tokens: 4096 },
  router: { default: 'main', rules: [{ when: 'vision', to: 'main' }] },
  roles: { main: { model: 'TandemLLM' }, side: { model: 'TandemLLM', lane: 'side' } },
  packages: { enabled: ['core'] },
  monitor: { mode: 'shadow', budget_tokens: 512 },
  residency: { keep_warm: [], idle_unload_s: 900 },
  deciders: { compaction: 'auto', summary: 'auto' },
  'profiles.default': { writer: 'auto', temperature: 0.6 },
  'profiles.safe': { writer: 'off', temperature: 0.2 },
};

function diffOf(a: unknown, b: unknown, path = ''): DiffRow[] {
  if (stable(a) === stable(b)) return [];
  const obj = (x: unknown): x is Record<string, unknown> => !!x && typeof x === 'object' && !Array.isArray(x);
  if (!obj(a) || !obj(b)) return [{ path: path || '.', before: a ?? null, after: b ?? null }];
  return [...new Set([...Object.keys(a), ...Object.keys(b)])].sort().flatMap((k) => diffOf(a[k], b[k], path ? `${path}.${k}` : k));
}

class ConductorScope implements ScopeImpl {
  private docs = new Map<string, { n: number; doc: Record<string, unknown>; at: number | null; who: string | null; note: string }[]>();
  constructor(
    private parts: PartSwitches,
    t0: number,
  ) {
    for (const [k, doc] of Object.entries(BUILT_IN)) this.docs.set(k, [{ n: 0, doc: copy(doc), at: null, who: null, note: 'built in' }]);
    // the router was changed once before: a stored version to restore
    this.docs.get('router')!.push({ n: 1, doc: { default: 'main', rules: [{ when: 'vision', to: 'main' }, { when: 'long', to: 'side' }] }, at: t0 - 86_400_000, who: 'admin', note: 'route long prompts to the side lane' });
  }
  private why(): string | null {
    return this.parts.on.conductor ? null : 'the conductor is off: its documents are kept and read when it is switched on';
  }
  private cur(k: string) {
    const v = this.docs.get(k);
    if (!v) no('unknown_key', `not a document of the conductor: ${k}`, { keys: [k] });
    return v![v!.length - 1];
  }
  private locked(k: string): string | null {
    return k.startsWith('profiles.') ? (LOCKED[k.slice(9)] ?? null) : null;
  }
  targets(): Target[] {
    return HERE;
  }
  schema() {
    return {
      writable: true,
      why: this.why(),
      keys: [...this.docs.keys()].map((k) => {
        const kind = k.startsWith('profiles.') ? `profile ${k.slice(9)}` : k;
        return spec(k, 'document', { restart: 'hot', writable: !this.locked(k), why: this.locked(k) ? `locked: ${this.locked(k)}` : undefined, text: `the conductor’s ${kind} document` });
      }),
    };
  }
  get() {
    return { writable: true, why: this.why(), version: null, values: [...this.docs.keys()].map((k) => ({ key: k, value: copy(this.cur(k).doc), set: this.cur(k).n > 0, source: this.cur(k).n > 0 ? 'stored' : 'built-in', version: `${k}@${this.cur(k).n}` })) };
  }
  private check(k: string, doc: Record<string, unknown>): string[] {
    const errs: string[] = [];
    if (k === 'router' && !Array.isArray(doc.rules)) errs.push('rules: a list of rules is required');
    if (k === 'router' && typeof doc.default !== 'string') errs.push('default: the role every other request goes to is required');
    if (k === 'monitor' && doc.mode !== undefined && !['off', 'shadow', 'on'].includes(doc.mode as string)) errs.push('mode: one of off, shadow, on');
    return errs;
  }
  private docChange(k: string, doc: Record<string, unknown>): Change | null {
    const cur = this.cur(k);
    const errs = this.check(k, doc);
    if (errs.length) no('invalid_document', `${k}: ${errs[0]}`, { key: k, errors: errs });
    const d = diffOf(cur.doc, doc);
    return d.length ? change(k, { version: cur.n }, { version: 'new' }, { was_set: cur.n > 0, is_set: true, restart: 'hot', diff: d }) : null;
  }
  plan(_t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    const changes: Change[] = [];
    const docs: Record<string, { doc?: unknown; restore?: number; base: number }> = {};
    const state: Record<string, number> = {};
    for (const k of [...new Set([...Object.keys(sets), ...unsets])].sort()) {
      const cur = this.cur(k);
      if (this.locked(k)) no('locked', `${k} is locked: ${this.locked(k)}`, { key: k });
      state[k] = cur.n;
      let doc: unknown;
      if (k in sets) {
        doc = sets[k];
        if (!doc || typeof doc !== 'object' || Array.isArray(doc)) no('bad_value', `${k} is a whole document (an object)`, { key: k });
      } else {
        if (cur.n === 0) continue;
        doc = this.docs.get(k)![0].doc;
      }
      const c = this.docChange(k, doc as Record<string, unknown>);
      if (c) {
        changes.push(c);
        docs[k] = { doc, base: cur.n };
      }
    }
    return planOf(changes, state, { warnings: this.parts.on.conductor ? [] : ['the conductor is off: the documents are stored and read when it is switched on'], native: { docs } });
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    const before: string[] = [];
    const after: string[] = [];
    const restore: RestoreRef[] = [];
    const note = ctx.note ?? `settings change set ${ctx.change_set}`;
    for (const [k, item] of Object.entries(p.native.docs as Record<string, { doc?: unknown; restore?: number; base: number }>).sort()) {
      const list = this.docs.get(k)!;
      const doc = item.restore != null ? list.find((v) => v.n === item.restore)!.doc : item.doc;
      const n = list[list.length - 1].n + 1;
      list.push({ n, doc: copy(doc) as Record<string, unknown>, at: ctx.now, who: ctx.who, note: item.restore != null ? `${note}: restore of v${item.restore}` : note });
      before.push(`${k}@${item.base}`);
      after.push(`${k}@${n}`);
      restore.push({ scope: 'conductor', target: t, version: `${k}@${item.base}` });
    }
    return { version_before: before.join(','), version_after: after.join(','), restore };
  }
  versions() {
    const out: Version[] = [];
    for (const [k, list] of this.docs)
      for (const v of [...list].reverse()) out.push({ id: `${k}@${v.n}`, at: iso(v.at), who: v.who, note: v.note, change_set: /cs-[0-9a-f]{12}/.exec(v.note)?.[0] ?? null, keys: [k], current: v === list[list.length - 1] });
    return { current: null, versions: out };
  }
  planRestore(_t: string, version: string): ScopePlan {
    const i = version.lastIndexOf('@');
    const k = version.slice(0, i);
    const n = Number(version.slice(i + 1));
    const got = i > 0 && Number.isInteger(n) ? this.docs.get(k)?.find((v) => v.n === n) : undefined;
    if (!got) no('no_version', `no version '${version}' of the conductor: <document>@<n>`);
    const cur = this.cur(k);
    let c: Change | null;
    try {
      c = this.docChange(k, got!.doc);
    } catch (e) {
      if (e instanceof Refuse) no('restore_invalid', `version ${version} is not valid now: ${e.message}`, { key: k });
      throw e;
    }
    return planOf(c ? [c] : [], { [k]: cur.n }, { native: { docs: c ? { [k]: { restore: n, base: cur.n } } : {} } });
  }
}

// ---- engine ---------------------------------------------------------------------------------------
type EngineKey = { default: string | null; type: KeyType; restart: RestartClass; fingerprint?: boolean; managed: boolean; why?: string; values?: string[] };
export const ENGINE_KEYS: Record<string, EngineKey> = {
  MAX_QUEUE: { default: '16', type: 'integer', restart: 'engine', managed: true },
  QSE_ALLOW_FROM: { default: '', type: 'cidrs', restart: 'hot', managed: true },
  QSE_MODE: { default: 'engine', type: 'enum', restart: 'engine', managed: true, values: ['engine', 'chat'] },
  QSE_EXT: { default: '', type: 'module', restart: 'engine', managed: true },
  QWEN38_PREFIX_CHUNK: { default: '2048', type: 'integer', restart: 'engine', fingerprint: true, managed: true },
  HF_TOKEN: { default: null, type: 'string', restart: 'engine', managed: false, why: 'may hold a secret' },
  PORT: { default: '8000', type: 'integer', restart: 'engine', managed: false, why: 'the install’s' },
};
/** the engine's mode as the settings interface shows it, over the two overlay keys `mode set` writes */
const MODE_VARS: Record<string, string> = { mode: 'QSE_MODE', 'mode.ext': 'QSE_EXT' };
const SHOWN: Record<string, string> = { QSE_MODE: 'mode', QSE_EXT: 'mode.ext' };
const DEFAULT_EXT = 'tandemapp.engine_ext';
const WHEEL = 'tandemapp-0.15.0-py3-none-any.whl';
const MODULE_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}$/;
const HTTP_WHY = 'reached over HTTP only: change its settings on its own host (tandem remote settings), or pair it over SSH';
const secretKey = (k: string): boolean => /TOKEN|SECRET|PASSWORD|API_KEY/.test(k) || ENGINE_KEYS[k]?.why === 'may hold a secret';

interface EngineHost {
  id: string;
  alias: string;
  backends: string[];
  versions: { n: number; set: Record<string, string>; at: number; note: string }[];
  current: number;
  good: number;
  /** the chat extension is installed on the host */
  ext: boolean;
  /** how long its last restart took, as this controller saw it (seconds) */
  took?: number;
}
interface RestartItem {
  id: string;
  host: EngineHost;
  change_set: string;
  kind: 'settings' | 'restore' | 'mode';
  who: string;
  when: string;
  deadline_s: number | null;
  at: number | null;
  created: number;
  drain: number | null;
  outcome: 'ok' | 'fail' | 'timeout';
  busy: number;
  next: Record<string, string>;
  base: number;
  note: string;
  job: string;
  end: { state: Restart['state']; at: number; message: string } | null;
}

class EngineScope implements ScopeImpl {
  hosts: EngineHost[] = [];
  restarts: RestartItem[] = [];
  outcome: 'ok' | 'fail' | 'timeout' = 'ok';
  busyS = 6;
  unreachable: string | null = null;
  /** this controller has a wheel of its own to install the chat extension from */
  wheel = true;
  private seq = 0;
  now = 0;
  onEnd: (r: RestartItem, state: Restart['state']) => void = () => undefined;
  constructor(
    private paired: () => { id: string; alias: string }[],
    private seeded: boolean,
    t0: number,
  ) {
    if (seeded) this.hosts.push({ id: 'eh-desk', alias: 'spark-desk', backends: ['engine'], versions: [{ n: 0, set: {}, at: t0 - 7 * 86_400_000, note: 'the install' }, { n: 1, set: { MAX_QUEUE: '16' }, at: t0 - 86_400_000, note: 'settings apply' }], current: 1, good: 1, ext: false });
  }
  private sync(): void {
    for (const p of this.paired()) if (!this.hosts.some((h) => h.id === p.id)) this.hosts.push({ id: p.id, alias: p.alias, backends: [`engine.${p.id}`], versions: [{ n: 0, set: {}, at: this.now, note: 'the install' }], current: 0, good: 0, ext: false });
  }
  targets(): Target[] {
    this.sync();
    const out: Target[] = this.hosts.map((h) => ({ id: h.id, label: h.alias, writable: true, why: null, transport: 'ssh' }));
    if (this.seeded) out.push({ id: 'http:bench', label: 'bench', writable: false, why: HTTP_WHY, transport: 'http' });
    return out;
  }
  private host(t: string): EngineHost {
    const h = this.hosts.find((x) => x.id === t);
    if (!h) no('read_only', 'this engine is reached over HTTP only: its settings change on its own host or after pairing it over SSH');
    if (this.unreachable === t) no('engine_unreachable', SENTENCE.engine_unreachable, { code: 'transport' });
    return h!;
  }
  private over(h: EngineHost): Record<string, string> {
    return h.versions.find((v) => v.n === h.current)!.set;
  }
  schema(t: string) {
    if (t.startsWith('http:')) return { writable: false, why: HTTP_WHY, keys: [spec('QSE_ALLOW_FROM', 'cidrs', { restart: 'hot', writable: false, why: 'read-only over HTTP' })] };
    this.host(t);
    return {
      writable: true,
      why: null,
      keys: [
        spec('mode', 'enum', { values: ['engine', 'chat'], restart: 'engine', default: 'engine', text: 'how the engine runs: engine, the bare engine; chat, with TandemApp’s conductor and Chat inside it' }),
        spec('mode.ext', 'module', { restart: 'engine', default: DEFAULT_EXT, nullable: true, text: 'the extension chat mode runs; TandemApp installs its own when the engine host lacks it' }),
        ...Object.entries(ENGINE_KEYS)
          .filter(([k]) => !SHOWN[k])
          .map(([k, d]) => spec(k, d.type, { restart: d.restart, fingerprint: d.fingerprint, default: d.default, values: d.values, secret: secretKey(k), writable: d.managed && !secretKey(k), why: d.why })),
      ],
    };
  }
  get(t: string) {
    if (t.startsWith('http:')) return { writable: false, why: HTTP_WHY, version: '3', values: [{ key: 'QSE_ALLOW_FROM', value: '192.0.2.0/24', set: true, source: 'managed' }] };
    const h = this.host(t);
    const over = this.over(h);
    return {
      writable: true,
      why: null,
      version: String(h.current),
      values: [
        { key: 'mode', value: over.QSE_MODE ?? 'engine', set: true, source: 'QSE_MODE' in over ? 'managed' : 'default' },
        { key: 'mode.ext', value: over.QSE_EXT || null, set: !!over.QSE_EXT, source: 'QSE_EXT' in over ? 'managed' : 'default' },
        ...Object.entries(ENGINE_KEYS).filter(([k]) => !SHOWN[k]).map(([k, d]) => ({ key: k, value: secretKey(k) ? null : (over[k] ?? d.default), set: k in over || k === 'HF_TOKEN' || k === 'PORT', source: k in over ? 'managed' : k === 'HF_TOKEN' ? 'local' : k === 'PORT' ? 'install' : 'default' })),
      ],
    };
  }
  versions(t: string) {
    if (t.startsWith('http:')) no('read_only', 'this engine is reached over HTTP only: its versions are read on its own host');
    const h = this.host(t);
    return { current: String(h.current), versions: [...h.versions].reverse().map((v) => ({ id: String(v.n), at: iso(v.at), who: null, note: v.note, change_set: null, keys: Object.keys(v.set).map((k) => SHOWN[k] ?? k).sort(), current: v.n === h.current, good: v.n === h.good, values: Object.fromEntries(Object.entries(v.set).map(([k, x]) => [SHOWN[k] ?? k, k === 'QSE_EXT' ? x || null : x])) })) };
  }
  private estimate(h: EngineHost): RestartEstimate {
    return h.took != null ? { seconds: h.took, source: 'measured' } : { seconds: DEFAULT_RESTART_S, source: 'default' };
  }
  /** the wheel to install first when chat mode needs an extension the host lacks; the refusal with its fix */
  private extNeed(h: EngineHost, ext: string): { name: string; bytes: number; module: string } | null {
    if (h.ext && ext === DEFAULT_EXT) return null;
    if (!ext.startsWith('tandemapp.')) no('extension_missing', `the engine host has no extension ${ext} that loads, and TandemApp installs only its own (tandemapp.*): install ${ext} there first (tandem remote ext install), or choose ${DEFAULT_EXT}`, { key: 'mode.ext' });
    if (!this.wheel) no('extension_missing', `the engine host has no ${ext} that loads, and this controller cannot build its wheel (no checkout): set TANDEM_NODE_WHEEL to the tandemapp wheel of this release and plan again, or install the extension on the engine host (tandem remote ext install)`, { key: 'mode.ext' });
    return { name: WHEEL, bytes: 4_200_000, module: ext };
  }
  private modeArgs(sets: Record<string, unknown>, cur: string): { mode: string | null; ext: string | null } {
    const mode = 'mode' in sets ? sets.mode : null;
    if ('mode' in sets && mode !== 'engine' && mode !== 'chat') no('bad_value', 'mode is engine or chat', { key: 'mode' });
    const ext = (sets['mode.ext'] ?? null) as string | null;
    if (ext !== null && (typeof ext !== 'string' || !MODULE_RE.test(ext))) no('bad_value', `mode.ext is a dotted module name, like ${DEFAULT_EXT}`, { key: 'mode.ext' });
    if ('mode.ext' in sets && (mode ?? cur) !== 'chat') no('bad_value', 'mode.ext goes with chat mode: choose chat as well', { key: 'mode.ext' });
    return { mode: mode as string | null, ext };
  }
  private planFor(t: string, sets: Record<string, unknown>, unsets: string[], restore: number | null): ScopePlan {
    const h = this.host(t);
    const over = this.over(h);
    const next = { ...over };
    const changes: Change[] = [];
    for (const k of [...Object.keys(sets), ...unsets]) {
      if (SHOWN[k]) no('locked', `${k} changes through the key ${SHOWN[k]}: the mode installs the extension chat mode needs`, { key: k });
      if (MODE_VARS[k]) continue;
      const d = ENGINE_KEYS[k];
      if (!d) no('unknown_key', `not a setting of this engine: ${k}`, { key: k, keys: [k] });
      if (!d.managed || secretKey(k)) no('locked', `${k} is not changed from here: ${d.why ?? 'the engine keeps it'}`, { key: k });
    }
    const curMode = over.QSE_MODE ?? 'engine';
    const curExt = over.QSE_EXT || null;
    const { mode, ext } = this.modeArgs(sets, curMode);
    const touched = 'mode' in sets || 'mode.ext' in sets;
    // the mode alone: `mode set`, as the engine has it
    if (restore === null && touched && !unsets.length && Object.keys(sets).every((k) => MODE_VARS[k])) {
      const newMode = mode ?? curMode;
      const newExt = newMode === 'chat' ? (ext ?? (curMode === 'chat' && curExt ? curExt : DEFAULT_EXT)) : null;
      if (newMode === curMode && newExt === (newMode === 'chat' ? curExt : null)) return planOf([], ['no_change', t, over]);
      const install = newMode === 'chat' ? this.extNeed(h, newExt!) : null;
      if (newMode !== curMode) changes.push(change('mode', curMode, newMode, { was_set: 'QSE_MODE' in over, is_set: true, restart: 'engine' }));
      if (newExt !== curExt) changes.push(change('mode.ext', curExt, newExt, { was_set: curExt !== null, is_set: newExt !== null, restart: 'engine' }));
      const target = { ...over, QSE_MODE: newMode, QSE_EXT: newExt ?? '' };
      return planOf(changes, [h.current, target, install], { restart: 'engine', fingerprint_keys: [], warnings: install ? [`the engine host has no ${install.module} that loads: TandemApp installs its own wheel first (${install.name}, 4 MB)`] : [], native: { next: target, hot: false, restore: null, base: h.current, install, kind: 'mode' }, estimate: this.estimate(h) });
    }
    if ('mode' in sets) {
      next.QSE_MODE = mode!;
      next.QSE_EXT = mode === 'chat' ? (ext ?? (curMode === 'chat' && curExt ? curExt : DEFAULT_EXT)) : '';
    } else if ('mode.ext' in sets) next.QSE_EXT = ext ?? DEFAULT_EXT;
    if (unsets.includes('mode')) for (const k of ['QSE_MODE', 'QSE_EXT']) if (k in over) delete next[k];
    if (unsets.includes('mode.ext') && 'QSE_EXT' in over) delete next.QSE_EXT;
    for (const [k, raw] of Object.entries(sets)) {
      if (MODE_VARS[k]) continue;
      const d = ENGINE_KEYS[k];
      if (typeof raw === 'boolean' || (typeof raw !== 'string' && typeof raw !== 'number')) no('bad_value', `${k}: an engine setting is a string or a number`, { key: k });
      const v = String(raw);
      if (d.type === 'integer' && !/^\d+$/.test(v)) no('bad_value', `${k} is a whole number`, { code: 'bad_value', key: k });
      if (d.values && !d.values.includes(v)) no('bad_value', `${k} is one of ${d.values.join(', ')}`, { key: k });
      if (d.type === 'cidrs' && v && !v.split(',').every((c) => /^\s*[0-9a-fA-F:.]+\/\d{1,3}\s*$/.test(c))) no('bad_value', `${k} is a list of networks, like 192.0.2.0/24`, { code: 'bad_value', key: k });
      next[k] = v;
    }
    // a key the overlay does not set is unset already: no change, the plan drops it
    for (const k of unsets) if (!MODE_VARS[k]) delete next[k];
    const install = touched && next.QSE_MODE === 'chat' ? this.extNeed(h, next.QSE_EXT || DEFAULT_EXT) : null;
    for (const k of Object.keys(ENGINE_KEYS)) {
      const d = ENGINE_KEYS[k];
      const from = over[k] ?? d.default;
      const to = next[k] ?? d.default;
      const show = (v: string | null) => (k === 'QSE_EXT' ? v || null : v);
      if (from !== to || k in over !== k in next) changes.push(change(SHOWN[k] ?? k, show(from), show(to), { was_set: k in over, is_set: k in next, restart: d.restart, fingerprint: d.fingerprint }));
    }
    const hot = changes.length > 0 && restore === null && changes.every((c) => c.restart === 'hot');
    const fp = changes.filter((c) => c.fingerprint).map((c) => c.key);
    const warnings = [...(install ? [`the engine host has no ${install.module} that loads: TandemApp installs its own wheel first (${install.name}, 4 MB)`] : []), ...(fp.length ? [`${feeds(fp)} the disk cache’s fingerprint`] : [])];
    return planOf(changes, [h.current, next, restore, install], { restart: hot ? 'hot' : 'engine', fingerprint_keys: fp, warnings, native: { next, hot, restore, base: h.current, install }, estimate: hot ? null : this.estimate(h) });
  }
  plan(t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    return this.planFor(t, sets, unsets, null);
  }
  planRestore(t: string, version: string): ScopePlan {
    const h = this.host(t);
    const got = /^\d+$/.test(version) ? h.versions.find((v) => v.n === Number(version)) : undefined;
    if (!got) no('no_version', `no version ${version} of this engine’s overlay`);
    const cur = this.over(h);
    const sets = Object.fromEntries(Object.entries(got!.set).filter(([k, v]) => cur[k] !== v));
    const unsets = Object.keys(cur).filter((k) => !(k in got!.set));
    if (!Object.keys(sets).length && !unsets.length) return planOf([], ['none', t, version, h.current]);
    return this.planFor(t, sets, unsets, got!.n);
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    const h = this.host(t);
    const nat = p.native as { next: Record<string, string>; hot: boolean; restore: number | null; base: number; install: unknown; kind?: 'mode' };
    const restore: RestoreRef[] = [{ scope: 'engine', target: t, version: String(nat.base) }];
    const note = nat.restore != null ? `restore of version ${nat.restore}` : nat.kind === 'mode' ? `mode ${nat.next.QSE_MODE}` : 'settings apply';
    if (nat.hot) {
      const n = h.versions[h.versions.length - 1].n + 1;
      h.versions.push({ n, set: nat.next, at: ctx.now, note });
      h.current = h.good = n;
      return { version_before: String(nat.base), version_after: String(n), restore };
    }
    // one job at a time on an engine host, as the engine itself has it
    if (this.restarts.some((x) => x.host === h && !x.end)) no('engine_busy', SENTENCE.engine_busy, { code: 'busy' });
    // the extension first (`ext install`: no restart of its own), then the switch
    if (nat.install) h.ext = true;
    const when = ctx.when ?? 'idle';
    const deadline = when === 'now' ? null : (ctx.deadline_s ?? 600);
    const at = when.startsWith('at=') ? Date.parse(when.slice(3)) : null;
    const stamp = new Date(ctx.now).toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, '');
    const item: RestartItem = {
      id: `rs-${digest([ctx.change_set, t, ++this.seq]).slice(0, 12)}`, host: h, change_set: ctx.change_set, kind: nat.restore != null ? 'restore' : nat.kind === 'mode' ? 'mode' : 'settings', who: ctx.who, when, deadline_s: deadline, at, created: ctx.now,
      drain: null, outcome: this.outcome, busy: when === 'now' ? 0 : this.busyS * 1000, next: nat.next, base: nat.base, note, job: `${stamp}-${digest([ctx.now, this.seq]).slice(0, 6)}`, end: null,
    };
    this.outcome = 'ok';
    this.restarts.push(item);
    return { version_before: String(nat.base), version_after: null, restore, restart: this.view(item, ctx.now) };
  }
  private finish(r: RestartItem, state: Restart['state'], at: number, message: string): void {
    if (r.end) return;
    r.end = { state, at, message };
    this.onEnd(r, state);
  }
  /** Where a restart stands at `now`: its state follows the clock. */
  private stage(r: RestartItem, now: number): { state: Restart['state']; left: number | null; phase: string } {
    if (r.end) return { state: r.end.state, left: null, phase: r.end.state };
    if (r.at != null && now < r.at) return { state: 'scheduled', left: null, phase: 'waiting for the time' };
    r.drain ??= Math.max(r.created, r.at ?? r.created);
    const deadline = r.deadline_s ? r.drain + r.deadline_s * 1000 : null;
    const idle = r.outcome === 'timeout' && r.deadline_s ? Infinity : r.drain + r.busy;
    if (now < idle) {
      if (deadline != null && now >= deadline) {
        this.finish(r, 'timeout', deadline, 'the engine was still busy at the deadline: nothing was changed');
        return this.stage(r, now);
      }
      return { state: 'draining', left: deadline == null ? null : Math.max(0, Math.round((deadline - now) / 1000)), phase: 'waiting for idle' };
    }
    if (now < idle + RESTART_MS) return { state: 'restarting', left: null, phase: now < idle + RESTART_MS / 2 ? 'stopping' : 'starting' };
    if (r.outcome === 'fail') {
      this.finish(r, 'failed', idle + RESTART_MS, 'the engine did not start with the new settings: its health check gave up after 120 s');
      return this.stage(r, now);
    }
    if (r.host.current === r.base) {
      const n = r.host.versions[r.host.versions.length - 1].n + 1;
      r.host.versions.push({ n, set: r.next, at: idle + RESTART_MS, note: r.note });
      r.host.current = r.host.good = n;
    }
    if (now < idle + RESTART_MS + WARM_MS) return { state: 'warming', left: null, phase: 'done' };
    // what it took from the job's acting to the end of the warm-up: the next plan's estimate
    if (!r.end) r.host.took = Math.round((RESTART_MS + WARM_MS) / 1000);
    this.finish(r, 'done', idle + RESTART_MS + WARM_MS, 'the engine runs the new settings');
    return this.stage(r, now);
  }
  view(r: RestartItem, now: number): Restart {
    const s = this.stage(r, now);
    const drained = s.state === 'draining' || s.state === 'restarting' || s.state === 'warming';
    const jobState = s.state === 'scheduled' || s.state === 'draining' ? 'waiting' : s.state === 'restarting' ? 'acting' : s.state === 'timeout' ? 'failed' : s.state === 'warming' ? 'done' : s.state;
    return {
      id: r.id, host: r.host.id, alias: r.host.alias, change_set: r.change_set, kind: r.kind, who: r.who, state: s.state, message: r.end?.message ?? null, when: r.when, deadline_s: r.deadline_s,
      deadline_at: r.drain != null && r.deadline_s ? iso(r.drain + r.deadline_s * 1000) : null, remaining_s: s.left, created_at: iso(r.created), ended_at: iso(r.end?.at ?? null),
      job: { id: r.job, kind: r.kind, state: jobState, phase: s.phase, message: r.end?.message ?? null, waiting_for: s.state === 'draining' ? 'idle' : null, created_at: iso(r.created), ended_at: iso(r.end?.at ?? null) } as Restart['job'],
      backends: r.drain != null ? [...r.host.backends] : [], draining: drained,
      warm: s.state === 'done' ? { ok: true, backends: Object.fromEntries(r.host.backends.map((b) => [b, { ok: true, why: null }])) } : null,
      restored: s.state === 'failed' ? { version: r.base, healthy: true } : null, can_cancel: s.state === 'scheduled' || s.state === 'draining',
    };
  }
  /** `said`: the cancel's own audit row, written before the restart's end as the controller does */
  cancel(id: string, who: string, now: number, said: (r: RestartItem) => void = () => undefined): Restart {
    const r = this.restarts.find((x) => x.id === id);
    if (!r) no('no_restart', `no restart '${id}'`);
    const s = this.stage(r!, now);
    if (r!.end) no('ended', `restart ${id} is ${s.state} already`);
    if (s.state !== 'scheduled' && s.state !== 'draining') no('too_late', `the engine is ${s.state}: a restart that acts cannot be cancelled`);
    said(r!);
    this.finish(r!, 'cancelled', now, `cancelled by ${who}: the engine keeps the settings it runs`);
    return this.view(r!, now);
  }
  /** the alias of the engine host that drains `backend` now, as the gateway asks */
  drained(backend: string, now: number): string | null {
    for (const r of this.restarts) if (this.view(r, now).draining && r.host.backends.includes(backend)) return `${r.host.alias} (restarting for a settings change)`;
    return null;
  }
}

// ---- the interface --------------------------------------------------------------------------------
// ---- plugins ----------------------------------------------------------------------------------------
const PLUGIN_TYPE: Record<string, KeyType> = { string: 'string', integer: 'integer', number: 'number', boolean: 'boolean', array: 'list', object: 'object' };

/** One plugin's keys (proxy/pluginsettings.py): what its manifest makes of them, its own versions. */
class PluginKeys extends KeyValueScope {
  private who = 'admin';
  private at = 0;
  private confirmed = false;
  constructor(
    private reg: MockPlugins,
    private id: string,
  ) {
    super('plugins');
  }
  private form(prop: string) {
    const sch = this.reg.schema(this.reg.need(this.id));
    return sch ? schemaForm(sch).fields.find((f) => f.path[0] === prop) : undefined;
  }
  spec(): Record<string, KeySpec> {
    const p = this.reg.need(this.id);
    const out: Record<string, KeySpec> = { enabled: spec('enabled', 'boolean', { default: false, text: 'the plugin host may start it' }) };
    const sch = this.reg.schema(p);
    for (const f of sch ? schemaForm(sch).fields : []) {
      const k = `settings.${f.path[0]}`;
      const named = (f.control === 'select' || f.control === 'choice') && f.options!.every((o) => typeof o.value === 'string');
      out[k] = spec(k, named ? 'enum' : (PLUGIN_TYPE[f.type] ?? 'object'), { ...(named ? { values: f.options!.map((o) => o.value as string) } : {}), nullable: f.nullable || !f.required, default: f.has_default ? f.default : null, ...(f.description ? { text: f.description } : {}) });
    }
    for (const x of p.manifest.secrets ?? []) out[`secrets.${x.name}`] = spec(`secrets.${x.name}`, 'string', { secret: true, nullable: true, ...(x.description ? { text: x.description } : {}) });
    for (const c of this.reg.requested(p)) out[`capabilities.${c}`] = spec(`capabilities.${c}`, 'boolean', { default: false, text: `granted: the plugin may use it (deny by default)${needsConfirm(c) ? '; granting it takes a second confirm' : ''}` });
    if (p.manifest.trust === 'host') out.trust = spec('trust', 'enum', { values: ['sandboxed', 'host'], default: 'sandboxed', text: 'host: the plugin runs as a process of the app’s user, outside any sandbox (granting it takes a second confirm); sandboxed: it does not start' });
    return out;
  }
  values(): Record<string, Val> {
    const p = this.reg.need(this.id);
    const extra = { label: p.manifest.title ?? p.id };
    const out: Record<string, Val> = { enabled: { value: p.enabled, set: p.enabled, source: 'plugin', extra } };
    const eff = this.reg.effective(p);
    const props = (this.reg.schema(p)?.properties ?? {}) as Record<string, unknown>;
    for (const k of Object.keys(props)) out[`settings.${k}`] = { value: eff[k] ?? null, set: k in p.settings, source: 'plugin', extra };
    for (const [n, h] of Object.entries(this.reg.hints(p))) out[`secrets.${n}`] = { value: null, set: h.set, source: 'plugin', extra: { ...extra, hint: h.sha256_6 } };
    for (const c of this.reg.requested(p)) out[`capabilities.${c}`] = { value: c in p.granted, set: c in p.granted, source: 'plugin', extra };
    if (p.manifest.trust === 'host') out.trust = { value: p.trust_granted, set: p.trust_granted !== 'sandboxed', source: 'plugin', extra };
    return out;
  }
  check(sets: Record<string, unknown>, unsets: string[], s: Record<string, KeySpec>): void {
    const p = this.reg.need(this.id);
    const bad = (k: string, why: string): never => no('bad_value', `${k}: ${why}`, { key: k });
    for (const [k, v] of Object.entries(sets)) {
      if (k === 'enabled') {
        if (typeof v !== 'boolean') bad(k, 'true or false');
        if (v && p.integrity !== 'ok') bad(k, `${p.id}: its tree is ${p.integrity} since the install; install it again`);
      } else if (k.startsWith('settings.')) {
        if (v === null) continue;
        const f = this.form(k.slice(9))!;
        const errs = Object.values(validateField({ ...f, required: true }, v, [k]));
        if (errs.length) bad(k, errs[0].charAt(0).toLowerCase() + errs[0].slice(1).replace(/\.$/, ''));
      } else if (s[k].secret) {
        if (v !== null && (typeof v !== 'string' || v.length < 1 || v.length > 8192)) bad(k, 'a string of 1 to 8192 characters');
      } else if (k.startsWith('capabilities.')) {
        if (typeof v !== 'boolean') bad(k, 'true or false');
      } else if (k === 'trust' && v !== 'sandboxed' && v !== 'host') bad(k, 'sandboxed or host');
    }
    for (const k of unsets) {
      if (!k.startsWith('settings.')) continue;
      const f = this.form(k.slice(9))!;
      if (f.required && !f.has_default) bad(k, 'it is required and has no default: set a value');
    }
  }
  plan(t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    const p = super.plan(t, sets, unsets);
    const row = this.reg.need(this.id);
    // a plan is of this install of the plugin as it stands: one removed and installed again is another
    p.state = digest([p.state, row.installed_at, row.row_version]);
    // a switch or a grant set to what holds already is no change
    p.changes = p.changes.filter((c) => c.secret || c.key.startsWith('settings.') || stable(c.from) !== stable(c.to));
    const changed = new Set(p.changes.map((c) => c.key));
    const grants: { key: string; capability: string; confirm: boolean }[] = [];
    for (const [k, v] of Object.entries(sets)) {
      if (!changed.has(k)) continue;
      if (k.startsWith('capabilities.') && v === true) grants.push({ key: k, capability: k.slice(13), confirm: needsConfirm(k.slice(13)) });
      else if (k === 'trust' && v === 'host') grants.push({ key: k, capability: 'trust', confirm: true });
    }
    const enabling = changed.has('enabled') && sets.enabled === true;
    if (!grants.length && !enabling) return p;
    if (!this.reg.trust) {
      const key = grants[0]?.key ?? 'enabled';
      no('trust_required', `${key}: granting a plugin a capability or enabling it takes pairing’s rights: set an admin password (App settings) to do it from another machine, or do it from this one`, { key });
    }
    if (enabling) p.warnings.push(`enabled: the plugin host may start ${row.id} (its own code, with the capabilities granted to it)`);
    for (const g of grants) p.warnings.push(`${g.key}: ${row.id} gets ${g.capability}${g.confirm ? ': apply asks a second time' : ''}`);
    p.grant_keys = grants.filter((g) => g.confirm).map((g) => g.key).sort();
    p.state = digest([p.state, grants]);
    return p;
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    this.who = ctx.who;
    this.at = ctx.now;
    this.confirmed = (p.grant_keys ?? []).length > 0;
    return super.apply(t, p, ctx);
  }
  write(sets: Record<string, unknown>, unsets: string[]): void {
    const w = { enabled: null as boolean | null, settings_set: {} as Record<string, unknown>, settings_unset: [] as string[], secrets_set: {} as Record<string, string>, secrets_unset: [] as string[], grant: [] as string[], revoke: [] as string[], trust: undefined as 'sandboxed' | 'host' | undefined, confirmed: this.confirmed };
    for (const [k, v] of Object.entries(sets)) {
      if (k === 'enabled') w.enabled = !!v;
      else if (k.startsWith('settings.')) v === null ? w.settings_unset.push(k.slice(9)) : (w.settings_set[k.slice(9)] = v);
      else if (k.startsWith('secrets.')) v === null ? w.secrets_unset.push(k.slice(8)) : (w.secrets_set[k.slice(8)] = v as string);
      else if (k.startsWith('capabilities.')) (v ? w.grant : w.revoke).push(k.slice(13));
      else if (k === 'trust') w.trust = v as 'sandboxed' | 'host';
    }
    for (const k of unsets) {
      if (k === 'enabled') w.enabled = false;
      else if (k.startsWith('settings.')) w.settings_unset.push(k.slice(9));
      else if (k.startsWith('secrets.')) w.secrets_unset.push(k.slice(8));
      else if (k.startsWith('capabilities.')) w.revoke.push(k.slice(13));
      else if (k === 'trust') w.trust = 'sandboxed';
    }
    try {
      this.reg.write(this.id, w, this.who, this.at);
    } catch (e) {
      no('store_refused', (e as Error).message);
    }
  }
}

/** The plugins scope: one target a plugin, each with its own keys and its own versions. */
class PluginsScope implements ScopeImpl {
  /** a plugin's keys, with the install they are of */
  private keys = new Map<string, { of: number; keys: PluginKeys }>();
  constructor(private reg: MockPlugins) {}
  private of(t: string): PluginKeys {
    // a plugin that was removed takes its versions with it, also when it is installed again
    for (const [id, k] of [...this.keys]) if (this.reg.rows[id]?.installed_at !== k.of) this.keys.delete(id);
    let k = this.keys.get(t);
    if (!k) this.keys.set(t, (k = { of: this.reg.rows[t].installed_at, keys: new PluginKeys(this.reg, t) }));
    return k.keys;
  }
  targets(): Target[] {
    return this.reg.list().map((p) => ({ id: p.id, label: p.manifest.title ?? p.id, writable: true, why: p.integrity === 'ok' ? null : `its tree is ${p.integrity} since the install` }));
  }
  schema(t: string) {
    return this.of(t).schema();
  }
  get(t: string) {
    return this.of(t).get();
  }
  plan(t: string, sets: Record<string, unknown>, unsets: string[]): ScopePlan {
    return this.of(t).plan(t, sets, unsets);
  }
  apply(t: string, p: ScopePlan, ctx: Ctx): Applied {
    return this.of(t).apply(t, p, ctx);
  }
  versions(t: string) {
    return this.of(t).versions();
  }
  planRestore(t: string, version: string): ScopePlan {
    return this.of(t).planRestore(t, version);
  }
}

interface Part {
  scope: Scope;
  target: string;
  kind: 'set' | 'restore';
  version: string | null;
  sets: Record<string, unknown>;
  unsets: string[];
  plan?: ScopePlan;
}

export interface MockSettingsOptions {
  /** What runs here's switches (mock/appsettings.ts): the app scope reads and writes them */
  parts: PartSwitches;
  /** the engine hosts paired in this mock */
  engines?: () => { id: string; alias: string }[];
  /** false: no engine target until one is paired (the first run) */
  seeded?: boolean;
  /** the hook registry holds its sample hooks and providers (control({hooks}) changes it); `demo`:
   *  more of them, with the traffic the hooks pages show (mock/hooks.ts) */
  hooks?: boolean | 'demo';
  /** the plugin registry holds hello-tool (true) or the demo set (mock/plugins.ts) */
  plugins?: boolean | 'demo';
  /** the controller's accounts are on (mock/auth.ts): the `auth` and `api` scopes exist too */
  accounts?: MockAuth;
  now?: number;
}

export class MockSettings {
  private scopes: Partial<Record<Scope, ScopeImpl>>;
  /** the scopes this controller has, in the order they apply */
  private order: Scope[] = SCOPES;
  private engine: EngineScope;
  private hooks: HooksScope;
  /** the hooks and the decision providers: the settings' hooks scope and their own two APIs
   *  (/v1/fleet/hooks, /v1/fleet/decision-providers) are views of this one registry */
  readonly registry: MockHooks;
  /** the plugins: the settings' plugins scope and their own API (/v1/fleet/plugins) are views of
   *  this one registry */
  readonly plugins: MockPlugins;
  private audits: (AuditRow & { n: number })[] = [];
  private done = new Map<string, { same: string; result: Result; at: number; restarts: string[] }>();
  private fails = new Map<string, string>();
  private epoch = 0;
  private busy = false;
  private seq = 0;

  constructor(o: MockSettingsOptions) {
    const t0 = o.now ?? Date.now();
    this.engine = new EngineScope(o.engines ?? (() => []), o.seeded ?? true, t0);
    this.registry = new MockHooks(t0);
    this.hooks = new HooksScope(this.registry);
    if (o.hooks) this.registry.seed(o.hooks);
    this.plugins = new MockPlugins(t0);
    if (o.plugins) this.plugins.seed(o.plugins);
    this.scopes = { app: new AppScope(o.parts), backends: new BackendsScope(), conductor: new ConductorScope(o.parts, t0), engine: this.engine, hooks: this.hooks, plugins: new PluginsScope(this.plugins) };
    if (o.accounts) {
      this.scopes.auth = new AccountKeys('auth', o.accounts);
      this.scopes.api = new AccountKeys('api', o.accounts);
      this.order = WITH_ACCOUNTS;
    }
    this.engine.onEnd = (r, state) => {
      this.audit(r.end!.at, r.who, 'settings.restart', `engine/${r.host.id}`, [], r.change_set, state);
      // a restart that did not end well: the result it belongs to holds no longer
      if (state !== 'done') for (const [k, v] of this.done) if (v.restarts.includes(r.id)) this.done.delete(k);
    };
  }

  /** What no click produces (POST /__mock/settings); see the head of this file. */
  control(b: { restart?: string; busy_s?: number; fail?: string | null; busy?: boolean; move?: boolean; hooks?: boolean; trust?: boolean; unreachable?: string | null; wheel?: boolean }): Record<string, unknown> {
    if (b.restart === 'ok' || b.restart === 'fail' || b.restart === 'timeout') this.engine.outcome = b.restart;
    if (typeof b.busy_s === 'number') this.engine.busyS = b.busy_s;
    if (b.fail === null) this.fails.clear();
    else if (typeof b.fail === 'string') {
      const [route, type] = b.fail.split(':');
      if (!(type in SETTINGS_ERRORS)) throw new Error(`no settings error ${type}`);
      this.fails.set(route, type);
    }
    if (typeof b.busy === 'boolean') this.busy = b.busy;
    if (b.move) this.epoch++;
    if (typeof b.hooks === 'boolean') this.hooks.plugged = b.hooks;
    if (typeof b.trust === 'boolean') {
      this.hooks.trust = b.trust;
      this.plugins.trust = b.trust;
    }
    if ('unreachable' in b) this.engine.unreachable = b.unreachable ?? null;
    if (typeof b.wheel === 'boolean') this.engine.wheel = b.wheel;
    return { fails: Object.fromEntries(this.fails), busy: this.busy, hooks: this.hooks.plugged, trust: this.hooks.trust, wheel: this.engine.wheel };
  }

  /** The gateway's answer for a backend while its engine host drains: 503 with Retry-After; null
   *  while it serves. */
  gateway(backend: string, nowMs = Date.now()): Reply | null {
    const who = this.engine.drained(backend, nowMs);
    return who ? { status: 503, body: { error: { type: 'node_draining', message: `${who}: try again in 30 s` } }, headers: { 'Retry-After': '30' } } : null;
  }

  private audit(at: number, who: string, action: string, objectId: string | null, keys: string[], cs: string | null, result: string): void {
    const [scope, target] = (objectId ?? '').split('/');
    this.audits.push({ n: ++this.seq, at: iso(at)!, ts_ms: at, who, action, object: 'settings', object_id: objectId, scope: scope || null, target: target || null, keys, change_set: cs, result });
  }

  private scope(name: unknown): ScopeImpl {
    if (typeof name !== 'string' || !this.order.includes(name as Scope)) no('no_scope', `no scope '${String(name)}' (one of ${this.order.join(', ')})`);
    return this.scopes[name as Scope]!;
  }

  private target(name: Scope, t: unknown): string {
    const ts = this.scope(name).targets();
    if (t == null) {
      if (ts.length === 1) return ts[0].id;
      no('target_required', `the ${name} scope has ${ts.length} targets: name one (${ts.map((x) => x.id).join(', ') || 'none yet'})`);
    }
    if (!ts.some((x) => x.id === t)) no('no_target', `the ${name} scope has no target '${String(t)}'`);
    return t as string;
  }

  private parts(body: unknown): Part[] {
    const b = body as { changes?: unknown } | null;
    if (!b || typeof b !== 'object' || !Array.isArray(b.changes) || !b.changes.length) no('bad_request', 'changes is a list of one or more parts');
    const seen = new Set<string>();
    const out = (b!.changes as Record<string, unknown>[]).map((c, i): Part => {
      if (!c || typeof c !== 'object') no('bad_request', `changes[${i}] is an object`);
      const extra = Object.keys(c).filter((k) => !['scope', 'target', 'set', 'unset', 'restore'].includes(k)).sort();
      if (extra.length) no('bad_request', `changes[${i}]: unknown field '${extra[0]}'`);
      this.scope(c.scope);
      const scope = c.scope as Scope;
      const target = this.target(scope, c.target);
      if (seen.has(`${scope}/${target}`)) no('bad_request', `two parts for ${scope}/${target}: one part a scope and target`);
      seen.add(`${scope}/${target}`);
      if ('restore' in c) {
        if ('set' in c || 'unset' in c) no('bad_request', `changes[${i}]: a restore sets nothing else`);
        if (typeof c.restore !== 'string' && typeof c.restore !== 'number') no('bad_request', `changes[${i}].restore is a version’s id`);
        return { scope, target, kind: 'restore', version: String(c.restore), sets: {}, unsets: [] };
      }
      const sets = (c.set ?? {}) as Record<string, unknown>;
      const unsets = (c.unset ?? []) as string[];
      if (typeof sets !== 'object' || Array.isArray(sets) || !Array.isArray(unsets)) no('bad_request', `changes[${i}]: set is {key: value}, unset a list of keys`);
      if (!Object.keys(sets).length && !unsets.length) no('bad_request', `changes[${i}]: set or unset at least one key`);
      const both = unsets.find((k) => k in sets);
      if (both) no('bad_request', `changes[${i}]: ${both} is both set and unset`);
      return { scope, target, kind: 'set', version: null, sets, unsets };
    });
    return out.sort((a, b) => this.order.indexOf(a.scope) - this.order.indexOf(b.scope) || (a.target < b.target ? -1 : a.target > b.target ? 1 : 0));
  }

  private planParts(parts: Part[]): Plan {
    for (const p of parts) {
      try {
        p.plan = p.kind === 'restore' ? this.scopes[p.scope]!.planRestore(p.target, p.version!) : this.scopes[p.scope]!.plan(p.target, p.sets, p.unsets);
      } catch (e) {
        // a part's refusal names its scope and target, and the key of a part of one key when it is about a value
        if (!(e instanceof Refuse)) throw e;
        const keys = [...Object.keys(p.sets), ...p.unsets];
        e.facts = { scope: p.scope, target: p.target, ...e.facts };
        if (e.facts.key == null && keys.length === 1 && KEY_ERRORS.includes(e.type)) e.facts.key = keys[0];
        throw e;
      }
    }
    const msg = [this.epoch, ...parts.map((p) => [p.scope, p.target, p.kind, p.version, digest(p.sets), [...p.unsets].sort(), p.plan!.state, p.plan!.changes.map((c) => c.key)])];
    const confirm = digest(['confirm', msg]).slice(0, 32);
    const fp = parts.some((p) => p.plan!.fingerprint_keys.length) ? digest(['fingerprint', msg]).slice(0, 32) : null;
    const cg = parts.some((p) => p.plan!.grant_keys?.length) ? digest(['grant', msg]).slice(0, 32) : null;
    const views: PartPlan[] = parts.map((p) => ({
      scope: p.scope, target: p.target, kind: p.kind, version: p.version, changed: p.plan!.changes.length > 0, changes: p.plan!.changes,
      restart: p.plan!.changes.length ? p.plan!.restart : 'none', restart_needed: p.plan!.changes.length > 0 && (p.plan!.restart === 'app' || p.plan!.restart === 'engine'),
      restart_estimate: p.plan!.changes.length && p.plan!.restart === 'engine' ? (p.plan!.estimate ?? null) : null,
      fingerprint_keys: p.plan!.fingerprint_keys, grant_keys: p.plan!.grant_keys ?? [], warnings: p.plan!.warnings,
    }));
    const changed = views.some((v) => v.changed);
    return {
      contract_version: CONTRACT, change_set: `cs-${confirm.slice(0, 12)}`, parts: views, changed, restart: maxRestart(views.map((v) => v.restart)), restart_needed: views.some((v) => v.restart_needed),
      fingerprint_keys: views.flatMap((v) => v.fingerprint_keys.map((k) => `${v.scope}/${v.target}:${k}`)).sort(),
      grant_keys: views.flatMap((v) => v.grant_keys.map((k) => `${v.scope}/${v.target}:${k}`)).sort(),
      needs_confirm: changed, confirm: changed ? confirm : null, confirm_fingerprint: changed ? fp : null, confirm_grant: changed ? cg : null,
    };
  }

  private result(p: Part, state: PartResult['state'], got: Partial<Applied> = {}, error: PartResult['error'] = null): PartResult {
    return { scope: p.scope, target: p.target, kind: p.kind, state, keys: (p.plan?.changes ?? []).map((c) => c.key), version_before: got.version_before ?? null, version_after: got.version_after ?? null, restore: got.restore ?? [], restart: got.restart ?? null, error };
  }

  private apply(body: unknown, who: string, now: number): Result {
    const b = body as Record<string, unknown>;
    const parts = this.parts(body);
    const extra = Object.keys(b).filter((k) => !['changes', 'confirm', 'confirm_fingerprint', 'confirm_grant', 'when', 'deadline_s', 'note'].includes(k)).sort();
    if (extra.length) no('bad_request', `unknown field '${extra[0]}'`);
    const when = (b.when ?? null) as string | null;
    if (when !== null && (typeof when !== 'string' || !(when === 'now' || when === 'idle' || (when.startsWith('at=') && !Number.isNaN(Date.parse(when.slice(3))) && /(Z|[+-]\d\d:\d\d)$/.test(when))))) no('bad_request', 'when is now, idle or at=<ISO 8601 time with a zone>');
    const deadline = (b.deadline_s ?? null) as number | null;
    if (deadline !== null && (!Number.isInteger(deadline) || deadline < 10 || deadline > 86400)) no('bad_request', 'deadline_s is a whole number 10..86400');
    if (deadline !== null && when === 'now') no('bad_request', 'deadline_s is the wait for idle: it goes with when idle or at=');
    const note = (b.note ?? null) as string | null;
    if (note !== null && (typeof note !== 'string' || note.length > 200)) no('bad_request', 'note is text of at most 200 characters');
    const confirm = b.confirm;
    if (confirm != null && typeof confirm !== 'string') no('bad_request', 'confirm is the plan’s token');
    const same = digest(parts.map((p) => [p.scope, p.target, p.kind, p.version, p.sets, [...p.unsets].sort()]));
    const before = typeof confirm === 'string' ? this.done.get(confirm) : undefined;
    if (before && before.same === same && now - before.at <= REPLAY_MS) return { ...before.result, replayed: true };
    if (this.busy) no('busy', SENTENCE.busy);
    const plan = this.planParts(parts);
    if (!plan.changed) return { contract_version: CONTRACT, change_set: plan.change_set, state: 'unchanged', replayed: false, parts: parts.map((p) => this.result(p, 'unchanged')), applied: [], failed: null, not_run: [] };
    if (!confirm) no('confirm_required', 'apply needs the plan’s confirm: POST the plan first', { plan });
    if (confirm !== plan.confirm) no('stale_plan', 'the settings changed since this plan, or the confirm is not its: plan again', { plan });
    if (plan.confirm_fingerprint) {
      if (!b.confirm_fingerprint) no('fingerprint_confirm_required', `${feeds(plan.fingerprint_keys)} the disk cache’s fingerprint: every saved prefix misses after the restart; apply needs confirm_fingerprint as well`, { plan });
      if (b.confirm_fingerprint !== plan.confirm_fingerprint) no('stale_plan', 'confirm_fingerprint is not this plan’s: plan again', { plan });
    }
    if (plan.confirm_grant) {
      if (!b.confirm_grant) no('grant_confirm_required', `${plan.grant_keys.join(', ')}: the full payload (prompts and answers as they are) would go to a destination outside the LAN; apply needs confirm_grant as well`, { plan });
      if (b.confirm_grant !== plan.confirm_grant) no('stale_plan', 'confirm_grant is not this plan’s: plan again', { plan });
    }
    const cs = plan.change_set;
    const ctx: Ctx = { who, change_set: cs, when, deadline_s: deadline, note, now };
    const results: PartResult[] = [];
    let failed: string | null = null;
    for (const p of parts) {
      const name = `${p.scope}/${p.target}`;
      const keys = p.plan!.changes.map((c) => c.key);
      const action = p.kind === 'restore' ? 'settings.restore' : 'settings.apply';
      if (failed !== null) results.push(this.result(p, 'not_run'));
      else if (!keys.length) results.push(this.result(p, 'unchanged'));
      else {
        try {
          const forced = this.fails.get(`apply-${p.scope}`);
          if (forced) no(forced, SENTENCE[forced] ?? forced);
          const got = this.scopes[p.scope]!.apply(p.target, p.plan!, ctx);
          const state = got.restart ? 'scheduled' : 'applied';
          results.push(this.result(p, state, got));
          this.audit(now, who, action, name, keys, cs, state);
        } catch (e) {
          if (!(e instanceof Refuse)) throw e;
          failed = name;
          results.push(this.result(p, 'failed', {}, { type: e.type, message: e.message }));
          this.audit(now, who, action, name, keys, cs, 'failed');
        }
      }
    }
    const applied = results.filter((r) => r.state === 'applied' || r.state === 'scheduled').map((r) => `${r.scope}/${r.target}`);
    const state = failed === null ? 'applied' : applied.length ? 'partial' : 'failed';
    this.audit(now, who, 'settings.change_set', null, [...new Set(results.flatMap((r) => r.keys))].sort(), cs, state);
    const out: Result = { contract_version: CONTRACT, change_set: cs, state, replayed: false, parts: results, applied, failed, not_run: results.filter((r) => r.state === 'not_run').map((r) => `${r.scope}/${r.target}`) };
    this.done.set(confirm as string, { same, result: copy(out), at: now, restarts: results.map((r) => r.restart?.id).filter((x): x is string => !!x) });
    return out;
  }

  private one(scope: string, body: unknown, restore = false): Record<string, unknown> {
    const b = (body ?? {}) as Record<string, unknown>;
    if (typeof b !== 'object' || Array.isArray(b)) no('bad_request', 'the body is an object');
    const known = ['target', 'confirm', 'confirm_fingerprint', 'confirm_grant', 'when', 'deadline_s', 'note', ...(restore ? ['version'] : ['set', 'unset'])];
    const extra = Object.keys(b).filter((k) => !known.includes(k)).sort();
    if (extra.length) no('bad_request', `unknown field '${extra[0]}'`);
    if (restore && !('version' in b)) no('bad_request', 'version is the id of the version to restore');
    const c: Record<string, unknown> = { scope, ...(b.target != null ? { target: b.target } : {}), ...(restore ? { restore: b.version } : { ...('set' in b ? { set: b.set } : {}), ...('unset' in b ? { unset: b.unset } : {}) }) };
    const out: Record<string, unknown> = { changes: [c] };
    for (const k of ['confirm', 'confirm_fingerprint', 'confirm_grant', 'when', 'deadline_s', 'note']) if (k in b) out[k] = b[k];
    return out;
  }

  /** One request. `path` from /v1/settings on; `who` the admin's session name. */
  handle(method: string, path: string, q: URLSearchParams, body: unknown, nowMs = Date.now(), who = 'admin'): Reply {
    const parts = path.replace(/\/+$/, '').split('/').slice(3).map((p) => decodeURIComponent(p));
    this.engine.now = nowMs;
    this.registry.now = nowMs;
    this.plugins.now = nowMs;
    // the clock moves every restart on, whoever asks
    for (const r of this.engine.restarts) this.engine.view(r, nowMs);
    try {
      return { status: 200, body: this.route(method, parts, q, body, nowMs, who) };
    } catch (e) {
      if (e instanceof Refuse) return { status: e.status, body: { error: { type: e.type, message: e.message, ...e.facts } } };
      throw e;
    }
  }

  private forced(route: string): void {
    const type = this.fails.get(route);
    if (type) no(type, SENTENCE[type] ?? `${type} (the mock was told to refuse ${route})`);
  }

  private route(method: string, parts: string[], q: URLSearchParams, body: unknown, now: number, who: string): unknown {
    const v = { contract_version: CONTRACT };
    const target = q.get('target');
    if (method === 'GET' && !parts.length) {
      this.forced('overview');
      return { ...v, scopes: this.order.map((scope, order) => ({ scope, order, targets: this.scopes[scope]!.targets(), why: null })), order: this.order, operations: OPS };
    }
    if (method === 'POST' && parts.join('/') === 'plan') {
      this.forced('plan');
      return this.planParts(this.parts(body));
    }
    if (method === 'POST' && parts.join('/') === 'apply') {
      this.forced('apply');
      return this.apply(body, who, now);
    }
    if (method === 'GET' && parts.join('/') === 'audit') {
      this.forced('audit');
      const cs = q.get('change_set');
      if (cs !== null && !/^cs-[0-9a-f]{12}$/.test(cs)) no('bad_request', 'change_set is a change set’s id (cs-...)');
      const limit = Number(q.get('limit') ?? 100);
      if (!Number.isInteger(limit) || limit < 1 || limit > 500) no('bad_request', 'limit is a whole number 1..500');
      const rows = this.audits.filter((r) => !cs || r.change_set === cs).sort((a, b) => b.ts_ms - a.ts_ms || b.n - a.n).slice(0, limit);
      return { ...v, rows: rows.map(({ n: _n, ...r }) => r) };
    }
    if (parts[0] === 'restarts') {
      if (method === 'GET' && parts.length === 1) {
        this.forced('restarts');
        return { ...v, restarts: [...this.engine.restarts].sort((a, b) => b.created - a.created).map((r) => this.engine.view(r, now)) };
      }
      if (method === 'GET' && parts.length === 2) {
        const r = this.engine.restarts.find((x) => x.id === parts[1]);
        if (!r) no('no_restart', `no restart '${parts[1]}'`);
        return { ...v, ...this.engine.view(r!, now) };
      }
      if (method === 'POST' && parts.length === 3 && parts[2] === 'cancel') {
        this.forced('cancel');
        return { ...v, ...this.engine.cancel(parts[1], who, now, (r) => this.audit(now, who, 'settings.cancel', `engine/${r.host.id}`, [], r.change_set, 'cancelled')) };
      }
      no('not_found', `no route ${method} /v1/settings/${parts.join('/')}`);
    }
    if (parts.length && this.order.includes(parts[0] as Scope)) {
      const scope = parts[0] as Scope;
      const rest = parts.slice(1).join('/');
      if (method === 'GET' && (rest === '' || rest === 'schema' || rest === 'versions')) {
        this.forced(rest === '' ? 'values' : rest);
        const t = this.target(scope, target);
        const s = this.scopes[scope]!;
        return { ...v, scope, target: t, ...(rest === '' ? s.get(t) : rest === 'schema' ? s.schema(t) : s.versions(t)) };
      }
      if (method === 'POST' && rest === 'plan') return this.planParts(this.parts(this.one(scope, body)));
      if (method === 'POST' && rest === 'apply') return this.apply(this.one(scope, body), who, now);
      if (method === 'POST' && rest === 'restore') {
        const b = this.one(scope, body, true);
        return b.confirm ? this.apply(b, who, now) : this.planParts(this.parts(b));
      }
    }
    return no('not_found', `no route ${method} /v1/settings${parts.length ? '/' : ''}${parts.join('/')}`);
  }
}
