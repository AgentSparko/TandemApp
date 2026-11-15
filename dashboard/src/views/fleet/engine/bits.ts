// The parts the Add engine pages share (VIS-278): a finding's card (the controller's sentence, its
// fix, the facts worth a line, the buttons that go with it), a sentence with its commands set as
// code, a fingerprint to compare (by eye, or by pasting the terminal's own), and the slip: the three
// things the engine host printed, as rows the page fills in beside the terminal. Templates only,
// no element and no state, so they render in a test without a browser.

import { html, nothing, type TemplateResult } from 'lit';
import type { EngineFinding } from '../../../api/engines';
import { compareFingerprint, factsOf, keyType, segments, toneOf, type Compare } from '../../../lib/engine-words';
import { icon } from '../../../ui/bits';

/** A copy button that is safe inside a form (it never submits it). */
export function copyBtn(text: string, label: string): TemplateResult {
  return html`<button type="button" class="btn btn-ghost btn-sm eh-copy" aria-label=${label} title=${label} data-copy
    @click=${async (e: Event) => {
      const b = e.currentTarget as HTMLButtonElement;
      try {
        await navigator.clipboard.writeText(text);
        b.classList.add('is-done');
        setTimeout(() => b.classList.remove('is-done'), 1200);
      } catch {
        /* the clipboard is blocked: the text is on the page to select */
      }
    }}>${icon('copy')}<span class="copy-done">${icon('check')}</span></button>`;
}

/** A sentence of the controller's, its `commands` as code. */
export function sentence(text: string): TemplateResult {
  return html`${segments(text).map((s) => (s.code ? html`<code>${s.text}</code>` : s.text))}`;
}

/** What went wrong and what to do about it: the sentence, the fix, the facts, then the buttons.
 *  An error is announced at once; a notice that waits for the user is not. */
export function findingCard(f: EngineFinding, o: { actions?: TemplateResult | typeof nothing; wait?: number; quiet?: boolean } = {}): TemplateResult {
  const tone = toneOf(f);
  const facts = factsOf(f);
  return html`<div class="eh-finding is-${tone}" role=${tone === 'err' && !o.quiet ? 'alert' : 'status'} data-finding=${f.code}>
    <span class="eh-finding-g" aria-hidden="true">${icon(tone === 'info' ? 'info' : 'warn')}</span>
    <div class="eh-finding-b">
      <p class="eh-finding-m">${sentence(f.message)}</p>
      <p class="eh-finding-f">${sentence(f.fix)}</p>
      ${facts.length
        ? html`<dl class="eh-facts">${facts.map((x) => html`<div><dt>${x.label}</dt><dd class=${x.mono ? 'num eh-wrap' : ''}>${x.value}</dd></div>`)}</dl>`
        : nothing}
      ${o.wait ? html`<p class="small muted" data-wait>You can try again in ${o.wait} s.</p>` : nothing}
      ${o.actions && o.actions !== nothing ? html`<div class="eh-actions">${o.actions}</div>` : nothing}
    </div>
  </div>`;
}

const COMPARE: Record<Exclude<Compare, 'empty'>, { cls: string; text: string }> = {
  same: { cls: 'ok-ink', text: 'The same. This is the engine host you mean.' },
  different: { cls: 'err-ink', text: 'Different. Stop here: something else answers at this address.' },
  unreadable: { cls: 'muted', text: 'No fingerprint in that text yet. It starts with SHA256: and is 50 characters long.' },
};

/** A fingerprint to compare with the one the engine host printed: shown whole, copyable, and a
 *  field that takes the terminal's own and says whether the two are the same. */
export function fingerprint(fp: string | null, o: { type?: string | null; pasted?: string; onPaste?: (text: string) => void; id?: string } = {}): TemplateResult {
  if (!fp) return html`<p class="small muted" data-fp="none">This engine shows no fingerprint here. The code still proves the machine: a wrong machine cannot answer it.</p>`;
  const cmp = compareFingerprint(fp, o.pasted ?? '');
  const id = o.id ?? 'eh-fp';
  return html`<div class="eh-fp" data-fp=${fp} data-compare=${cmp}>
    <div class="eh-fp-v"><span class="num eh-wrap" id=${id}>${o.type ? html`<span class="eh-fp-t">${keyType(o.type)}</span> ` : nothing}${fp}</span>${copyBtn(fp, 'Copy the fingerprint')}</div>
    ${o.onPaste
      ? html`<label class="eh-fp-paste"><span>Paste the terminal’s fingerprint to compare (optional)</span>
            <input class="num" data-in="fp-paste" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="SHA256:…" .value=${o.pasted ?? ''} aria-describedby="${id}-cmp" @input=${(e: Event) => o.onPaste!((e.target as HTMLInputElement).value)} /></label>
          <p class="small ${cmp === 'empty' ? 'muted' : COMPARE[cmp].cls}" id="${id}-cmp" role="status">${cmp === 'empty' ? 'Or compare the two by eye: every character counts.' : COMPARE[cmp].text}</p>`
      : nothing}
  </div>`;
}

export interface SlipRow {
  /** what the terminal calls it (a <label> when the value is a field) */
  label: TemplateResult | string;
  /** the value, a field, or the placeholder that stands for it */
  value: TemplateResult | string;
  /** what the user does with it now */
  hint?: TemplateResult | string;
  /** the row the page waits for */
  now?: boolean;
  /** the page holds this one already */
  have?: boolean;
  id: 'code' | 'fingerprint' | 'address';
}

/** The slip: the engine host's last printed lines as rows, the same three on both screens. The
 *  page fills in what it has; the row it waits for is marked. */
export function slip(rows: SlipRow[], o: { caption: string }): TemplateResult {
  return html`<div class="eh-slip" role="group" aria-label=${o.caption}>
    <div class="eh-slip-h" aria-hidden="true"><span class="eh-slip-p">$</span><span>${o.caption}</span></div>
    <dl class="eh-slip-rows">${rows.map(
      (r) => html`<div class="eh-slip-row ${r.now ? 'is-now' : ''} ${r.have ? 'is-have' : ''}" data-slip=${r.id}>
        <dt>${r.label}</dt>
        <dd class="eh-slip-v">${r.value}</dd>
        ${r.hint ? html`<dd class="eh-slip-hint">${r.hint}</dd>` : nothing}
      </div>`,
    )}</dl>
  </div>`;
}

/** a placeholder for a value only the terminal has: dots in its shape, read as "shown there" */
export function ghost(shape: string, says: string): TemplateResult {
  return html`<span class="eh-ghost" aria-hidden="true">${shape}</span><span class="sr-only">${says}</span>`;
}

/** A command to run on the engine host, with a copy button. */
export function command(text: string): TemplateResult {
  return html`<span class="eh-cmd"><code>${text}</code>${copyBtn(text, `Copy ${text}`)}</span>`;
}
