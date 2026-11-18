// <qse-token-dialog> (SRV-889): the smart dialog that makes an API key. Four steps: what the key is
// for (a purpose fills in its rights and name), what it may run (every backend and model, or a
// picker of backends with their models), how long it lives and its limits, and a review that says
// in one sentence what the key will do. The made key shows once, with the setup for its purpose.
// Opened by the Access page (Create a token) and the API page (Endpoint and keys).

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, state } from 'lit/decorators.js';
import { auth, LIMIT_KEYS, type Me, type TokenOnce } from '../../api/auth';
import { config } from '../../api/config';
import { DAY_CHOICES, LIMIT_WORDS, can, scopeLabel, scopesFor } from '../../lib/access';
import { PURPOSES, catalogOf, everything, firstModel, planBody, planSummary, purposeDef, runCounts, runError, setupFor, type CatalogBackend, type Plan, type Purpose } from '../../lib/token-plan';
import { LightElement } from '../../ui/base';
import { copyButton, icon } from '../../ui/bits';
import '../../styles/token-dialog.css';

type Step = 'purpose' | 'run' | 'life' | 'review' | 'made';
const STEPS: { id: Step; label: string }[] = [
  { id: 'purpose', label: 'For' },
  { id: 'run', label: 'Models' },
  { id: 'life', label: 'Expiry' },
  { id: 'review', label: 'Review' },
];

@customElement('qse-token-dialog')
export class TokenDialog extends LightElement {
  @state() private step: Step = 'purpose';
  @state() private plan: Plan = this.fresh();
  @state() private me: Me | null = null;
  @state() private cat: CatalogBackend[] | null = null;
  @state() private catError: string | null = null;
  @state() private error: string | null = null;
  @state() private busy = false;
  @state() private made: TokenOnce | null = null;
  @state() private filter = '';
  @state() private open = new Set<string>();

  private fresh(): Plan {
    return { purpose: 'agent', label: 'coding agent', scopes: ['inference:openai'], run: everything(), days: '90', limits: {} };
  }

  /** open the dialog for `me` (the page's session answer) */
  async show(me: Me): Promise<void> {
    this.me = me;
    this.plan = this.fresh();
    this.step = 'purpose';
    this.error = null;
    this.made = null;
    this.filter = '';
    this.open = new Set();
    await this.updateComplete;
    const d = this.querySelector<HTMLDialogElement>('dialog');
    if (d && !d.open) d.showModal();
    // the name has the focus first, not the close button
    d?.querySelector<HTMLInputElement>('input[name="label"]')?.focus();
    if (this.cat === null) void this.loadCatalog();
  }

  private async loadCatalog(): Promise<void> {
    try {
      const o = await config.overview();
      this.cat = catalogOf(o.backends);
      this.catError = null;
    } catch (e) {
      this.cat = [];
      this.catError = `The backends could not be read (${e instanceof Error ? e.message : String(e)}); the key can still run every backend or name patterns.`;
    }
  }

  private close(): void {
    this.querySelector<HTMLDialogElement>('dialog')?.close();
  }

  /** Enter in a text field goes on: Next, or Make the key on the review */
  private onKey(e: KeyboardEvent): void {
    const t = e.target as HTMLElement;
    if (e.key !== 'Enter' || !(t instanceof HTMLInputElement) || t.type === 'checkbox' || this.step === 'made') return;
    e.preventDefault();
    if (this.step === 'review') void this.create();
    else this.next();
  }

  /** a closed dialog holds no key: the made key and its setup leave the page at once */
  private forget(): void {
    this.made = null;
    this.step = 'purpose';
    this.error = null;
  }

  private set(p: Partial<Plan>): void {
    this.plan = { ...this.plan, ...p };
    this.error = null;
  }

  private pickPurpose(id: Purpose): void {
    const d = purposeDef(id);
    const allowed = scopesFor(this.me).map((s) => s.id);
    const named = PURPOSES.some((p) => p.name === this.plan.label) || !this.plan.label;
    this.set({ purpose: id, scopes: d.scopes.filter((s) => allowed.includes(s)), label: named ? d.name : this.plan.label });
  }

  private runs(): boolean {
    return this.plan.scopes.some((s) => s.startsWith('inference:'));
  }

  private next(): void {
    const order: Step[] = this.runs() ? ['purpose', 'run', 'life', 'review'] : ['purpose', 'life', 'review'];
    if (this.step === 'purpose') {
      if (!this.plan.label.trim()) return void (this.error = 'Give the key a name.');
      if (!this.plan.scopes.length) return void (this.error = 'Pick at least one thing the key may do.');
    }
    if (this.step === 'run') {
      const e = runError(this.plan.run);
      if (e) return void (this.error = e);
    }
    const i = order.indexOf(this.step);
    this.step = order[Math.min(i + 1, order.length - 1)];
    this.error = null;
  }
  private back(): void {
    const order: Step[] = this.runs() ? ['purpose', 'run', 'life', 'review'] : ['purpose', 'life', 'review'];
    this.step = order[Math.max(order.indexOf(this.step) - 1, 0)];
    this.error = null;
  }

  private async create(): Promise<void> {
    const body = planBody(this.plan, scopesFor(this.me).map((s) => s.id), can(this.me, 'users'));
    if (typeof body === 'string') return void (this.error = body);
    this.busy = true;
    try {
      this.made = await auth.createToken(body);
      this.step = 'made';
      this.dispatchEvent(new CustomEvent('token-made', { detail: this.made, bubbles: true, composed: true }));
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
    } finally {
      this.busy = false;
    }
  }

  // ---- the backend and model picker ----------------------------------------------------------------
  private toggleBackend(id: string, on: boolean): void {
    const b = { ...this.plan.run.backends };
    if (on) b[id] = 'all';
    else delete b[id];
    this.set({ run: { ...this.plan.run, backends: b } });
  }
  private toggleModel(c: CatalogBackend, m: string, on: boolean): void {
    const b = { ...this.plan.run.backends };
    const cur = b[c.id] === 'all' ? [...c.models] : [...((b[c.id] as string[] | undefined) ?? [])];
    const next = on ? [...new Set([...cur, m])] : cur.filter((x) => x !== m);
    if (next.length === 0) delete b[c.id];
    else b[c.id] = next.length === c.models.length ? 'all' : next;
    this.set({ run: { ...this.plan.run, backends: b } });
  }
  private toggleOpen(id: string): void {
    const s = new Set(this.open);
    if (s.has(id)) s.delete(id);
    else s.add(id);
    this.open = s;
  }

  private pickerView(): TemplateResult {
    const r = this.plan.run;
    const q = this.filter.trim().toLowerCase();
    const cat = (this.cat ?? []).filter((c) => !q || c.label.toLowerCase().includes(q) || c.id.includes(q) || c.models.some((m) => m.toLowerCase().includes(q)));
    const counts = runCounts(r, this.cat ?? []);
    return html`<div class="tk-run">
      <div class="seg tk-seg" role="radiogroup" aria-label="What the key may run">
        <button type="button" role="radio" class="seg-btn ${r.every ? 'is-on' : ''}" aria-checked=${r.every} data-run="every" @click=${() => this.set({ run: { ...r, every: true } })}>Every backend and model</button>
        <button type="button" role="radio" class="seg-btn ${r.every ? '' : 'is-on'}" aria-checked=${!r.every} data-run="pick" @click=${() => this.set({ run: { ...r, every: false } })}>Only the ones I pick</button>
      </div>
      ${r.every
        ? html`<p class="small muted">The key runs every model this TandemApp serves now, and every one added later.</p>`
        : html`${this.cat === null ? html`<p class="small muted" data-loading>Reading the backends…</p>` : nothing}
            ${this.catError ? html`<p class="small err-ink">${this.catError}</p>` : nothing}
            ${this.cat && this.cat.length > 4 ? html`<input class="tk-filter" type="search" placeholder="Find a backend or model" .value=${this.filter} @input=${(e: Event) => (this.filter = (e.target as HTMLInputElement).value)} aria-label="Find a backend or model" />` : nothing}
            <ul class="tk-tree" data-picker>
              ${cat.map((c) => {
                const v = r.backends[c.id];
                const all = v === 'all';
                const some = Array.isArray(v) && v.length > 0;
                const open = this.open.has(c.id) || (!!q && c.models.some((m) => m.toLowerCase().includes(q)));
                return html`<li class="tk-b ${c.enabled ? '' : 'is-off'}" data-backend=${c.id}>
                  <div class="tk-brow">
                    <label class="ac-check"><input type="checkbox" .checked=${all} .indeterminate=${some} @change=${(e: Event) => this.toggleBackend(c.id, (e.target as HTMLInputElement).checked)} />
                      <span><b>${c.label}</b> <span class="muted small">${c.kind}${c.enabled ? '' : ', switched off'} · ${c.models.length} model${c.models.length === 1 ? '' : 's'}${all ? ', every one' : some ? `, ${(v as string[]).length} picked` : ''}</span></span></label>
                    ${c.models.length ? html`<button type="button" class="btn btn-ghost btn-sm" aria-expanded=${open} data-models=${c.id} @click=${() => this.toggleOpen(c.id)}>${open ? 'Hide models' : 'Pick models'}</button>` : nothing}
                  </div>
                  ${open
                    ? html`<ul class="tk-models">${c.models
                        .filter((m) => !q || m.toLowerCase().includes(q) || c.label.toLowerCase().includes(q))
                        .map((m) => html`<li><label class="ac-check"><input type="checkbox" data-model=${`${c.id}/${m}`} .checked=${all || (Array.isArray(v) && v.includes(m))} @change=${(e: Event) => this.toggleModel(c, m, (e.target as HTMLInputElement).checked)} /><span class="num">${m}</span></label></li>`)}</ul>`
                    : nothing}
                </li>`;
              })}
              ${this.cat && !cat.length ? html`<li class="small muted">${q ? 'Nothing matches.' : 'No backend yet: add one under API, Backends.'}</li>` : nothing}
            </ul>
            <p class="small tk-count" data-count>${counts.backends ? `${counts.backends} backend${counts.backends === 1 ? '' : 's'}, ${counts.models} model${counts.models === 1 ? '' : 's'}. A backend picked whole also takes the models it serves later.` : 'Nothing picked yet.'}</p>`}
      <details class="tk-adv" ?open=${!!this.plan.run.patterns}><summary class="small">Name patterns</summary>
        <label class="field field-block ac-field"><span>Model names</span><input name="patterns" class="num" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="e.g. small-*, deepseek-*" .value=${r.patterns} @input=${(e: Event) => this.set({ run: { ...r, patterns: (e.target as HTMLInputElement).value } })} /><small class="muted">Optional. Names or patterns the client sends, separated by commas; a pattern may also name a backend, such as <span class="num">openrouter/anthropic/*</span>.</small></label>
      </details>
    </div>`;
  }

  // ---- the steps ----------------------------------------------------------------------------------
  private purposeView(): TemplateResult {
    const allowed = scopesFor(this.me);
    const ids = allowed.map((s) => s.id);
    const usable = PURPOSES.filter((p) => p.id === 'custom' || p.scopes.some((s) => ids.includes(s)));
    return html`<div class="tk-purposes" role="radiogroup" aria-label="What the key is for">
        ${usable.map((p) => html`<button type="button" role="radio" class="tk-purpose ${this.plan.purpose === p.id ? 'is-on' : ''}" aria-checked=${this.plan.purpose === p.id} data-purpose=${p.id} @click=${() => this.pickPurpose(p.id)}><b>${p.label}</b><span class="small muted">${p.text}</span></button>`)}
      </div>
      <label class="field field-block ac-field"><span>Name</span><input name="label" maxlength="64" autocomplete="off" spellcheck="false" .value=${this.plan.label} @input=${(e: Event) => this.set({ label: (e.target as HTMLInputElement).value })} /><small class="muted">What it is for, such as “laptop opencode”. Shown in usage and on the Access page.</small></label>
      <details class="tk-adv" ?open=${this.plan.purpose === 'custom'}><summary class="small">What it may do (${this.plan.scopes.length ? this.plan.scopes.map(scopeLabel).join(', ') : 'nothing yet'})</summary>
        <fieldset class="ac-set"><legend class="sr-only">Rights</legend>
          ${allowed.map((s) => html`<label class="ac-check"><input type="checkbox" name="scopes" value=${s.id} .checked=${this.plan.scopes.includes(s.id)} @change=${(e: Event) => this.set({ scopes: (e.target as HTMLInputElement).checked ? [...this.plan.scopes, s.id] : this.plan.scopes.filter((x) => x !== s.id) })} /><span><b>${s.label}</b> <span class="muted">${s.text}</span></span></label>`)}
        </fieldset>
      </details>`;
  }

  private lifeView(): TemplateResult {
    const admin = can(this.me, 'users');
    return html`<label class="field field-block ac-field"><span>Expires after</span>
        <select name="days" @change=${(e: Event) => this.set({ days: (e.target as HTMLSelectElement).value })}>${DAY_CHOICES.map((n) => html`<option value=${String(n)} ?selected=${String(n) === this.plan.days}>${n} days</option>`)}${admin ? html`<option value="never" ?selected=${this.plan.days === 'never'}>Never</option>` : nothing}</select></label>
      <details class="tk-adv"><summary class="small">Limits</summary>
        <p class="small muted">Empty: your role's limit, when an admin set one; never more than it. Cost in dollars, for models with a price.</p>
        <div class="ac-grid">${LIMIT_KEYS.map((k) => html`<label class="field field-block ac-field"><span>${LIMIT_WORDS[k]}</span><input name=${`limit.${k}`} class="num" inputmode="decimal" autocomplete="off" placeholder="none" .value=${this.plan.limits[k] ?? ''} @input=${(e: Event) => this.set({ limits: { ...this.plan.limits, [k]: (e.target as HTMLInputElement).value } })} /></label>`)}</div>
      </details>`;
  }

  private reviewView(): TemplateResult {
    const cat = this.cat ?? [];
    const r = this.plan.run;
    const picked = r.every ? [] : Object.keys(r.backends);
    return html`<p class="tk-summary" data-summary>${planSummary(this.plan, cat, scopeLabel)}</p>
      <dl class="tk-review">
        <div><dt>Name</dt><dd>${this.plan.label}</dd></div>
        <div><dt>May do</dt><dd>${this.plan.scopes.map(scopeLabel).join(', ')}</dd></div>
        ${this.runs()
          ? html`<div><dt>Runs</dt><dd>${r.every ? 'every backend and model' : html`<ul class="tk-picked">${picked.map((b) => {
              const c = cat.find((x) => x.id === b);
              const v = r.backends[b];
              return html`<li><b>${c?.label ?? b}</b>: ${v === 'all' ? 'every model' : html`<span class="num">${(v as string[]).join(', ')}</span>`}</li>`;
            })}</ul>`}${r.patterns.trim() ? html`<div class="small muted">patterns <span class="num">${r.patterns}</span></div>` : nothing}</dd></div>`
          : nothing}
        <div><dt>Expires</dt><dd>${this.plan.days === 'never' ? 'never' : `in ${this.plan.days} days`}</dd></div>
      </dl>`;
  }

  private madeView(t: TokenOnce): TemplateResult {
    const model = firstModel(this.plan, this.cat ?? []);
    return html`<p data-once><b>Copy the key now. It is shown this once.</b></p>
      <div class="ac-secret"><code class="num" data-secret="token">${t.token}</code>${copyButton(t.token, 'Copy the key')}</div>
      <p class="small muted">TandemApp keeps only its hash; a lost key is rotated or made again.</p>
      <dl class="ac-snips tk-setup">${setupFor(this.plan.purpose, location.origin, t.token, model).map((s) => html`<div><dt class="small muted">${s.label}</dt><dd><div class="ac-secret"><code class="num tk-pre">${s.value}</code>${copyButton(s.value, `Copy: ${s.label}`)}</div></dd></div>`)}</dl>`;
  }

  render() {
    const step = this.step;
    const order = this.runs() ? STEPS : STEPS.filter((s) => s.id !== 'run');
    const at = order.findIndex((s) => s.id === step);
    const body = step === 'purpose' ? this.purposeView() : step === 'run' ? this.pickerView() : step === 'life' ? this.lifeView() : step === 'review' ? this.reviewView() : this.made ? this.madeView(this.made) : nothing;
    const title = step === 'made' ? `${this.made?.label ?? 'The key'} is made` : 'New API key';
    return html`<dialog class="act-dialog tk-dialog" aria-labelledby="tk-t" data-dialog="token" @cancel=${(e: Event) => this.busy && e.preventDefault()} @close=${() => this.forget()} @keydown=${(e: KeyboardEvent) => this.onKey(e)}>
      <div class="act-body">
        <header class="act-head"><h2 id="tk-t">${title}</h2><button type="button" class="btn btn-ghost btn-sm" aria-label="Close" data-act="tk-close" @click=${() => this.close()}>${icon('x')}</button></header>
        ${step === 'made' ? nothing : html`<ol class="tk-steps" aria-label="Steps">${order.map((s, i) => html`<li class="${i === at ? 'is-on' : i < at ? 'is-done' : ''}" aria-current=${i === at ? 'step' : nothing}>${s.label}</li>`)}</ol>`}
        ${body}
        ${this.error ? html`<p class="login-error" role="alert" data-dialog-error>${this.error}</p>` : nothing}
        <footer class="act-foot">
          ${step === 'made'
            ? html`<button type="button" class="btn btn-primary" data-act="tk-done" @click=${() => this.close()}>I copied it</button>`
            : html`${at > 0 ? html`<button type="button" class="btn btn-ghost" data-act="tk-back" ?disabled=${this.busy} @click=${() => this.back()}>Back</button>` : html`<button type="button" class="btn btn-ghost" data-act="tk-cancel" @click=${() => this.close()}>Cancel</button>`}
                ${step === 'review'
                  ? html`<button type="button" class="btn btn-primary" data-act="tk-create" ?disabled=${this.busy} @click=${() => void this.create()}>${this.busy ? 'Making…' : 'Make the key'}</button>`
                  : html`<button type="button" class="btn btn-primary" data-act="tk-next" @click=${() => this.next()}>Next</button>`}`}
        </footer>
      </div>
    </dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-token-dialog': TokenDialog;
  }
}
