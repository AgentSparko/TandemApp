// What the hooks pages share (VIS-282): their links, a role's mark, a chip, a refusal of the hooks'
// API as the settings pages draw theirs, a field with its error, the bar that shows how much of a
// time budget is used, a copy button, and the block that shows a secret once with the code that
// checks a signature. Templates only, so they render in a test without a browser.

import { html, nothing, svg, type TemplateResult } from 'lit';
import type { HookError, Role } from '../../api/hooks';
import type { Confirm } from '../../lib/hooks-flow';
import { ROLE_WORDS, VERIFY, hookErrorWords, ms, type Use } from '../../lib/hooks';
import type { Tone } from '../../lib/settings-words';
import { icon } from '../../ui/bits';

const path = (root: string, parts: string[], q?: Record<string, string>): string => {
  const s = q ? new URLSearchParams(q).toString() : '';
  return `#/app/hooks${root}${parts.map((p) => `/${encodeURIComponent(p)}`).join('')}${s ? `?${s}` : ''}`;
};
/** the hooks pages: the pipeline, `new`, `hook/<id>`, `trace` */
export const hooksHref = (parts: string[] = [], q?: Record<string, string>): string => path('', parts, q);
/** the decision providers' pages: the list, `new`, `p/<id>` */
export const providersHref = (parts: string[] = []): string => path('/providers', parts);

export type Page = 'pipeline' | 'providers' | 'trace';

/** The pages of the Hooks area, and what can be added from the one that shows. */
export function hooksNav(on: Page, o: { hooks?: number; providers?: number } = {}): TemplateResult {
  const tab = (id: Page, label: string, href: string, n?: number) => html`<a class="hk-tab ${on === id ? 'is-on' : ''}" href=${href} aria-current=${on === id ? 'page' : nothing} data-hooks-tab=${id}>${label}${n != null ? html`<span class="hk-tab-n num">${n}</span>` : nothing}</a>`;
  return html`<nav class="hk-nav" aria-label="Hooks pages">
    <div class="hk-tabs">${tab('pipeline', 'Pipeline', hooksHref(), o.hooks)}${tab('providers', 'Decision providers', providersHref(), o.providers)}${tab('trace', 'Trace', hooksHref(['trace']))}</div>
    ${on === 'providers'
      ? html`<a class="btn btn-primary" href=${providersHref(['new'])} data-act="provider-new">${icon('plus')} Add a provider</a>`
      : html`<a class="btn btn-primary" href=${hooksHref(['new'])} data-act="hook-new">${icon('plus')} Add a hook</a>`}
  </nav>`;
}

export const back = (href: string, label: string): TemplateResult => html`<a class="hk-back small" href=${href} data-act="back">${icon('left')}<span>${label}</span></a>`;

export function chip(tone: Tone | 'quiet', text: string, o: { title?: string; data?: string } = {}): TemplateResult {
  return html`<span class="st-chip is-${tone === 'muted' ? 'quiet' : tone}" title=${o.title ?? nothing} data-chip=${o.data ?? tone}>${text}</span>`;
}

const g = (p: TemplateResult) => svg`<svg class="hk-role-g" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${p}</g></svg>`;
/** a role's mark: what it does to the value, as a shape (the word stands beside it) */
const ROLE_GLYPH: Record<Role, TemplateResult> = {
  // a value that comes out changed
  transform: g(svg`<path d="M2 5h7l-2-2M14 11H7l2 2"/>`),
  // a gate
  guard: g(svg`<path d="M8 2 3 4v4c0 3 2.2 5 5 6 2.8-1 5-3 5-6V4z"/>`),
  // one way in, two ways out
  decider: g(svg`<path d="M2 8h5M7 8l5-4M7 8l5 4M12 4h2M12 12h2"/>`),
  // an eye beside the path
  observer: g(svg`<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="1.8"/>`),
};
export const roleMark = (role: Role): TemplateResult => html`<span class="hk-role is-${role}" data-role=${role}>${ROLE_GLYPH[role]}<span>${ROLE_WORDS[role].label}</span></span>`;

/** A refusal of the hooks' or the providers' API: the sentence, what to do, the controller's words. */
export function refused(e: Pick<HookError, 'type' | 'message' | 'facts'>, o: { quiet?: boolean } = {}): TemplateResult {
  const w = hookErrorWords(e);
  return html`<div class="st-refusal" role=${o.quiet ? 'status' : 'alert'} data-error=${e.type}>
    <span aria-hidden="true">${icon('warn')}</span>
    <div><p class="st-refusal-t">${w.title}</p>${w.fix ? html`<p class="small">${w.fix}</p>` : nothing}${w.detail ? html`<p class="xs muted st-wrap">${w.detail}</p>` : nothing}</div>
  </div>`;
}

/** The second question before a full payload leaves the LAN: where it goes, and the two ways on. */
export function confirmLeave(c: Confirm, o: { busy: boolean; go: string; onGo: () => void; onBack: () => void }): TemplateResult {
  return html`<section class="rs-second" role="alert" data-confirm-needed>
    <h3>${icon('warn')} Prompts and answers leave the LAN</h3>
    <p class="small">This hook would get the full payload: every prompt and answer as it is, sent to <span class="num">${c.destination}</span>, outside the LAN.</p>
    <p class="small">TandemApp keeps who granted it and when. Go on only if that destination may read them.</p>
    <div class="st-acts">
      <button type="button" class="btn btn-ghost" data-act="confirm-back" ?disabled=${o.busy} @click=${o.onBack}>Back</button>
      <button type="button" class="btn rs-go-warn" data-act="confirm-go" ?disabled=${o.busy} @click=${o.onGo}>${o.busy ? 'Sending…' : o.go}</button>
    </div>
  </section>`;
}

/** A labelled field with the sentence under it: what it is for, or what is wrong with it. */
export function field(id: string, label: string, control: TemplateResult, o: { hint?: string | TemplateResult; error?: string; wide?: boolean } = {}): TemplateResult {
  return html`<div class="hk-field ${o.wide ? 'is-wide' : ''} ${o.error ? 'is-bad' : ''}" data-field=${id}>
    <label class="hk-label" for="hk-${id}">${label}</label>
    ${control}
    ${o.error ? html`<p class="st-err small" role="alert" id="hk-${id}-e" data-invalid>${o.error}</p>` : o.hint ? html`<p class="xs muted" id="hk-${id}-e">${o.hint}</p>` : nothing}
  </div>`;
}

export function textInput(id: string, value: string, on: (v: string) => void, o: { placeholder?: string; mono?: boolean; bad?: boolean; type?: string; mode?: string; blur?: () => void } = {}): TemplateResult {
  return html`<input id="hk-${id}" class="st-input ${o.mono ? 'num' : ''}" type=${o.type ?? 'text'} inputmode=${o.mode ?? nothing} data-in=${id} spellcheck="false" autocapitalize="off" autocomplete="off" placeholder=${o.placeholder ?? ''} aria-invalid=${!!o.bad} aria-describedby="hk-${id}-e"
    .value=${value} @input=${(e: Event) => on((e.target as HTMLInputElement).value)} @blur=${o.blur ?? nothing} />`;
}

/** A choice of a few cards, one of them on: what each means stands on it. */
export function cards<T extends string>(name: string, value: T | '', options: { id: T; label: string; text: string; note?: string; off?: string }[], on: (v: T) => void, o: { label: string; staged?: boolean } = { label: '' }): TemplateResult {
  return html`<div class="hk-cards" role="radiogroup" aria-label=${o.label} data-cards=${name}>
    ${options.map(
      (x) => html`<label class="hk-card ${value === x.id ? 'is-on' : ''} ${x.off ? 'is-off' : ''}" data-card=${x.id}>
        <input type="radio" name="hk-${name}" .checked=${value === x.id} ?disabled=${!!x.off} @change=${() => on(x.id)} />
        <b>${x.label}</b>
        <span class="small muted">${x.text}</span>
        ${x.note ? html`<span class="xs hk-card-note">${x.note}</span>` : nothing}
        ${x.off ? html`<span class="xs muted">${x.off}</span>` : nothing}
      </label>`,
    )}
  </div>`;
}

/** How much of a time budget the hooks on the path use: a segment a hook, by its p95. The fill
 *  runs from cobalt to signal orange over the budget's length, so it warms as the budget fills. */
export function useBar(u: Use, o: { label: string }): TemplateResult {
  const scale = Math.max(u.budget_ms, u.used_ms) || 1;
  return html`<div class="hk-use ${u.over ? 'is-over' : ''}" role="img" aria-label=${`${o.label}: ${ms(u.used_ms)} of ${u.budget_ms} ms`} data-use=${u.used_ms} data-budget=${u.budget_ms}>
    <div class="hk-use-track">
      ${u.used_ms > 0
        ? html`<div class="hk-use-fill" style="width:${((u.used_ms / scale) * 100).toFixed(2)}%;background-size:${((u.budget_ms / u.used_ms) * 100).toFixed(1)}% 100%">
            ${u.parts.map((p) => html`<span class="hk-use-seg" style="flex:${p.ms}" title=${`${p.name}: p95 ${ms(p.ms)}`}></span>`)}
          </div>`
        : nothing}
      ${u.over ? html`<span class="hk-use-mark" style="left:${((u.budget_ms / scale) * 100).toFixed(2)}%"></span>` : nothing}
    </div>
  </div>`;
}

/** A copy button that never submits a form. */
export function copyBtn(text: string, label: string): TemplateResult {
  return html`<button type="button" class="btn btn-sm" aria-label=${label} data-copy
    @click=${async (e: Event) => {
      const b = e.currentTarget as HTMLButtonElement;
      try {
        await navigator.clipboard.writeText(text);
        b.classList.add('is-done');
        setTimeout(() => b.classList.remove('is-done'), 1400);
      } catch {
        /* the clipboard is blocked: the text is on the page to select */
      }
    }}>${icon('copy')}<span class="copy-done">${icon('check')}</span><span>Copy</span></button>`;
}

/** A secret, shown this once: the value to copy, what to do with it, and the code that checks a
 *  call's signature with it. */
export function secretOnce(secret: string, lang: 'python' | 'node', o: { onLang: (l: 'python' | 'node') => void; note: string }): TemplateResult {
  return html`<section class="hk-secret" data-secret-once>
    <h3>${icon('info')} Copy the signing secret now</h3>
    <p class="small">${o.note} TandemApp stores it sealed and never shows it again. If it is lost, rotate it for a new one.</p>
    <div class="hk-secret-v"><code class="num" data-secret-value>${secret}</code>${copyBtn(secret, 'Copy the secret')}</div>
    <p class="small">Every call carries <code>X-Tandem-Timestamp</code> and <code>X-Tandem-Signature: v1=…</code>, the HMAC-SHA256 of the timestamp, a dot and the body, keyed with this secret. Check it on the raw bytes before you parse them:</p>
    <div class="hk-code">
      <div class="hk-code-h">
        <div class="seg" role="group" aria-label="Language">
          ${(['python', 'node'] as const).map((l) => html`<button type="button" class="seg-btn ${lang === l ? 'is-on' : ''}" aria-pressed=${lang === l} data-lang=${l} @click=${() => o.onLang(l)}>${l === 'python' ? 'Python' : 'Node'}</button>`)}
        </div>
        ${copyBtn(VERIFY[lang], 'Copy the code')}
      </div>
      <pre class="hk-pre" data-verify=${lang}><code>${VERIFY[lang]}</code></pre>
    </div>
    <p class="xs muted">Also refuse a delivery id you have seen within five minutes. To try it without writing a receiver: <code>python contracts/hooks-v1/fake_server.py --port 9300 --secret &lt;the secret&gt;</code></p>
  </section>`;
}

/** two figures side by side: what it is and how much */
export function fact(label: string, value: TemplateResult | string, o: { tone?: Tone; sub?: string; data?: string } = {}): TemplateResult {
  return html`<div class="hk-fact ${o.tone ? `is-${o.tone}` : ''}" data-fact=${o.data ?? label}>
    <dt>${label}</dt>
    <dd>${value}</dd>
    ${o.sub ? html`<dd class="xs muted hk-fact-sub">${o.sub}</dd>` : nothing}
  </div>`;
}
