// <qse-endpoint> (SRV-889): the API tab's first page, "Endpoint and keys". The one address every
// client uses (OpenAI-compatible and Messages API), the person's own API keys with what each may
// run, and New API key, which opens the smart dialog (views/tokens/token-dialog.ts). Managing keys
// (rotate, revoke, every person's) stays on the Access page; the backends and their routing on
// API, Backends.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { auth, type Me, type Token } from '../../api/auth';
import { mayCreate, scopeLabel } from '../../lib/access';
import { LightElement } from '../../ui/base';
import { copyButton, icon, skeleton } from '../../ui/bits';
import './token-dialog';
import '../../styles/access.css';

const day = (at: string | null): string => (at ? at.slice(0, 10) : 'never');

@customElement('qse-endpoint')
export class QseEndpoint extends LightElement {
  @state() private me: Me | null = null;
  @state() private tokens: Token[] | null = null;
  @state() private error: string | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      this.me = await auth.me();
      this.tokens = this.me?.authenticated ? await auth.tokens() : [];
      this.error = null;
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
      this.tokens = this.tokens ?? [];
    }
  }

  private newKey(): void {
    if (this.me) void this.querySelector('qse-token-dialog')?.show(this.me);
  }

  private keyRow(t: Token): TemplateResult {
    const runs = t.scopes.some((s) => s.startsWith('inference:'));
    const where = !runs ? 'runs no models' : t.backends?.length ? `backends ${t.backends.join(', ')}${t.models.length ? ` · models ${t.models.join(', ')}` : ''}` : t.models.length ? `models ${t.models.join(', ')}` : 'every backend and model';
    return html`<li class="ac-row" data-key=${t.id}>
      <div class="ac-t"><b>${t.label}</b>${t.state !== 'active' ? html` <span class="tag">${t.state}</span>` : nothing}</div>
      <p class="small muted">${t.scopes.map(scopeLabel).join(', ')} · <span class="num ac-wrap">${where}</span> · ${t.expires_at ? `expires ${day(t.expires_at)}` : 'never expires'} · <span class="num">${t.hint ?? ''}</span></p>
    </li>`;
  }

  render() {
    const origin = location.origin;
    const mine = (this.tokens ?? []).filter((t) => t.kind === 'user');
    const may = mayCreate(this.me);
    return html`<header class="page-head" data-page="api">
        <div class="eyebrow">Fleet</div>
        <h1>Endpoint and keys</h1>
        <p class="page-sub">One address for every client, OpenAI-compatible or Messages API. A key decides which backends and models a client may run.</p>
      </header>
      ${this.error ? html`<p class="login-error" role="alert">${this.error} <button type="button" class="btn btn-sm" @click=${() => void this.load()}>Try again</button></p>` : nothing}
      <section class="panel" data-endpoint>
        <h2 class="panel-title">The endpoint</h2>
        <dl class="ac-snips">
          <div><dt class="small muted">OpenAI-compatible base URL</dt><dd><div class="ac-secret"><code class="num">${origin}/v1</code>${copyButton(`${origin}/v1`, 'Copy the OpenAI base URL')}</div></dd></div>
          <div><dt class="small muted">Messages API base URL</dt><dd><div class="ac-secret"><code class="num">${origin}</code>${copyButton(origin, 'Copy the Messages API base URL')}</div></dd></div>
        </dl>
        <p class="small muted">Every request needs a key. Whether other devices may use it is set under <a href="#/access/exposure">Users & access, API exposure</a>; which backend serves a model, under <a href="#/api/backends">Backends</a>.</p>
      </section>
      <section class="panel" data-keys>
        <div class="panel-head"><h2 class="panel-title">Your keys</h2>${may ? html`<button type="button" class="btn btn-primary" data-act="key-new" @click=${() => this.newKey()}>${icon('plus')} New API key</button>` : nothing}</div>
        ${this.tokens === null ? skeleton(3) : mine.length ? html`<ul class="ac-list">${mine.map((t) => this.keyRow(t))}</ul>` : html`<p class="small muted" data-empty>${may ? 'No key yet. A key is shown once, when it is made.' : 'Your role makes no keys; an admin can allow it.'}</p>`}
        <p class="small muted">Rotate or revoke a key on <a href="#/account/tokens">My tokens</a>.</p>
      </section>
      <qse-token-dialog @token-made=${() => void this.load()}></qse-token-dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-endpoint': QseEndpoint;
  }
}
