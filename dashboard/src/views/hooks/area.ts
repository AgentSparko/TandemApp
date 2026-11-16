// <qse-hooks>: the Hooks pages of the settings area (VIS-282), a chunk of its own that loads when
// Settings → Hooks opens. Below #/app/hooks:
//
//   (nothing)            the pipeline: a request's path with the hooks at each stop (pipeline.ts)
//   new[?stage=]         add a hook (add.ts)
//   hook/<id>[?test=]    one hook: status, settings, secret, playground, calls (hook.ts)
//   trace[?request=]     one request across every stop, from the journals (trace.ts)
//   providers            the decision providers: who answers what, health, the report (providers.ts)
//   providers/new        add a provider
//   providers/p/<id>     one provider
//
// The state is two flows free of the DOM (lib/hooks-flow.ts); this element reads the route, binds
// the templates' handlers to them and keeps what is read fresh. Every switch is staged into the
// settings area's own draft, so its bar, its review sheet and its versions serve these pages too.
// What is written on the hooks' API itself (a hook made, moved or removed, a secret rotated) is
// told to the settings area, which reads its scope again.

import { html, nothing, type PropertyValues, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { hooks as hooksApi, providers as providersApi, type HookError } from '../../api/hooks';
import { HooksFlow, ProvidersFlow } from '../../lib/hooks-flow';
import { HERE, HOOKS_SCOPE, capUse, hookKey, pipeline } from '../../lib/hooks';
import { providerKey } from '../../lib/providers';
import { parseHash } from '../../lib/router';
import { draft } from '../../lib/settings-draft';
import { LightElement } from '../../ui/base';
import { icon } from '../../ui/bits';
import { signInCard } from '../monitor/bits';
import { checkSession } from '../monitor/data';
import { addView } from './add';
import { hooksHref, hooksNav, providersHref, refused } from './bits';
import { hookGone, hookView } from './hook';
import { pipelineView, type Drag } from './pipeline';
import { addProviderView, providerGone, providerView, providersView } from './providers';
import { traceView } from './trace';
import '../../styles/hooks.css';

/** how often what is read is read again while its page shows, in seconds */
const FRESH_S = 6;

@customElement('qse-hooks')
export class QseHooks extends LightElement {
  /** the route below #/app/hooks */
  @property({ attribute: false }) path: string[] = [];
  /** the settings interface takes changes of the hooks scope here */
  @property({ type: Boolean }) writable = false;
  /** what the settings area last read of the hooks scope: when it changes, a change was applied */
  @property({ attribute: false }) stamp: unknown = null;

  private flow = new HooksFlow(hooksApi, draft, () => this.requestUpdate());
  private prov = new ProvidersFlow(providersApi, draft, () => this.requestUpdate());
  private drag: Drag | null = null;
  /** the language the signature check is shown in */
  private code: 'python' | 'node' = 'python';
  private asked = false;
  private at = '';
  private secs = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private unsub: (() => void) | null = null;
  /** the playground is scrolled to once, when a link asks for it */
  private toPlayground = false;

  connectedCallback(): void {
    super.connectedCallback();
    this.unsub = draft.on(() => this.requestUpdate());
    window.addEventListener('qse-signed-in', this.onSignedIn);
    this.timer = setInterval(() => this.tick(), 1000);
  }
  disconnectedCallback(): void {
    this.unsub?.();
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    if (this.timer) clearInterval(this.timer);
    // a secret that was shown leaves with the page
    this.flow.closeAdd();
    this.flow.secret({ step: 'idle' });
    super.disconnectedCallback();
  }
  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('path')) this.enter();
    else if (changed.has('stamp') && changed.get('stamp') != null) void this.fresh(true);
  }
  protected updated(): void {
    if (!this.toPlayground || !this.flow.one || this.flow.list === null) return;
    const el = this.querySelector('#hk-playground');
    if (!el) return;
    this.toPlayground = false;
    el.scrollIntoView({ block: 'start' });
  }

  private onSignedIn = (): void => {
    this.asked = false;
    this.requestUpdate();
  };

  /** every few seconds, while the tab shows: the figures, breakers, journals and probes again */
  private tick(): void {
    if (document.visibilityState !== 'visible' || ++this.secs % FRESH_S) return;
    void this.fresh(false);
  }

  private async fresh(all: boolean): Promise<void> {
    const p = this.path;
    if (p[0] === 'providers') {
      if (p[1] === 'new') return;
      if (p[1] === 'p' && p[2]) return all ? this.prov.open(p[2]) : void (await Promise.all([this.prov.probe(p[2], false), this.prov.readCalls()]));
      return this.prov.load();
    }
    if (p[0] === 'new' || this.flow.one?.edit) return;
    if (p[0] === 'trace') return all ? this.flow.readTrace() : undefined;
    await this.flow.load();
    if (p[0] === 'hook' && p[1] && this.flow.one && !this.flow.one.missing) await this.flow.readCalls();
  }

  /** The route changed: read what its page shows. */
  private enter(): void {
    const p = this.path;
    const q = parseHash().params;
    const key = `${p.join('/')}?${q.toString()}`;
    if (key === this.at) return;
    const was = this.at;
    this.at = key;
    // a secret shown on the page that is left goes with it
    if (was.startsWith('new') && p[0] !== 'new' && this.flow.add?.created) this.flow.closeAdd();
    if (p[0] === 'providers') {
      if (p[1] === 'new') {
        if (!this.prov.add) this.prov.startAdd();
        if (!this.prov.info) void this.prov.load();
        if (!this.flow.list) void this.flow.load();
      } else if (p[1] === 'p' && p[2]) {
        void this.prov.open(p[2]);
        if (!this.flow.list) void this.flow.load();
      } else void this.prov.load();
      return;
    }
    if (p[0] === 'new') {
      const stage = q.get('stage') ?? '';
      void (this.flow.table ? Promise.resolve() : this.flow.load()).then(() => {
        if (!this.flow.add || this.flow.add.created) this.flow.startAdd(stage);
        else if (stage && stage !== this.flow.add.form.stage) this.flow.editAdd({ stage });
      });
    } else if (p[0] === 'hook' && p[1]) {
      const test = q.get('test') ?? undefined;
      this.toPlayground = !!test;
      void (this.flow.table ? Promise.resolve() : this.flow.load()).then(() => this.flow.open(p[1], test));
    } else if (p[0] === 'trace') void this.flow.readTrace(q.get('request') ?? '');
    else void this.flow.load();
  }

  /** the settings area reads its hooks scope again: a hook or a provider was added, moved or removed */
  private told(): void {
    this.dispatchEvent(new CustomEvent('qse-hooks-changed', { bubbles: true }));
  }

  /** the sign-in, where a refusal asks for it */
  private signIn(e: HookError | null | undefined, what: string): TemplateResult | typeof nothing {
    if (e?.type !== 'unauthorized') return nothing;
    if (!this.asked) {
      this.asked = true;
      void checkSession(true);
    }
    return signInCard(what);
  }

  private unreadable(e: HookError | null, retry: () => void): TemplateResult {
    return e
      ? html`<section class="panel" data-hooks-unread>${refused(e)}${this.signIn(e, 'The hooks')}<div class="st-acts"><button type="button" class="btn btn-sm" data-act="reload" @click=${retry}>${icon('refresh')} Try again</button></div></section>`
      : html`<div class="skeleton" style="height:240px" aria-busy="true"></div>`;
  }

  // ---- the hooks ------------------------------------------------------------------------------------
  private pipelinePage(): TemplateResult {
    const f = this.flow;
    const nav = hooksNav('pipeline', { hooks: f.list?.length });
    if (!f.table || !f.list) return html`${nav}${this.unreadable(f.refused, () => void f.load())}`;
    const shown = f.shown;
    return html`${nav}
      ${f.refused ? refused(f.refused, { quiet: true }) : nothing}
      ${pipelineView({ table: f.table, stops: pipeline(f.table, shown), cap: capUse(f.table, shown), writable: this.writable, drag: this.drag }, {
        mode: (id, mode) => f.setMode(id, mode),
        move: (stage, role, from, to) => void f.move(stage, role, from, to),
        drag: (d) => {
          this.drag = d;
          this.requestUpdate();
        },
      })}`;
  }

  private addPage(): TemplateResult {
    const f = this.flow;
    if (!f.table || !f.add) return this.unreadable(f.refused, () => void f.load().then(() => f.startAdd(parseHash().params.get('stage') ?? '')));
    const a = f.add;
    return addView(
      { state: a, table: f.table, builtins: f.builtins, here: (f.list ?? []).filter((h) => h.stages.includes(a.form.stage)).length, lang: this.code, signIn: this.signIn(a.refused, 'A hook with the full payload') },
      {
        edit: (p) => f.editAdd(p),
        pick: () => f.pickStage(),
        create: (confirmed) =>
          void f.create(confirmed).then((h) => {
            if (h) this.told();
          }),
        unconfirm: () => f.editAdd({}),
        lang: (l) => {
          this.code = l;
          this.requestUpdate();
        },
        done: (test) => {
          const h = a.created?.hook;
          f.closeAdd();
          if (h) location.hash = hooksHref(['hook', h.id], test ? { test: h.stages[0] } : undefined);
        },
      },
    );
  }

  private hookPage(id: string): TemplateResult {
    const f = this.flow;
    const o = f.one;
    if (o?.id === id && o.missing) return hookGone(id);
    const h = f.hook(id);
    if (!f.table || !o || o.id !== id || !h) return this.unreadable(o?.refused ?? f.refused, () => void f.load().then(() => f.open(id)));
    return hookView(
      { hook: h, table: f.table, one: o, writable: this.writable, now: f.now, lang: this.code, signIn: this.signIn(o.edit?.refused ?? o.secret.refused ?? o.remove.refused, 'A hook’s secret and the full payload') },
      {
        stage: (field, value) => f.stage(id, field, value),
        type: (field, raw) => f.type(field, raw),
        tidy: (field) => f.tidy(field),
        undo: (field) => {
          delete o.text[field];
          for (const k of Object.keys(o.text)) if (field === 'scope' && k.startsWith('scope.')) delete o.text[k];
          draft.drop(HOOKS_SCOPE, HERE, hookKey(id, field));
        },
        edit: () => f.startEdit(),
        editField: (p) => f.editEdit(p),
        save: (confirmed) =>
          void f.save(confirmed).then((ok) => {
            if (ok) this.told();
          }),
        cancelEdit: () => f.cancelEdit(),
        play: (p) => f.play(p),
        resetEvent: () => f.resetEvent(),
        test: () => void f.test(),
        secret: (p) => f.secret(p),
        rotate: () => void f.rotate().then(() => this.told()),
        lang: (l) => {
          this.code = l;
          this.requestUpdate();
        },
        askRemove: (on) => f.askRemove(on),
        remove: () =>
          void f.remove().then((ok) => {
            if (!ok) return;
            this.told();
            location.hash = hooksHref();
          }),
        reload: () => void f.readCalls(),
      },
    );
  }

  private tracePage(): TemplateResult {
    const f = this.flow;
    return html`${hooksNav('trace', { hooks: f.list?.length })}
      ${traceView(f.trace, {
        show: (request) => (location.hash = hooksHref(['trace'], request ? { request } : undefined)),
        reload: () => void f.readTrace(),
        replay: (on) => f.openReplay(on),
      })}`;
  }

  // ---- the decision providers -----------------------------------------------------------------------
  private providersPage(sub: string[]): TemplateResult {
    const p = this.prov;
    const hooks = this.flow.list ?? [];
    const reportRefused = p.reportRefused ? refused(p.reportRefused, { quiet: true }) : nothing;
    if (sub[0] === 'new') {
      if (!p.info || !p.add) return this.unreadable(p.refused, () => void p.load());
      return addProviderView(
        { state: p.add, info: p.info, list: p.shown, hooks, signIn: this.signIn(p.add.refused, 'A decision provider') },
        {
          edit: (x) => p.editAdd(x),
          create: () =>
            void p.create().then((made) => {
              if (!made) return;
              this.told();
              location.hash = providersHref(['p', made.id]);
            }),
        },
      );
    }
    if (sub[0] === 'p' && sub[1]) {
      const id = sub[1];
      const o = p.one;
      if (o?.id === id && o.missing) return providerGone(id);
      const shown = p.provider(id);
      if (!p.info || !o || o.id !== id || !shown) return this.unreadable(o?.refused ?? p.refused, () => void p.open(id));
      return providerView(
        { provider: shown, info: p.info, list: p.shown, health: p.health[id], report: p.report, reportRefused, hours: p.hours, one: o, hooks, writable: this.writable, signIn: this.signIn(o.edit?.refused ?? o.remove.refused, 'A decision provider') },
        {
          stage: (pid, field, value) => p.stage(pid, field, value),
          hours: (h) => void p.readReport(h),
          share: (raw) => p.share(id, raw),
          undo: (field) => draft.drop(HOOKS_SCOPE, HERE, providerKey(id, field)),
          question: (q, on) => p.question(id, q, on),
          map: (q, field, raw) => p.map(id, q, field, raw),
          tidy: (field) => p.tidy(field),
          keyField: (q, open) => p.keyField(id, q, open),
          key: (q, raw) => p.key(id, q, raw),
          removeKey: (q) => p.removeKey(id, q),
          undoKey: (q) => p.undoKey(id, q),
          probe: () => void p.probe(id, true),
          edit: () => p.startEdit(),
          editField: (x) => p.editEdit(x),
          save: () =>
            void p.save().then((ok) => {
              if (ok) this.told();
            }),
          cancelEdit: () => p.cancelEdit(),
          setTest: (x) => p.setTest(x),
          test: () => void p.test(),
          reload: () => void p.readCalls(),
          askRemove: (on) => p.askRemove(on),
          remove: () =>
            void p.remove().then((ok) => {
              if (!ok) return;
              this.told();
              location.hash = providersHref();
            }),
        },
      );
    }
    const nav = hooksNav('providers', { hooks: this.flow.list?.length, providers: p.info?.providers.length });
    if (!p.info) return html`${nav}${this.unreadable(p.refused, () => void p.load())}`;
    return html`${nav}
      ${p.refused ? refused(p.refused, { quiet: true }) : nothing}
      ${this.writable ? nothing : html`<p class="note small" data-readonly>${icon('info')} The settings interface does not take changes here, so the modes are shown as they are.</p>`}
      ${providersView({ info: p.info, list: p.shown, health: p.health, report: p.report, reportRefused, hours: p.hours, writable: this.writable }, { stage: (id, field, value) => p.stage(id, field, value), hours: (h) => void p.readReport(h) })}`;
  }

  render() {
    const p = this.path;
    if (p[0] === 'providers') return this.providersPage(p.slice(1));
    if (p[0] === 'new') return this.addPage();
    if (p[0] === 'hook' && p[1]) return this.hookPage(p[1]);
    if (p[0] === 'trace') return this.tracePage();
    return this.pipelinePage();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-hooks': QseHooks;
  }
}
