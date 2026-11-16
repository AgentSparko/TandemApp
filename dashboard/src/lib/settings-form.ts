// A scope's form, from its schema (VIS-280): each key becomes a field with the control its type
// asks for, what it holds now or what is staged for it, why it is locked when it is, what changing
// it costs (a restart, the disk cache's fingerprint) and whether it can go back to what stands
// below it. The same generator serves every scope: the page's switches, the backends' fields, the
// conductor's documents, an engine host's keys, and each hook's and decision provider's switches
// (the hooks scope, proxy/hooksettings.py). Kept free of the DOM; views/settings/form.ts draws the
// fields.

import type { KeySpec, KeyValue, SchemaView, Scope, ValuesView, Version } from '../api/settings';
import { same, type DraftPart } from './settings-draft';

/** switch: on or off; choice: a few named values side by side; lines: a list of words, one a line;
 *  json: a whole document; secret: set or unset, never shown; routes: the routing rules in order */
export type Control = 'switch' | 'choice' | 'select' | 'number' | 'text' | 'lines' | 'json' | 'secret' | 'routes';

export interface Chip {
  /** live: an engine restarts; warn: the disk cache's fingerprint; quiet: nothing is interrupted */
  tone: 'live' | 'warn' | 'quiet';
  text: string;
  title?: string;
}

export interface Field {
  id: string;
  scope: Scope;
  target: string;
  key: string;
  label: string;
  /** set in monospace: the label is the key itself (an engine's variable) */
  mono: boolean;
  /** the scope's own sentence for the key */
  text: string | null;
  spec: KeySpec;
  held: KeyValue | null;
  control: Control;
  /** what the control shows: the staged value, else what holds now */
  value: unknown;
  staged: 'set' | 'unset' | null;
  locked: boolean;
  /** why it is locked, when the field has a reason of its own */
  why: string | null;
  chips: Chip[];
  hints: string[];
  /** the button that hands the key back to what stands below it; null when there is no such thing */
  reset: string | null;
}

export interface Group {
  id: string;
  title: string;
  sub: string | null;
  fields: Field[];
}

export const fieldId = (scope: Scope, target: string, key: string): string => `${scope}|${target}|${key}`;

export const FINGERPRINT_NOTE = 'It feeds the disk cache’s fingerprint: after the restart every saved prefix and parked chat on that engine misses once.';

export function controlOf(spec: KeySpec, held: Pick<KeyValue, 'value'> | null = null): Control {
  if (spec.secret || spec.type === 'secret') return 'secret';
  if (spec.type === 'boolean') return 'switch';
  if (spec.type === 'enum') {
    const v = spec.values ?? [];
    if (v.length === 2 && v.includes('on') && v.includes('off')) return 'switch';
    return v.length <= 4 ? 'choice' : 'select';
  }
  if (spec.type === 'integer' || spec.type === 'number') return 'number';
  if (spec.type === 'list') {
    if (spec.key === 'routes') return 'routes';
    const sample = held?.value ?? spec.default;
    return sample == null || (Array.isArray(sample) && sample.every((x) => typeof x === 'string')) ? 'lines' : 'json';
  }
  if (spec.type === 'object' || spec.type === 'document') return 'json';
  return 'text';
}

const WORDS: Record<string, string> = { url: 'URL', key: 'API key', dcgm_url: 'DCGM URL', usd: 'USD' };

/** `timeouts.first_byte` -> `Timeouts: first byte` */
export function humanize(name: string): string {
  if (WORDS[name]) return WORDS[name];
  const [head, ...rest] = name.split('.');
  const say = (s: string) => WORDS[s] ?? s.split('_').map((w) => WORDS[w] ?? w).join(' ');
  const h = say(head);
  const out = rest.length ? `${h}: ${rest.map(say).join(' ')}` : h;
  return out.charAt(0).toUpperCase() + out.slice(1);
}

/** a scope's own words for a key, as a sentence */
export const sentence = (s: string): string => `${s.charAt(0).toUpperCase()}${s.slice(1)}${/[.!?]$/.test(s) ? '' : '.'}`;

/** The engine's mode (the settings interface's own key for QSE_MODE): the one engine setting with a
 *  name of its own on the page, with the extension chat mode runs beside it. */
export const MODE_KEY = 'mode';
export const EXT_KEY = 'mode.ext';
/** what a switch of the mode costs and keeps, under the choice */
export const MODE_NOTE = 'A switch restarts the engine once, when it is idle. Chat mode installs TandemApp’s chat extension first when the engine host lacks it. Back to the bare engine, the chat extension and its data stay on the engine host’s disk: chat mode finds them again';
export const EXT_NOTE = 'The module chat mode runs. TandemApp installs its own on the engine host when it is missing there';
export const MODE_WORDS: Record<string, { label: string; text: string }> = {
  engine: { label: 'Bare engine', text: 'The engine serves its model and nothing else runs inside it.' },
  chat: { label: 'Chat mode', text: 'The engine runs its own conductor and chat extension in front of its model.' },
};

/** A key of the hooks scope: a hook's (`hooks.<id>.<field>`) or a decision provider's
 *  (`providers.<id>.<field>`, `providers.<id>.questions.<question>.api_key`). */
export interface HookKey {
  kind: 'hook' | 'provider';
  id: string;
  field: string;
  /** a question's own API key */
  question: string | null;
}

export function hookKey(key: string): HookKey | null {
  const q = /^providers\.(.+)\.questions\.([a-z_]+)\.api_key$/.exec(key);
  if (q) return { kind: 'provider', id: q[1], field: 'api_key', question: q[2] };
  const m = /^(hooks|providers)\.(.+)\.([a-z_]+)$/.exec(key);
  return m ? { kind: m[1] === 'hooks' ? 'hook' : 'provider', id: m[2], field: m[3], question: null } : null;
}

const HOOK_LABEL: Record<string, string> = { enabled: 'On', mode: 'Mode', priority: 'Priority', scope: 'Runs on', policy: 'When it fails', timeout_ms: 'Time limit (ms)', payload: 'What it sees', secret: 'Signing secret' };
const PROVIDER_LABEL: Record<string, string> = { enabled: 'On', mode: 'Mode', ab_percent: 'A/B share (%)', sticky: 'A/B sticks to', questions: 'Questions', api_key: 'API key' };
/** the order of a hook's and a provider's fields in their group */
const HOOK_ORDER = ['enabled', 'mode', 'priority', 'payload', 'scope', 'policy', 'timeout_ms', 'secret'];
const PROVIDER_ORDER = ['enabled', 'mode', 'ab_percent', 'sticky', 'questions', 'api_key'];

export function labelOf(scope: Scope, key: string, held: KeyValue | null = null): { label: string; mono: boolean } {
  if (scope === 'app') return { label: key === 'chat' ? 'Chat' : key === 'conductor' ? 'Conductor' : humanize(key), mono: false };
  if (scope === 'engine') return key === MODE_KEY ? { label: 'Mode', mono: false } : key === EXT_KEY ? { label: 'Chat extension', mono: false } : { label: key, mono: true };
  if (scope === 'conductor') return { label: key.startsWith('profiles.') ? `Profile ${key.slice(9)}` : humanize(key), mono: false };
  if (scope === 'backends') {
    if (key === 'routes') return { label: 'Routing rules', mono: false };
    if (key.startsWith('users.')) return { label: held?.label ? `Key of ${held.label}` : `Key ${key.split('.')[1]}`, mono: false };
    return { label: humanize(key.split('.').slice(2).join('.')), mono: false };
  }
  if (scope === 'plugins') {
    if (key === 'enabled') return { label: 'On', mono: false };
    if (key === 'trust') return { label: 'Where it runs', mono: false };
    const [group, ...rest] = key.split('.');
    return group === 'settings' ? { label: humanize(rest.join('.')), mono: false } : { label: rest.join('.'), mono: true };
  }
  const h = hookKey(key);
  if (h?.question) return { label: `API key for ${h.question}`, mono: false };
  const own = h && (h.kind === 'hook' ? HOOK_LABEL : PROVIDER_LABEL)[h.field];
  return { label: own ?? humanize(h ? h.field : key.replace(/^hooks\./, '')), mono: false };
}

/** A value in a few words, for a plan's line or a hint. */
export function shown(v: unknown, max = 72): string {
  if (v === null || v === undefined) return 'not set';
  if (v === '') return 'empty';
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  if (typeof v === 'string' || typeof v === 'number') return String(v).length > max ? `${String(v).slice(0, max - 1)}…` : String(v);
  if (Array.isArray(v) && v.every((x) => typeof x === 'string')) {
    const s = v.length ? v.join(', ') : 'none';
    return s.length > max ? `${s.slice(0, max - 1)}…` : s;
  }
  const s = JSON.stringify(v);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function chipsOf(scope: Scope, spec: KeySpec): Chip[] {
  const out: Chip[] = [];
  if (spec.restart === 'engine') out.push({ tone: 'live', text: 'restarts the engine', title: 'The engine restarts once, when it is idle. Its backends answer “try again” while it does.' });
  else if (spec.restart === 'app') out.push({ tone: 'live', text: 'restarts TandemApp' });
  else if (spec.restart === 'hot' && scope === 'engine') out.push({ tone: 'quiet', text: 'no restart', title: 'The running engine takes it at once.' });
  if (spec.fingerprint) out.push({ tone: 'warn', text: 'saved caches start cold', title: FINGERPRINT_NOTE });
  return out;
}

const RESET: Record<Scope, string> = {
  app: 'Use the install’s setting',
  backends: 'Clear',
  conductor: 'Back to the built-in one',
  engine: 'Use the engine’s own value',
  hooks: 'Use the default',
  plugins: 'Use the default',
};

function resetOf(scope: Scope, spec: KeySpec, held: KeyValue | null): string | null {
  if (!held?.set) return null;
  if (spec.secret) return spec.nullable ? 'Remove the key' : null;
  if (scope === 'app') return held.source === 'page' ? RESET.app : null;
  if (scope === 'conductor') return RESET.conductor;
  if (scope === 'engine') return held.source === 'managed' ? RESET.engine : null;
  return spec.nullable && held.value !== null ? RESET[scope] : null;
}

const ENGINE_LAYER: Record<string, string> = { managed: 'Set from TandemApp', default: 'The engine’s default', install: 'From its install', profile: 'From its model’s profile', setup: 'From its Setup Guide', local: 'From its local file', mode: 'From its mode' };

function hintsOf(scope: Scope, spec: KeySpec, held: KeyValue | null, control: Control): string[] {
  const out: string[] = [];
  if (scope === 'engine' && spec.key === MODE_KEY) return out; // the two choices say it
  if (control !== 'secret' && control !== 'json' && control !== 'routes' && spec.default !== null && spec.default !== undefined && spec.default !== '' && !(Array.isArray(spec.default) && !spec.default.length)) out.push(`Default ${shown(spec.default, 40)}`);
  if (!held) return out;
  if (scope === 'app') out.push(held.source === 'page' ? 'Set on this page' : 'As installed');
  else if (scope === 'engine') out.push(ENGINE_LAYER[held.source] ?? `From ${held.source}`);
  else if (scope === 'conductor') out.push(held.set ? `Stored${held.version ? `, version ${held.version.split('@').pop()}` : ''}` : 'The built-in one');
  return out;
}

function lockOf(schema: Pick<SchemaView, 'writable' | 'why'>, spec: KeySpec): { locked: boolean; why: string | null } {
  if (!schema.writable) return { locked: true, why: null }; // the page says the scope's reason once
  if (spec.writable) return { locked: false, why: null };
  if (spec.variable) return { locked: true, why: `Set by ${spec.variable} in the environment. Change it there.` };
  if (spec.why === 'may hold a secret') return { locked: true, why: 'It may hold a secret: it is set on the engine host, never from here.' };
  if (spec.why?.startsWith('locked: ')) return { locked: true, why: `Locked: ${spec.why.slice(8)}.` };
  return { locked: true, why: spec.why ? `Not changed from here: ${spec.why}.` : 'Not changed from here.' };
}

/** The sentence under a label: the scope's own, unless it only says the label again (a document
 *  of the conductor, a backend's key, a user's key); the engine's mode gets the page's. */
function textOfKey(scope: Scope, spec: KeySpec): string | null {
  if (scope === 'engine' && spec.key === MODE_KEY) return MODE_NOTE;
  if (scope === 'engine' && spec.key === EXT_KEY) return EXT_NOTE;
  if (scope === 'conductor' || (scope === 'backends' && (spec.key.startsWith('users.') || spec.key.endsWith('.key')))) return null;
  return spec.text ?? null;
}

/** A scope's keys as fields, in the schema's order. */
export function fieldsOf(schema: SchemaView, values: Pick<ValuesView, 'values'> | null, part: DraftPart | undefined): Field[] {
  const by = new Map((values?.values ?? []).map((v) => [v.key, v]));
  return schema.keys.map((spec) => {
    const held = by.get(spec.key) ?? null;
    const control = controlOf(spec, held);
    const staged = part && spec.key in part.set ? 'set' : part?.unset.includes(spec.key) ? 'unset' : null;
    const value = staged === 'set' ? part!.set[spec.key] : staged === 'unset' ? spec.default : (held?.value ?? null);
    const { label, mono } = labelOf(schema.scope, spec.key, held);
    return {
      id: fieldId(schema.scope, schema.target, spec.key),
      scope: schema.scope,
      target: schema.target,
      key: spec.key,
      label,
      mono,
      text: textOfKey(schema.scope, spec),
      spec,
      held,
      control,
      value,
      staged,
      ...lockOf(schema, spec),
      chips: chipsOf(schema.scope, spec),
      hints: hintsOf(schema.scope, spec, held, control),
      reset: resetOf(schema.scope, spec, held),
    };
  });
}

const BACKEND_ORDER = ['label', 'url', 'enabled', 'key', 'models_mode', 'models', 'fallback', 'max_inflight', 'inject_usage'];

function backendRank(key: string): string {
  const f = key.split('.').slice(2).join('.');
  const i = BACKEND_ORDER.indexOf(f);
  return i >= 0 ? `0${String(i).padStart(2, '0')}` : `1${f}`;
}

/** The fields in the groups the page shows: a backend each, the routing rules and the users' keys
 *  for the backends; for an engine by what a change costs: its mode (with the extension while
 *  chat is chosen), what applies at once, what needs a restart, what is set on its own host. */
export function groupsOf(scope: Scope, fields: Field[]): Group[] {
  if (scope === 'backends') {
    const out: Group[] = [];
    const ids = [...new Set(fields.filter((f) => f.key.startsWith('backends.')).map((f) => f.key.split('.')[1]))];
    for (const id of ids) {
      const own = fields.filter((f) => f.key.startsWith(`backends.${id}.`)).sort((a, b) => (backendRank(a.key) < backendRank(b.key) ? -1 : 1));
      const name = own.find((f) => f.key === `backends.${id}.label`)?.value;
      out.push({ id: `backend:${id}`, title: typeof name === 'string' && name ? name : id, sub: id, fields: own });
    }
    const routes = fields.filter((f) => f.key === 'routes');
    if (routes.length) out.push({ id: 'routes', title: 'Routing rules', sub: routes[0].text ? sentence(routes[0].text) : null, fields: routes });
    const users = fields.filter((f) => f.key.startsWith('users.'));
    if (users.length) out.push({ id: 'users', title: 'Users’ keys', sub: 'A key switched off is refused until it is switched on again.', fields: users });
    return out;
  }
  if (scope === 'engine') {
    // the mode first; the extension beside it while chat is chosen (or staged)
    const chat = fields.some((f) => f.key === MODE_KEY && f.value === 'chat');
    const mode = fields.filter((f) => !f.locked && (f.key === MODE_KEY || (f.key === EXT_KEY && (chat || f.staged !== null))));
    const off = fields.filter((f) => f.key === EXT_KEY && !mode.includes(f));
    const rest = fields.filter((f) => !mode.includes(f) && !off.includes(f));
    const open = rest.filter((f) => !f.spec || f.spec.writable);
    return [
      { id: 'mode', title: 'How it runs', sub: null, fields: mode },
      { id: 'hot', title: 'Applies at once', sub: 'The running engine takes these without a restart.', fields: open.filter((f) => f.spec.restart === 'hot' || f.spec.restart === 'none') },
      { id: 'restart', title: 'Needs a restart', sub: 'The engine reads these when it starts. A change restarts it once, when it is idle.', fields: open.filter((f) => f.spec.restart === 'engine' || f.spec.restart === 'app') },
      { id: 'host', title: 'Set on the engine host', sub: 'Shown for reference. These are not changed from TandemApp.', fields: rest.filter((f) => !f.spec.writable) },
    ].filter((g) => g.fields.length);
  }
  if (scope === 'hooks') {
    // a group a hook, then a group a decision provider, each named after it
    const out: Group[] = [];
    const rest: Field[] = [];
    const by = new Map<string, Group>();
    for (const f of fields) {
      const h = hookKey(f.key);
      if (!h) {
        rest.push(f);
        continue;
      }
      const id = `${h.kind}:${h.id}`;
      let g = by.get(id);
      if (!g) {
        g = { id, title: h.id, sub: h.kind === 'hook' ? `Hook ${h.id}` : `Decision provider ${h.id}`, fields: [] };
        by.set(id, g);
        out.push(g);
      }
      if (typeof f.held?.label === 'string' && f.held.label) g.title = f.held.label;
      g.fields.push(f);
    }
    const rank = (f: Field): string => {
      const h = hookKey(f.key)!;
      const order = h.kind === 'hook' ? HOOK_ORDER : PROVIDER_ORDER;
      const i = order.indexOf(h.field);
      return `${i >= 0 ? String(i).padStart(2, '0') : '99'}${h.question ?? ''}`;
    };
    for (const g of out) g.fields.sort((a, b) => (rank(a) < rank(b) ? -1 : rank(a) > rank(b) ? 1 : 0));
    out.sort((a, b) => (a.id.startsWith('hook:') === b.id.startsWith('hook:') ? 0 : a.id.startsWith('hook:') ? -1 : 1));
    if (rest.length) out.push({ id: 'hooks', title: 'Other settings', sub: null, fields: rest });
    return out;
  }
  if (scope === 'plugins') {
    // by what a key is: how it runs, its own settings, its secrets, what it was granted
    const of = (id: string, title: string, sub: string | null, has: (k: string) => boolean): Group => ({ id: `plugin:${id}`, title, sub, fields: fields.filter((f) => has(f.key)) });
    return [
      of('run', 'How it runs', null, (k) => k === 'enabled' || k === 'trust'),
      of('settings', 'Settings', 'Each property of the plugin’s own schema.', (k) => k.startsWith('settings.')),
      of('secrets', 'Secrets', 'Write-only: set or removed, never shown.', (k) => k.startsWith('secrets.')),
      of('capabilities', 'Capabilities', 'On: granted. A grant takes pairing’s rights.', (k) => k.startsWith('capabilities.')),
    ].filter((g) => g.fields.length);
  }
  return fields.length ? [{ id: scope, title: scope === 'conductor' ? 'Documents' : '', sub: null, fields }] : [];
}

export type Parsed = { ok: true; value: unknown } | { ok: true; unset: true } | { ok: false; error: string };

/** What a field's text means, or why it means nothing yet. `asText`: an engine's values are text,
 *  a number too. */
export function parseInput(spec: KeySpec, control: Control, raw: string, asText = false): Parsed {
  const t = raw.trim();
  if (control === 'number') {
    if (!t) return spec.nullable ? { ok: true, unset: true } : { ok: false, error: 'A number is needed here.' };
    if (spec.type === 'integer' ? !/^-?\d+$/.test(t) : !Number.isFinite(Number(t))) return { ok: false, error: spec.type === 'integer' ? 'A whole number, like 16.' : 'A number, like 2.5.' };
    return { ok: true, value: asText ? t : Number(t) };
  }
  if (control === 'lines') return { ok: true, value: raw.split('\n').map((x) => x.trim()).filter(Boolean) };
  if (control === 'json') {
    let v: unknown;
    try {
      v = JSON.parse(raw);
    } catch (e) {
      return { ok: false, error: `Not valid JSON yet: ${(e as Error).message}` };
    }
    if (spec.type === 'list' ? !Array.isArray(v) : !v || typeof v !== 'object' || Array.isArray(v)) return { ok: false, error: spec.type === 'list' ? 'A list: [ … ].' : 'A whole document: { … }.' };
    return { ok: true, value: v };
  }
  if (!t && spec.nullable) return { ok: true, unset: true };
  return { ok: true, value: spec.type === 'string' ? raw : t };
}

/** The text a field's control starts with. */
export function textOf(control: Control, value: unknown): string {
  if (value === null || value === undefined) return '';
  if (control === 'lines') return Array.isArray(value) ? value.join('\n') : '';
  if (control === 'json') return JSON.stringify(value, null, 2);
  return String(value);
}

/** an engine's values travel as text */
export const asText = (f: Pick<Field, 'scope' | 'held'>): boolean => f.scope === 'engine' || typeof f.held?.value === 'string';

export interface VersionDiff {
  key: string;
  /** in the version */
  then: unknown;
  /** now */
  now: unknown;
}

/** What differs between a version and now, key by key; null when the version carries no values (a
 *  conductor document: its plan shows the diff). An engine's version is its overlay, so it is
 *  compared with what TandemApp sets there now. Secrets are in neither. */
export function versionDiff(scope: Scope, v: Pick<Version, 'values'>, values: KeyValue[], secrets: Set<string> = new Set()): VersionDiff[] | null {
  if (!v.values) return null;
  const now: Record<string, unknown> = {};
  for (const x of values) if (!secrets.has(x.key) && (scope !== 'engine' || x.source === 'managed')) now[x.key] = x.value;
  const keys = [...new Set([...Object.keys(v.values), ...Object.keys(now)])].filter((k) => !secrets.has(k)).sort();
  return keys.filter((k) => !same(v.values![k], now[k])).map((k) => ({ key: k, then: v.values![k], now: now[k] }));
}

export interface RouteRule {
  pattern: string;
  backend: string;
  enabled: boolean;
  /** the environment sets this rule: it keeps its place and its value */
  variable?: string;
}

export const asRoutes = (v: unknown): RouteRule[] => (Array.isArray(v) ? (v as RouteRule[]) : []);
