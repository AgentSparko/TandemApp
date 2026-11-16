// What the plugins pages share (VIS-284): a refusal of the plugins' API as the settings pages draw
// theirs, a plugin's state as a mark, a capability with what it means and how much it risks, what
// a plugin adds, and a sha256 that can be copied. Templates only, so they render in a test
// without a browser.

import { html, nothing, type TemplateResult } from 'lit';
import type { PluginError } from '../../api/plugins';
import { CONTRIB_ORDER, CONTRIB_WORDS, RISK_WORDS, pluginErrorWords, type CapWords, type Contribution, type Problem, type StateWords } from '../../lib/plugins';
import { icon } from '../../ui/bits';
import { chip, copyBtn } from '../hooks/parts';

/** A refusal of a plugins route: the sentence, what to do, the controller's own words. */
export function refused(e: Pick<PluginError, 'type' | 'message' | 'facts'>, o: { quiet?: boolean } = {}): TemplateResult {
  const w = pluginErrorWords(e);
  return html`<div class="st-refusal" role=${o.quiet ? 'status' : 'alert'} data-error=${e.type}>
    <span aria-hidden="true">${icon('warn')}</span>
    <div><p class="st-refusal-t">${w.title}</p>${w.fix ? html`<p class="small">${w.fix}</p>` : nothing}${w.detail ? html`<p class="xs muted st-wrap" data-verbatim>${w.detail}</p>` : nothing}</div>
  </div>`;
}

/** a plugin's state: a dot of its tone and the word */
export const stateMark = (s: StateWords, o: { title?: boolean } = {}): TemplateResult => html`<span class="plg-state is-${s.tone}" title=${o.title === false ? nothing : s.text} data-state=${s.label}><i aria-hidden="true"></i>${s.label}</span>`;

/** One refusal of a check: what is wrong in plain words, what to change, and the controller's
 *  sentence as it came. */
export function problem(p: Problem): TemplateResult {
  return html`<li class="plg-problem" data-problem=${p.code}>
    <p class="plg-problem-t">${p.title}</p>
    <p class="small">${p.fix}</p>
    <p class="xs muted st-wrap" data-verbatim>${p.field ? html`<span class="num">${p.field}</span>: ` : nothing}${p.message}</p>
  </li>`;
}

/** A capability: what it lets the plugin do, what that means, how much it risks. `end`: what
 *  stands at its right (a switch on the plugin's page, nothing in a preview). */
export function capRow(c: CapWords, o: { end?: TemplateResult | typeof nothing; under?: TemplateResult | typeof nothing; staged?: boolean } = {}): TemplateResult {
  const r = RISK_WORDS[c.risk];
  return html`<li class="plg-cap is-${c.risk} ${o.staged ? 'is-staged' : ''}" data-capability=${c.capability} data-risk=${c.risk}>
    <div class="plg-cap-b">
      <p class="plg-cap-h"><b>${c.title}</b>${chip(r.tone, r.label, { title: r.text, data: 'risk' })}${c.confirm ? chip('quiet', 'asked twice', { title: 'Granting it takes a second confirm in the review.', data: 'confirm' }) : nothing}</p>
      <p class="small muted">${c.text}</p>
      <p class="xs"><code class="num">${c.capability}</code></p>
      ${o.under ?? nothing}
    </div>
    ${o.end ?? nothing}
  </li>`;
}

/** What a plugin adds, a group a kind. */
export function contribList(items: Contribution[], o: { links?: boolean } = {}): TemplateResult {
  if (!items.length) return html`<p class="small muted" data-contrib-none>It adds nothing to TandemApp.</p>`;
  return html`<div class="plg-contribs" data-contribs=${items.length}>
    ${CONTRIB_ORDER.filter((k) => items.some((x) => x.kind === k)).map((k) => {
      const own = items.filter((x) => x.kind === k);
      const w = CONTRIB_WORDS[k];
      return html`<section class="plg-contrib" data-contrib=${k}>
        <h3 class="hk-sub">${own.length} ${own.length === 1 ? w.one : w.many}</h3>
        <p class="xs muted">${w.text}</p>
        <ul class="plg-rows">
          ${own.map(
            (x) => html`<li class="plg-row" data-name=${x.name} data-live=${x.live === null ? 'unknown' : String(x.live)}>
              <span class="plg-row-b">
                <span class="plg-row-h"><b class="num">${x.name}</b>${x.marks.map((m) => chip(m.tone, m.text, { data: 'mark' }))}${x.live === true ? chip('ok', 'registered', { data: 'live' }) : x.live === false ? chip('quiet', 'not registered', { title: 'It is registered while the plugin runs, with what it was granted.', data: 'live' }) : nothing}</span>
                ${x.text ? html`<span class="small muted">${x.text}</span>` : nothing}
              </span>
              ${o.links !== false && x.href ? html`<a class="btn btn-sm ${x.kind === 'panels' && x.live ? 'btn-primary' : 'btn-ghost'}" href=${x.href} data-act=${x.kind === 'panels' ? 'panel-open' : 'contrib-link'}>${x.link}${icon('right')}</a>` : nothing}
            </li>`,
          )}
        </ul>
      </section>`;
    })}
  </div>`;
}

/** A sha256 in full, to compare with what the author published. */
export function sha(value: string | null, label = 'sha256 of its files'): TemplateResult {
  if (!value) return html`<p class="small muted" data-sha="none">No sha256: the files could not be read.</p>`;
  return html`<div class="plg-sha" data-sha=${value}><span class="xs muted">${label}</span><code class="num">${value}</code>${copyBtn(value, 'Copy the sha256')}</div>`;
}
