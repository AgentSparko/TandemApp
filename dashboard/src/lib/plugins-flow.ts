// The plugins pages' state (VIS-284), free of the DOM: the installed list with each plugin's
// state; an install from its source to its result (the source is checked first and its report
// shown: nothing is installed before the user saw what it asks for); one plugin's page (its
// status, its log, start, stop and restart, the check of its files, its removal); and a panel
// with the page's side of its bridge.
//
// What a plugin is set to is the settings interface's plugins scope, one target a plugin: on or
// off, each property of its settings, each secret, each capability and its trust level are keys
// there. This flow stages them into the settings area's draft, so the area's bar, review sheet
// (with its second confirm for a risky grant) and versions serve a plugin as they serve a hook.
// What runs at once is only what the plugin host does at once: start, stop, restart, a check of
// the files, and the install and removal themselves.

import { asPluginError, type Inspection, type Installed, type Panel, type PluginError, type PluginFull, type PluginList, type PluginsApi, type Source, type Status, type Trust, type Verified } from '../api/plugins';
import type { KeyValue, SettingsApi } from '../api/settings';
import { PLUGINS_SCOPE, SOURCE_EMPTY, fieldText, formOf, getAt, parseField, pathKey, setAt, sourceErrors, sourceField, sourceOf, validateField, versionMove, type SourceForm, type Step } from './plugins';
import type { FormField } from './schema-form';
import { same, type Draft } from './settings-draft';

export const KEY = {
  enabled: 'enabled',
  trust: 'trust',
  setting: (prop: string): string => `settings.${prop}`,
  secret: (name: string): string => `secrets.${name}`,
  cap: (capability: string): string => `capabilities.${capability}`,
};

export const TAILS = [50, 200, 1000];
/** a panel's bridge: how many calls may be under way at once, and how large one call's params may be */
export const BRIDGE_INFLIGHT = 8;
export const BRIDGE_PARAMS_MAX = 65536;

export type SourceField = 'path' | 'url' | 'commit' | 'subdir';

export interface InstallState {
  form: SourceForm;
  /** the plugin an upgrade was started from */
  upgrade: string | null;
  /** the form was sent once: its errors show */
  tried: boolean;
  errors: Partial<Record<SourceField, string>>;
  step: 'form' | 'inspecting' | 'report' | 'installing' | 'done';
  report: Inspection | null;
  /** what refused the check or the install */
  refused: PluginError | null;
  /** an older version: the user asked for the way back */
  downgrade: boolean;
  done: Installed | null;
}

export interface LogState {
  lines: string[] | null;
  tail: number;
  busy: boolean;
  /** read again with the status while the page shows */
  follow: boolean;
  refused: PluginError | null;
  at: number | null;
}

export type Act = 'start' | 'stop' | 'restart' | 'verify';

export interface OneState {
  id: string;
  /** no such plugin (any more) */
  missing: boolean;
  refused: PluginError | null;
  plugin: PluginFull | null;
  status: Status | null;
  statusRefused: PluginError | null;
  /** what the plugins scope holds for it, key by key: which values are set and which are defaults */
  held: Record<string, KeyValue> | null;
  logs: LogState;
  act: { busy: Act | null; refused: PluginError | null; verified: Verified | null };
  remove: { step: 'idle' | 'ask' | 'busy'; refused: PluginError | null };
  /** a setting's value while it is being edited and does not hold yet */
  work: Record<string, unknown>;
  /** what is typed in a field, by its path's key */
  text: Record<string, string>;
  /** what is wrong with a value, by its path's key */
  invalid: Record<string, string>;
  /** a text that means nothing yet (a number that is none, JSON that does not parse) */
  unparsed: Record<string, string>;
  /** the secrets whose field is open, with what is typed */
  secretOpen: Record<string, string>;
}

export interface Finding {
  title: string;
  fix: string;
  detail: string | null;
}

export interface BridgeNote {
  method: string;
  ok: boolean;
  why: string | null;
  at: number;
}

export interface PanelState {
  id: string;
  name: string;
  step: 'reading' | 'ready' | 'failed';
  plugin: PluginFull | null;
  panel: Panel | null;
  /** why there is no frame: what is wrong and what to do */
  finding: Finding | null;
  /** the bridge's calls so far, and the newest few */
  calls: number;
  recent: BridgeNote[];
  inflight: number;
  /** the frame is loaded anew when this changes */
  load: number;
}

export interface BridgeReply {
  type: 'tandem-bridge-result';
  id: string | number;
  result?: unknown;
  error?: { type: string; message: string };
}

/** What a panel's address answers: its status and content type; status 0 when nothing answered. */
export type Probe = (url: string) => Promise<{ status: number; type: string }>;

/** a path's key is the place `at` or one below it */
const under = (key: string, at: string): boolean => key === at || key.startsWith(`${at}\u0000`);

const LOGS: LogState = { lines: null, tail: 200, busy: false, follow: false, refused: null, at: null };

export class PluginsFlow {
  info: PluginList | null = null;
  /** each plugin's state as the plugin host tells it; null where it could not be read */
  statuses: Record<string, Status | null> = {};
  refused: PluginError | null = null;
  install: InstallState | null = null;
  one: OneState | null = null;
  panel: PanelState | null = null;
  /** the property whose field is being typed in: what is typed there stays, whatever the draft does */
  private editing: string | null = null;

  constructor(
    private api: PluginsApi,
    private settings: Pick<SettingsApi, 'values'>,
    private draft: Draft,
    private changed: () => void = () => undefined,
    private clock: () => number = () => Date.now(),
    private probe: Probe = async () => ({ status: 200, type: 'text/html' }),
  ) {}

  get now(): number {
    return this.clock();
  }

  // ---- the list -------------------------------------------------------------------------------------

  /** The installed plugins, then the state of each one the plugin host runs. */
  async load(): Promise<void> {
    try {
      this.info = await this.api.list();
      this.refused = null;
    } catch (e) {
      this.refused = asPluginError(e);
      this.changed();
      return;
    }
    this.changed();
    const run = this.info.plugins.filter((p) => p.kind === 'plugin');
    const got = await Promise.all(run.map((p) => this.api.status(p.id).catch(() => null)));
    this.statuses = Object.fromEntries(run.map((p, i) => [p.id, got[i]]));
    this.changed();
  }

  // ---- install --------------------------------------------------------------------------------------

  /** A fresh install form; `from`: the source of the plugin an upgrade replaces. */
  startInstall(from: { id: string; source: Source } | null = null): void {
    this.install = { form: from ? formOf(from.source) : { ...SOURCE_EMPTY }, upgrade: from?.id ?? null, tried: false, errors: {}, step: 'form', report: null, refused: null, downgrade: false, done: null };
    this.changed();
  }

  /** The form changed: a report of another source no longer stands. */
  editSource(p: Partial<SourceForm>): void {
    const s = this.install;
    if (!s || s.step === 'inspecting' || s.step === 'installing') return;
    const form = { ...s.form, ...p };
    this.install = { ...s, form, errors: s.tried ? sourceErrors(form) : {}, step: 'form', report: null, refused: null, downgrade: false, done: null };
    this.changed();
  }

  /** Check the source: every check, the manifest and what it asks for. Nothing is installed. */
  async inspect(): Promise<boolean> {
    const s = this.install;
    if (!s || s.step === 'inspecting' || s.step === 'installing') return false;
    const errors = sourceErrors(s.form);
    if (Object.keys(errors).length) {
      this.install = { ...s, tried: true, errors, step: 'form', report: null, refused: null };
      this.changed();
      return false;
    }
    this.install = { ...s, tried: true, errors: {}, step: 'inspecting', report: null, refused: null, downgrade: false, done: null };
    this.changed();
    try {
      const report = await this.api.inspect(sourceOf(s.form));
      if (this.install?.step === 'inspecting') this.install = { ...this.install, step: 'report', report };
    } catch (e) {
      const err = asPluginError(e);
      const f = sourceField(err.facts.field);
      // a refusal of the source lands on its field
      if (this.install?.step === 'inspecting') this.install = { ...this.install, step: 'form', refused: err, errors: f ? { [f]: err.message.replace(/^source\.\w+:\s*/, '') } : {} };
    }
    this.changed();
    return this.install?.step === 'report';
  }

  setDowngrade(on: boolean): void {
    if (!this.install) return;
    this.install = { ...this.install, downgrade: on };
    this.changed();
  }

  /** Why Install cannot go now; null when it can. */
  installBlocked(): string | null {
    const s = this.install;
    if (!s || s.step !== 'report' || !s.report) return 'Check the source first.';
    if (!s.report.ok) return 'The checks refused it.';
    const move = versionMove(s.report);
    if (move === 'same') return s.report.installed!.sha256 === s.report.sha256 ? 'This version is installed already.' : 'A changed plugin carries a new version.';
    if (move === 'downgrade' && !s.downgrade) return 'Say that you want the older version.';
    return null;
  }

  /** Install what the report is of. A plugin of that name is replaced: an upgrade, or with the
   *  user's word a way back to an older version. */
  async commit(): Promise<Installed | null> {
    const s = this.install;
    if (!s || this.installBlocked() || !s.report) return null;
    this.install = { ...s, step: 'installing', refused: null };
    this.changed();
    try {
      const done = await this.api.install(sourceOf(s.form), { upgrade: !!s.report.installed, allow_downgrade: versionMove(s.report) === 'downgrade' && s.downgrade });
      this.install = { ...this.install, step: 'done', done };
      // an upgrade drops what no longer fits: what was staged for the old version goes with it
      if (done.action === 'upgraded') this.draft.dropPart(PLUGINS_SCOPE, done.plugin.id);
      this.changed();
      return done;
    } catch (e) {
      this.install = { ...this.install, step: 'report', refused: asPluginError(e) };
      this.changed();
      return null;
    }
  }

  closeInstall(): void {
    this.install = null;
    this.changed();
  }

  // ---- one plugin -----------------------------------------------------------------------------------

  /** Open a plugin's page: the plugin, its state, what the settings interface holds for it. */
  async open(id: string): Promise<void> {
    if (this.one?.id !== id) this.one = { id, missing: false, refused: null, plugin: null, status: null, statusRefused: null, held: null, logs: { ...LOGS }, act: { busy: null, refused: null, verified: null }, remove: { step: 'idle', refused: null }, work: {}, text: {}, invalid: {}, unparsed: {}, secretOpen: {} };
    this.changed();
    await this.read();
    if (this.one?.id === id && this.one.plugin?.kind === 'plugin' && this.one.logs.lines === null) void this.readLogs();
  }

  private mine(id: string): OneState | null {
    return this.one?.id === id ? this.one : null;
  }

  /** The plugin, its status and its settings' values, read again. */
  async read(): Promise<void> {
    const id = this.one?.id;
    if (!id) return;
    let plugin: PluginFull;
    try {
      plugin = await this.api.get(id);
    } catch (e) {
      const err = asPluginError(e);
      const o = this.mine(id);
      if (o) {
        if (err.type === 'not_found') o.missing = true;
        else o.refused = err;
      }
      this.changed();
      return;
    }
    const o = this.mine(id);
    if (!o) return;
    o.plugin = plugin;
    o.missing = false;
    o.refused = null;
    this.changed();
    const run = plugin.kind === 'plugin';
    const [status, held] = await Promise.all([
      run ? this.api.status(id).catch((e) => asPluginError(e)) : Promise.resolve(null),
      this.settings.values(PLUGINS_SCOPE, id).then((v) => Object.fromEntries(v.values.map((x) => [x.key, x]))).catch(() => null),
    ]);
    const now = this.mine(id);
    if (!now) return;
    if (status && 'state' in status) {
      now.status = status;
      now.statusRefused = null;
    } else if (status) now.statusRefused = status;
    now.held = held ?? now.held;
    this.changed();
  }

  /** What the page reads again while it shows: the state, and the log when it is followed. */
  async refresh(): Promise<void> {
    const o = this.one;
    if (!o?.plugin || o.missing || o.plugin.kind !== 'plugin' || o.act.busy) return;
    const id = o.id;
    try {
      const s = await this.api.status(id);
      const now = this.mine(id);
      if (now) {
        // its state moved: what it is set to may have (the host turns off what keeps failing), and its log has new lines
        const moved = now.status?.state !== s.state;
        now.status = s;
        now.statusRefused = null;
        if (moved) void this.read().then(() => (this.mine(id)?.logs.follow ? undefined : this.readLogs()));
      }
    } catch (e) {
      const now = this.mine(id);
      if (now) now.statusRefused = asPluginError(e);
    }
    if (this.mine(id)?.logs.follow) await this.readLogs();
    this.changed();
  }

  async readLogs(tail?: number): Promise<void> {
    const o = this.one;
    if (!o || o.logs.busy) return;
    const id = o.id;
    o.logs = { ...o.logs, tail: tail ?? o.logs.tail, busy: true };
    this.changed();
    try {
      const lines = await this.api.logs(id, o.logs.tail);
      const now = this.mine(id);
      if (now) now.logs = { ...now.logs, lines, busy: false, refused: null, at: this.clock() };
    } catch (e) {
      const now = this.mine(id);
      if (now) now.logs = { ...now.logs, busy: false, refused: asPluginError(e) };
    }
    this.changed();
  }

  followLogs(on: boolean): void {
    if (!this.one) return;
    this.one.logs = { ...this.one.logs, follow: on };
    this.changed();
    if (on) void this.readLogs();
  }

  /** Start, stop or restart it now; or hold its files against the sha256 recorded at its install. */
  async act(what: Act): Promise<boolean> {
    const o = this.one;
    if (!o || o.act.busy) return false;
    const id = o.id;
    o.act = { busy: what, refused: null, verified: what === 'verify' ? null : o.act.verified };
    this.changed();
    try {
      if (what === 'verify') {
        const v = await this.api.verify(id);
        const now = this.mine(id);
        if (now) now.act = { busy: null, refused: null, verified: v };
      } else {
        const s = await this.api[what](id);
        const now = this.mine(id);
        if (now) {
          now.status = s;
          now.act = { ...now.act, busy: null };
        }
      }
    } catch (e) {
      const now = this.mine(id);
      if (now) now.act = { ...now.act, busy: null, refused: asPluginError(e) };
      this.changed();
      return false;
    }
    this.changed();
    await this.read();
    if (what !== 'verify') void this.readLogs();
    return true;
  }

  askRemove(on: boolean): void {
    if (!this.one || this.one.remove.step === 'busy') return;
    this.one.remove = { step: on ? 'ask' : 'idle', refused: null };
    this.changed();
  }

  /** Remove the plugin now. While a hook names it the controller refuses, and says which. */
  async remove(): Promise<boolean> {
    const o = this.one;
    if (!o || o.remove.step !== 'ask') return false;
    const id = o.id;
    o.remove = { step: 'busy', refused: null };
    this.changed();
    try {
      await this.api.remove(id);
      this.draft.dropPart(PLUGINS_SCOPE, id);
      if (this.info) this.info = { ...this.info, plugins: this.info.plugins.filter((p) => p.id !== id) };
      if (this.mine(id)) this.one = null;
      this.changed();
      return true;
    } catch (e) {
      const err = asPluginError(e);
      const now = this.mine(id);
      if (now) {
        now.remove = { step: 'ask', refused: err };
        // the hooks that name it, as the refusal lists them
        if (err.type === 'referenced' && err.facts.hooks && now.plugin) now.plugin = { ...now.plugin, references: err.facts.hooks };
      }
      this.changed();
      return false;
    }
  }

  // ---- what is staged -------------------------------------------------------------------------------

  private staged(key: string): { kind: 'set'; value: unknown } | { kind: 'unset' } | null {
    return this.one ? this.draft.staged(PLUGINS_SCOPE, this.one.id, key) : null;
  }
  isStaged(key: string): boolean {
    return this.staged(key) !== null;
  }
  undo(key: string): void {
    const o = this.one;
    if (!o) return;
    this.draft.drop(PLUGINS_SCOPE, o.id, key);
    if (key.startsWith('settings.')) this.forget(key.slice(9));
    if (key.startsWith('secrets.')) delete o.secretOpen[key.slice(8)];
    this.changed();
  }

  /** on or off, as the page shows it: what is staged, else what holds */
  get enabled(): boolean {
    const s = this.staged(KEY.enabled);
    return s?.kind === 'set' ? s.value === true : s?.kind === 'unset' ? false : !!this.one?.plugin?.enabled;
  }
  setEnabled(on: boolean): void {
    const p = this.one?.plugin;
    if (!p) return;
    if (on === p.enabled) this.draft.drop(PLUGINS_SCOPE, p.id, KEY.enabled);
    else this.draft.set(PLUGINS_SCOPE, p.id, KEY.enabled, on);
  }

  granted(capability: string): boolean {
    const s = this.staged(KEY.cap(capability));
    return s?.kind === 'set' ? s.value === true : s?.kind === 'unset' ? false : !!this.one?.plugin?.capabilities.granted.includes(capability);
  }
  grant(capability: string, on: boolean): void {
    const p = this.one?.plugin;
    if (!p || !p.capabilities.requested.includes(capability)) return;
    if (on === p.capabilities.granted.includes(capability)) this.draft.drop(PLUGINS_SCOPE, p.id, KEY.cap(capability));
    else this.draft.set(PLUGINS_SCOPE, p.id, KEY.cap(capability), on);
  }

  get trust(): Trust {
    const s = this.staged(KEY.trust);
    return s?.kind === 'set' ? (s.value as Trust) : s?.kind === 'unset' ? 'sandboxed' : (this.one?.plugin?.trust.granted ?? 'sandboxed');
  }
  setTrust(to: 'sandboxed' | 'host'): void {
    const p = this.one?.plugin;
    if (!p || p.trust.requested !== 'host') return;
    if (to === p.trust.granted) this.draft.drop(PLUGINS_SCOPE, p.id, KEY.trust);
    else this.draft.set(PLUGINS_SCOPE, p.id, KEY.trust, to);
  }

  /** Stage what a plugin needs to run: on, every capability it asks for, and the host when it
   *  asks for it. Nothing is written: the review lists each grant and asks twice for a risky one. */
  setUp(): void {
    const p = this.one?.plugin;
    if (!p) return;
    for (const c of p.capabilities.requested) this.grant(c, true);
    if (p.trust.requested === 'host') this.setTrust('host');
    this.setEnabled(true);
  }

  // ---- the settings form ----------------------------------------------------------------------------

  private field(prop: string): FormField | undefined {
    return this.one?.plugin?.settings_form?.fields.find((f) => f.path[0] === prop);
  }
  private heldOf(prop: string): { value: unknown; set: boolean } {
    const o = this.one!;
    const h = o.held?.[KEY.setting(prop)];
    // without the scope's own answer every value counts as set: equal to it is no change
    return { value: o.plugin!.settings[prop], set: h ? h.set : true };
  }
  /** a property is set (not its default): it can go back */
  isSet(prop: string): boolean {
    return !!this.one?.held?.[KEY.setting(prop)]?.set;
  }

  /** A property's value as the form shows it: what is being typed, what is staged, what holds. */
  value(prop: string): unknown {
    const o = this.one;
    if (!o?.plugin) return undefined;
    if (prop in o.work) return o.work[prop];
    const s = this.staged(KEY.setting(prop));
    if (s?.kind === 'set') return s.value;
    if (s?.kind === 'unset') {
      const f = this.field(prop);
      return f?.has_default ? f.default : undefined;
    }
    return o.plugin.settings[prop];
  }

  private forget(prop: string): void {
    const o = this.one;
    if (!o) return;
    delete o.work[prop];
    for (const m of [o.text, o.invalid, o.unparsed]) for (const k of Object.keys(m)) if (under(k, prop)) delete m[k];
  }

  /** what is wrong at a place of the form: a text that means nothing yet, else a value that does not hold */
  errorAt(path: Step[]): string | undefined {
    const k = pathKey(path);
    return this.one?.unparsed[k] ?? this.one?.invalid[k];
  }

  /** A property's new value: held against its field, then staged; while it does not hold (or a
   *  text under it means nothing yet) it is kept beside what is wrong, and nothing of it is staged. */
  private take(prop: string, next: unknown): void {
    const o = this.one;
    const f = this.field(prop);
    if (!o?.plugin || !f) return;
    for (const k of Object.keys(o.invalid)) if (under(k, prop)) delete o.invalid[k];
    const bad = validateField(f, next, [prop]);
    Object.assign(o.invalid, bad);
    const key = KEY.setting(prop);
    if (Object.keys(bad).length || Object.keys(o.unparsed).some((k) => under(k, prop))) {
      o.work[prop] = next;
      this.draft.drop(PLUGINS_SCOPE, o.id, key);
    } else {
      delete o.work[prop];
      const held = this.heldOf(prop);
      // what holds already is no change: a set value typed again, or a default that was never set
      if (next === undefined ? !held.set : same(next, held.value)) this.draft.drop(PLUGINS_SCOPE, o.id, key);
      else if (next === undefined) this.draft.unset(PLUGINS_SCOPE, o.id, key, held);
      else this.draft.set(PLUGINS_SCOPE, o.id, key, next, held);
    }
    this.changed();
  }

  /** A switch, a choice, a list's item added or removed: the value at `path` (its first step the
   *  property), `undefined` to take it out. */
  put(path: Step[], value: unknown): void {
    const prop = String(path[0]);
    const o = this.one;
    if (!o) return;
    // what was typed below it no longer stands; a list that loses an item renumbers the rest
    const gone = pathKey(value === undefined && typeof path[path.length - 1] === 'number' ? path.slice(0, -1) : path);
    for (const m of [o.text, o.unparsed]) for (const k of Object.keys(m)) if (under(k, gone)) delete m[k];
    this.take(prop, setAt(this.value(prop), path.slice(1), value));
  }

  /** A text field's content changed: what is typed stays as typed; what it means is staged once
   *  it holds. */
  type(path: Step[], field: FormField, raw: string): void {
    const o = this.one;
    if (!o) return;
    const k = pathKey(path);
    const prop = String(path[0]);
    o.text[k] = raw;
    this.editing = prop;
    const p = parseField(field, raw);
    if (!p.ok) {
      o.unparsed[k] = p.error;
      this.take(prop, this.value(prop));
      return;
    }
    delete o.unparsed[k];
    this.take(prop, setAt(this.value(prop), path.slice(1), p.value));
  }

  /** The field lost the focus: a text that holds is tidied. */
  tidy(path: Step[]): void {
    const o = this.one;
    if (!o) return;
    this.editing = null;
    const k = pathKey(path);
    if (!o.unparsed[k] && !o.invalid[k]) delete o.text[k];
    this.changed();
  }

  /** what a text field shows: what is typed, else its value */
  textAt(path: Step[], field: FormField): string {
    const o = this.one;
    if (!o) return '';
    return o.text[pathKey(path)] ?? fieldText(field, getAt(this.value(String(path[0])), path.slice(1)));
  }

  /** Hand a property back to its default. */
  reset(prop: string): void {
    const o = this.one;
    if (!o) return;
    this.forget(prop);
    this.draft.unset(PLUGINS_SCOPE, o.id, KEY.setting(prop), { set: this.isSet(prop) });
    this.changed();
  }

  /** The draft changed elsewhere (discarded, applied): what was typed for a property that is no
   *  longer staged, and holds, goes. */
  synced(): void {
    const o = this.one;
    if (!o) return;
    const props = new Set([...Object.keys(o.text), ...Object.keys(o.unparsed)].map((k) => k.split('\u0000')[0]));
    for (const prop of props) if (prop !== this.editing && !(prop in o.work) && !this.isStaged(KEY.setting(prop))) this.forget(prop);
    for (const n of Object.keys(o.secretOpen)) if (o.secretOpen[n] && !this.isStaged(KEY.secret(n))) delete o.secretOpen[n];
  }

  // ---- secrets --------------------------------------------------------------------------------------

  /** Open or close a secret's field. A typed value lives in this page's memory until it is applied. */
  secretField(name: string, open: boolean): void {
    const o = this.one;
    if (!o) return;
    if (open) o.secretOpen[name] = o.secretOpen[name] ?? '';
    else {
      delete o.secretOpen[name];
      if (this.staged(KEY.secret(name))?.kind === 'set') this.draft.drop(PLUGINS_SCOPE, o.id, KEY.secret(name));
    }
    this.changed();
  }
  secretType(name: string, raw: string): void {
    const o = this.one;
    if (!o) return;
    o.secretOpen[name] = raw;
    if (raw) this.draft.set(PLUGINS_SCOPE, o.id, KEY.secret(name), raw, null, true);
    else this.draft.drop(PLUGINS_SCOPE, o.id, KEY.secret(name));
  }
  /** Stage the stored value's removal. */
  secretRemove(name: string): void {
    const o = this.one;
    if (!o?.plugin) return;
    delete o.secretOpen[name];
    this.draft.unset(PLUGINS_SCOPE, o.id, KEY.secret(name), { set: !!o.plugin.secrets[name]?.set });
  }
  secretState(name: string): 'set' | 'unset' | null {
    return this.staged(KEY.secret(name))?.kind ?? null;
  }

  // ---- a panel --------------------------------------------------------------------------------------

  /** Open a panel: the plugin must run and list it, and its address must answer a page. Anything
   *  else is a finding with what to do, never an empty frame. */
  async openPanel(id: string, name: string): Promise<void> {
    const load = (this.panel?.load ?? 0) + 1;
    const st: PanelState = { id, name, step: 'reading', plugin: null, panel: null, finding: null, calls: 0, recent: [], inflight: 0, load };
    this.panel = st;
    this.changed();
    const fail = (finding: Finding): void => {
      if (this.panel !== st) return;
      st.step = 'failed';
      st.finding = finding;
      this.changed();
    };
    let panels: Panel[];
    try {
      st.plugin = await this.api.get(id);
      panels = await this.api.panels(id);
    } catch (e) {
      const err = asPluginError(e);
      return fail(err.type === 'not_found' ? { title: `No plugin ${id} is installed.`, fix: 'It was removed. Go back to the list.', detail: null } : { title: 'The panel could not be read.', fix: 'Try again.', detail: err.message });
    }
    if (this.panel !== st) return;
    const declared = st.plugin.manifest.contributes?.panels?.some((p) => p.name === name);
    if (!declared) return fail({ title: `${st.plugin.title || st.plugin.name} has no panel ${name}.`, fix: 'Open one of the panels its page lists.', detail: null });
    const panel = panels.find((p) => p.name === name);
    if (!panel) return fail({ title: 'The plugin is not running, so its panel is not served.', fix: st.plugin.enabled ? 'Start it on its page, then open the panel again.' : 'Turn it on, then open the panel again.', detail: null });
    // a frame is pointed only at a panel's own address on this controller
    if (!panel.url.startsWith('/plugin-panels/')) return fail({ title: 'The panel has no address this page can open.', fix: 'Update TandemApp on the controller’s machine.', detail: panel.url });
    const got = await this.probe(panel.url).catch(() => ({ status: 0, type: '' }));
    if (this.panel !== st) return;
    if (got.status !== 200) return fail({ title: 'The panel did not load.', fix: got.status === 0 ? 'TandemApp did not answer at its address. Check that the controller still runs, then load it again.' : 'The plugin lists the panel, but its page is not there. That is a fault in the plugin: tell its author.', detail: got.status ? `${panel.url} answered ${got.status}` : null });
    if (!/^text\/html\b/.test(got.type)) return fail({ title: 'The panel is not a page.', fix: 'Its address answers something other than HTML. That is a fault in the plugin: tell its author.', detail: `${panel.url} answered ${got.type || 'no content type'}` });
    st.panel = panel;
    st.step = 'ready';
    this.changed();
  }

  closePanel(): void {
    this.panel = null;
  }

  private note(st: PanelState, method: string, ok: boolean, why: string | null): void {
    st.calls++;
    st.recent = [{ method, ok, why, at: this.clock() }, ...st.recent].slice(0, 6);
    this.changed();
  }

  /** The page's side of the bridge: one message a panel posted. A message that is not a bridge
   *  call gets no answer. A method the plugin was not given, params that are not an object or too
   *  large, and more calls at once than the page relays are refused here; the rest goes to the
   *  controller, which checks it against the plugin's capabilities once more. */
  async relay(data: unknown): Promise<BridgeReply | null> {
    const st = this.panel;
    if (!st?.panel || st.step !== 'ready') return null;
    const m = data as { type?: unknown; id?: unknown; method?: unknown; params?: unknown } | null;
    if (!m || typeof m !== 'object' || m.type !== 'tandem-bridge') return null;
    if (typeof m.id !== 'string' && !(typeof m.id === 'number' && Number.isFinite(m.id))) return null;
    const id = m.id;
    const no = (method: string, type: string, message: string): BridgeReply => {
      this.note(st, method, false, message);
      return { type: 'tandem-bridge-result', id, error: { type, message } };
    };
    if (typeof m.method !== 'string' || !m.method) return no('(no method)', 'bad_request', 'method: the bridge method');
    const method = m.method.slice(0, 64);
    if (!(st.panel.bridge.methods as string[]).includes(method)) return no(method, 'refused', `the bridge does not offer ${method} to this plugin`);
    if (m.params != null && (typeof m.params !== 'object' || Array.isArray(m.params))) return no(method, 'bad_request', 'params: an object');
    let size = 0;
    try {
      size = m.params == null ? 0 : JSON.stringify(m.params).length;
    } catch {
      return no(method, 'bad_request', 'params: an object that is JSON');
    }
    if (size > BRIDGE_PARAMS_MAX) return no(method, 'bad_request', `params: at most ${BRIDGE_PARAMS_MAX} characters of JSON`);
    if (st.inflight >= BRIDGE_INFLIGHT) return no(method, 'busy', `at most ${BRIDGE_INFLIGHT} calls at once`);
    st.inflight++;
    try {
      const result = await this.api.bridge(st.id, method, (m.params ?? undefined) as Record<string, unknown> | undefined, st.name);
      this.note(st, method, true, null);
      return { type: 'tandem-bridge-result', id, result };
    } catch (e) {
      const err = asPluginError(e);
      this.note(st, method, false, err.message);
      return { type: 'tandem-bridge-result', id, error: { type: err.type, message: err.message } };
    } finally {
      st.inflight--;
    }
  }
}
