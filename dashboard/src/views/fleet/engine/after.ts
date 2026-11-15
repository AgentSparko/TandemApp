// <qse-engine-after>: Add engine, after pairing (SRV-833). The engine host as it answers now (who
// it is, how it was paired, what it runs), the bare engine as the preselected way to run it, and
// while it serves no model the engine's own list to pick from: the plan says how much it downloads
// (or that the weights are on its disk), one button accepts the bare engine and starts the
// install, and the page follows it: the step, the GB so far, the steps done. The download runs on
// the engine host, so the page can be left and opened again. `eh-ready` once the engine serves.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { engines, type EngineModel, type EngineView, type EnginesApi } from '../../../api/engines';
import { AfterFlow, PROGRESS_EVERY_MS, barOf, modelName, planWords, stepWords, viaWords, type AfterState } from '../../../lib/engine-after';
import { SIGN_IN } from '../../../lib/engine-words';
import { LightElement } from '../../../ui/base';
import { icon } from '../../../ui/bits';
import { findingCard } from './bits';
import '../../../styles/wizard.css';
import '../../../styles/engine-add.css';

export interface AfterHandlers {
  pick(model: string): void;
  start(): void;
  again(): void;
  accept(): void;
  reload(): void;
  models(): void;
}

const STATE_WORDS: Record<string, string> = { healthy: 'running', loading: 'loading its model', draining: 'finishing its requests', failed: 'failed to start', down: 'not running' };

/** The engine host as it answers now: the status the pairing ends on. */
export function engineSummary(e: EngineView, method?: string | null): TemplateResult {
  const st = e.status?.engine;
  const model = st?.model ?? null;
  return html`<section class="panel eh-engine" data-panel="engine" data-engine-id=${e.id} aria-labelledby="eh-engine-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-engine-t"><span class="ok-ink" aria-hidden="true">${icon('check')}</span> ${e.alias} is paired</h2>
      <p class="panel-sub">TandemApp reaches it with a key of its own, ${viaWords(e, method)}. Its fingerprint is pinned: a machine with another one is refused.</p></div>
      ${st ? html`<span class="wz-pill ${st.state === 'healthy' ? 'is-ok' : 'is-warn'}" data-engine-state=${st.state}>engine ${STATE_WORDS[st.state] ?? st.state}</span>` : nothing}</header>
    <dl class="eh-kv">
      <div><dt>Address</dt><dd class="num eh-wrap">${e.address ?? '—'}${e.http.port ? html`<span class="muted">, engine port ${e.http.port}</span>` : nothing}</dd></div>
      <div><dt>Login</dt><dd class="num eh-wrap">${e.ssh.user ?? '—'}@${e.ssh.host ?? e.address ?? '—'}${e.ssh.port !== 22 ? `:${e.ssh.port}` : ''}</dd></div>
      <div><dt>Fingerprint</dt><dd class="num eh-wrap">${e.host_key ?? '—'}</dd></div>
      <div><dt>Engine</dt><dd>${e.engine_version ? html`<span class="num">${e.engine_version}</span>` : 'version not read'}</dd></div>
      <div><dt>Model</dt><dd data-engine-model>${model ?? (e.status ? 'none yet' : 'not read')}</dd></div>
    </dl>
  </section>`;
}

function modeCard(s: AfterState): TemplateResult {
  const m = s.mode;
  return html`<section class="panel eh-mode" data-panel="mode" data-mode-choice="engine" aria-labelledby="eh-mode-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-mode-t">How it runs</h2></div></header>
    <div class="eh-choice is-on" data-choice="engine">
      <span class="eh-choice-g" aria-hidden="true">${icon('check')}</span>
      <div><p class="eh-choice-t">Bare engine <span class="wz-pill">preselected</span></p>
        <p class="small muted">The engine serves its model and nothing else runs beside it. This is how a new engine starts, and it is what the next button sets.</p>
        ${m ? html`<p class="small" role="status" data-mode-result=${m.changed ? 'changed' : 'kept'}>${m.changed ? 'Set. The engine restarts once, when it is idle.' : 'It runs this way already: nothing changed.'}</p>` : nothing}</div>
    </div>
  </section>`;
}

function modelRow(m: EngineModel, s: AfterState, on: AfterHandlers): TemplateResult {
  const picked = s.picked === m.id;
  return html`<label class="eh-model ${picked ? 'is-on' : ''}" data-model=${m.id}>
    <input type="radio" name="eh-model" .checked=${picked} ?disabled=${!!s.busy} @change=${() => on.pick(m.id)} />
    <span class="eh-model-b"><span class="eh-model-t"><b>${m.name}</b>${m.params ? html`<span class="num muted">${m.params}</span>` : nothing}${m.recommended ? html`<span class="wz-pill is-ok">recommended</span>` : nothing}</span>
      ${m.pitch ? html`<span class="small muted">${m.pitch}</span>` : nothing}
      ${m.best_speed ? html`<span class="xs muted">Its fastest measured rate: <span class="num">${Math.round(m.best_speed.tok_s)}</span> tokens a second.</span>` : nothing}</span>
  </label>`;
}

function pickPanel(s: AfterState, on: AfterHandlers): TemplateResult {
  const name = modelName(s.models, s.picked);
  const ready = !!s.plan && !s.planning && !s.busy;
  return html`<section class="panel" data-panel="models" aria-labelledby="eh-models-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-models-t" tabindex="-1">Pick a model</h2>
      <p class="panel-sub">The engine serves no model yet. These are the ones it offers; it downloads the one you pick by itself.</p></div></header>
    ${s.models === null && !s.error ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:70%"></div><div class="skeleton-line" style="width:52%"></div></div>` : nothing}
    ${s.models?.length === 0 ? html`<p class="small muted" data-empty>The engine lists no model. Update the engine on its host, then check again.</p>` : nothing}
    ${s.models?.length
      ? html`<div class="eh-models" role="radiogroup" aria-labelledby="eh-models-t">${s.models.map((m) => modelRow(m, s, on))}</div>
          <p class="small eh-plan" role="status" data-plan=${s.plan ? (s.plan.on_disk ? 'on-disk' : 'download') : s.planning ? 'reading' : 'none'}>${s.planning ? `Asking the engine what ${name} needs…` : s.plan ? planWords(s.plan) : ''}</p>
          ${s.planError ? findingCard(s.planError, { actions: s.planError.code === SIGN_IN ? nothing : html`<button type="button" class="btn btn-sm" @click=${() => on.pick(s.picked!)}>${icon('refresh')} Ask again</button>` }) : nothing}
          <div class="eh-actions"><button type="button" class="btn btn-primary" data-act="start" ?disabled=${!ready} @click=${on.start}>${s.busy === 'start' ? 'Starting…' : s.plan?.on_disk ? `Use the bare engine and start ${name}` : `Use the bare engine and download ${name}`}</button></div>
          <p class="xs muted">The download runs on the engine host, not in this page: you can leave and come back under Add engine.</p>`
      : nothing}
    ${s.error ? findingCard(s.error, { actions: s.error.code === SIGN_IN ? nothing : html`<button type="button" class="btn btn-sm" data-act="models-again" @click=${s.models ? on.start : on.models}>${icon('refresh')} Try again</button>` }) : nothing}
  </section>`;
}

function installPanel(s: AfterState, on: AfterHandlers): TemplateResult {
  const p = s.progress;
  const name = modelName(s.models, s.jobModel);
  const failed = s.phase === 'failed';
  const bar = p ? barOf(p) : { value: 0, text: 'starting', kind: 'steps' as const };
  const pc = Math.round(bar.value * 100);
  return html`<section class="panel eh-install" data-panel="install" data-job-state=${p?.state ?? 'queued'} aria-labelledby="eh-install-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-install-t" tabindex="-1">${failed ? `${name} was not installed` : `Installing ${name}`}</h2>
      <p class="panel-sub">${failed ? 'The engine keeps running as it was.' : 'The engine does this by itself: it downloads the weights, checks them, and restarts with the model.'}</p></div></header>
    ${failed
      ? html`<div class="eh-finding is-err" role="alert" data-finding="job_failed"><span class="eh-finding-g" aria-hidden="true">${icon('warn')}</span><div class="eh-finding-b">
          <p class="eh-finding-m">${p?.message ?? 'The install stopped before it finished.'}</p>
          <p class="eh-finding-f">Try again. If it stops at the same place, run <code>tandem doctor</code> on the engine host.</p>
          <div class="eh-actions"><button type="button" class="btn btn-primary btn-sm" data-act="again" @click=${on.again}>${icon('refresh')} Pick again</button></div></div></div>`
      : html`<div class="eh-prog-h"><span class="eh-prog-step" data-step=${p?.step ?? ''}>${p ? stepWords(p) : 'Starting'}</span><span class="num small" data-bar-text>${bar.text}</span></div>
          <div class="wz-prog ${p?.state === 'done' ? 'is-done' : ''}" role="progressbar" aria-labelledby="eh-install-t" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${pc} aria-valuetext=${`${p ? stepWords(p) : 'Starting'}: ${bar.text}`}><i style="width:${pc}%"></i></div>
          ${p ? html`<p class="small muted" data-steps>${bar.kind === 'download' ? html`${p.steps_done} of ${p.steps} steps done. ` : nothing}The download is the long part.</p>` : nothing}
          ${s.progressError ? findingCard(s.progressError, { quiet: true }) : nothing}
          <p class="xs muted">You can leave this page. The install goes on, and Add engine shows it again.</p>`}
  </section>`;
}

export function afterView(s: AfterState, on: AfterHandlers, method?: string | null): TemplateResult {
  const e = s.engine;
  if (s.phase === 'load') return html`${e ? engineSummary(e, method) : nothing}<div class="skeleton" aria-busy="true" style="height:160px"></div>`;
  if (s.phase === 'unreadable')
    return html`<section class="panel" data-panel="unreadable" aria-labelledby="eh-unread-t">
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-unread-t" tabindex="-1">${e ? `${e.alias} does not answer` : 'This engine host cannot be read'}</h2>
        <p class="panel-sub">${e ? 'It is in your fleet, and TandemApp cannot read its status right now.' : 'TandemApp could not open it.'}</p></div></header>
      ${s.error ? findingCard(s.error, { actions: s.error.code === SIGN_IN ? nothing : html`<button type="button" class="btn btn-sm" data-act="reload" @click=${on.reload}>${icon('refresh')} Check again</button><a class="btn btn-ghost btn-sm" href="#/add-engine">Add another engine</a>` }) : nothing}
    </section>`;
  return html`${e ? engineSummary(e, method) : nothing}${modeCard(s)}
    ${s.phase === 'pick'
      ? pickPanel(s, on)
      : s.phase === 'installing' || s.phase === 'failed'
        ? installPanel(s, on)
        : s.phase === 'serving'
          ? html`<section class="panel" data-panel="serving" aria-labelledby="eh-serving-t">
              <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-serving-t" tabindex="-1">It serves ${e?.status?.engine.model ?? 'a model'} already</h2><p class="panel-sub">No model to pick. Accept the bare engine and send a first message.</p></div></header>
              ${s.error ? findingCard(s.error, { actions: nothing }) : nothing}
              <div class="eh-actions"><button type="button" class="btn btn-primary" data-act="accept" ?disabled=${s.busy === 'accept'} @click=${on.accept}>${s.busy === 'accept' ? 'Setting…' : 'Use the bare engine and continue'}</button></div>
            </section>`
          : nothing}`;
}

@customElement('qse-engine-after')
export class QseEngineAfter extends LightElement {
  @property({ attribute: false }) api: EnginesApi = engines;
  @property({ type: String }) engineId = '';
  /** the view the pairing just answered: saves the first read */
  @property({ attribute: false }) engine: EngineView | null = null;
  /** how it was paired just now (code, password, key); absent when the page was opened later */
  @property({ type: String }) method: string | null = null;
  /** the wizard came here from the step before: once the step has read the engine, its heading
   *  takes the focus (a page opened on this step leaves the focus alone) */
  @property({ type: Boolean }) announce = false;
  private announced = false;
  private flow: AfterFlow | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private told = false;

  connectedCallback(): void {
    super.connectedCallback();
    this.timer = setInterval(() => {
      if (this.flow?.state.phase === 'installing' && document.visibilityState !== 'hidden') void this.flow.tick();
    }, PROGRESS_EVERY_MS);
  }

  disconnectedCallback(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    super.disconnectedCallback();
  }

  willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('engineId') && this.engineId && this.flow?.state.id !== this.engineId) {
      this.told = false;
      this.flow = new AfterFlow(this.api, this.engineId, () => this.onChange());
      void this.flow.open(this.engine?.id === this.engineId ? this.engine : null);
    }
  }

  updated(): void {
    if (!this.announce || this.announced || !this.flow || this.flow.state.phase === 'load') return;
    this.announced = true;
    this.querySelector<HTMLElement>('h2[tabindex="-1"]')?.focus();
  }

  /** the wizard asks again once someone signed in */
  refresh(): void {
    void this.flow?.open();
  }

  private onChange(): void {
    const s = this.flow!.state;
    if ([s.error, s.planError, s.progressError].some((f) => f?.code === SIGN_IN)) this.dispatchEvent(new CustomEvent('eh-signin', { bubbles: true }));
    if (s.phase === 'ready' && !this.told) {
      this.told = true;
      this.dispatchEvent(new CustomEvent('eh-ready', { detail: { engine: s.engine }, bubbles: true }));
    }
    this.requestUpdate();
  }

  render() {
    const f = this.flow;
    if (!f) return nothing;
    return afterView(
      f.state,
      {
        pick: (m) => void f.pick(m),
        start: () => void f.start(),
        again: () => void f.again(),
        accept: () => void f.accept(),
        reload: () => void f.open(),
        models: () => void f.loadModels(),
      },
      this.method,
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-engine-after': QseEngineAfter;
  }
}
