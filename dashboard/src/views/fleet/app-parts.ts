// <qse-app-parts>: Settings → App, "What runs here" (OPS-288, VIS-280, SRV-845). A new TandemApp is
// bare: the gateway in front of the engines it pairs, with Chat and the conductor off and nothing
// connected. Here an admin switches Chat and the conductor on and off (each from the controller's
// plan: what changes, then its confirm), sees each engine host's own mode (the engine's setting,
// switched over SSH where it is paired that way), and disables or removes what is connected: the
// backends, and Chat's MCP servers while Chat is on. Every change asks first, in a dialog that says
// what it changes. The words are lib/app-parts.ts.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { describeError, request } from '../../api/client';
import { config } from '../../api/config';
import { engines as enginesApi } from '../../api/engines';
import { fleet, type AppParts, type PartPlan } from '../../api/fleet';
import type { McpServer } from '../../chat/types';
import { backendChanges, backendRows, doneWords, ENGINE_MODE_NOTE, engineModeChanges, isParts, mcpChanges, partRows, planTitle, type BackendAct, type BackendRow, type McpAct, type PartRow } from '../../lib/app-parts';
import { poll, type Poller } from '../../lib/poll';
import { LightElement } from '../../ui/base';
import { signInCard } from '../monitor/bits';
import { checkSession } from '../monitor/data';

type Pending =
  | { kind: 'part'; plan: PartPlan }
  | { kind: 'backend'; row: BackendRow; act: BackendAct }
  | { kind: 'mcp'; server: McpServer; act: McpAct }
  | { kind: 'engine'; id: string; alias: string; to: 'engine' | 'chat' };

const MCP_PATH = '/v1/chat-app/mcp/servers';

@customElement('qse-app-parts')
export class QseAppParts extends LightElement {
  @state() private parts: AppParts | null = null;
  @state() private mcp: McpServer[] | null = null;
  @state() private modes: Record<string, string | null> = {};
  @state() private pending: Pending | null = null;
  @state() private changes: string[] = [];
  @state() private busy = '';
  @state() private msg = '';
  @state() private err = '';
  @state() private signIn = false;
  private poller: Poller | null = null;
  private modesFor = '';

  private get dialog(): HTMLDialogElement | null {
    return this.querySelector('dialog[data-dialog="part"]');
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.poller = poll(() => this.load(), 15000);
    this.poller.start();
    window.addEventListener('qse-signed-in', this.onSignedIn);
  }
  disconnectedCallback(): void {
    this.poller?.stop();
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    super.disconnectedCallback();
  }
  private onSignedIn = (): void => {
    this.signIn = false;
    void this.poller?.refresh();
  };

  private async load(): Promise<void> {
    try {
      const p = await fleet.parts();
      this.parts = p;
      void this.loadMcp(p);
      void this.loadModes(p);
    } catch (e) {
      // an older controller has no switches: the panel stays away
      if (describeError(e).status !== 404) this.err = describeError(e).message;
    }
  }

  private async loadMcp(p: AppParts): Promise<void> {
    if (!p.mcp.available) {
      this.mcp = null;
      return;
    }
    try {
      this.mcp = await request<McpServer[]>(MCP_PATH);
    } catch {
      this.mcp = null;
    }
  }

  /** each SSH engine host's mode, read once per list (a status over SSH each) */
  private async loadModes(p: AppParts): Promise<void> {
    const key = p.engines.map((e) => e.id).join(',');
    if (key === this.modesFor) return;
    this.modesFor = key;
    const out: Record<string, string | null> = {};
    await Promise.all(
      p.engines.map(async (e) => {
        try {
          out[e.id] = (await enginesApi.engine(e.id)).mode;
        } catch {
          out[e.id] = null;
        }
      }),
    );
    this.modes = out;
  }

  private async refused(e: unknown): Promise<boolean> {
    if (describeError(e).status !== 401) return false;
    await checkSession(true);
    this.signIn = true;
    return true;
  }

  // ---- the plan, then the confirm ---------------------------------------------------------------

  private async ask(row: PartRow): Promise<void> {
    this.busy = `plan-${row.id}`;
    this.err = '';
    this.msg = '';
    try {
      const plan = await fleet.planPart(row.id, !row.on);
      if (!plan.changed) {
        this.msg = plan.changes.join(' ');
        void this.poller?.refresh();
        return;
      }
      this.open({ kind: 'part', plan }, plan.changes);
    } catch (e) {
      if (!(await this.refused(e))) this.err = describeError(e).message;
    } finally {
      this.busy = '';
    }
  }

  private open(p: Pending, changes: string[]): void {
    this.pending = p;
    this.changes = changes;
    void this.updateComplete.then(() => this.dialog?.showModal());
  }

  private dialogTitle(p: Pending): string {
    switch (p.kind) {
      case 'part':
        return planTitle(p.plan);
      case 'backend':
        return `${p.act === 'remove' ? 'Remove' : p.act === 'disable' ? 'Disable' : 'Enable'} ${p.row.name}`;
      case 'mcp':
        return `${p.act === 'remove' ? 'Remove' : 'Disable'} ${p.server.name}`;
      case 'engine':
        return `${p.alias}: ${p.to === 'engine' ? 'bare engine' : 'chat mode'}`;
    }
  }

  private async apply(): Promise<void> {
    const p = this.pending;
    if (!p) return;
    this.busy = 'apply';
    this.err = '';
    try {
      if (p.kind === 'part') {
        const r = await fleet.switchPart(p.plan.part, p.plan.on, p.plan.confirm ?? '');
        if (isParts(r)) {
          this.parts = r;
          this.msg = doneWords(r) ?? '';
          void this.loadMcp(r);
        }
      } else if (p.kind === 'backend') {
        if (p.act === 'remove') await config.remove(p.row.id);
        else await config.update(p.row.id, { enabled: p.act === 'enable' });
        this.msg = `${p.row.name}: ${p.act === 'remove' ? 'removed' : p.act === 'disable' ? 'disabled' : 'enabled'}.`;
      } else if (p.kind === 'mcp') {
        const path = `${MCP_PATH}/${encodeURIComponent(p.server.id)}`;
        if (p.act === 'remove') await request<void>(path, { method: 'DELETE' }, 'none');
        else await request<McpServer>(path, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled_global: false }) });
        this.msg = `${p.server.name}: ${p.act === 'remove' ? 'removed' : 'disabled'}.`;
      } else {
        const m = await enginesApi.setMode(p.id, p.to);
        this.modes = { ...this.modes, [p.id]: m.mode };
        this.msg = m.changed ? `${p.alias}: ${p.to === 'engine' ? 'bare engine' : 'chat mode'}; it restarts once, when idle.` : `${p.alias} is in that mode already.`;
      }
      this.dialog?.close();
      void this.poller?.refresh();
    } catch (e) {
      if (await this.refused(e)) this.dialog?.close();
      else this.err = describeError(e).message;
    } finally {
      this.busy = '';
    }
  }

  // ---- the panel --------------------------------------------------------------------------------

  private partCard(r: PartRow): TemplateResult {
    return html`<li class="app-perm" data-part=${r.id} data-on=${r.on}>
      <div class="app-perm-head"><b>${r.title}</b><span class="app-state is-${r.tone}">${r.badge}</span></div>
      <p class="small">${r.why}</p>
      <p class="small muted" data-part-detail>${r.detail}</p>
      <p class="xs faint">${r.source}</p>
      <div class="app-acts"><button type="button" class="btn btn-sm ${r.on ? '' : 'btn-primary'}" data-act=${`part-${r.id}`} ?disabled=${this.busy !== ''} @click=${() => void this.ask(r)}>${this.busy === `plan-${r.id}` ? 'Reading the plan…' : r.action}</button></div>
    </li>`;
  }

  private engineList(p: AppParts): TemplateResult {
    if (!p.engines.length) return html`<p class="small muted" data-engines-none>${ENGINE_MODE_NOTE} No engine host is paired over SSH here: switch one on its own host with <code class="app-cmd">tandem remote mode set engine</code> or <code class="app-cmd">chat</code>.</p>`;
    return html`<p class="small muted">${ENGINE_MODE_NOTE}</p>
      <ul class="app-parts-list" data-engines>${p.engines.map((e) => {
        const mode = this.modes[e.id];
        const to = mode === 'chat' ? 'engine' : 'chat';
        return html`<li data-engine=${e.id}><span><b>${e.alias}</b> <span class="small muted">${mode == null ? 'mode not read' : mode === 'chat' ? 'chat mode' : 'bare engine'}</span></span>
          ${mode == null ? nothing : html`<button type="button" class="btn btn-sm btn-ghost" data-act="engine-mode" ?disabled=${this.busy !== ''} @click=${() => this.open({ kind: 'engine', id: e.id, alias: e.alias, to }, engineModeChanges(e.alias, to))}>${to === 'engine' ? 'Bare engine…' : 'Chat mode…'}</button>`}</li>`;
      })}</ul>`;
  }

  private backendList(p: AppParts): TemplateResult {
    const rows = backendRows(p);
    if (!rows.length) return html`<p class="small muted" data-backends-none>None. The engines you pair serve through the gateway; other servers are added on the API page.</p>`;
    return html`<ul class="app-parts-list" data-backends>${rows.map(
      (b) => html`<li data-backend=${b.id}><span><b>${b.name}</b> <span class="small muted">${b.kind}${b.enabled ? '' : ', disabled'}</span></span>
        ${b.env
          ? html`<span class="xs muted">declared in the env file (TANDEM_BACKENDS): changed there</span>`
          : html`<span class="app-parts-acts">${b.acts.map((a) => html`<button type="button" class="btn btn-sm ${a === 'remove' ? 'btn-ghost app-danger' : 'btn-ghost'}" data-act=${`backend-${a}`} ?disabled=${this.busy !== ''} @click=${() => this.open({ kind: 'backend', row: b, act: a }, backendChanges(b, a))}>${a === 'remove' ? 'Remove…' : a === 'disable' ? 'Disable…' : 'Enable…'}</button>`)}</span>`}
      </li>`,
    )}</ul>`;
  }

  private mcpList(p: AppParts): TemplateResult {
    if (!p.mcp.available) return html`<p class="small muted" data-mcp-none>MCP servers belong to Chat, and Chat is off: none runs.</p>`;
    const list = this.mcp;
    if (!list) return html`<p class="small muted">Reading Chat’s MCP servers…</p>`;
    if (!list.length) return html`<p class="small muted" data-mcp-none>None. Chat’s settings add them.</p>`;
    return html`<ul class="app-parts-list" data-mcp>${list.map(
      (m) => html`<li data-mcp-server=${m.id}><span><b>${m.name}</b> <span class="small muted">${m.transport}, ${m.status}${m.enabled_global === false ? ', off by default' : ''}</span></span>
        <span class="app-parts-acts">${m.enabled_global === false ? nothing : html`<button type="button" class="btn btn-sm btn-ghost" data-act="mcp-disable" ?disabled=${this.busy !== ''} @click=${() => this.open({ kind: 'mcp', server: m, act: 'disable' }, mcpChanges(m, 'disable'))}>Disable…</button>`}
        <button type="button" class="btn btn-sm btn-ghost app-danger" data-act="mcp-remove" ?disabled=${this.busy !== ''} @click=${() => this.open({ kind: 'mcp', server: m, act: 'remove' }, mcpChanges(m, 'remove'))}>Remove…</button></span>
      </li>`,
    )}</ul>`;
  }

  render() {
    const p = this.parts;
    if (!p) return this.err ? html`<p class="small err-ink">${this.err}</p>` : nothing;
    const pend = this.pending;
    return html`<section class="panel" data-panel="parts">
        <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">What runs here</h2><p class="panel-sub">A new TandemApp runs only the gateway in front of the engines it pairs. Switch on what you want; each switch says what it changes first.</p></div></header>
        <ul class="app-perms" data-parts>${partRows(p).map((r) => this.partCard(r))}</ul>
        <h3 class="eyebrow app-parts-h">Engine hosts’ own mode</h3>
        ${this.engineList(p)}
        <h3 class="eyebrow app-parts-h">Connected</h3>
        <p class="small"><b>Backends</b></p>
        ${this.backendList(p)}
        <p class="small"><b>MCP servers</b></p>
        ${this.mcpList(p)}
        ${this.signIn ? signInCard('These switches') : nothing}
        ${this.msg ? html`<p class="small ok-ink" role="status" data-parts-done>${this.msg}</p>` : nothing}
        ${this.err ? html`<p class="small err-ink" role="alert" data-parts-error>${this.err}</p>` : nothing}
      </section>
      <dialog class="act-dialog" aria-labelledby="part-title" data-dialog="part" @close=${() => ((this.pending = null), (this.changes = []))}>
        <form method="dialog" class="act-body" @submit=${(e: Event) => (e.preventDefault(), void this.apply())}>
          <header class="act-head"><h2 id="part-title">${pend ? this.dialogTitle(pend) : ''}</h2></header>
          <section class="act-sec"><h3 class="eyebrow">What changes</h3><ul data-changes>${this.changes.map((c) => html`<li>${c}</li>`)}</ul></section>
          ${this.err && pend ? html`<p class="small err-ink" role="alert">${this.err}</p>` : nothing}
          <footer class="act-foot">
            <button type="button" class="btn btn-ghost" data-act="part-cancel" @click=${() => this.dialog?.close()}>Cancel</button>
            <button type="submit" class="btn btn-primary" data-act="part-go" ?disabled=${this.busy === 'apply'}>${this.busy === 'apply' ? 'Working…' : pend ? this.dialogTitle(pend) : 'Go'}</button>
          </footer>
        </form>
      </dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-app-parts': QseAppParts;
  }
}
