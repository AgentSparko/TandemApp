// <qse-engine-find>: Add engine's first step (SRV-827). What to look for on the engine host (the
// three things its install printed), the engines the LAN advertises (name, version, address and
// port, fingerprint; one already in the fleet says so and leads to its setup), and an address
// field for an engine the search cannot see (another network, a tailnet, the search not allowed).
// A row or a good address starts the pairing and hands it to the wizard (`eh-pair`); a refusal
// shows the controller's sentence and its fix where it happened. `eh-other` opens the other ways.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { engines, type DiscoveredEngine, type EngineFinding, type EnginesApi, type PairStart } from '../../../api/engines';
import { ENGINE_PORT, FindFlow, listWords, probeNext, rowKey, startNext, type FindState, type ProbeNext } from '../../../lib/engine-find';
import { SIGN_IN, hostPort, isHostKeyStop } from '../../../lib/engine-words';
import { LightElement } from '../../../ui/base';
import { icon } from '../../../ui/bits';
import { command, findingCard, ghost, slip } from './bits';
import '../../../styles/wizard.css';
import '../../../styles/engine-add.css';

export interface FindHandlers {
  search(): void;
  pick(e: DiscoveredEngine): void;
  address(v: string): void;
  port(v: string): void;
  check(): void;
  other(host?: string): void;
}

/** the three things the engine host printed, before the page has any of them */
export function guide(): TemplateResult {
  return html`<section class="panel eh-guide" data-panel="guide" aria-labelledby="eh-guide-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-guide-t">First, on the engine host</h2>
      <p class="panel-sub">The engine host is the machine with the GPU. Install the TandemLLM engine there: the install ends by printing three things. Keep that terminal open, the page asks for them.</p></div></header>
    ${slip(
      [
        { id: 'code', label: 'Pairing code', value: ghost('••• •••', 'six digits, shown in the terminal'), hint: 'You type it in the next step. Good for 5 minutes and 5 tries.' },
        { id: 'fingerprint', label: 'Fingerprint', value: ghost('SHA256:••••••••••••••••', 'starts with SHA256, shown in the terminal'), hint: 'You compare it with the one this page shows.' },
        { id: 'address', label: 'Address', value: ghost('•••.•••.•••.•••:8000', 'the engine’s address and port, shown in the terminal'), hint: 'The page looks for it below. Type it when the search does not find it.' },
      ],
      { caption: 'the last lines the engine host printed' },
    )}
    <p class="small muted eh-guide-again">Installed earlier, or the code ran out? Run ${command('tandem pair')} on the engine host for a new code.</p>
  </section>`;
}

function actions(next: ProbeNext, on: FindHandlers, again: () => void, host?: string): TemplateResult | typeof nothing {
  if (next === 'other-ways') return html`<button type="button" class="btn btn-primary btn-sm" data-act="other-ways" @click=${() => on.other(host)}>Connect another way</button>`;
  if (next === 'new-code') return html`<button type="button" class="btn btn-sm" data-act="again" @click=${again}>${icon('refresh')} I ran it: check again</button><button type="button" class="btn btn-ghost btn-sm" data-act="other-ways" @click=${() => on.other(host)}>Connect another way</button>`;
  return html`<button type="button" class="btn btn-sm" data-act="again" @click=${again}>${icon('refresh')} Try again</button>`;
}

/** a finding that the sign-in card above answers needs no button of its own; a fingerprint that
 *  is not the advertised one gets none either: nothing on the page pairs it anyway */
const act = (f: EngineFinding, on: FindHandlers, again: () => void, host?: string) => (f.code === SIGN_IN || isHostKeyStop(f) ? nothing : actions(startNext(f), on, again, host));

function row(e: DiscoveredEngine, s: FindState, on: FindHandlers): TemplateResult {
  const key = rowKey(e);
  const busy = s.busyRow === key;
  const err = s.rowError[key];
  const meta = html`<span class="eh-row-name">${e.name}</span>${e.version ? html`<span class="wz-pill">${e.version}</span>` : nothing}<span class="num eh-row-addr">${hostPort(e.address, e.port)}</span>`;
  const fp = e.fingerprint ? html`<span class="num eh-row-fp eh-wrap">${e.fingerprint}</span>` : html`<span class="eh-row-fp muted">advertises no fingerprint</span>`;
  return html`<li class="eh-item" data-engine=${key} data-in-fleet=${e.node ? 'true' : 'false'}>
    ${e.node
      ? html`<div class="eh-row is-in"><span class="eh-row-main"><span class="eh-row-top">${meta}</span>${fp}</span>
          <span class="eh-row-go"><span class="wz-pill is-ok">${icon('check')} in your fleet${e.alias && e.alias !== e.name ? html` as ${e.alias}` : nothing}</span><a class="btn btn-sm" href=${`#/add-engine/${encodeURIComponent(e.node)}`}>Open its setup</a></span></div>`
      : html`<button type="button" class="eh-row" ?disabled=${!!s.busyRow} aria-busy=${busy} aria-label=${`Pair with ${e.name} at ${hostPort(e.address, e.port)}`} @click=${() => on.pick(e)}>
          <span class="eh-row-main"><span class="eh-row-top">${meta}</span>${fp}</span>
          <span class="eh-row-go" aria-hidden="true">${busy ? 'Asking…' : html`Pair ${icon('right')}`}</span>
        </button>`}
    ${err ? findingCard(err, { actions: act(err, on, () => on.pick(e), e.address) }) : nothing}
  </li>`;
}

function found(s: FindState, on: FindHandlers): TemplateResult {
  const v = s.view;
  const list = v?.engines ?? [];
  const lanFinding = v && (v.lan === 'denied' || v.lan === 'failed') ? v.finding : null;
  const empty = !s.searching && v && v.lan === 'ok' && list.length === 0;
  return html`<section class="panel" data-panel="found" data-lan=${v?.lan ?? 'not_run'} aria-labelledby="eh-found-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-found-t" tabindex="-1">Engines on your network</h2>
      <p class="panel-sub" role="status" data-found-words>${listWords(s)}</p></div>
      <div class="panel-tools"><button type="button" class="btn btn-sm" data-act="search" ?disabled=${s.searching} @click=${on.search}>${icon('refresh')} Search again</button></div></header>
    ${s.searchError ? findingCard(s.searchError, { actions: s.searchError.code === SIGN_IN ? nothing : html`<button type="button" class="btn btn-sm" @click=${on.search}>${icon('refresh')} Try again</button>` }) : nothing}
    ${lanFinding ? findingCard(lanFinding) : nothing}
    ${list.length
      ? html`<ul class="eh-list">${list.map((e) => row(e, s, on))}</ul>`
      : s.searching
        ? html`<div class="skeleton" aria-hidden="true"><div class="skeleton-line" style="width:72%"></div><div class="skeleton-line" style="width:54%"></div></div>`
        : nothing}
    ${empty ? html`<p class="small muted" data-empty>The engine may be off, on another network, or reached over a tailnet. Type its address: the terminal printed it.</p>` : nothing}
    ${list.length ? html`<p class="xs muted eh-list-note">An engine in this list only says who it is. The code proves it in the next step.</p>` : nothing}
  </section>`;
}

function byAddress(s: FindState, on: FindHandlers): TemplateResult {
  const p = s.probe;
  const next = p ? probeNext(p) : null;
  const host = p?.address;
  return html`<section class="panel" data-panel="by-address" aria-labelledby="eh-addr-t">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-addr-t">Or type its address</h2>
      <p class="panel-sub">For an engine the search does not see. A tailnet name or address works too.</p></div></header>
    <form class="eh-addr" novalidate @submit=${(e: Event) => (e.preventDefault(), on.check())}>
      <label class="field field-block eh-addr-a"><span>Address</span><input data-in="address" name="address" autocomplete="off" autocapitalize="off" spellcheck="false" inputmode="url" placeholder="192.0.2.80" .value=${s.address} aria-invalid=${s.formError ? 'true' : 'false'} aria-describedby="eh-addr-err" @input=${(e: Event) => on.address((e.target as HTMLInputElement).value)} /></label>
      <label class="field field-block eh-addr-p"><span>Port</span><input class="num" data-in="port" name="port" inputmode="numeric" autocomplete="off" placeholder=${String(ENGINE_PORT)} .value=${s.port} @input=${(e: Event) => on.port((e.target as HTMLInputElement).value)} /></label>
      <button type="submit" class="btn btn-primary" data-act="check" ?disabled=${s.probing}>${s.probing ? 'Checking…' : 'Check address'}</button>
    </form>
    <p class="small err-ink" id="eh-addr-err" role="alert" data-form-error>${s.formError ?? ''}</p>
    ${s.probeError ? findingCard(s.probeError, { actions: act(s.probeError, on, on.check, host) }) : nothing}
    ${p && next === 'in-fleet'
      ? html`<div class="eh-finding is-info" role="status" data-finding="in_fleet"><span class="eh-finding-g" aria-hidden="true">${icon('check')}</span><div class="eh-finding-b"><p class="eh-finding-m">The engine at ${hostPort(p.address, p.port)} is in your fleet already.</p><div class="eh-actions"><a class="btn btn-sm btn-primary" href=${`#/add-engine/${encodeURIComponent(p.node!)}`}>Open its setup</a></div></div></div>`
      : p?.finding && next
        ? findingCard(p.finding, { actions: actions(next, on, on.check, host) })
        : nothing}
  </section>`;
}

export function findView(s: FindState, on: FindHandlers): TemplateResult {
  return html`${guide()}
    <div class="eh-two">${found(s, on)}${byAddress(s, on)}</div>
    <p class="small eh-ways" data-ways><span class="muted">No code to type?</span> <button type="button" class="eh-link" data-act="other-ways" @click=${() => on.other()}>Other ways to connect: a password once, or a key you paste</button></p>`;
}

@customElement('qse-engine-find')
export class QseEngineFind extends LightElement {
  @property({ attribute: false }) api: EnginesApi = engines;
  private flow: FindFlow | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    this.flow = new FindFlow(this.api, () => this.onChange());
    void this.flow.open();
  }

  /** the wizard asks for a new search once someone signed in */
  refresh(): void {
    void this.flow?.search();
  }

  private onChange(): void {
    const s = this.flow!.state;
    const needs = [s.searchError, s.probeError, ...Object.values(s.rowError)].some((f) => f?.code === SIGN_IN);
    if (needs) this.dispatchEvent(new CustomEvent('eh-signin', { bubbles: true }));
    this.requestUpdate();
  }

  private pair(got: { start: PairStart; name: string } | null): void {
    if (got) this.dispatchEvent(new CustomEvent('eh-pair', { detail: got, bubbles: true }));
  }

  render() {
    const f = this.flow;
    if (!f) return nothing;
    return findView(f.state, {
      search: () => void f.search(),
      pick: (e) => void f.pick(e).then((g) => this.pair(g)),
      address: (v) => f.setAddress(v),
      port: (v) => f.setPort(v),
      check: () => void f.check().then((g) => this.pair(g)),
      other: (host) => this.dispatchEvent(new CustomEvent('eh-other', { detail: { host: host ?? null }, bubbles: true })),
    });
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-engine-find': QseEngineFind;
  }
}
