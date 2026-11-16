// What the pages under Settings share beyond the settings pages' own rows (VIS-282, VIS-284): the
// way back to a list, a chip, a field with its error, a text input, a choice of cards, a copy
// button and a figure with its label. The hooks pages and the plugins pages both draw with them,
// so they sit apart from what only the hooks pages need. Templates only, so they render in a test
// without a browser.

import { html, nothing, type TemplateResult } from 'lit';
import type { Tone } from '../../lib/settings-words';
import { icon } from '../../ui/bits';

export const back = (href: string, label: string): TemplateResult => html`<a class="hk-back small" href=${href} data-act="back">${icon('left')}<span>${label}</span></a>`;

export function chip(tone: Tone | 'quiet', text: string, o: { title?: string; data?: string } = {}): TemplateResult {
  return html`<span class="st-chip is-${tone === 'muted' ? 'quiet' : tone}" title=${o.title ?? nothing} data-chip=${o.data ?? tone}>${text}</span>`;
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

/** two figures side by side: what it is and how much */
export function fact(label: string, value: TemplateResult | string, o: { tone?: Tone; sub?: string; data?: string } = {}): TemplateResult {
  return html`<div class="hk-fact ${o.tone ? `is-${o.tone}` : ''}" data-fact=${o.data ?? label}>
    <dt>${label}</dt>
    <dd>${value}</dd>
    ${o.sub ? html`<dd class="xs muted hk-fact-sub">${o.sub}</dd>` : nothing}
  </div>`;
}
