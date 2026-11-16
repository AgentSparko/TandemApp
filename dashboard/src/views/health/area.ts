// <qse-health>: the health page (SRV-835), Monitor → Health. It says in words how this TandemApp,
// each engine host and the plugins stand, what to do about each thing that is wrong, and runs the
// fixes TandemApp can run itself. A chunk of its own, loaded when the page opens.
//
// The page reads when it opens and every 20 seconds while it shows; Check again asks the
// controller for a fresh check, at most once every three seconds, as the controller allows. A fix
// shows its call under way, then what it left: the finding gone, a job still running (the target
// is then read again every few seconds), or the finding with its new reason. An action that asks
// first (Upgrade) opens a dialog: the engine host's plan, then the apply with the plan's confirm.
// A call refused for want of the admin password brings the controller's sign-in to the top of the
// page; a target that took too many fixes in a minute holds its buttons and counts down.
// #/health/<target> opens the page on that card.

import { html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { health, type HealthApi } from '../../api/health';
import { registerContext, type ContextContent } from '../../lib/context';
import { cardTarget, signInWhat } from '../../lib/health';
import { HealthFlow, POLL_MS } from '../../lib/health-flow';
import { poll, type Poller } from '../../lib/poll';
import { LightElement } from '../../ui/base';
import { signInCard } from '../monitor/bits';
import { checkSession } from '../monitor/data';
import { cardElementId, dialogView, healthView, type HealthHandlers } from './view';
import '../../styles/health.css';

@customElement('qse-health')
export class QseHealth extends LightElement {
  /** the segments after #/health: a target's id opens the page on its card */
  @property({ attribute: false }) path: string[] = [];
  @property({ attribute: false }) api: HealthApi = health;
  private flow: HealthFlow | null = null;
  private poller: Poller | null = null;
  private clock: ReturnType<typeof setInterval> | null = null;
  private unsub: (() => void)[] = [];
  /** the card the route named was scrolled to */
  private jumped: string | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    const flow = (this.flow = new HealthFlow(this.api, () => this.requestUpdate()));
    void checkSession();
    this.unsub.push(registerContext('health', () => this.context()));
    window.addEventListener('qse-signed-in', this.onSignedIn);
    // the first read shows as loading; the ones after it are the page's own
    this.poller = poll(() => flow.load({ quiet: flow.loaded }), POLL_MS);
    this.poller.start();
    // ages, countdowns, and the follow after a fix move once a second
    this.clock = setInterval(() => {
      flow.tick();
      this.requestUpdate();
    }, 1000);
  }

  disconnectedCallback(): void {
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    this.poller?.stop();
    this.poller = null;
    if (this.clock) clearInterval(this.clock);
    this.clock = null;
    this.unsub.forEach((u) => u());
    this.unsub = [];
    super.disconnectedCallback();
  }

  private onSignedIn = (): void => void this.flow?.signedIn();

  private get dialog(): HTMLDialogElement | null {
    return this.querySelector('dialog[data-dialog="fix"]');
  }

  protected updated(): void {
    const d = this.dialog;
    const open = !!this.flow?.dialog;
    if (d) {
      if (open && !d.open) {
        d.showModal();
        (d.querySelector<HTMLElement>('[data-in="ref"]') ?? d.querySelector<HTMLElement>('#hl-dlg-t'))?.focus();
      } else if (!open && d.open) d.close();
    }
    // the card the route names, once it is there
    const want = this.path[0] ?? null;
    if (want && want !== this.jumped && this.flow?.view) {
      this.jumped = want;
      this.show(want, false);
    }
  }

  private show(id: string, smooth = true): void {
    const el = this.querySelector<HTMLElement>(`#${CSS.escape(cardElementId(id))}`);
    if (!el) return;
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ block: 'start', behavior: smooth && !still ? 'smooth' : 'auto' });
    el.querySelector<HTMLElement>('h2')?.focus({ preventScroll: true });
  }

  private on: HealthHandlers = {
    refresh: () => void this.flow?.refresh(),
    recheck: (c) => void this.flow?.recheck(cardTarget(c)),
    press: (row, c) => void this.flow?.press(row, c.title),
    jump: (c) => this.show(c.id),
    dismiss: (key) => this.flow?.dismiss(key),
    ref: (text) => this.flow?.dialogSet({ ref: text }),
    when: (w) => this.flow?.dialogSet({ when: w }),
    go: () => void this.flow?.dialogGo(),
    apply: () => void this.flow?.dialogApply(),
    back: () => this.flow?.dialogBack(),
    close: () => this.flow?.dialogClose(),
  };

  private context(): ContextContent {
    return {
      title: 'Health',
      body: html`<section class="csec"><div class="csec-h"><span class="eyebrow">How to read it</span></div>
          <dl class="hl-legend">
            <div><dt>Does not work</dt><dd>Something is broken until it is fixed.</dd></div>
            <div><dt>Works, but worse</dt><dd>It serves, with a weak spot.</dd></div>
            <div><dt>Will pass</dt><dd>Nothing to do: it clears by itself.</dd></div>
          </dl>
          <p class="xs faint">A card’s line of checks runs in order. Where one fails the line breaks, and the checks after it are not run.</p></section>
        <section class="csec"><div class="csec-h"><span class="eyebrow">Fixes</span></div>
          <p class="small">A filled button is a fix TandemApp runs itself, over SSH or on this machine. The other buttons check again or show the steps.</p>
          <p class="xs muted">Each fix that changes something is written to the audit list. A target takes six fixes a minute.</p></section>
        <section class="csec"><div class="csec-h"><span class="eyebrow">Elsewhere</span></div>
          <p class="small"><a href="#/add-engine">Add engine</a></p><p class="small"><a href="#/app">Settings</a></p><p class="small"><a href="#/events">Events &amp; Alerts</a></p></section>`,
    };
  }

  render() {
    const f = this.flow;
    if (!f) return nothing;
    const card = f.signIn ? signInCard(signInWhat(f.signIn)) : nothing;
    const d = f.dialog;
    return html`${healthView(f, this.on, { focus: this.path[0] ?? null, signIn: d ? nothing : card })}
      <dialog class="act-dialog hl-dialog" aria-labelledby="hl-dlg-t" data-dialog="fix" data-phase=${d?.phase ?? nothing} @close=${() => f.dialogClose()} @cancel=${(e: Event) => (d?.phase === 'planning' || d?.phase === 'applying') && e.preventDefault()}>
        <div class="act-body">${dialogView(d, this.on, card)}</div>
      </dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-health': QseHealth;
  }
}
