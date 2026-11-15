// <qse-engine-connect>: Add engine, the other ways (SRV-831), for an engine host with no code to
// type. One form (address, SSH port, user, and the engine's folder when it is not the usual one)
// and two ways. Password once: read the host's fingerprint, compare it, then "It matches: connect"
// with the password, which is used for that one login and kept nowhere (the field empties after
// each try). Paste a key: the page shows the line to add on the engine host, with copy buttons for
// the line and for one command that adds it, the fingerprint is confirmed, and the page asks every
// three seconds until the host takes the key, showing why not yet. A host key that changed or is
// not the one confirmed shows both fingerprints and offers no way to connect anyway. Paired:
// `eh-paired` with the engine.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { engines, type EnginesApi, type HostKeyView } from '../../../api/engines';
import { CHECK_EVERY_MS, ConnectFlow, compareOf, fingerprintCommand, pasteCommand, shownFingerprint, stopped, type ConnectState, type Way } from '../../../lib/engine-connect';
import { SIGN_IN } from '../../../lib/engine-words';
import { LightElement } from '../../../ui/base';
import { icon } from '../../../ui/bits';
import { command, copyBtn, findingCard, fingerprint } from './bits';
import '../../../styles/wizard.css';
import '../../../styles/engine-add.css';

export interface ConnectHandlers {
  way(w: Way): void;
  field(k: 'host' | 'port' | 'user' | 'dir', v: string): void;
  pasted(text: string): void;
  password(text: string): void;
  read(): void;
  connect(): void;
  makeLine(): void;
  confirmKey(): void;
  check(): void;
  refuse(): void;
  reset(): void;
  back(): void;
}

const WAYS: { id: Way; label: string; text: string }[] = [
  { id: 'password', label: 'Password, once', text: 'TandemApp logs in once with your password, adds its own key and ends the session. The password is used for that one login and kept nowhere.' },
  { id: 'key', label: 'Paste a key', text: 'TandemApp makes a key and shows one line. You add the line on the engine host yourself; no password is typed here.' },
];

/** who the host says it is: the fingerprint to compare, and where the engine host prints its own */
function hostKey(s: ConnectState, hk: Pick<HostKeyView, 'type' | 'known'> | null, on: ConnectHandlers): TemplateResult {
  const fp = shownFingerprint(s);
  const host = s.plan?.host ?? s.key?.host ?? s.host;
  return html`<div class="eh-hostkey" data-hostkey=${hk?.known ?? 'new'}>
    <h3 class="eh-h3">${hk?.known === 'same' ? 'The fingerprint TandemApp knows for this host' : `Check the fingerprint of ${host}`}</h3>
    <p class="small muted">${hk?.known === 'same' ? 'It is the one this TandemApp pinned for the address before.' : html`It is the first time TandemApp talks to this host. On the engine host, ${command(fingerprintCommand(hk?.type))} prints its own: the two must be the same.`}</p>
    ${fingerprint(fp, { type: hk?.type, pasted: s.pasted, onPaste: on.pasted, id: 'eh-ssh-fp' })}
  </div>`;
}

function target(s: ConnectState, on: ConnectHandlers, locked: boolean): TemplateResult {
  const set = (k: 'host' | 'port' | 'user' | 'dir') => (e: Event) => on.field(k, (e.target as HTMLInputElement).value);
  const folderOpen = !!s.dir || s.error?.code === 'not_installed' || s.error?.code === 'bad_engine_dir' || !!s.key?.engine_dir_guessed;
  return html`<div class="eh-target">
    <label class="field field-block eh-t-host"><span>Address</span><input data-in="host" name="host" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="192.0.2.80" ?readonly=${locked} .value=${s.host} @input=${set('host')} /></label>
    <label class="field field-block eh-t-port"><span>SSH port</span><input class="num" data-in="ssh-port" name="port" inputmode="numeric" autocomplete="off" placeholder="22" ?readonly=${locked} .value=${s.port} @input=${set('port')} /></label>
    <label class="field field-block eh-t-user"><span>User</span><input data-in="user" name="user" autocomplete="username" autocapitalize="off" spellcheck="false" placeholder="the login on the engine host" ?readonly=${locked} .value=${s.user} @input=${set('user')} /></label>
    <details class="eh-folder" ?open=${folderOpen}>
      <summary class="small">The engine is not in the usual folder</summary>
      <label class="field field-block"><span>Engine folder on that host</span><input class="num" data-in="dir" name="dir" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder=${s.way === 'key' ? '/home/spark/TandemLLM' : '~/TandemLLM'} ?readonly=${locked} .value=${s.dir} @input=${set('dir')} /><span class="xs muted">Leave it empty for the usual one: <code>TandemLLM</code> in the user’s home.</span></label>
    </details>
  </div>`;
}

function passwordWay(s: ConnectState, password: string, on: ConnectHandlers): TemplateResult {
  const cmp = compareOf(s);
  const again = !s.plan && !!s.confirmed;
  if (!s.plan && !again)
    return html`<div class="eh-actions"><button type="submit" class="btn btn-primary" data-act="read" ?disabled=${s.busy === 'read'}>${s.busy === 'read' ? 'Reading…' : 'Read its fingerprint'}</button></div>`;
  return html`${s.plan ? hostKey(s, s.plan.host_key, on) : html`<p class="small muted" data-confirmed>You confirmed this host’s fingerprint: <span class="num eh-wrap">${s.confirmed!.fingerprint}</span>. TandemApp refuses any other.</p>`}
    <label class="field field-block eh-pass"><span>Password for ${s.plan?.user ?? s.user}</span><input type="password" data-in="password" name="password" autocomplete="off" .value=${password} @input=${(e: Event) => on.password((e.target as HTMLInputElement).value)} /><span class="xs muted">Used for this one login. It is not stored, logged or shown again.</span></label>
    <div class="eh-actions">
      <button type="submit" class="btn btn-primary" data-act="connect" ?disabled=${!!s.busy || !password || cmp === 'different'}>${s.busy === 'connect' ? 'Connecting…' : again ? 'Connect' : 'It matches: connect'}</button>
      ${s.plan ? html`<button type="button" class="btn btn-ghost" data-act="no-match" ?disabled=${!!s.busy} @click=${on.refuse}>It does not match</button>` : nothing}
    </div>`;
}

function keyWay(s: ConnectState, on: ConnectHandlers): TemplateResult {
  const k = s.key;
  if (!k) return html`<div class="eh-actions"><button type="submit" class="btn btn-primary" data-act="line" ?disabled=${s.busy === 'line'}>${s.busy === 'line' ? 'Making it…' : 'Make the key line'}</button></div>`;
  const fp = shownFingerprint(s);
  const cmd = pasteCommand(k);
  const cmp = compareOf(s);
  const confirmed = !!s.confirmed;
  return html`<ol class="eh-steps">
    <li data-key-step="line">
      <h3 class="eh-h3">Add this line on the engine host</h3>
      <p class="small muted">One line, at the end of <code>${k.file}</code> of the user <b>${k.user}</b> on ${k.host}. It lets this key run the engine’s control commands and nothing else.</p>
      <pre class="eh-line num" data-line tabindex="0" aria-label="The line to add">${k.line}</pre>
      <div class="eh-actions">
        <span class="eh-copy-l">${copyBtn(k.line, 'Copy the line')}<span class="small">Copy the line</span></span>
        ${cmd ? html`<span class="eh-copy-l">${copyBtn(cmd, 'Copy one command that adds the line')}<span class="small">Copy one command that adds it</span></span>` : nothing}
      </div>
      ${k.engine_dir_guessed ? html`<p class="small warn-ink" data-guessed>The line names the engine’s folder as <span class="num">${k.engine_dir}</span>, which is a guess. If the engine is elsewhere, set its folder above and make a new line.</p>` : nothing}
    </li>
    <li data-key-step="fingerprint">
      ${fp
        ? hostKey(s, k.host_key, on)
        : html`<h3 class="eh-h3">Check the host’s fingerprint</h3>${k.finding ? findingCard(k.finding, { quiet: true, actions: html`<button type="button" class="btn btn-sm" data-act="ssh-again" ?disabled=${!!s.busy} @click=${on.check}>${icon('refresh')} Try SSH again</button>` }) : nothing}`}
      ${fp && !confirmed
        ? html`<div class="eh-actions"><button type="button" class="btn btn-primary" data-act="confirm-key" ?disabled=${!!s.busy || cmp === 'different'} @click=${on.confirmKey}>It matches: start checking</button><button type="button" class="btn btn-ghost" data-act="no-match" ?disabled=${!!s.busy} @click=${on.refuse}>It does not match</button></div>`
        : nothing}
    </li>
    <li data-key-step="wait">
      <h3 class="eh-h3">TandemApp waits for the line</h3>
      ${s.polling
        ? html`${s.waiting ? findingCard(s.waiting) : nothing}
            <p class="small muted" role="status" data-polling>Asked ${s.checks} time${s.checks === 1 ? '' : 's'}. It asks again every ${CHECK_EVERY_MS / 1000} seconds; the page goes on by itself once the host takes the key.</p>
            <div class="eh-actions"><button type="button" class="btn btn-sm" data-act="check" ?disabled=${s.busy === 'check'} @click=${on.check}>${icon('refresh')} Ask now</button></div>`
        : html`<p class="small muted">${confirmed ? 'The checking stopped. Fix what is said below, then make a new line.' : 'It starts once you said the fingerprint matches.'}</p>`}
    </li>
  </ol>`;
}

export function connectView(s: ConnectState, password: string, on: ConnectHandlers): TemplateResult {
  const stop = stopped(s);
  const read = !!s.plan || !!s.key;
  const submit = (e: Event) => {
    e.preventDefault();
    // Enter in a field: the step's own button. A key line that is made is not made again by it
    if (s.way === 'key') {
      if (!s.key) on.makeLine();
    } else if (s.plan || s.confirmed) on.connect();
    else on.read();
  };
  return html`<section class="panel eh-connect" data-panel="connect" data-way=${s.way} aria-labelledby="eh-connect-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-connect-t" tabindex="-1">Other ways to connect</h2>
      <p class="panel-sub">For an engine host with no code to type: an older engine, or a machine you have a login for. Both give TandemApp a key that can run the engine’s control commands and nothing else.</p></div></header>
    <div class="seg eh-ways-seg" role="radiogroup" aria-label="How to connect">${WAYS.map(
      (w) => html`<button type="button" class="seg-btn ${s.way === w.id ? 'is-on' : ''}" role="radio" aria-checked=${s.way === w.id} data-way-btn=${w.id} ?disabled=${!!s.busy} @click=${() => on.way(w.id)}>${w.label}</button>`,
    )}</div>
    <p class="small eh-way-text">${WAYS.find((w) => w.id === s.way)!.text}</p>
    <form class="eh-form" novalidate @submit=${submit}>
      ${target(s, on, s.polling || !!s.busy)}
      <p class="small err-ink" role="alert" data-form-error>${s.formError ?? ''}</p>
      ${s.refused ? html`<div class="eh-finding is-warn" role="status" data-finding="refused"><span class="eh-finding-g" aria-hidden="true">${icon('warn')}</span><div class="eh-finding-b"><p class="eh-finding-m">Stopped. Nothing was sent to that host.</p><p class="eh-finding-f">A fingerprint that does not match can mean another machine answers at that address. Check the address, or pair by code: the code proves the machine.</p></div></div>` : nothing}
      ${stop ? nothing : s.way === 'password' ? passwordWay(s, password, on) : keyWay(s, on)}
      ${s.error
        ? findingCard(s.error, {
            actions: s.error.code === SIGN_IN ? nothing : stop || (read && s.way === 'key' && !s.polling) ? html`<button type="button" class="btn btn-sm" data-act="reset" @click=${on.reset}>Start over</button>` : nothing,
          })
        : nothing}
    </form>
    <footer class="eh-foot"><button type="button" class="btn btn-ghost" data-act="back" @click=${on.back}>Back to Add engine</button></footer>
  </section>`;
}

@customElement('qse-engine-connect')
export class QseEngineConnect extends LightElement {
  @property({ attribute: false }) api: EnginesApi = engines;
  /** the address the step before was looking at */
  @property({ type: String }) host = '';
  /** the password, in this element only until the one call that uses it */
  @state() private password = '';
  private flow: ConnectFlow | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    this.flow = new ConnectFlow(this.api, () => this.onChange(), this.host);
    this.timer = setInterval(() => {
      const s = this.flow?.state;
      if (s?.polling && !s.busy && document.visibilityState !== 'hidden') void this.flow!.check().then((p) => this.paired(p));
    }, CHECK_EVERY_MS);
    void this.updateComplete.then(() => this.querySelector<HTMLInputElement>(this.host ? '[data-in=user]' : '[data-in=host]')?.focus());
  }

  disconnectedCallback(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.password = '';
    super.disconnectedCallback();
  }

  private onChange(): void {
    if (this.flow?.state.error?.code === SIGN_IN) this.dispatchEvent(new CustomEvent('eh-signin', { bubbles: true }));
    this.requestUpdate();
  }

  private paired(p: unknown): void {
    if (p) this.dispatchEvent(new CustomEvent('eh-paired', { detail: { paired: p }, bubbles: true }));
  }

  private async connect(): Promise<void> {
    const pw = this.password;
    // the field empties with the try: a wrong password is typed again, never read back
    this.password = '';
    this.paired(await this.flow!.connect(pw));
  }

  render() {
    const f = this.flow;
    if (!f) return nothing;
    return connectView(f.state, this.password, {
      way: (w) => ((this.password = ''), f.setWay(w)),
      field: (k, v) => f.setField(k, v),
      pasted: (t) => f.setPasted(t),
      password: (t) => (this.password = t),
      read: () => void f.read(),
      connect: () => void this.connect(),
      makeLine: () => void f.makeLine(),
      confirmKey: () => void f.confirmKey().then((p) => this.paired(p)),
      check: () => void f.check().then((p) => this.paired(p)),
      refuse: () => ((this.password = ''), f.refuse()),
      reset: () => ((this.password = ''), f.reset()),
      back: () => this.dispatchEvent(new CustomEvent('eh-cancel', { bubbles: true })),
    });
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-engine-connect': QseEngineConnect;
  }
}
