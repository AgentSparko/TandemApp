// The health page's templates (SRV-835): the verdict in the page's head, a roll call of what was
// checked, then one card a target. A card is its name, the trace (its checks in the order they
// ran: a line that breaks where a check failed, dashed for the ones that could not run after it),
// its state in words, its findings worst first (the controller's sentence, its fix, the button
// when there is one), and the checks that passed, folded. A changed host key shows both
// fingerprints and the steps to pair again: no button there connects. Templates only, no element
// and no state, so they render in a test without a browser; the element is area.ts.

import { html, nothing, svg, type TemplateResult } from 'lit';
import type { EngineFinding } from '../../api/engines';
import type { UpgradeSide } from '../../api/health';
import { SIGN_IN, segments } from '../../lib/engine-words';
import type { DialogState, HealthFlow } from '../../lib/health-flow';
import { SEVERITY_WORDS, actionOf, ago, busyWords, cardTarget, cardsOf, factsOf, fingerprintsOf, foldWords, guideLink, isGuide, pairAgain, reachOf, stateWords, traceOf, verdict, type Card, type Fact, type Note, type Row, type Stop, type StopState } from '../../lib/health';
import { SETTINGS_TABS } from '../../lib/menu';
import { icon, skeleton } from '../../ui/bits';

export interface HealthHandlers {
  refresh(): void;
  recheck(card: Card): void;
  press(row: Row, card: Card): void;
  jump(card: Card): void;
  dismiss(key: string): void;
  ref(text: string): void;
  when(w: 'idle' | 'now'): void;
  go(): void;
  apply(): void;
  back(): void;
  close(): void;
}

/** a mark a state: never a colour alone */
const GLYPH: Record<StopState, TemplateResult> = {
  ok: icon('check'),
  error: svg`<svg class="icon" viewBox="0 0 20 20" aria-hidden="true" width="18" height="18"><circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m7.3 7.3 5.4 5.4M12.7 7.3l-5.4 5.4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  warning: icon('warn'),
  info: icon('info'),
  skip: svg`<svg class="icon" viewBox="0 0 20 20" aria-hidden="true" width="18" height="18"><circle cx="10" cy="10" r="6.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.6 2.6"/></svg>`,
};

/** A sentence of the controller's, its `commands` as code. */
function say(text: string): TemplateResult {
  return html`${segments(text).map((s) => (s.code ? html`<code>${s.text}</code>` : s.text))}`;
}

/** a card whose only findings will pass by themselves reads as that, not as all clear */
const toneOf = (c: Card): StopState => (c.state === 'ok' && c.rows.length ? 'info' : c.state);

/** the Plugins page of the settings area, where this build has one */
const PLUGIN_PAGES = (SETTINGS_TABS as readonly { id: string }[]).some((t) => t.id === 'plugins');

function facts(list: Fact[]): TemplateResult | typeof nothing {
  if (!list.length) return nothing;
  return html`<dl class="hl-facts">${list.map((x) => html`<div><dt>${x.label}</dt><dd class="${x.mono ? 'num hl-wrap' : ''}">${x.value}</dd></div>`)}</dl>`;
}

/** what the page says for a call the controller refused, or that did not get through */
function refusal(e: EngineFinding, extra: TemplateResult | typeof nothing = nothing): TemplateResult {
  return html`<div class="hl-refusal" role=${e.code === SIGN_IN ? 'status' : 'alert'} data-refusal=${e.code}>
    <span class="hl-refusal-g" aria-hidden="true">${icon('warn')}</span>
    <div class="hl-refusal-b"><p class="hl-say">${say(e.message)}</p><p class="hl-fix">${say(e.fix)}</p></div>
    ${extra}
  </div>`;
}

// ---- the trace and the fold ----------------------------------------------------------------------

function trace(stops: Stop[]): TemplateResult | typeof nothing {
  // one check is no line: the card's state says it
  if (stops.length < 2) return nothing;
  return html`<ol class="hl-trace" data-trace aria-label="The checks, in the order they ran">${stops.map(
    (s) => html`<li class="is-${s.state}" data-check=${s.check} data-ok=${String(s.ok)} title=${s.says}><i aria-hidden="true"></i><span class="hl-trace-l">${s.short}</span><span class="sr-only">: ${s.says}</span></li>`,
  )}</ol>`;
}

function fold(c: Card, stops: Stop[]): TemplateResult | typeof nothing {
  const quiet = stops.filter((s) => s.state === 'ok' || s.state === 'skip');
  const reach = c.target && c.kind !== 'plugins' ? reachOf(c.target) : [];
  if (!quiet.length && !reach.length) return nothing;
  return html`<details class="hl-fold" data-checks>
    <summary><span>${quiet.length ? foldWords(stops) : 'How TandemApp reaches it'}</span></summary>
    ${quiet.length
      ? html`<ul class="hl-checks">${quiet.map((s) => html`<li class="is-${s.state}" data-check=${s.check} data-ok=${String(s.ok)}><span aria-hidden="true">${GLYPH[s.state]}</span><span>${s.says}</span></li>`)}</ul>`
      : nothing}
    ${reach.length ? html`<div class="hl-reach" data-reach>${facts(reach)}</div>` : nothing}
  </details>`;
}

// ---- a finding ------------------------------------------------------------------------------------

function note(n: Note, wait: number, dismiss: (() => void) | null): TemplateResult {
  const g = n.kind === 'cleared' ? icon('check') : n.kind === 'working' ? html`<i class="hl-pulse"></i>` : icon(n.kind === 'still' ? 'info' : 'warn');
  return html`<div class="hl-note is-${n.kind}" role=${n.kind === 'refused' ? 'alert' : 'status'} data-note=${n.kind} data-note-action=${n.action}>
    <span class="hl-note-g" aria-hidden="true">${g}</span>
    <div class="hl-note-b">
      <p>${say(n.text)}</p>
      ${n.more ? html`<p class="hl-note-m">${say(n.more)}</p>` : nothing}
      ${wait ? html`<p class="hl-note-m" data-wait>The buttons of this card work again in ${wait} s.</p>` : nothing}
    </div>
    ${dismiss ? html`<button type="button" class="icon-btn hl-note-x" aria-label="Dismiss" data-act="dismiss" @click=${dismiss}>${icon('x')}</button>` : nothing}
  </div>`;
}

/** the button of a finding: the fix TandemApp runs, a check again, or the steps of a guide */
function rowButton(f: HealthFlow, row: Row, card: Card, on: HealthHandlers): TemplateResult | typeof nothing {
  const a = actionOf(row.finding);
  if (row.pairing) {
    // a refused pairing is no target, so its finding carries no action: the way on is Add engine,
    // by a new code. A host key that is not the one shown gets none: nothing here pairs or
    // connects past it
    if (!pairAgain(row.finding.code)) return nothing;
    return html`<a class="btn hl-act" href="#/add-engine" data-act="open-add-engine">Open Add engine</a>`;
  }
  if (!a || row.target === null) {
    const plugin = row.finding.facts.plugin;
    return PLUGIN_PAGES && plugin ? html`<a class="btn hl-act" href=${`#/app/plugins/p/${encodeURIComponent(plugin)}`} data-act="open-plugin">Open its page</a>` : nothing;
  }
  const busy = f.busy[row.target];
  const wait = f.waitLeft(row.target);
  const guide = isGuide(a);
  return html`<button type="button" class="btn ${a.changes ? 'btn-primary' : ''} hl-act" data-act=${a.id} data-changes=${String(a.changes)} aria-expanded=${guide ? String(f.shown.has(row.key)) : nothing}
    ?disabled=${!!busy || wait > 0} title=${wait > 0 ? `Too many fixes in a minute: again in ${wait} s` : nothing} @click=${() => on.press(row, card)}>${busy?.key === row.key ? 'Working…' : `${a.label}${a.confirm ? '…' : ''}`}</button>`;
}

function findingRow(f: HealthFlow, row: Row, card: Card, on: HealthHandlers): TemplateResult {
  const x = row.finding;
  const fps = fingerprintsOf(x);
  const n = f.notes[row.key];
  const g = f.shown.has(row.key) ? f.guides[row.key] : null;
  const busy = row.target !== null && f.busy[row.target]?.key === row.key ? f.busy[row.target] : null;
  const link = g ? guideLink(g) : null;
  return html`<li class="hl-row is-${x.severity}" data-finding=${x.code} data-severity=${x.severity} data-row=${row.key}>
    <span class="hl-row-g" aria-hidden="true">${GLYPH[x.severity]}</span>
    <div class="hl-row-b">
      <p class="hl-say"><span class="sr-only">${SEVERITY_WORDS[x.severity]}: </span>${say(x.sentence)}</p>
      <p class="hl-fix" data-fix>${say(x.fix)}</p>
      ${row.pairing ? html`<p class="small muted" data-pairing><span class="num hl-wrap">${row.pairing.address}:${row.pairing.port}</span>, refused ${ago(f.since(row.pairing.at))}</p>` : nothing}
      ${fps
        ? html`<dl class="hl-fps" data-fingerprints>${fps.map((p) => html`<div><dt>${p.label}</dt><dd class="num hl-wrap">${p.value}</dd></div>`)}</dl>
            <p class="small hl-stop" data-no-connect>${x.code === 'host_key_changed' ? 'TandemApp does not connect to this host while the two differ.' : 'Nothing was paired.'}</p>`
        : nothing}
      ${facts(factsOf(x))}
    </div>
    <div class="hl-row-a">${rowButton(f, row, card, on)}</div>
    ${busy ? html`<div class="hl-live" role="status" data-busy=${busy.action}><i class="hl-live-bar" aria-hidden="true"></i><span>${busyWords(busy.action)}</span></div>` : nothing}
    ${n ? html`<div class="hl-row-x">${note(n, n.kind === 'wait' ? f.waitLeft(n.target) : 0, () => on.dismiss(row.key))}</div>` : nothing}
    ${g && link
      ? html`<div class="hl-row-x"><div class="hl-guide" data-guide=${g.open}>
          <ol class="hl-steps">${g.steps.map((s) => html`<li>${say(s)}</li>`)}</ol>
          <a class="btn btn-sm" href=${link.href} data-act="guide-open">${link.label}</a>
        </div></div>`
      : nothing}
  </li>`;
}

// ---- a card ---------------------------------------------------------------------------------------

function cardView(f: HealthFlow, c: Card, on: HealthHandlers, focus: string | null): TemplateResult {
  const stops = traceOf(c);
  const tone = toneOf(c);
  const target = cardTarget(c);
  const checking = target === null ? f.refreshing : f.checking.has(target);
  const held = target === null && f.refreshLeft() > 0;
  const fixed = f.fixed(c);
  return html`<section class="panel hl-card is-${tone}" id=${cardElementId(c.id)} data-card=${c.id} data-kind=${c.kind} data-state=${c.state} data-focus=${focus === c.id ? 'true' : nothing} aria-labelledby="hl-t-${c.id}">
    <header class="hl-card-h">
      <div class="hl-card-t">
        <h2 class="panel-title" id="hl-t-${c.id}" tabindex="-1">${c.title}</h2>
        <p class="hl-meta">${c.meta.map((m) => html`<span class="${m.mono ? 'num hl-wrap' : ''}">${m.text}</span>`)}</p>
      </div>
      ${trace(stops)}
    </header>
    <div class="hl-status">
      <span class="hl-state is-${tone}" data-state-words><span aria-hidden="true">${GLYPH[tone]}</span>${stateWords(c)}</span>
      ${c.target ? html`<span class="small muted" data-age>checked ${ago(f.age(c.target))}</span>` : nothing}
      <button type="button" class="btn btn-ghost btn-sm hl-recheck" data-act="recheck" aria-label=${`Check ${c.title} again`} ?disabled=${checking || held} @click=${() => on.recheck(c)}>${icon('refresh')}<span>${checking ? 'Checking…' : 'Check again'}</span></button>
    </div>
    ${fixed.length ? html`<ul class="hl-fixed" data-fixed>${fixed.map((n) => html`<li>${note(n, 0, null)}<p class="xs muted hl-was">Before: ${n.was}</p></li>`)}</ul>` : nothing}
    ${c.rows.length ? html`<ul class="hl-rows">${c.rows.map((r) => findingRow(f, r, c, on))}</ul>` : nothing}
    ${fold(c, stops)}
  </section>`;
}

// ---- the page -------------------------------------------------------------------------------------

function head(o: { state: string; title: string; sub: string | TemplateResult; tools?: TemplateResult | typeof nothing; setup?: boolean }): TemplateResult {
  return html`<header class="page-head hl-head" data-page="health" data-state=${o.state} data-setup=${o.setup ? 'true' : nothing}>
    <div class="hl-head-t">
      <div class="eyebrow">Health</div>
      <h1 class="hl-verdict is-${o.state}" data-verdict>${o.state in GLYPH ? html`<span class="hl-mark" aria-hidden="true">${o.state === 'error' ? icon('x') : GLYPH[o.state as StopState]}</span>` : nothing}<span>${o.title}</span></h1>
      <p class="page-sub" role="status" data-verdict-sub>${o.sub}</p>
    </div>
    ${o.tools ?? nothing}
  </header>`;
}

/** The whole page: its head, then what the flow holds. `signIn`: the controller's sign-in card,
 *  when a call was refused for want of the admin password. */
export function healthView(f: HealthFlow, on: HealthHandlers, o: { focus?: string | null; signIn?: TemplateResult | typeof nothing } = {}): TemplateResult {
  const v = f.view;
  const signIn = o.signIn ?? nothing;
  const again = html`<button type="button" class="btn" data-act="refresh" ?disabled=${f.refreshing} @click=${on.refresh}>${icon('refresh')} ${f.refreshing ? 'Checking…' : 'Try again'}</button>`;
  if (!v) {
    if (!f.loaded)
      return html`${head({ state: 'loading', title: 'Checking…', sub: 'TandemApp looks at itself and at each engine host over SSH. This takes a few seconds.' })}
        <div class="hl-cards" aria-busy="true" data-loading>${[4, 3].map((n) => html`<section class="panel hl-card">${skeleton(n)}</section>`)}</div>`;
    if (f.signIn)
      return html`${head({ state: 'locked', title: 'Sign in to see the health checks.', sub: 'This TandemApp has an admin password, and reading its health needs it.' })}${signIn}`;
    return html`${head({ state: 'unread', title: 'The health checks did not answer.', sub: 'Nothing was checked, so nothing is known: this is not a finding about an engine.' })}
      ${f.error ? refusal(f.error, again) : again}`;
  }
  const vd = verdict(v);
  const cards = cardsOf(v);
  const left = f.refreshLeft();
  const oldest = Math.max(...[v.controller, ...v.engines].map((t) => f.age(t)));
  const when = f.refreshing ? 'Checking everything…' : f.limited && left > 0 ? 'Checked a moment ago: this is that answer.' : `Checked ${ago(oldest)}.${left > 0 ? ` Again in ${left} s.` : ''}`;
  const tools = html`<div class="hl-head-a">
    ${v.setup.first_page === 'add_engine' ? html`<a class="btn btn-primary" href="#/add-engine" data-act="setup">${v.actions.add_engine?.label ?? 'Add engine'}</a>` : nothing}
    <button type="button" class="btn" data-act="refresh" ?disabled=${f.refreshing || left > 0} @click=${on.refresh}>${icon('refresh')} ${v.actions.retry?.label ?? 'Check again'}</button>
    <span class="small muted hl-checked" data-checked ?data-limited=${f.limited && left > 0}>${when}</span>
  </div>`;
  return html`${head({ state: v.state, title: vd.title, sub: vd.sub, tools, setup: vd.setup })}
    ${f.signIn ? signIn : nothing}
    ${f.error ? refusal(f.error) : nothing}
    ${cards.length > 3
      ? html`<nav class="hl-roll" aria-label="What was checked" data-roll>${cards.map((c) => {
          const tone = toneOf(c);
          const n = c.rows.filter((r) => r.finding.severity !== 'info').length;
          return html`<button type="button" class="hl-chip is-${tone}" data-jump=${c.id} @click=${() => on.jump(c)}><span aria-hidden="true">${GLYPH[tone]}</span><span>${c.title}</span>${n ? html`<b class="num">${n}</b>` : nothing}<span class="sr-only">: ${stateWords(c)}</span></button>`;
        })}</nav>`
      : nothing}
    <div class="hl-cards" data-cards>${cards.map((c) => cardView(f, c, on, o.focus ?? null))}</div>`;
}

// ---- the dialog -----------------------------------------------------------------------------------

function side(s: UpgradeSide | undefined): TemplateResult {
  if (!s) return html`<span class="muted">not said</span>`;
  return html`<b>${s.version ?? s.ref ?? 'unknown'}</b>${s.ref && s.version ? html` <span class="muted">(${s.ref})</span>` : nothing}${s.engine_api ? html` <span class="muted">engine API ${s.engine_api}</span>` : nothing}${s.commit
    ? html` <span class="num muted">${s.commit.slice(0, 7)}</span>`
    : nothing}`;
}

/** The dialog of an action that asks first (Upgrade: the release, the engine host's plan, the
 *  apply with the plan's confirm). */
export function dialogView(d: DialogState | null, on: HealthHandlers, signIn: TemplateResult | typeof nothing = nothing): TemplateResult | typeof nothing {
  if (!d) return nothing;
  const upgrade = d.action.id === 'upgrade';
  const busy = d.phase === 'planning' || d.phase === 'applying';
  const p = d.plan;
  // no release typed and none set on the controller: the field's own words, the controller's below
  const noRef = upgrade && d.phase === 'ask' && d.error?.code === 'bad_request';
  const err = !d.error
    ? nothing
    : d.error.code === SIGN_IN && signIn !== nothing
      ? signIn
      : noRef
        ? html`<div class="hl-noref" role="alert" data-refusal="bad_request"><p class="small err-ink">Type the release to upgrade to, a tag such as v0.16.0: this TandemApp is set to none.</p>${d.error.detail ? html`<p class="xs muted">It said: ${d.error.detail}</p>` : nothing}</div>`
        : refusal(d.error);
  const live = (text: string) => html`<div class="hl-live" role="status" data-busy=${d.action.id}><i class="hl-live-bar" aria-hidden="true"></i><span>${text}</span></div>`;
  const whenField = html`<fieldset class="hl-when" data-when>
    <legend>When the engine restarts for it</legend>
    <label><input type="radio" name="hl-when" value="idle" .checked=${d.when === 'idle'} ?disabled=${busy} @change=${() => on.when('idle')} /><span>When it is idle <span class="muted">nothing that runs is cut off</span></span></label>
    <label><input type="radio" name="hl-when" value="now" .checked=${d.when === 'now'} ?disabled=${busy} @change=${() => on.when('now')} /><span>Now <span class="muted">the requests that run end</span></span></label>
  </fieldset>`;
  let body: TemplateResult;
  if (d.phase === 'done')
    body = html`${d.note ? note(d.note, 0, null) : nothing}
      <footer class="act-foot"><button type="button" class="btn btn-primary" data-act="dlg-close" @click=${on.close}>Close</button></footer>`;
  else if (d.phase === 'plan' || d.phase === 'applying')
    body = html`${d.planRef ? html`<p class="small" data-plan-ref=${d.refSource ?? 'asked'}>Release <b class="num">${d.planRef}</b>${d.refSource === 'default' ? ', the one this TandemApp is set to (TANDEM_ENGINE_REF)' : d.defaultRef ? `; this TandemApp is set to ${d.defaultRef}` : ''}.</p>` : nothing}${p
        ? html`<dl class="hl-plan" data-plan>
              ${p.from || p.to ? html`<div><dt>From</dt><dd>${side(p.from)}</dd></div><div><dt>To</dt><dd>${side(p.to)}</dd></div>` : nothing}
              ${p.venv ? html`<div><dt>Python environment</dt><dd>${p.venv === 'synced' ? 'installed again for the new release' : 'kept as it is'}</dd></div>` : nothing}
              ${p.keeps?.length ? html`<div><dt>Kept</dt><dd class="num hl-wrap">${p.keeps.join(', ')}</dd></div>` : nothing}
              ${p.rollback ? html`<div><dt>If it fails</dt><dd>back to ${p.rollback}</dd></div>` : nothing}
            </dl>
            ${p.steps?.length ? html`<p class="small muted">What the engine host does, in this order:</p><ol class="hl-steps" data-plan-steps>${p.steps.map((s) => html`<li>${say(s)}</li>`)}</ol>` : nothing}
            ${p.warnings?.length ? html`<ul class="hl-warnings" data-plan-warnings>${p.warnings.map((w) => html`<li>${icon('warn')}<span>${w}</span></li>`)}</ul>` : nothing}`
        : html`<p class="small">The plan is ready.</p>`}
      ${upgrade ? whenField : nothing}
      ${d.phase === 'applying' ? live(upgrade ? `Sending the upgrade to ${d.title}…` : `${d.action.label}…`) : nothing}
      ${err}
      ${!p?.confirm ? html`<p class="small err-ink" role="alert" data-no-confirm>The plan came without its confirm, so it cannot be applied. Ask for the plan again.</p>` : nothing}
      <footer class="act-foot">
        <button type="button" class="btn btn-ghost" data-act="dlg-back" ?disabled=${busy} @click=${on.back}>Back</button>
        <button type="button" class="btn btn-primary" data-act="dlg-apply" ?disabled=${busy || !p?.confirm} @click=${on.apply}>${d.phase === 'applying' ? 'Working…' : upgrade ? (d.when === 'now' ? 'Upgrade now' : 'Upgrade when idle') : d.action.label}</button>
      </footer>`;
  else
    body = html`<form class="hl-ask" novalidate @submit=${(e: Event) => (e.preventDefault(), on.go())}>
      ${upgrade
        ? html`<p class="small">TandemApp asks ${d.title} for its plan first. Nothing changes until you confirm that plan.</p>
            <label class="field field-block"><span>Release to upgrade to</span>
              <input class="num" data-in="ref" name="ref" placeholder="v0.16.0" autocomplete="off" autocapitalize="off" spellcheck="false" aria-describedby="hl-ref-h" ?disabled=${busy} .value=${d.ref} @input=${(e: Event) => on.ref((e.target as HTMLInputElement).value)} /></label>
            <p class="xs muted" id="hl-ref-h">A release tag. Leave it empty for the release this TandemApp is set to.</p>`
        : html`<p class="small">${d.action.label} changes ${d.title}. ${say(d.row.finding.fix)}</p>`}
      ${d.phase === 'planning' ? live(upgrade ? `Asking ${d.title} for its plan…` : `${d.action.label}…`) : nothing}
      ${err}
      <footer class="act-foot">
        <button type="button" class="btn btn-ghost" data-act="dlg-cancel" ?disabled=${busy} @click=${on.close}>Cancel</button>
        <button type="submit" class="btn btn-primary" data-act="dlg-go" ?disabled=${busy}>${busy ? 'Working…' : upgrade ? 'Show the plan' : d.action.label}</button>
      </footer>
    </form>`;
  return html`<header class="act-head"><h2 id="hl-dlg-t" tabindex="-1">${upgrade ? `Upgrade the engine on ${d.title}` : `${d.action.label}: ${d.title}`}</h2>
      <button type="button" class="btn btn-ghost btn-icon" aria-label="Close" data-act="dlg-x" ?disabled=${busy} @click=${on.close}>${icon('x')}</button></header>
    <div class="hl-dlg" data-phase=${d.phase}>${body}</div>`;
}

/** the id of a card's element: the page scrolls to it */
export const cardElementId = (id: string): string => `hl-card-${id}`;
