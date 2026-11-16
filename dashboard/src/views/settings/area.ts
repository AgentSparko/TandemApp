// <qse-settings>: the settings area (VIS-280, SRV-845), Settings → App and the pages beside it:
// App (what this TandemApp runs: the Chat and conductor switches, what is connected, then the
// controller's own page: its host, its service, the admin password, the permissions, the
// configuration file), Backends, Conductor, Engines (one page an engine host), Hooks and History.
// A chunk of its own, loaded when the area opens.
//
// Every page but History is a scope of the settings interface, drawn from its schema
// (lib/settings-form.ts): nothing here knows a key by name but the engine's mode. A change is
// staged, not written: the bar at the bottom carries the staged changes across the pages, and
// Review opens the run sheet (sheet.ts): the plan of the whole change set first (what changes, what
// restarts, when), then its confirm (a second one for a key that feeds a disk cache's
// fingerprint), then how each part ended, with Restore on what was applied. An engine restart shows
// on every page of the area while it is under way: its state, the countdown while it waits for
// idle, and Cancel while that is still possible. Each scope keeps its versions (history.ts); a
// restore is staged and planned like any other change. The dock at the bottom keeps a running
// restart's countdown and Cancel in reach beside the staged changes. History lists the audit rows, one change
// set's alone when it is named in the route (#/app/history?change_set=cs-…).

import { html, nothing, type PropertyValues, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { settings as api, type AuditRow, type Overview, type PartChange, type Result, type SchemaView, type Scope, type SettingsError, type Target, type ValuesView, type VersionsView } from '../../api/settings';
import { SETTINGS_TABS, settingsHref, type SettingsTab } from '../../lib/menu';
import { parseHash, setParams } from '../../lib/router';
import { draft, refusedField, same } from '../../lib/settings-draft';
import { ChangeSetFlow, RestartWatch, asError } from '../../lib/settings-flow';
import { asText, fieldId, fieldsOf, groupsOf, parseInput, type Field } from '../../lib/settings-form';
import { placeOf } from '../../lib/settings-words';
import { LightElement } from '../../ui/base';
import { icon } from '../../ui/bits';
import { signInCard } from '../monitor/bits';
import { checkSession } from '../monitor/data';
import '../fleet/app-parts';
import '../fleet/app-settings';
import { groupPanel, lock, type FormHandlers, type FormView } from './form';
import { auditList, versionsPanel, type VersionsState } from './history';
import { refusal, restartCard, restartLine, runSheet, type SheetHandlers } from './sheet';
import '../../styles/settings.css';

interface ScopeData {
  schema: SchemaView | null;
  values: ValuesView | null;
  error: SettingsError | null;
}
interface VersionData {
  view: VersionsView | null;
  error: SettingsError | null;
  compare: string | null;
}

const HERE = 'controller';
const HEAD: Record<SettingsTab, string> = {
  app: 'What this TandemApp runs, where its controller runs, who may change the fleet, and its configuration.',
  backends: 'The servers behind the one API: each backend’s address, limits and key, the routing rules, and which users’ keys are on.',
  conductor: 'The conductor’s documents as this controller keeps them, each edited whole and versioned.',
  engines: 'Each engine host’s own settings. Some apply at once; the others restart the engine once, when it is idle.',
  hooks: 'The settings of the hooks that run on requests.',
  history: 'Every change made through these settings, and the engine restarts they caused.',
};
const dkey = (scope: Scope, target: string): string => `${scope}|${target}`;

@customElement('qse-settings')
export class QseSettings extends LightElement {
  /** the route below #/app: the page, then an engine host's id */
  @property({ attribute: false }) path: string[] = [];

  private overview: Overview | null = null;
  private overviewErr: SettingsError | null = null;
  private data = new Map<string, ScopeData>();
  private vers = new Map<string, VersionData>();
  private audit: AuditRow[] | null = null;
  private auditErr: SettingsError | null = null;
  private filter = '';
  private find = '';
  private signIn = false;
  private form: FormView = { raw: new Map(), invalid: new Map(), secrets: new Set(), open: new Set(), backends: [] };
  /** the sheet shows the draft (not a part's restore): an apply that went through empties it */
  private fromDraft = false;
  private seen: Result | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private secs = 0;
  private unsub: (() => void)[] = [];
  private flow = new ChangeSetFlow(api, () => this.onFlow());
  private watch = new RestartWatch(api, () => this.requestUpdate(), () => Date.now(), (r) => void this.reload('engine', r.host));

  private get tab(): SettingsTab {
    return SETTINGS_TABS.find((t) => t.id === this.path[0])?.id ?? 'app';
  }
  private get targets(): Target[] {
    return this.overview?.scopes.find((s) => s.scope === 'engine')?.targets ?? [];
  }
  /** the engine host the Engines page shows: the route's, else the first */
  private get engine(): Target | null {
    return this.targets.find((t) => t.id === this.path[1]) ?? (this.path[1] ? null : (this.targets[0] ?? null));
  }
  private get dialog(): HTMLDialogElement | null {
    return this.querySelector('dialog[data-dialog="review"]');
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.unsub.push(draft.on(() => this.requestUpdate()));
    window.addEventListener('qse-signed-in', this.onSignedIn);
    void this.loadOverview();
    void this.watch.read();
    this.timer = setInterval(() => this.tick(), 1000);
  }
  disconnectedCallback(): void {
    for (const u of this.unsub) u();
    this.unsub = [];
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    if (this.timer) clearInterval(this.timer);
    super.disconnectedCallback();
  }
  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('path')) this.enter();
  }
  protected updated(): void {
    const d = this.dialog;
    if (!d) return;
    const open = this.flow.state.phase !== 'closed';
    if (open && !d.open) {
      d.showModal();
      d.querySelector<HTMLElement>('#rs-title')?.focus();
    } else if (!open && d.open) d.close();
  }

  private onSignedIn = (): void => {
    this.signIn = false;
    if (this.flow.state.phase === 'plan') void this.flow.again();
    else this.requestUpdate();
  };

  /** every second: the countdown moves; every other one, while a restart is under way, it is read */
  private tick(): void {
    if (document.visibilityState !== 'visible' || this.watch.none) return;
    this.secs++;
    const busy = this.watch.active.length > 0;
    if (busy) this.requestUpdate();
    if (busy ? this.secs % 2 === 0 : this.secs % 15 === 0) void this.watch.read();
  }

  // ---- reading --------------------------------------------------------------------------------------
  private async loadOverview(): Promise<void> {
    try {
      this.overview = await api.overview();
      this.overviewErr = null;
    } catch (e) {
      this.overviewErr = asError(e);
    }
    this.enter();
    this.requestUpdate();
  }

  /** The page opened: read what it shows and does not hold yet. */
  private enter(): void {
    const tab = this.tab;
    if (tab === 'history') {
      this.filter = parseHash().params.get('change_set') ?? '';
      void this.loadAudit();
      return;
    }
    const scope = SETTINGS_TABS.find((t) => t.id === tab)!.scope!;
    const target = scope === 'engine' ? this.engine?.id : HERE;
    if (target && !this.data.has(dkey(scope, target))) void this.reload(scope, target);
  }

  private async reload(scope: Scope, target: string): Promise<void> {
    const k = dkey(scope, target);
    const was = this.data.get(k);
    const next: ScopeData = { schema: was?.schema ?? null, values: was?.values ?? null, error: null };
    this.data.set(k, next);
    this.requestUpdate();
    try {
      const [schema, values] = await Promise.all([api.schema(scope, target), api.values(scope, target)]);
      this.data.set(k, { schema, values, error: null });
      if (scope === 'backends') {
        const ids = [...new Set(schema.keys.filter((x) => x.key.startsWith('backends.')).map((x) => x.key.split('.')[1]))];
        // a routing rule names one of these; the first backend's fold starts open
        this.form.backends = ids;
        if (!was?.schema && ids.length) this.form.open.add(`backend:${ids[0]}`);
      }
    } catch (e) {
      this.data.set(k, { ...next, error: asError(e) });
    }
    if (this.vers.get(k)?.view) void this.loadVersions(scope, target);
    this.requestUpdate();
  }

  private async loadVersions(scope: Scope, target: string): Promise<void> {
    const k = dkey(scope, target);
    const was = this.vers.get(k) ?? { view: null, error: null, compare: null };
    try {
      this.vers.set(k, { ...was, view: await api.versions(scope, target), error: null });
    } catch (e) {
      this.vers.set(k, { ...was, error: asError(e) });
    }
    this.requestUpdate();
  }

  private async loadAudit(): Promise<void> {
    try {
      this.audit = (await api.audit({ change_set: this.filter || null, limit: 200 })).rows;
      this.auditErr = null;
    } catch (e) {
      this.audit = null;
      this.auditErr = asError(e);
    }
    this.requestUpdate();
  }

  /** After a change: everything this page holds is read again. */
  private refresh(): void {
    void this.loadOverview();
    for (const k of this.data.keys()) {
      const [scope, target] = k.split('|') as [Scope, string];
      void this.reload(scope, target);
    }
    if (this.tab === 'history') void this.loadAudit();
  }

  // ---- the draft ------------------------------------------------------------------------------------
  private handlers: FormHandlers = {
    edit: (f, raw) => {
      this.form.raw.set(f.id, raw);
      if (f.control === 'secret') {
        this.form.invalid.delete(f.id);
        if (raw) draft.set(f.scope, f.target, f.key, raw, null, true);
        else draft.drop(f.scope, f.target, f.key);
        return;
      }
      const p = parseInput(f.spec, f.control, raw, asText(f));
      if (!p.ok) {
        this.form.invalid.set(f.id, p.error);
        this.requestUpdate();
        return;
      }
      this.form.invalid.delete(f.id);
      if ('unset' in p) {
        // a key that is unset already: nothing to stage (the controller would drop it too)
        if (f.held?.set && f.held.value !== null) draft.unset(f.scope, f.target, f.key, f.held);
        else draft.drop(f.scope, f.target, f.key);
      } else this.stage(f, p.value);
    },
    blur: (f) => {
      if (f.control === 'secret' || this.form.invalid.has(f.id)) return;
      this.form.raw.delete(f.id);
      this.requestUpdate();
    },
    set: (f, value) => {
      this.form.invalid.delete(f.id);
      this.stage(f, value);
    },
    unset: (f) => draft.unset(f.scope, f.target, f.key, f.held),
    undo: (f) => {
      this.form.raw.delete(f.id);
      this.form.invalid.delete(f.id);
      this.form.secrets.delete(f.id);
      draft.drop(f.scope, f.target, f.key);
      this.requestUpdate();
    },
    secret: (f, open) => {
      if (open) this.form.secrets.add(f.id);
      else {
        this.form.secrets.delete(f.id);
        this.form.raw.delete(f.id);
        if (draft.staged(f.scope, f.target, f.key)?.kind === 'set') draft.drop(f.scope, f.target, f.key);
      }
      this.requestUpdate();
      if (open) void this.updateComplete.then(() => this.querySelector<HTMLElement>(`[id="${f.id}"]`)?.focus());
    },
    toggle: (g, open) => {
      if (open) this.form.open.add(g);
      else this.form.open.delete(g);
      this.requestUpdate();
    },
  };

  /** a value that is what holds now is no change, wherever it comes from */
  private stage(f: Field, value: unknown): void {
    if (f.held && same(f.held.value, value)) draft.drop(f.scope, f.target, f.key);
    else draft.set(f.scope, f.target, f.key, value, f.held);
  }

  private discard(): void {
    this.form.raw.clear();
    this.form.invalid.clear();
    this.form.secrets.clear();
    draft.clear();
  }

  private review(): void {
    if (this.form.invalid.size || draft.empty) return;
    this.fromDraft = true;
    void this.flow.review(draft.changes());
  }

  private onFlow(): void {
    const s = this.flow.state;
    if (s.error?.status === 401 && !this.signIn) {
      this.signIn = true;
      void checkSession(true);
    }
    // a plan refused for one key: the sentence stands under that field too, in a group that is open;
    // the refusal names the part (its scope and target), so the same key on another engine stays clean
    const hit = s.phase === 'plan' && !s.plan && this.fromDraft && s.error ? refusedField(draft.parts, s.error.facts) : null;
    if (hit) {
      const id = fieldId(hit.scope, hit.target, hit.key);
      this.form.invalid.set(id, s.error!.message);
      this.form.open.add(hit.scope === 'backends' && hit.key.startsWith('backends.') ? `backend:${hit.key.split('.')[1]}` : id);
    }
    if (s.phase === 'result' && s.result && s.result !== this.seen) {
      this.seen = s.result;
      for (const p of s.result.parts) if (p.restart) this.watch.add(p.restart);
      // what went through is no longer a draft; after a failure it stays, to be planned again
      if (this.fromDraft && (s.result.state === 'applied' || s.result.state === 'unchanged')) this.discard();
      this.refresh();
    }
    this.requestUpdate();
  }

  private sheet: SheetHandlers = {
    apply: () => void this.flow.apply(),
    back: () => this.flow.back(),
    close: () => this.flow.close(),
    again: () => void this.flow.again(),
    when: (p) => this.flow.setWhen(p),
    note: (t) => this.flow.setNote(t),
    restore: (changes: PartChange[]) => {
      this.fromDraft = false;
      void this.flow.review(changes);
    },
    cancelRestart: (id) => void this.cancel(id),
  };

  private async cancel(id: string): Promise<void> {
    await this.watch.cancel(id);
    if (this.watch.refused?.error.status === 401) {
      this.signIn = true;
      void checkSession(true);
    }
    this.requestUpdate();
  }

  // ---- the pages ------------------------------------------------------------------------------------
  private versions(scope: Scope, target: string, d: ScopeData): TemplateResult {
    const k = dkey(scope, target);
    const v = this.vers.get(k) ?? { view: null, error: null, compare: null };
    const s: VersionsState = {
      scope, target, view: v.view, error: v.error, compare: v.compare, values: d.values?.values ?? [],
      secrets: new Set((d.schema?.keys ?? []).filter((x) => x.secret).map((x) => x.key)),
      staged: draft.part(scope, target)?.restore ?? null, writable: !!d.schema?.writable,
    };
    return versionsPanel(s, {
      open: () => void this.loadVersions(scope, target),
      compare: (id) => {
        this.vers.set(k, { ...v, compare: id });
        this.requestUpdate();
      },
      restore: (id) => {
        // a restore stands for its part alone: what was typed for it goes
        for (const m of [this.form.raw, this.form.invalid]) for (const f of [...m.keys()]) if (f.startsWith(`${k}|`)) m.delete(f);
        draft.restore(scope, target, id);
      },
      undo: () => draft.dropPart(scope, target),
    });
  }

  private unread(scope: Scope, target: string, d: ScopeData | undefined): TemplateResult | null {
    if (d?.schema) return null;
    if (d?.error)
      return html`<section class="panel" data-unread=${scope}>${refusal(d.error)}<div class="st-acts"><button type="button" class="btn btn-sm" data-act="reload" @click=${() => void this.reload(scope, target)}>${icon('refresh')} Try again</button></div></section>`;
    return html`<div class="skeleton" style="height:220px" aria-busy="true"></div>`;
  }

  private scopePage(scope: Scope, target: string, o: { fold?: (id: string) => boolean; before?: TemplateResult | typeof nothing; empty?: TemplateResult } = {}): TemplateResult {
    const d = this.data.get(dkey(scope, target));
    const un = this.unread(scope, target, d);
    if (un) return un;
    const fields = fieldsOf(d!.schema!, d!.values, draft.part(scope, target));
    const q = this.find.trim().toLowerCase();
    const shown = scope === 'engine' && q ? fields.filter((f) => f.key.toLowerCase().includes(q) || f.label.toLowerCase().includes(q)) : fields;
    const groups = groupsOf(scope, shown);
    return html`${d!.error ? refusal(d!.error, { quiet: true }) : nothing}
      ${d!.schema!.why && fields.length ? html`<p class="note small st-why" data-scope-why>${d!.schema!.writable ? icon('info') : lock}<span>${d!.schema!.why.charAt(0).toUpperCase()}${d!.schema!.why.slice(1)}.</span></p>` : nothing}
      ${o.before ?? nothing}
      ${scope === 'engine' && fields.length > 8 ? html`<label class="field field-search st-find">${icon('search')}<input type="search" data-in="find" placeholder="Find a setting" aria-label="Find a setting" .value=${this.find} @input=${(e: Event) => ((this.find = (e.target as HTMLInputElement).value), this.requestUpdate())} /></label>` : nothing}
      ${fields.length ? groups.map((g) => groupPanel(g, this.form, this.handlers, { fold: o.fold?.(g.id) })) : (o.empty ?? nothing)}
      ${fields.length && !groups.length ? html`<p class="small muted" data-find-none>No setting matches “${this.find}”.</p>` : nothing}
      ${fields.length && d!.schema!.writable ? this.versions(scope, target, d!) : nothing}`;
  }

  private appPage(): TemplateResult {
    const d = this.data.get(dkey('app', HERE));
    const fields = d?.schema ? fieldsOf(d.schema, d.values, draft.part('app', HERE)) : null;
    return html`<qse-app-parts .fields=${fields} .why=${d?.schema && !d.schema.writable ? d.schema.why : null} .refused=${d?.error ?? null}></qse-app-parts>
      ${d?.schema?.writable ? this.versions('app', HERE, d) : nothing}
      <qse-app-settings></qse-app-settings>`;
  }

  private enginesPage(): TemplateResult {
    const why = this.overview?.scopes.find((s) => s.scope === 'engine')?.why;
    if (!this.overview) return this.overviewErr ? html`<section class="panel">${refusal(this.overviewErr)}</section>` : html`<div class="skeleton" style="height:160px" aria-busy="true"></div>`;
    if (!this.targets.length)
      return html`<section class="panel st-empty" data-engines-none>
        <h2 class="panel-title">No engine host yet</h2>
        <p class="small muted">${why ?? 'An engine host’s settings show here once it is paired: what applies at once, what needs a restart, and its versions.'}</p>
        <div class="st-acts"><a class="btn btn-primary" href="#/add-engine">Add engine</a></div>
      </section>`;
    const t = this.engine;
    const active = t ? this.watch.active.filter((r) => r.host === t.id) : [];
    return html`<nav class="st-hosts" aria-label="Engine hosts">
        ${this.targets.map((x) => {
          const n = draft.count('engine', x.id);
          return html`<a class="st-host ${x.id === t?.id ? 'is-on' : ''}" href=${settingsHref('engines', x.id)} aria-current=${x.id === t?.id ? 'page' : nothing} data-target=${x.id} data-writable=${x.writable}>
            <b>${x.label}</b><span class="xs muted">${x.transport === 'http' ? html`${lock} HTTP only, read-only` : 'paired over SSH'}</span>${n ? html`<span class="st-count">${n} staged</span>` : nothing}
          </a>`;
        })}
      </nav>
      ${!t
        ? html`<section class="panel">${refusal({ type: 'no_target', message: `no engine host ${this.path[1]}`, facts: {} } as SettingsError, { quiet: true })}</section>`
        : this.scopePage('engine', t.id, {
            fold: (id) => id === 'host',
            // the restart that is under way stands on the area's head already; here: nothing staged can apply meanwhile
            before: active.length ? html`<p class="note small" data-host-busy>${icon('info')} A restart of ${t.label} is under way. A change staged now is planned against the settings it runs until then.</p>` : nothing,
            empty: html`<section class="panel st-empty"><p class="small muted">This engine answers no setting over HTTP.</p></section>`,
          })}`;
  }

  private hooksEmpty(why: string | null): TemplateResult {
    return html`<section class="panel st-empty" data-hooks-none>
      <h2 class="panel-title">No hooks yet</h2>
      <p class="small">${why ? `${why.charAt(0).toUpperCase()}${why.slice(1)}.` : 'No hook is registered on this TandemApp.'}</p>
      <p class="small muted">A hook brings its own settings when it registers: whether it only watches or acts, its place in the order, what happens when it fails, a plugin’s own options. They show on this page then, with the same plan, versions and history as every other one. There is nothing to set up before that.</p>
      <p class="small muted">Until then the pieces that shape a request are set under <a href=${settingsHref('conductor')}>Conductor</a> and <a href=${settingsHref('backends')}>Backends</a>.</p>
    </section>`;
  }

  private page(): TemplateResult {
    switch (this.tab) {
      case 'app':
        return this.appPage();
      case 'backends':
        return this.scopePage('backends', HERE, {
          fold: (id) => id.startsWith('backend:'),
          before: html`<p class="small muted st-lead">A backend or a user’s key is added and removed on the <a href="#/api/backends">Backends page</a>, which hands a new key out once. Here: what each one is set to.</p>`,
        });
      case 'conductor':
        return this.scopePage('conductor', HERE, {
          before: html`<p class="small muted st-lead">The <a href="#/conductor">Conductor pages</a> edit routing, profiles, packages, the monitor and the deciders piece by piece. A change made there is a version here too.${this.data.get(dkey('conductor', HERE))?.schema?.why ? html` The conductor itself is switched on under <a href=${settingsHref('app')}>App</a>.` : nothing}</p>`,
        });
      case 'engines':
        return this.enginesPage();
      case 'hooks': {
        const d = this.data.get(dkey('hooks', HERE));
        return this.scopePage('hooks', HERE, { empty: this.hooksEmpty(d?.schema?.why ?? null) });
      }
      default:
        return this.historyPage();
    }
  }

  private historyPage(): TemplateResult {
    const ended = this.watch.list.filter((r) => !this.watch.active.includes(r));
    return html`${ended.length
        ? html`<section class="panel" data-panel="restarts"><header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Engine restarts</h2><p class="panel-sub">The restarts settings changes caused, newest first. The controller keeps the last fifty while it runs.</p></div></header>
            <div class="st-restarts">${ended.map((r) => restartCard(r, this.watch, null))}</div></section>`
        : nothing}
      ${auditList(this.audit, this.auditErr, this.overview, this.filter, {
        filter: (cs) => {
          this.filter = cs;
          this.audit = null;
          setParams({ change_set: cs || null });
          void this.loadAudit();
        },
      })}`;
  }

  /** What stays in reach at the bottom of every page of the area: a restart under way (its
   *  countdown and Cancel, wherever the page is scrolled to) and the staged changes. */
  private dock(): TemplateResult | typeof nothing {
    const live = this.flow.state.phase === 'closed' ? this.watch.active : [];
    const bar = this.bar();
    if (!live.length && bar === nothing) return nothing;
    const show = () => this.querySelector('[data-restarts-active]')?.scrollIntoView({ block: 'center' });
    return html`<div class="st-dock" data-dock>${live.map((r) => restartLine(r, this.watch, this.sheet.cancelRestart, show))}${bar}</div>`;
  }

  private bar(): TemplateResult | typeof nothing {
    const n = draft.count();
    if (!n) return nothing;
    const bad = this.form.invalid.size;
    return html`<div class="st-bar" role="region" aria-label="Staged changes" data-bar>
      <div class="st-bar-l">
        <b data-bar-count>${n} staged ${n === 1 ? 'change' : 'changes'}</b>
        <span class="small muted">${draft.parts.map((p) => `${placeOf(p, this.overview)} ${p.restore !== null ? '(restore)' : Object.keys(p.set).length + p.unset.length}`).join(', ')}</span>
        ${bad ? html`<span class="small err-ink" role="status" data-bar-invalid>${bad === 1 ? 'One field needs' : `${bad} fields need`} a correction first.</span>` : html`<span class="small muted">Nothing is written until you review and apply.</span>`}
      </div>
      <div class="st-bar-a">
        <button type="button" class="btn btn-ghost" data-act="discard" @click=${() => this.discard()}>Discard</button>
        <button type="button" class="btn btn-primary" data-act="review" ?disabled=${bad > 0} @click=${() => this.review()}>Review ${n === 1 ? 'the change' : `${n} changes`}</button>
      </div>
    </div>`;
  }

  render() {
    const tab = this.tab;
    const t = SETTINGS_TABS.find((x) => x.id === tab)!;
    const s = this.flow.state;
    const gone = this.overviewErr?.status === 404;
    return html`<header class="page-head" data-page="app" data-settings-tab=${tab}><div class="eyebrow">Settings</div><h1>${t.label}</h1><p class="page-sub">${HEAD[tab]}</p></header>
      <nav class="st-tabs" aria-label="Settings pages">
        ${SETTINGS_TABS.map((x) => {
          const n = x.scope ? draft.count(x.scope) : 0;
          return html`<a class="st-tab ${x.id === tab ? 'is-on' : ''}" href=${settingsHref(x.id)} aria-current=${x.id === tab ? 'page' : nothing} data-tab=${x.id}>${x.label}${n ? html`<span class="st-dot" title=${`${n} staged`}><span class="sr-only">, ${n} staged</span></span>` : nothing}</a>`;
        })}
      </nav>
      ${this.watch.active.length ? html`<div class="st-restarts" data-restarts-active>${this.watch.active.map((r) => restartCard(r, this.watch, this.sheet.cancelRestart))}</div>` : nothing}
      ${this.signIn && s.phase === 'closed' ? signInCard('Changes to the settings') : nothing}
      ${gone && tab !== 'app'
        ? html`<section class="panel st-empty" data-settings-none><h2 class="panel-title">This controller has no settings pages yet</h2><p class="small muted">It answers none of the settings interface. Update TandemApp on the controller’s machine; the App page works as before.</p></section>`
        : this.overviewErr && !gone && tab !== 'app' && tab !== 'history' && !this.overview
          ? html`<section class="panel">${refusal(this.overviewErr)}<div class="st-acts"><button type="button" class="btn btn-sm" @click=${() => void this.loadOverview()}>${icon('refresh')} Try again</button></div></section>`
          : this.page()}
      ${this.dock()}
      <dialog class="act-dialog rs-dialog" aria-labelledby="rs-title" data-dialog="review" data-phase=${s.phase} @close=${() => this.flow.close()} @cancel=${(e: Event) => s.phase === 'applying' && e.preventDefault()}>
        <div class="act-body">${runSheet(s, this.overview, this.watch, this.flow.blocked(), this.sheet, this.signIn ? signInCard('Changes to the settings') : nothing)}</div>
      </dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-settings': QseSettings;
  }
}
