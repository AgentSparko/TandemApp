// <qse-engine-pair>: Add engine, pairing by code (SRV-829). The slip again, now with what the page
// has: the address it reached and the fingerprint the engine's pairing window shows, to compare
// with the terminal's (by eye, or pasted), and the one row the page lacks: the 6-digit code. The
// code proves the fingerprint, so there is nothing to confirm besides typing it. A refusal shows
// the controller's sentence and fix: the tries left after a wrong code; a new code after it ran
// out (`tandem pair` there, then one button here); both fingerprints and no way on when the host
// key is not the one shown. Paired: `eh-paired` with the engine. Leaving forgets the pairing.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { engines, type EnginesApi, type PairStart, type PairedView } from '../../../api/engines';
import { PairFlow, RAN_OUT, blocked, compareOf, ranOut, secondsLeft, waitLeft, type PairState } from '../../../lib/engine-pair';
import { SIGN_IN, clockLeft, hostPort } from '../../../lib/engine-words';
import { LightElement } from '../../../ui/base';
import { icon } from '../../../ui/bits';
import { command, findingCard, fingerprint, slip } from './bits';
import '../../../styles/wizard.css';
import '../../../styles/engine-add.css';

export interface PairHandlers {
  code(text: string): void;
  pasted(text: string): void;
  submit(): void;
  renew(): void;
  cancel(): void;
  other(): void;
}

export function pairView(s: PairState, now: number, on: PairHandlers): TemplateResult {
  const out = ranOut(s, now);
  const why = blocked(s, now);
  const left = secondsLeft(s, now);
  const wait = waitLeft(s, now);
  const over = s.ended !== null || out;
  const err = s.error ?? (out ? RAN_OUT : null);
  const target = hostPort(s.start.address, s.start.port);
  const tries = s.triesLeft;
  const codeHint = over
    ? 'This code is over.'
    : html`${left !== null ? html`Good for <span class="num eh-left" data-left>${clockLeft(left)}</span> more` : 'Good for 5 minutes'}${tries !== null ? html`, <span data-tries>${tries} ${tries === 1 ? 'try' : 'tries'} left</span>` : nothing}.`;
  return html`<section class="panel eh-pair" data-panel="pair" data-ended=${s.ended ?? (out ? 'spent' : 'no')} aria-labelledby="eh-pair-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-pair-t" tabindex="-1">Pair with ${s.name}</h2>
      <p class="panel-sub">Find these three lines in the engine host’s terminal. Check that the fingerprint is the same there, then type the code.</p></div></header>
    <form novalidate @submit=${(e: Event) => (e.preventDefault(), on.submit())}>
      ${slip(
        [
          { id: 'address', label: 'Address', have: true, value: html`<span class="num eh-wrap">${target}</span>`, hint: 'The engine this page reached.' },
          {
            id: 'fingerprint', label: 'Fingerprint', have: !!s.start.fingerprint,
            value: fingerprint(s.start.fingerprint, { pasted: s.pasted, onPaste: on.pasted, id: 'eh-pair-fp' }),
            hint: s.start.fingerprint ? 'It must be the same in the terminal. If it differs, do not type the code.' : undefined,
          },
          {
            id: 'code', now: !over, label: html`<label for="eh-code">Pairing code</label>`,
            value: html`<span class="eh-code-row"><input id="eh-code" class="wz-code num" data-in="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="000000" aria-describedby="eh-code-hint" ?disabled=${over || s.busy === 'code'} .value=${s.code} @input=${(e: Event) => on.code((e.target as HTMLInputElement).value)} />
              <button type="submit" class="btn btn-primary" data-act="pair" ?disabled=${why !== null}>${s.busy === 'code' ? 'Pairing…' : 'Pair'}</button></span>`,
            hint: html`<span id="eh-code-hint">${codeHint}</span>`,
          },
        ],
        { caption: `the engine host’s terminal, and what this page has of it` },
      )}
    </form>
    ${compareOf(s) === 'different' ? html`<p class="small err-ink eh-pair-diff" role="alert" data-different>The fingerprint you pasted is not the one above, so Pair is off. Check that this is the right machine and the right address.</p>` : nothing}
    ${err
      ? findingCard(err, {
          wait: wait || undefined,
          actions:
            err.code === SIGN_IN
              ? nothing
              : s.ended === 'stop'
                ? html`<button type="button" class="btn btn-sm" data-act="cancel" @click=${on.cancel}>Back to Add engine</button>`
                : over
                  ? html`<span class="small">On the engine host: ${command('tandem pair')}</span><button type="button" class="btn btn-primary btn-sm" data-act="renew" ?disabled=${s.busy === 'renew'} @click=${on.renew}>${icon('refresh')} ${s.busy === 'renew' ? 'Asking…' : 'I ran it: take the new code'}</button>`
                  : nothing,
        })
      : nothing}
    <footer class="eh-foot">
      <button type="button" class="btn btn-ghost" data-act="cancel" @click=${on.cancel}>Cancel</button>
      <button type="button" class="eh-link small" data-act="other-ways" @click=${on.other}>No code? Other ways to connect</button>
    </footer>
  </section>`;
}

@customElement('qse-engine-pair')
export class QseEnginePair extends LightElement {
  @property({ attribute: false }) api: EnginesApi = engines;
  /** the started pairing and the engine's name, from the step before */
  @property({ attribute: false }) start: PairStart | null = null;
  @property({ type: String }) name = '';
  private flow: PairFlow | null = null;
  private clock: ReturnType<typeof setInterval> | null = null;
  private done = false;

  connectedCallback(): void {
    super.connectedCallback();
    // the countdown and the wait move once a second
    this.clock = setInterval(() => this.requestUpdate(), 1000);
  }

  disconnectedCallback(): void {
    if (this.clock) clearInterval(this.clock);
    this.clock = null;
    // left without pairing (another page, a reload): the started pairing is forgotten
    if (!this.done) void this.flow?.cancel();
    super.disconnectedCallback();
  }

  willUpdate(changed: Map<string, unknown>): void {
    if (changed.has('start') && this.start && this.flow?.state.start.pairing !== this.start.pairing) {
      this.flow = new PairFlow(this.api, this.start, this.name || this.start.address, () => this.onChange());
      void this.updateComplete.then(() => this.querySelector<HTMLInputElement>('#eh-code')?.focus());
    }
  }

  private onChange(): void {
    if (this.flow?.state.error?.code === SIGN_IN) this.dispatchEvent(new CustomEvent('eh-signin', { bubbles: true }));
    this.requestUpdate();
  }

  private async submit(): Promise<void> {
    const paired: PairedView | null = (await this.flow?.submit()) ?? null;
    if (paired) {
      this.done = true;
      this.dispatchEvent(new CustomEvent('eh-paired', { detail: { paired }, bubbles: true }));
      return;
    }
    // a wrong code: the field is empty again and has the focus
    await this.updateComplete;
    this.querySelector<HTMLInputElement>('#eh-code:not([disabled])')?.focus();
  }

  /** a new code after `tandem pair`: the field has the focus again */
  private async renew(): Promise<void> {
    if (!(await this.flow?.renew())) return;
    await this.updateComplete;
    this.querySelector<HTMLInputElement>('#eh-code')?.focus();
  }

  private leave(event: 'eh-cancel' | 'eh-other'): void {
    const s = this.flow?.state.start;
    this.done = true;
    void this.flow?.cancel();
    this.dispatchEvent(new CustomEvent(event, { detail: { host: s?.address ?? null }, bubbles: true }));
  }

  render() {
    const f = this.flow;
    if (!f) return nothing;
    return pairView(f.state, Date.now(), {
      code: (t) => f.setCode(t),
      pasted: (t) => f.setPasted(t),
      submit: () => void this.submit(),
      renew: () => void this.renew(),
      cancel: () => this.leave('eh-cancel'),
      other: () => this.leave('eh-other'),
    });
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-engine-pair': QseEnginePair;
  }
}
