// <qse-engine-wizard>: the setup wizard (VIS-278), the first page of a TandemApp with no engine and
// Settings → Add engine after that. It shows one step at a time: Add engine (views/fleet/engine/
// find.ts), Pair by the code the engine host printed (pair.ts) or one of the other ways
// (connect.ts), then the bare engine and a model (after.ts), then a first message that the Chat tab
// sends. The steps after pairing live at #/add-engine/<engine host>: a reload, or another device,
// opens them again, a running download included. A step that is refused for want of the admin
// password brings the controller's sign-in to the top of the page. The other ways into a fleet
// (a server by its URL, a Spark with TandemNode) stay one link away. With Chat off (OPS-288: a new
// TandemApp's default) the last step offers the Playground and the switch instead of the Chat tab.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { EngineView, PairStart, PairedView } from '../../api/engines';
import { fleet as fleetApi } from '../../api/fleet';
import { refreshContext, registerContext, type ContextContent } from '../../lib/context';
import { FIRST_DEFAULT, STEP_LABEL, WIZARD_STEPS, engineHash, firstRun, headOf, stepOf, stepper, type Screen } from '../../lib/engine-wizard';
import { chatHash } from '../../lib/router';
import { onFleet, refreshFleet, type FleetState } from '../../lib/fleet-state';
import { LightElement } from '../../ui/base';
import { signInCard } from '../monitor/bits';
import { checkSession, needsSignIn, onSession } from '../monitor/data';
import { chatView } from './engine/first';
import './engine/after';
import './engine/connect';
import './engine/find';
import './engine/pair';
import '../../styles/wizard.css';
import '../../styles/engine-add.css';

@customElement('qse-engine-wizard')
export class QseEngineWizard extends LightElement {
  /** the segments after #/add-engine: an engine host's id for the steps after pairing */
  @property({ attribute: false }) path: string[] = [];
  @state() private fleet: FleetState | null = null;
  /** the step before pairing the page is on (after it, the route says) */
  @state() private stage: 'find' | 'pair' | 'connect' = 'find';
  @state() private start: PairStart | null = null;
  @state() private name = '';
  @state() private connectHost = '';
  /** the engine a pairing just answered, and how it was paired */
  @state() private engine: EngineView | null = null;
  @state() private method: string | null = null;
  /** the engine host that serves: the last step shows for it */
  @state() private readyId: string | null = null;
  @state() private text = FIRST_DEFAULT;
  @state() private askedSignIn = false;
  /** OPS-288: whether this TandemApp's Chat is on (null: not read yet, or an older controller) */
  @state() private chatOn: boolean | null = null;
  private first: boolean | null = null;
  private unsub: (() => void)[] = [];
  private shown: Screen | null = null;
  /** the page went from one step to another (the first paint is no move) */
  private moved = false;

  connectedCallback(): void {
    super.connectedCallback();
    void checkSession();
    this.unsub.push(
      onSession(() => this.requestUpdate()),
      onFleet((f) => {
        this.fleet = f;
        refreshContext();
      }),
      registerContext('add-engine', () => this.context()),
    );
    window.addEventListener('qse-signed-in', this.onSignedIn);
    this.addEventListener('eh-pair', this.onPair as EventListener);
    this.addEventListener('eh-other', this.onOther as EventListener);
    this.addEventListener('eh-cancel', this.onCancel);
    this.addEventListener('eh-paired', this.onPaired as EventListener);
    this.addEventListener('eh-ready', this.onReady as EventListener);
    this.addEventListener('eh-signin', this.onSignIn);
  }

  disconnectedCallback(): void {
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    this.unsub.forEach((u) => u());
    this.unsub = [];
    super.disconnectedCallback();
  }

  private get screen(): Screen {
    const id = this.path[0];
    if (id) return this.readyId === id ? 'chat' : 'after';
    return this.stage;
  }

  private onPair = (e: CustomEvent<{ start: PairStart; name: string }>): void => {
    this.start = e.detail.start;
    this.name = e.detail.name;
    this.stage = 'pair';
  };
  private onOther = (e: CustomEvent<{ host: string | null }>): void => {
    this.connectHost = e.detail.host ?? '';
    this.start = null;
    this.stage = 'connect';
  };
  private onCancel = (): void => {
    this.start = null;
    this.stage = 'find';
  };
  private onPaired = (e: CustomEvent<{ paired: PairedView }>): void => {
    const eng = e.detail.paired.engine;
    this.start = null;
    this.stage = 'find';
    if (!eng) return;
    this.engine = eng;
    this.method = e.detail.paired.via;
    refreshFleet();
    location.hash = engineHash(eng.id);
  };
  private onReady = (e: CustomEvent<{ engine: EngineView | null }>): void => {
    this.engine = e.detail.engine ?? this.engine;
    this.readyId = this.path[0] ?? null;
    refreshFleet();
  };
  private onSignIn = (): void => {
    this.askedSignIn = true;
    void checkSession(true);
  };
  /** signed in: the step that was refused asks again */
  private onSignedIn = (): void => {
    this.askedSignIn = false;
    (this.querySelector('qse-engine-find, qse-engine-after') as unknown as { refresh?: () => void } | null)?.refresh?.();
  };

  willUpdate(changed: Map<string, unknown>): void {
    // another engine host's route: what was held for the one before is gone
    if (changed.has('path') && this.engine && this.path[0] !== this.engine.id) {
      this.engine = null;
      this.method = null;
    }
    if (this.shown !== null && this.screen !== this.shown) this.moved = true;
    // a first run is what the fleet was when the page opened, this engine host aside: pairing the
    // first engine, or a reload on its steps, does not rename the page. Read here, not when the
    // fleet answers: an element upgraded late has its route only from its first update on
    if (this.first === null && this.fleet) this.first = firstRun(this.fleet.nodes.filter((n) => n.id !== this.path[0]));
  }

  updated(): void {
    const s = this.screen;
    if (s === this.shown) return;
    if (s === 'chat')
      void fleetApi
        .parts()
        .then((p) => (this.chatOn = p.chat.on))
        .catch(() => (this.chatOn = null));
    const was = this.shown;
    this.shown = s;
    refreshContext();
    // a new step: the page scrolls to its top and the step's heading takes the focus (the pairing
    // step gives it to the code field, the other ways to their first empty field, the step after
    // pairing to its heading once it has read the engine); the first paint leaves the focus alone
    if (was === null) return;
    window.scrollTo({ top: 0 });
    if (s === 'pair' || s === 'connect' || s === 'after') return;
    const step = this.querySelector<LightElement>('.eh-body > *');
    void (step?.updateComplete ?? Promise.resolve()).then(() => this.querySelector<HTMLElement>('.eh-body h2[tabindex="-1"]')?.focus());
  }

  private send(): void {
    if (this.text.trim()) location.hash = chatHash(this.text);
  }

  private context(): ContextContent {
    const at = WIZARD_STEPS.indexOf(stepOf(this.screen));
    return {
      title: 'Add engine',
      body: html`<section class="csec"><div class="csec-h"><span class="eyebrow">Steps</span></div>
          <ol class="wz-ctx-steps">${WIZARD_STEPS.map((s, i) => html`<li class=${i < at ? 'is-done' : i === at ? 'is-on' : ''}>${i < at ? '✓ ' : ''}${STEP_LABEL[s]}</li>`)}</ol>
          <p class="xs faint">After pairing the page keeps its place: leave during a download and come back here.</p></section>
        <section class="csec"><div class="csec-h"><span class="eyebrow">On the engine host</span></div>
          <p class="small"><code>tandem pair</code> shows a new code, its fingerprint and its address.</p>
          <p class="xs muted">A code is good for 5 minutes and 5 tries, and works once.</p></section>
        <section class="csec"><div class="csec-h"><span class="eyebrow">Other ways in</span></div>
          <p class="small"><a href="#/start">Connect a server by its URL</a></p><p class="small"><a href="#/add-spark">Add a Spark that runs TandemNode</a></p></section>`,
    };
  }

  private step(): TemplateResult {
    const id = this.path[0];
    switch (this.screen) {
      case 'find':
        return html`<qse-engine-find></qse-engine-find>`;
      case 'pair':
        return html`<qse-engine-pair .start=${this.start} .name=${this.name}></qse-engine-pair>`;
      case 'connect':
        return html`<qse-engine-connect .host=${this.connectHost}></qse-engine-connect>`;
      case 'after':
        return html`<qse-engine-after .engineId=${id} .engine=${this.engine} .method=${this.method} .announce=${this.moved}></qse-engine-after>`;
      case 'chat':
        return html`<div class="eh-chat">${chatView(this.engine, this.text, { text: (v) => (this.text = v), send: () => this.send() }, this.method, this.chatOn)}</div>`;
    }
  }

  render() {
    const s = this.screen;
    const head = headOf(this.first ?? firstRun(this.fleet?.nodes));
    return html`<header class="page-head" data-page="add-engine" data-first-run=${this.first ?? true}>
        <div class="eyebrow">Settings</div>
        <h1>${head.title}</h1>
        <p class="page-sub">${head.sub}</p>
      </header>
      <ol class="wz-stepper eh-stepper" aria-label="Setup steps">${stepper(s).map(
        (st) => html`<li class="wz-step ${st.state === 'on' ? 'is-on' : st.state === 'done' ? 'is-done' : ''}" data-step=${st.id} aria-current=${st.state === 'on' ? 'step' : 'false'}><span class="n" aria-hidden="true">${st.state === 'done' ? '✓' : st.n}</span><span class="l">${st.label}</span>${st.state === 'done' ? html`<span class="sr-only"> (done)</span>` : nothing}</li>`,
      )}</ol>
      ${needsSignIn() ? signInCard('Pairing and the model step') : this.askedSignIn ? html`<p class="note" role="status" data-no-password>No admin password is set on this TandemApp, so pairing works only from the machine it runs on. Open this page there, or set a password in <a href="#/app">App settings</a> on that machine.</p>` : nothing}
      <div class="eh-body" data-at=${s}>${this.step()}</div>
      ${s === 'find' ? html`<p class="small muted eh-else" data-else>Not a TandemLLM engine? <a href="#/start">Connect a server by its URL</a>, or <a href="#/add-spark">add a Spark that runs TandemNode</a>.</p>` : nothing}`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-engine-wizard': QseEngineWizard;
  }
}
