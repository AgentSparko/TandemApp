// The hooks pages' and the decision providers' pages' state (VIS-282), free of the DOM: what was
// read, what is being typed, what the controller refused and what to ask again.
//
// HooksFlow: the stage table and the hooks; a move in the order and every switch staged as keys of
// the settings interface (the draft, so they are planned, confirmed and versioned with the other
// settings); and what lives on the hooks' own API: adding a hook (a full payload outside the LAN
// answers 409 confirm_needed: the same request goes again with its confirm once the user agreed),
// changing where it runs or its endpoint (409 conflict: it changed meanwhile, so it is read again
// and shown as it stands), the secret that is answered once, the playground's test call, removing
// it, and a request's trace out of every hook's journal.
//
// ProvidersFlow: the same for the decision providers: the list with each one's health, its
// switches, question mapping and keys as settings keys, add, test, the report and the calls.
//
// views/hooks/ draws their state; tests drive them against the mock without a browser.

import { asHookError, type Health, type Hook, type HookCall, type HookError, type HookKind, type HookMode, type HookPatch, type HookTest, type HooksApi, type Provider, type ProviderCall, type ProviderList, type ProviderPatch, type ProviderTest, type ProvidersApi, type Report, type Role, type StageTable } from '../api/hooks';
import { HERE, HOOKS_SCOPE, PRIORITY_MAX, PRIORITY_MIN, bodyOf, builtinsFor, checkForm, dropStaged, fieldOf, movePriorities, newForm, parseEvent, recentRequests, rolesAt, sampleText, scopeError, scopeOf, scopeTexts, stageSwitch, traceOf, withStaged, type HookForm, type HookSwitch, type RecentRequest, type ScopeKey, type ShownHook, type Trace } from './hooks';
import { TIMEOUT_MAX_MS, apiKeyKey, checkProvider, mappingOf, newProviderForm, providerBody, stageKey, stageProvider, withStagedProviders, type Mapping, type ProviderForm, type ProviderSwitch, type ShownProvider } from './providers';
import type { Draft } from './settings-draft';

/** a full payload outside the LAN: what the user is asked before the request goes again */
export interface Confirm {
  token: string;
  destination: string;
}

export interface AddState {
  form: HookForm;
  /** the list of stops shows: until one is chosen, and again when the user wants another */
  picking: boolean;
  errors: Record<string, string>;
  busy: boolean;
  refused: HookError | null;
  confirm: Confirm | null;
  /** the hook that was made, and an http hook's secret: held in this page's memory until dismissed */
  created: { hook: Hook; secret: string | null } | null;
}

export interface EditState {
  name: string;
  stages: string[];
  endpoint: string;
  /** a built-in's options, as JSON */
  options: string;
  busy: boolean;
  errors: Record<string, string>;
  refused: HookError | null;
  confirm: Confirm | null;
}

export interface PlayState {
  stage: string;
  /** the event as typed: the stage's sample until it is changed */
  raw: string;
  busy: boolean;
  result: HookTest | null;
  refused: HookError | null;
  invalid: string | null;
}

export interface SecretState {
  step: 'idle' | 'ask' | 'busy' | 'shown';
  grace: number;
  /** the new secret, in this page's memory only until it is dismissed */
  value: string | null;
  refused: HookError | null;
}

export interface RemoveState {
  step: 'idle' | 'ask' | 'busy';
  refused: HookError | null;
}

export interface OneHook {
  id: string;
  missing: boolean;
  refused: HookError | null;
  calls: HookCall[] | null;
  callsRefused: HookError | null;
  edit: EditState | null;
  play: PlayState;
  secret: SecretState;
  remove: RemoveState;
  /** said once after something the user did not do: it changed on another screen */
  notice: string | null;
  /** what is typed in a field of the settings rows, and what is wrong with it */
  text: Record<string, string>;
  invalid: Record<string, string>;
}

export interface TraceState {
  request: string;
  loading: boolean;
  refused: HookError | null;
  recent: RecentRequest[] | null;
  trace: Trace | null;
  /** the replay note is open */
  replay: boolean;
}

const confirmOf = (e: HookError): Confirm | null => (e.type === 'confirm_needed' && e.facts.confirm ? { token: e.facts.confirm, destination: e.facts.destination ?? 'a destination outside the LAN' } : null);

export class HooksFlow {
  table: StageTable | null = null;
  list: Hook[] | null = null;
  builtins: string[] = [];
  kinds: HookKind[] = ['http', 'builtin'];
  refused: HookError | null = null;
  add: AddState | null = null;
  one: OneHook | null = null;
  trace: TraceState = { request: '', loading: false, refused: null, recent: null, trace: null, replay: false };
  private journals: Record<string, HookCall[]> = {};

  constructor(
    private api: HooksApi,
    private draft: Draft,
    private changed: () => void = () => undefined,
    private clock: () => number = () => Date.now(),
  ) {}

  /** the hooks as the page shows them: what is staged stands in for what holds */
  get shown(): ShownHook[] {
    return withStaged(this.list ?? [], this.draft.part(HOOKS_SCOPE, HERE));
  }
  hook(id: string): ShownHook | null {
    return this.shown.find((h) => h.id === id) ?? null;
  }
  get now(): number {
    return this.clock();
  }

  /** The stage table (once) and the hooks. */
  async load(): Promise<void> {
    try {
      const [table, list] = await Promise.all([this.table ? Promise.resolve(this.table) : this.api.stages(), this.api.list()]);
      this.table = table;
      this.list = list.hooks;
      this.builtins = list.builtins;
      this.kinds = list.kinds_available;
      this.refused = null;
    } catch (e) {
      this.refused = asHookError(e);
    }
    this.changed();
  }

  private put(h: Hook): void {
    this.list = this.list ? (this.list.some((x) => x.id === h.id) ? this.list.map((x) => (x.id === h.id ? h : x)) : [...this.list, h]) : [h];
  }

  // ---- the switches: keys of the settings interface -------------------------------------------------
  stage(id: string, field: HookSwitch, value: unknown): void {
    const h = this.list?.find((x) => x.id === id);
    if (h) stageSwitch(this.draft, h, field, value);
  }

  /** The list's one control: off, shadow or on. A hook that is switched off altogether is switched
   *  on with it, so the control always does what it says. */
  setMode(id: string, mode: HookMode): void {
    const h = this.hook(id);
    if (!h) return;
    this.stage(id, 'mode', mode);
    if (mode !== 'off' && !h.enabled) this.stage(id, 'enabled', true);
  }

  /** Move a hook within its role at a stage: the priorities that make the new order are staged. */
  move(stage: string, role: Role, from: number, to: number): Record<string, number> {
    const group = withStaged(this.list ?? [], this.draft.part(HOOKS_SCOPE, HERE)).filter((h) => h.stages.includes(stage) && h.role === role);
    const order = [...group].sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : 1));
    const next = movePriorities(order, from, to);
    for (const [id, p] of Object.entries(next)) this.stage(id, 'priority', p);
    return next;
  }

  /** A text field of a hook's settings rows: staged when it means something, marked when not. */
  type(field: 'priority' | 'timeout_ms' | `scope.${ScopeKey}`, raw: string): void {
    const o = this.one;
    const h = o && this.hook(o.id);
    if (!o || !h) return;
    o.text[field] = raw;
    delete o.invalid[field];
    const t = raw.trim();
    if (field === 'priority') {
      if (!/^-?\d+$/.test(t) || Number(t) < PRIORITY_MIN || Number(t) > PRIORITY_MAX) o.invalid[field] = `A whole number, ${PRIORITY_MIN} to ${PRIORITY_MAX}.`;
      else this.stage(h.id, 'priority', Number(t));
    } else if (field === 'timeout_ms') {
      const max = this.table?.max_timeout_ms ?? 30000;
      if (!t) this.stage(h.id, 'timeout_ms', null);
      else if (!/^\d+$/.test(t) || Number(t) < 1 || Number(t) > max) o.invalid[field] = `A whole number of milliseconds, 1 to ${max}. Empty: the stage’s budget.`;
      else this.stage(h.id, 'timeout_ms', Number(t));
    } else {
      const texts = { ...scopeTexts(h.scope), ...Object.fromEntries(Object.entries(o.text).filter(([k]) => k.startsWith('scope.')).map(([k, v]) => [k.slice(6), v])) };
      const scope = scopeOf(texts);
      const why = scopeError(scope);
      if (why) o.invalid[field] = why;
      else this.stage(h.id, 'scope', scope);
    }
    this.changed();
  }

  /** a field lost the focus: a valid text is tidied to what is staged */
  tidy(field: string): void {
    if (this.one && !this.one.invalid[field]) delete this.one.text[field];
    this.changed();
  }

  // ---- a new hook -----------------------------------------------------------------------------------
  startAdd(stage = ''): void {
    if (!this.table) return;
    const form = newForm(this.table, stage);
    this.add = { form, picking: !form.stage, errors: {}, busy: false, refused: null, confirm: null, created: null };
    this.changed();
  }

  /** The form changed. A role the stage does not take, or a built-in the role does not fit, goes. */
  editAdd(p: Partial<HookForm>): void {
    const a = this.add;
    if (!a || !this.table) return;
    const f = { ...a.form, ...p };
    if ('stage' in p) {
      const roles = rolesAt(this.table, f.stage);
      if (!roles.includes(f.role as Role)) f.role = roles.length === 1 ? roles[0] : '';
    }
    if (('stage' in p || 'role' in p || 'kind' in p) && f.kind === 'builtin' && !builtinsFor(this.builtins, f.role, f.stage).includes(f.target)) {
      const fit = builtinsFor(this.builtins, f.role, f.stage);
      f.target = fit.length === 1 ? fit[0] : '';
      f.options = {};
    }
    a.form = f;
    if ('stage' in p) a.picking = !f.stage;
    // an error stays until its field is right, and a new one shows only after a try
    if (Object.keys(a.errors).length) a.errors = checkForm(f, this.table);
    a.refused = null;
    a.confirm = null;
    this.changed();
  }

  /** Add the hook. `confirmed`: the user agreed to a full payload outside the LAN, so the same
   *  request goes again with the confirm the controller answered. */
  async create(confirmed = false): Promise<Hook | null> {
    const a = this.add;
    if (!a || !this.table || a.busy) return null;
    a.errors = checkForm(a.form, this.table);
    if (Object.keys(a.errors).length) {
      this.changed();
      return null;
    }
    a.busy = true;
    a.refused = null;
    this.changed();
    try {
      const r = await this.api.create({ ...bodyOf(a.form), ...(confirmed && a.confirm ? { confirm: a.confirm.token } : {}) });
      a.created = { hook: r.hook, secret: r.secret ?? null };
      a.confirm = null;
      this.put(r.hook);
      return r.hook;
    } catch (e) {
      const err = asHookError(e);
      a.confirm = confirmOf(err);
      if (!a.confirm) {
        a.refused = err;
        const f = fieldOf(err);
        if (f && err.type !== 'exists') a.errors = { ...a.errors, [f === 'stages' ? 'stages' : f]: err.message };
      }
      return null;
    } finally {
      a.busy = false;
      this.changed();
    }
  }

  /** Show the stops again, to choose another. */
  pickStage(): void {
    if (this.add) this.add.picking = true;
    this.changed();
  }

  /** The secret was saved (or there is none): it leaves this page's memory. */
  closeAdd(): void {
    this.add = null;
    this.changed();
  }

  // ---- one hook -------------------------------------------------------------------------------------
  private blank(id: string): OneHook {
    const h = this.list?.find((x) => x.id === id);
    const stage = h?.stages[0] ?? '';
    return { id, missing: false, refused: null, calls: null, callsRefused: null, edit: null, play: { stage, raw: sampleText(stage, this.table), busy: false, result: null, refused: null, invalid: null }, secret: { step: 'idle', grace: 3600, value: null, refused: null }, remove: { step: 'idle', refused: null }, notice: null, text: {}, invalid: {} };
  }

  /** A hook's page opens: the hook as it is now, and its journal. */
  async open(id: string, stage?: string): Promise<void> {
    if (this.one?.id !== id) this.one = this.blank(id);
    const o = this.one;
    this.changed();
    try {
      const h = await this.api.get(id);
      this.put(h);
      o.missing = false;
      o.refused = null;
      const at = stage && h.stages.includes(stage) ? stage : h.stages.includes(o.play.stage) ? o.play.stage : h.stages[0];
      if (at !== o.play.stage) o.play = { ...o.play, stage: at, raw: sampleText(at, this.table), result: null, invalid: null };
    } catch (e) {
      const err = asHookError(e);
      if (err.type === 'not_found') o.missing = true;
      else o.refused = err;
    }
    this.changed();
    if (!o.missing) await this.readCalls();
  }

  async readCalls(): Promise<void> {
    const o = this.one;
    if (!o) return;
    try {
      const r = await this.api.calls(o.id, 200);
      o.calls = r.calls;
      o.callsRefused = null;
      this.journals[o.id] = r.calls;
      const h = this.list?.find((x) => x.id === o.id);
      if (h) this.put({ ...h, metrics: r.metrics });
    } catch (e) {
      o.callsRefused = asHookError(e);
    }
    this.changed();
  }

  startEdit(): void {
    const o = this.one;
    const h = o && this.list?.find((x) => x.id === o.id);
    if (!o || !h) return;
    o.edit = { name: h.name, stages: [...h.stages], endpoint: h.endpoint ?? '', options: JSON.stringify(h.options, null, 2), busy: false, errors: {}, refused: null, confirm: null };
    o.notice = null;
    this.changed();
  }
  editEdit(p: Partial<Pick<EditState, 'name' | 'stages' | 'endpoint' | 'options'>>): void {
    if (!this.one?.edit) return;
    Object.assign(this.one.edit, p, { refused: null, confirm: null });
    this.changed();
  }
  cancelEdit(): void {
    if (this.one) this.one.edit = null;
    this.changed();
  }

  /** Save what the hooks' API holds of a hook: its name, where it runs, its endpoint or options.
   *  It changed meanwhile (409 conflict): it is read again, the form starts over from what stands
   *  now, and the page says so. A full hook's new endpoint outside the LAN asks first. */
  async save(confirmed = false): Promise<boolean> {
    const o = this.one;
    const e = o?.edit;
    const h = o && this.list?.find((x) => x.id === o.id);
    if (!o || !e || !h || !this.table || e.busy) return false;
    const errors: Record<string, string> = {};
    const patch: HookPatch = {};
    if (!e.name.trim() || e.name.trim().length > 80) errors.name = 'A name of 1 to 80 characters.';
    else if (e.name.trim() !== h.name) patch.name = e.name.trim();
    if (!e.stages.length) errors.stages = 'It runs at one stage at least.';
    else {
      const bad = e.stages.find((s) => !this.table!.stages[s]?.roles.includes(h.role));
      if (bad) errors.stages = `A ${h.role} cannot run at ${bad}.`;
      else if (e.stages.join('|') !== h.stages.join('|')) patch.stages = Object.keys(this.table.stages).filter((s) => e.stages.includes(s));
    }
    if (h.kind === 'http') {
      if (!/^https?:\/\/[^\s/@]+(\/\S*)?$/.test(e.endpoint.trim())) errors.endpoint = 'A URL that starts with http:// or https://.';
      else if (e.endpoint.trim() !== h.endpoint) patch.endpoint = e.endpoint.trim();
    } else {
      try {
        const v = JSON.parse(e.options || '{}');
        if (!v || typeof v !== 'object' || Array.isArray(v)) errors.options = 'An object: { … }.';
        else if (JSON.stringify(v) !== JSON.stringify(h.options)) patch.options = v;
      } catch (x) {
        errors.options = `Not valid JSON yet: ${(x as Error).message}`;
      }
    }
    e.errors = errors;
    if (Object.keys(errors).length) {
      this.changed();
      return false;
    }
    if (!Object.keys(patch).length) {
      o.edit = null;
      this.changed();
      return true;
    }
    e.busy = true;
    e.refused = null;
    this.changed();
    try {
      this.put(await this.api.patch(o.id, { ...patch, ...(confirmed && e.confirm ? { confirm: e.confirm.token } : {}) }));
      o.edit = null;
      return true;
    } catch (x) {
      const err = asHookError(x);
      e.busy = false;
      if (err.type === 'conflict') {
        // nothing was written: read it again and start over from what stands now
        try {
          this.put(await this.api.get(o.id));
        } catch {
          /* the read is tried again when the page is */
        }
        o.edit = null;
        this.startEdit();
        o.notice = 'This hook changed on another screen while you were editing it, so nothing was saved. The form shows it as it stands now.';
        return false;
      }
      e.confirm = confirmOf(err);
      if (!e.confirm) {
        e.refused = err;
        const f = fieldOf(err);
        if (f) e.errors = { [f]: err.message };
      }
      return false;
    } finally {
      if (o.edit) o.edit.busy = false;
      this.changed();
    }
  }

  // ---- the playground -------------------------------------------------------------------------------
  play(p: Partial<Pick<PlayState, 'stage' | 'raw'>>): void {
    const o = this.one;
    if (!o) return;
    if (p.stage && p.stage !== o.play.stage) o.play = { ...o.play, stage: p.stage, raw: sampleText(p.stage, this.table), result: null, refused: null, invalid: null };
    if (p.raw !== undefined) {
      const parsed = parseEvent(o.play.stage, p.raw, this.table);
      o.play = { ...o.play, raw: p.raw, invalid: parsed.ok ? null : parsed.error };
    }
    this.changed();
  }
  resetEvent(): void {
    if (this.one) this.one.play = { ...this.one.play, raw: sampleText(this.one.play.stage, this.table), invalid: null };
    this.changed();
  }

  /** The hook once against the stage's sample event (or the one typed). Nothing is applied or
   *  counted, and nothing goes to an engine. */
  async test(): Promise<void> {
    const o = this.one;
    if (!o || o.play.busy) return;
    const parsed = parseEvent(o.play.stage, o.play.raw, this.table);
    if (!parsed.ok) {
      o.play.invalid = parsed.error;
      this.changed();
      return;
    }
    o.play = { ...o.play, busy: true, refused: null, invalid: null };
    this.changed();
    try {
      o.play.result = await this.api.test(o.id, o.play.stage, parsed.event);
    } catch (e) {
      o.play.result = null;
      o.play.refused = asHookError(e);
    }
    o.play.busy = false;
    this.changed();
    // the journal has the call now, as a test
    await this.readCalls();
  }

  // ---- the secret -----------------------------------------------------------------------------------
  secret(p: Partial<Pick<SecretState, 'step' | 'grace'>>): void {
    const o = this.one;
    if (!o) return;
    // a secret that was shown leaves the page's memory with the panel
    o.secret = { ...o.secret, ...p, refused: null, ...(p.step === 'idle' ? { value: null } : {}) };
    this.changed();
  }

  /** A new secret, answered once: it is held until the user says it is saved. */
  async rotate(): Promise<void> {
    const o = this.one;
    if (!o || o.secret.step === 'busy') return;
    o.secret = { ...o.secret, step: 'busy', refused: null };
    this.changed();
    try {
      const r = await this.api.rotate(o.id, o.secret.grace);
      this.put(r.hook);
      o.secret = { ...o.secret, step: 'shown', value: r.secret };
    } catch (e) {
      o.secret = { ...o.secret, step: 'ask', refused: asHookError(e) };
    }
    this.changed();
  }

  // ---- removing -------------------------------------------------------------------------------------
  askRemove(on: boolean): void {
    if (this.one) this.one.remove = { step: on ? 'ask' : 'idle', refused: null };
    this.changed();
  }

  /** Remove the hook; what was staged for it goes with it (its keys no longer exist). */
  async remove(): Promise<boolean> {
    const o = this.one;
    if (!o || o.remove.step === 'busy') return false;
    o.remove = { step: 'busy', refused: null };
    this.changed();
    try {
      await this.api.remove(o.id);
    } catch (e) {
      const err = asHookError(e);
      if (err.type !== 'not_found') {
        o.remove = { step: 'ask', refused: err };
        this.changed();
        return false;
      }
    }
    dropStaged(this.draft, `hooks.${o.id}.`);
    this.list = (this.list ?? []).filter((h) => h.id !== o.id);
    delete this.journals[o.id];
    this.one = null;
    this.changed();
    return true;
  }

  // ---- a request's trace ----------------------------------------------------------------------------
  /** Every hook's journal, read again: the requests they still hold, and the one that is open. */
  async readTrace(request?: string): Promise<void> {
    const t = this.trace;
    if (request !== undefined && request !== t.request) Object.assign(t, { request, trace: null, replay: false });
    t.loading = true;
    this.changed();
    try {
      // the hooks as they are now: their names, what each sees, and the order they run in
      await this.load();
      if (this.refused) throw this.refused;
      const hooks = this.list ?? [];
      const [got, one] = await Promise.all([Promise.all(hooks.map((h) => this.api.calls(h.id, 500))), t.request ? this.api.trace(t.request) : Promise.resolve(null)]);
      this.journals = Object.fromEntries(got.map((r) => [r.hook, r.calls]));
      t.recent = recentRequests(this.journals);
      // the open request from the controller's own trace, which also holds the calls of a hook gone since
      const mine: Record<string, HookCall[]> = {};
      for (const c of one?.calls ?? []) (mine[c.hook] ??= []).push(c);
      t.trace = t.request ? traceOf(t.request, mine, hooks, this.table!) : null;
      t.refused = null;
    } catch (e) {
      t.refused = asHookError(e);
    }
    t.loading = false;
    this.changed();
  }

  openReplay(on: boolean): void {
    this.trace.replay = on;
    this.changed();
  }
}

// ---- decision providers ----------------------------------------------------------------------------

export interface AddProvider {
  form: ProviderForm;
  errors: Record<string, string>;
  busy: boolean;
  refused: HookError | null;
}

export interface OneProvider {
  id: string;
  missing: boolean;
  refused: HookError | null;
  calls: ProviderCall[] | null;
  callsRefused: HookError | null;
  edit: { name: string; endpoint: string; model: string; timeout: string; hook_id: string; busy: boolean; errors: Record<string, string>; refused: HookError | null } | null;
  test: { question: string; text: string; busy: boolean; result: ProviderTest | null; refused: HookError | null };
  remove: RemoveState;
  notice: string | null;
  /** the keys whose field is open ('*' or a question id), and what is typed in each */
  keyOpen: string[];
  text: Record<string, string>;
  invalid: Record<string, string>;
  probing: boolean;
}

export class ProvidersFlow {
  info: ProviderList | null = null;
  refused: HookError | null = null;
  health: Record<string, Health> = {};
  report: Report | null = null;
  reportRefused: HookError | null = null;
  hours = 24;
  add: AddProvider | null = null;
  one: OneProvider | null = null;

  constructor(
    private api: ProvidersApi,
    private draft: Draft,
    private changed: () => void = () => undefined,
  ) {}

  get shown(): ShownProvider[] {
    return withStagedProviders(this.info?.providers ?? [], this.draft.part(HOOKS_SCOPE, HERE));
  }
  provider(id: string): ShownProvider | null {
    return this.shown.find((p) => p.id === id) ?? null;
  }
  private held(id: string): Provider | undefined {
    return this.info?.providers.find((p) => p.id === id);
  }
  private put(p: Provider): void {
    if (!this.info) return;
    this.info = { ...this.info, providers: this.info.providers.some((x) => x.id === p.id) ? this.info.providers.map((x) => (x.id === p.id ? p : x)) : [...this.info.providers, p] };
    if (p.health) this.health[p.id] = p.health;
  }

  /** The providers with their health from the last probes (SRV-851), then the report beside
   *  them; a provider whose endpoints were not probed yet is probed once. */
  async load(): Promise<void> {
    try {
      this.info = await this.api.list();
      this.refused = null;
    } catch (e) {
      this.refused = asHookError(e);
      this.changed();
      return;
    }
    for (const p of this.info.providers) if (p.health) this.health[p.id] = p.health;
    this.changed();
    await Promise.all([...this.info.providers.filter((p) => !p.health || p.health.status === 'unknown').map((p) => this.probe(p.id, false)), this.readReport()]);
  }

  /** One provider's health; `force`: probe its endpoints now. */
  async probe(id: string, force = true): Promise<void> {
    if (this.one?.id === id && force) this.one.probing = true;
    this.changed();
    try {
      this.health[id] = await this.api.health(id, force);
    } catch {
      delete this.health[id];
    }
    if (this.one?.id === id) this.one.probing = false;
    this.changed();
  }

  async readReport(hours = this.hours): Promise<void> {
    this.hours = hours;
    try {
      this.report = await this.api.report(hours);
      this.reportRefused = null;
    } catch (e) {
      this.reportRefused = asHookError(e);
    }
    this.changed();
  }

  // ---- the switches: keys of the settings interface -------------------------------------------------
  stage(id: string, field: ProviderSwitch, value: unknown): void {
    const p = this.held(id);
    if (p) stageProvider(this.draft, p, field, value);
  }

  /** The A/B share as typed: staged when it is 0 to 100. */
  share(id: string, raw: string): void {
    const o = this.one;
    const t = raw.trim();
    const ok = /^\d+$/.test(t) && Number(t) <= 100;
    if (o?.id === id) {
      o.text.ab_percent = raw;
      if (ok) delete o.invalid.ab_percent;
      else o.invalid.ab_percent = 'A share from 0 to 100.';
    }
    if (ok) this.stage(id, 'ab_percent', Number(t));
    this.changed();
  }

  /** The mapping a provider would have: what is staged, else what holds. */
  private mapping(id: string): Mapping {
    const p = this.provider(id);
    return p ? mappingOf(p.questions) : {};
  }

  /** Answer a question more, or one less. A provider answers one question at least. */
  question(id: string, q: string, on: boolean): void {
    const m = this.mapping(id);
    if (on) m[q] ??= {};
    else {
      if (Object.keys(m).length <= 1) return;
      delete m[q];
      // its own key goes with the question: a key staged for it means nothing any more
      this.draft.drop(HOOKS_SCOPE, HERE, apiKeyKey(id, q));
    }
    this.stage(id, 'questions', m);
  }

  /** One field of a question's mapping, as typed; empty hands it back to the provider's own. */
  map(id: string, q: string, field: 'model' | 'endpoint' | 'timeout_ms', raw: string): void {
    const o = this.one;
    const k = `${q}.${field}`;
    const t = raw.trim();
    let bad: string | null = null;
    if (t && field === 'endpoint' && !/^https?:\/\/[^\s/@]+(\/\S*)?$/.test(t)) bad = 'A URL that starts with http:// or https://.';
    if (t && field === 'timeout_ms' && (!/^\d+$/.test(t) || Number(t) < 1 || Number(t) > TIMEOUT_MAX_MS)) bad = `1 to ${TIMEOUT_MAX_MS} ms.`;
    if (t && field === 'model' && /\s/.test(t)) bad = 'A model id, or auto.';
    if (o?.id === id) {
      o.text[k] = raw;
      if (bad) o.invalid[k] = bad;
      else delete o.invalid[k];
    }
    if (!bad) {
      const m = this.mapping(id);
      if (m[q]) {
        if (!t) delete m[q][field];
        else if (field === 'timeout_ms') m[q].timeout_ms = Number(t);
        else m[q][field] = t;
        this.stage(id, 'questions', m);
      }
    }
    this.changed();
  }

  tidy(field: string): void {
    if (this.one && !this.one.invalid[field]) delete this.one.text[field];
    this.changed();
  }

  /** Open or close a key's field ('*': the provider's own). Closing takes a typed key back. */
  keyField(id: string, q: string, open: boolean): void {
    const o = this.one;
    const p = this.held(id);
    if (!o || !p) return;
    o.keyOpen = open ? [...new Set([...o.keyOpen, q])] : o.keyOpen.filter((x) => x !== q);
    if (!open) {
      delete o.text[`key.${q}`];
      this.draft.drop(HOOKS_SCOPE, HERE, apiKeyKey(id, q === '*' ? null : q));
    }
    this.changed();
  }

  /** A key as typed: staged, write-only. It stays in this page's memory until it is applied. */
  key(id: string, q: string, raw: string): void {
    const p = this.held(id);
    if (!p) return;
    if (this.one?.id === id) this.one.text[`key.${q}`] = raw;
    if (raw && !/\s/.test(raw)) stageKey(this.draft, p, q === '*' ? null : q, raw);
    else this.draft.drop(HOOKS_SCOPE, HERE, apiKeyKey(id, q === '*' ? null : q));
    this.changed();
  }

  /** Stage the removal of a stored key. */
  removeKey(id: string, q: string): void {
    const p = this.held(id);
    if (p) stageKey(this.draft, p, q === '*' ? null : q, null);
  }
  undoKey(id: string, q: string): void {
    if (this.one) {
      this.one.keyOpen = this.one.keyOpen.filter((x) => x !== q);
      delete this.one.text[`key.${q}`];
    }
    this.draft.drop(HOOKS_SCOPE, HERE, apiKeyKey(id, q === '*' ? null : q));
  }

  // ---- a new provider -------------------------------------------------------------------------------
  startAdd(): void {
    this.add = { form: newProviderForm(), errors: {}, busy: false, refused: null };
    this.changed();
  }
  editAdd(p: Partial<ProviderForm>): void {
    const a = this.add;
    if (!a) return;
    a.form = { ...a.form, ...p };
    if (Object.keys(a.errors).length) a.errors = checkProvider(a.form, this.info?.providers ?? []);
    a.refused = null;
    this.changed();
  }
  async create(): Promise<Provider | null> {
    const a = this.add;
    if (!a || a.busy) return null;
    a.errors = checkProvider(a.form, this.info?.providers ?? []);
    if (Object.keys(a.errors).length) {
      this.changed();
      return null;
    }
    a.busy = true;
    a.refused = null;
    this.changed();
    try {
      const p = await this.api.create(providerBody(a.form));
      this.put(p);
      this.add = null;
      return p;
    } catch (e) {
      const err = asHookError(e);
      a.refused = err;
      const f = fieldOf(err);
      if (f && err.type === 'invalid') a.errors = { ...a.errors, [f]: err.message };
      return null;
    } finally {
      if (this.add) this.add.busy = false;
      this.changed();
    }
  }
  closeAdd(): void {
    this.add = null;
    this.changed();
  }

  // ---- one provider ---------------------------------------------------------------------------------
  async open(id: string): Promise<void> {
    if (this.one?.id !== id) this.one = { id, missing: false, refused: null, calls: null, callsRefused: null, edit: null, test: { question: '', text: '', busy: false, result: null, refused: null }, remove: { step: 'idle', refused: null }, notice: null, keyOpen: [], text: {}, invalid: {}, probing: false };
    const o = this.one;
    this.changed();
    try {
      if (!this.info) this.info = await this.api.list();
      const p = await this.api.get(id);
      this.put(p);
      o.missing = false;
      o.refused = null;
      if (!(o.test.question in p.questions)) o.test.question = Object.keys(p.questions)[0] ?? '';
    } catch (e) {
      const err = asHookError(e);
      if (err.type === 'not_found') o.missing = true;
      else o.refused = err;
    }
    this.changed();
    if (o.missing) return;
    await Promise.all([this.probe(id, false), this.readCalls(), this.readReport()]);
  }

  async readCalls(): Promise<void> {
    const o = this.one;
    if (!o) return;
    try {
      o.calls = (await this.api.calls(o.id, 300)).calls;
      o.callsRefused = null;
    } catch (e) {
      o.callsRefused = asHookError(e);
    }
    this.changed();
  }

  startEdit(): void {
    const o = this.one;
    const p = o && this.held(o.id);
    if (!o || !p) return;
    o.edit = { name: p.name, endpoint: p.endpoint ?? '', model: p.model, timeout: String(p.timeout_ms), hook_id: p.hook_id ?? '', busy: false, errors: {}, refused: null };
    o.notice = null;
    this.changed();
  }
  editEdit(p: Partial<Record<'name' | 'endpoint' | 'model' | 'timeout' | 'hook_id', string>>): void {
    if (!this.one?.edit) return;
    Object.assign(this.one.edit, p, { refused: null });
    this.changed();
  }
  cancelEdit(): void {
    if (this.one) this.one.edit = null;
    this.changed();
  }

  /** Save what the providers' API holds of a provider: its name, endpoint, model, time limit or
   *  hook. 409 conflict: it is read again and the form starts over from what stands now. */
  async save(): Promise<boolean> {
    const o = this.one;
    const e = o?.edit;
    const p = o && this.held(o.id);
    if (!o || !e || !p || e.busy) return false;
    const errors: Record<string, string> = {};
    const patch: ProviderPatch = {};
    if (!e.name.trim() || e.name.trim().length > 80) errors.name = 'A name of 1 to 80 characters.';
    else if (e.name.trim() !== p.name) patch.name = e.name.trim();
    if (p.kind === 'systemone-http') {
      if (!/^https?:\/\/[^\s/@]+(\/\S*)?$/.test(e.endpoint.trim())) errors.endpoint = 'A URL that starts with http:// or https://.';
      else if (e.endpoint.trim() !== p.endpoint) patch.endpoint = e.endpoint.trim();
    }
    if (p.kind === 'hook') {
      if (!e.hook_id) errors.hook_id = 'Pick the hook that answers.';
      else if (e.hook_id !== p.hook_id) patch.hook_id = e.hook_id;
    }
    if (!e.model.trim() || /\s/.test(e.model.trim())) errors.model = 'A model id, or auto.';
    else if (e.model.trim() !== p.model) patch.model = e.model.trim();
    if (!/^\d+$/.test(e.timeout.trim()) || Number(e.timeout) < 1 || Number(e.timeout) > TIMEOUT_MAX_MS) errors.timeout_ms = `A whole number of milliseconds, 1 to ${TIMEOUT_MAX_MS}.`;
    else if (Number(e.timeout) !== p.timeout_ms) patch.timeout_ms = Number(e.timeout);
    e.errors = errors;
    if (Object.keys(errors).length) {
      this.changed();
      return false;
    }
    if (!Object.keys(patch).length) {
      o.edit = null;
      this.changed();
      return true;
    }
    e.busy = true;
    e.refused = null;
    this.changed();
    try {
      this.put(await this.api.patch(o.id, patch));
      o.edit = null;
      return true;
    } catch (x) {
      const err = asHookError(x);
      if (err.type === 'conflict') {
        try {
          this.put(await this.api.get(o.id));
        } catch {
          /* the read is tried again when the page is */
        }
        o.edit = null;
        this.startEdit();
        o.notice = 'This provider changed on another screen while you were editing it, so nothing was saved. The form shows it as it stands now.';
        return false;
      }
      e.refused = err;
      const f = fieldOf(err);
      if (f) e.errors = { [f]: err.message };
      return false;
    } finally {
      if (o.edit) o.edit.busy = false;
      this.changed();
      void this.probe(o.id, false);
    }
  }

  setTest(p: Partial<{ question: string; text: string }>): void {
    if (this.one) this.one.test = { ...this.one.test, ...p };
    this.changed();
  }

  /** One live call with a sample question: nothing counted, nothing journaled. */
  async test(): Promise<void> {
    const o = this.one;
    if (!o || o.test.busy) return;
    o.test = { ...o.test, busy: true, refused: null };
    this.changed();
    try {
      o.test.result = await this.api.test(o.id, o.test.question || undefined, o.test.text.trim() || undefined);
    } catch (e) {
      o.test.result = null;
      o.test.refused = asHookError(e);
    }
    o.test.busy = false;
    this.changed();
  }

  askRemove(on: boolean): void {
    if (this.one) this.one.remove = { step: on ? 'ask' : 'idle', refused: null };
    this.changed();
  }
  async remove(): Promise<boolean> {
    const o = this.one;
    if (!o || o.remove.step === 'busy') return false;
    o.remove = { step: 'busy', refused: null };
    this.changed();
    try {
      await this.api.remove(o.id);
    } catch (e) {
      const err = asHookError(e);
      if (err.type !== 'not_found') {
        o.remove = { step: 'ask', refused: err };
        this.changed();
        return false;
      }
    }
    dropStaged(this.draft, `providers.${o.id}.`);
    if (this.info) this.info = { ...this.info, providers: this.info.providers.filter((p) => p.id !== o.id) };
    delete this.health[o.id];
    this.one = null;
    this.changed();
    return true;
  }
}

