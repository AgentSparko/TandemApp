// The run sheet (VIS-280): a change set drawn as the stops it makes, in the order they apply (app,
// backends, conductor, engine, hooks: the first failure stops the set, so the order is told). The
// same sheet goes from plan to result: before the apply each stop lists its changes, what it costs
// (a restart or none) and its warnings; a plan that restarts an engine asks when, and one with a
// fingerprint key asks a second time. After the apply each stop shows how it ended, with Restore on
// what was applied and the restart's card on what waits for one. The restart card (state, the
// countdown while it waits for idle, the engine's job, Cancel while it can still be cancelled, the
// version restored after a failed start) is the same on every page that shows a restart. Templates
// only, so they render in a test without a browser.

import { html, nothing, type TemplateResult } from 'lit';
import type { Change, Overview, PartChange, PartPlan, PartResult, Restart, SettingsError } from '../../api/settings';
import { FINGERPRINT_NOTE, shown } from '../../lib/settings-form';
import { restartsEngine, restoreOf, type FlowState } from '../../lib/settings-flow';
import { DEADLINES, PART_GLYPH, changeWords, costOf, dur, errorWords, partStateWords, placeOf, planSummary, restartModel, resultHead, targetLabel, whenWords, type When } from '../../lib/settings-words';
import { icon } from '../../ui/bits';

export interface SheetHandlers {
  apply(): void;
  back(): void;
  close(): void;
  /** plan the same changes again */
  again(): void;
  when(p: Partial<When>): void;
  note(text: string): void;
  /** a part's restore, as a change set of its own */
  restore(changes: PartChange[]): void;
  cancelRestart(id: string): void;
}

/** What the page knows of the restarts: the newest read of one, the seconds it still waits. */
export interface RestartView {
  get(id: string): Restart | undefined;
  left(r: Restart): number | null;
  cancelling: string | null;
  refused: { id: string; error: SettingsError } | null;
}

/** A refusal: the sentence, what to do, the controller's own words. */
export function refusal(e: SettingsError, o: { quiet?: boolean } = {}): TemplateResult {
  const w = errorWords(e);
  return html`<div class="st-refusal" role=${o.quiet ? 'status' : 'alert'} data-error=${e.type}>
    <span aria-hidden="true">${icon('warn')}</span>
    <div><p class="st-refusal-t">${w.title}</p>${w.fix ? html`<p class="small">${w.fix}</p>` : nothing}${w.detail ? html`<p class="xs muted st-wrap">${w.detail}</p>` : nothing}</div>
  </div>`;
}

// ---- the restart ----------------------------------------------------------------------------------

/** A restart: where it stands, the countdown while it waits, Cancel while that is possible. */
export function restartCard(r: Restart, v: RestartView, onCancel: ((id: string) => void) | null): TemplateResult {
  const now = v.get(r.id) ?? r;
  const m = restartModel(now, v.left(now));
  const refused = v.refused?.id === now.id ? v.refused.error : null;
  return html`<section class="rsc is-${m.tone}" role="status" aria-live="polite" data-restart=${now.id} data-state=${now.state}>
    <div class="rsc-h">
      <h3 class="rsc-t">${m.title}</h3>
      ${m.count ? html`<p class="rsc-count" data-countdown><span class="num">${m.count}</span><span class="xs">left to wait for idle</span></p>` : nothing}
    </div>
    ${m.used != null ? html`<div class="rsc-bar" role="progressbar" aria-label="The wait for idle" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(m.used * 100)}><span style="width:${(m.used * 100).toFixed(1)}%"></span></div>` : nothing}
    <ol class="rsc-steps" data-steps>${m.steps.map((s) => html`<li class="is-${s.state}" data-step=${s.state} aria-current=${s.state === 'now' ? 'step' : nothing}>${s.label}</li>`)}</ol>
    <p class="small rsc-say" data-restart-words>${m.text}</p>
    ${now.state === 'failed' && now.restored ? html`<p class="small" data-restored=${now.restored.version}>Restored: version <span class="num">${now.restored.version}</span>${now.restored.healthy ? '' : ' (not answering yet)'}.</p>` : nothing}
    <p class="xs faint rsc-meta"><span>Change set <span class="num">${now.change_set}</span></span>${m.job ? html`<span data-job>${m.job}</span>` : nothing}${now.deadline_s && !m.ended ? html`<span>Waits at most ${dur(now.deadline_s)}</span>` : nothing}</p>
    ${refused ? refusal(refused) : nothing}
    ${now.can_cancel && onCancel
      ? html`<div class="rsc-acts"><button type="button" class="btn rsc-cancel" data-act="restart-cancel" ?disabled=${v.cancelling === now.id} @click=${() => onCancel(now.id)}>${v.cancelling === now.id ? 'Cancelling…' : 'Cancel the restart'}</button><span class="xs muted">Cancelling changes nothing: the engine keeps the settings it runs.</span></div>`
      : nothing}
  </section>`;
}

/** A restart in one line, for the dock at the bottom of the area: wherever the page is scrolled to,
 *  what the engine does now, the countdown and Cancel stay in reach. The card above has the rest. */
export function restartLine(r: Restart, v: RestartView, onCancel: (id: string) => void, onShow: () => void): TemplateResult {
  const now = v.get(r.id) ?? r;
  const m = restartModel(now, v.left(now));
  return html`<div class="st-live" role="group" aria-label="Engine restart" data-restart-line=${now.id} data-state=${now.state}>
    <span class="st-live-t"><b>${m.title}</b>${m.count ? html`<span class="num st-live-n" data-countdown>${m.count}</span><span class="xs muted">left to wait for idle</span>` : nothing}</span>
    <span class="st-bar-a">
      <button type="button" class="btn btn-ghost btn-sm" data-act="restart-show" @click=${onShow}>Show it</button>
      ${now.can_cancel ? html`<button type="button" class="btn btn-sm rsc-cancel" data-act="restart-cancel" ?disabled=${v.cancelling === now.id} @click=${() => onCancel(now.id)}>${v.cancelling === now.id ? 'Cancelling…' : 'Cancel the restart'}</button>` : nothing}
    </span>
  </div>`;
}

// ---- the plan -------------------------------------------------------------------------------------

function changeRow(c: Change): TemplateResult {
  const w = changeWords(c);
  return html`<li class="rs-change" data-change=${c.key}>
    <span class="rs-key num">${c.key}</span>
    <span class="rs-val"><span class="rs-from">${w.from}</span><span class="rs-arrow" aria-hidden="true">→</span><span class="sr-only"> becomes </span><span class="rs-to">${w.to}</span></span>
    ${w.note ? html`<span class="xs muted">${w.note}</span>` : nothing}
    ${c.fingerprint ? html`<span class="st-chip is-warn" title=${FINGERPRINT_NOTE} data-chip="warn">saved caches start cold</span>` : nothing}
    ${c.diff?.length ? html`<ul class="rs-diff" data-diff>${c.diff.map((d) => html`<li><span class="num">${d.path}</span><span class="rs-val"><span class="rs-from">${shown(d.before, 48)}</span><span class="rs-arrow" aria-hidden="true">→</span><span class="sr-only"> becomes </span><span class="rs-to">${shown(d.after, 48)}</span></span></li>`)}</ul>` : nothing}
  </li>`;
}

function planStop(p: PartPlan, i: number, o: Overview | null): TemplateResult {
  const cost = costOf(p, targetLabel(o, p.scope, p.target));
  return html`<li class="rs-stop is-${cost.tone}" data-part=${`${p.scope}/${p.target}`} data-restart-class=${p.restart}>
    <span class="rs-n num" aria-hidden="true">${i + 1}</span>
    <div class="rs-b">
      <div class="rs-h"><b>${placeOf(p, o)}</b>${p.kind === 'restore' ? html`<span class="st-chip is-quiet">restores version ${p.version}</span>` : nothing}</div>
      <p class="rs-cost small" data-cost>${cost.text}</p>
      ${p.changes.length ? html`<ul class="rs-changes">${p.changes.map(changeRow)}</ul>` : nothing}
      ${p.warnings.map((w) => html`<p class="rs-warn small" data-warning>${icon('warn')}<span>${w.charAt(0).toUpperCase()}${w.slice(1)}.</span></p>`)}
    </div>
  </li>`;
}

function whenBlock(s: FlowState, names: string[], h: SheetHandlers, blocked: string | null): TemplateResult {
  const w = s.when;
  const opt = (mode: When['mode'], title: string, sub: string, extra: TemplateResult | typeof nothing = nothing) =>
    html`<label class="rs-opt ${w.mode === mode ? 'is-on' : ''}" data-when-mode=${mode}><input type="radio" name="rs-when" .checked=${w.mode === mode} @change=${() => h.when({ mode })} /><b>${title}</b><span class="xs muted">${sub}</span>${w.mode === mode ? extra : nothing}</label>`;
  const wait = html`<label class="rs-wait small"><span>Wait at most</span><select data-in="deadline" @change=${(e: Event) => h.when({ deadline_s: Number((e.target as HTMLSelectElement).value) })}>${DEADLINES.map((d) => html`<option value=${d} ?selected=${d === w.deadline_s}>${dur(d)}</option>`)}</select></label>`;
  return html`<fieldset class="rs-when" data-when>
    <legend>When ${names.join(' and ')} ${names.length === 1 ? 'restarts' : 'restart'}</legend>
    <div class="rs-opts">
      ${opt('idle', 'When it is idle', 'After the requests that run have ended.', wait)}
      ${opt('at', 'At a set time', 'Then once it is idle.', html`<input type="datetime-local" class="st-input" data-in="at" aria-label="The day and the time" .value=${w.at} @input=${(e: Event) => h.when({ at: (e.target as HTMLInputElement).value })} />${wait}`)}
      ${opt('now', 'Now', 'Without waiting.')}
    </div>
    <p class="small ${blocked ? 'err-ink' : ''}" role="status" data-when-words>${blocked ?? whenWords(w)}</p>
  </fieldset>`;
}

/** The second confirm: the keys that feed a disk cache's fingerprint, and what that costs. */
function secondConfirm(s: FlowState, o: Overview | null): TemplateResult {
  const keys = s.plan!.fingerprint_keys.map((k) => {
    const [where, key] = k.split(':');
    const [scope, target] = where.split('/');
    return { key, host: targetLabel(o, scope as PartPlan['scope'], target) };
  });
  const hosts = [...new Set(keys.map((k) => k.host))];
  return html`<section class="rs-second" role="alert" data-second>
    <h3>${icon('warn')} Saved caches on ${hosts.join(' and ')} start cold</h3>
    <p class="small">${keys.map((k, i) => html`${i ? ', ' : ''}<span class="num">${k.key}</span>`)} ${keys.length === 1 ? 'feeds' : 'feed'} the disk cache’s fingerprint. After the restart every saved prefix and every parked chat there misses once: the first turn of each chat reads its whole context again.</p>
    <p class="small">Nothing is lost for good, and nothing else changes. Apply only if that is what you want now.</p>
  </section>`;
}

const sheetHead = (title: string, id: string | null, sub: TemplateResult | string | typeof nothing = nothing) =>
  html`<header class="act-head rs-head"><div><h2 id="rs-title" tabindex="-1">${title}</h2>${sub === nothing ? nothing : html`<p class="small muted" data-summary>${sub}</p>`}</div>${id ? html`<span class="rs-id num xs" title="This change set’s id: the history lists it" data-change-set=${id}>${id}</span>` : nothing}</header>`;

function planSheet(s: FlowState, o: Overview | null, blocked: string | null, h: SheetHandlers, signIn: TemplateResult | typeof nothing): TemplateResult {
  const plan = s.plan;
  if (s.phase === 'planning' || (!plan && !s.error))
    return html`${sheetHead('Reading the plan…', null)}<div class="skeleton" aria-busy="true" data-planning><div class="skeleton-line" style="width:72%"></div><div class="skeleton-line" style="width:54%"></div><div class="skeleton-line" style="width:64%"></div></div>
      <footer class="act-foot"><button type="button" class="btn btn-ghost" data-act="sheet-close" @click=${h.close}>Cancel</button></footer>`;
  if (!plan)
    return html`${sheetHead('This change set cannot be planned', null, 'Nothing was written.')}${refusal(s.error!)}${signIn}
      <footer class="act-foot"><button type="button" class="btn btn-ghost" data-act="sheet-close" @click=${h.close}>Back to the settings</button><button type="button" class="btn" data-act="sheet-again" @click=${h.again}>Read the plan again</button></footer>`;
  const sum = planSummary(plan, o);
  const applying = s.phase === 'applying';
  const n = plan.parts.reduce((x, p) => x + p.changes.length, 0);
  return html`${sheetHead(s.second ? 'One more confirm' : sum.title, plan.change_set, s.second ? nothing : sum.text)}
    ${s.moved && !s.second ? html`<p class="note note-info small" role="status" data-moved>${s.error ? errorWords(s.error).title : 'The settings changed since this plan was read.'} This is the plan as it is now. Check it, then apply.</p>` : nothing}
    ${s.error && !(s.moved && s.error.type === 'stale_plan') ? refusal(s.error) : nothing}
    ${s.second
      ? secondConfirm(s, o)
      : html`<ol class="rs" data-plan>${plan.parts.map((p, i) => planStop(p, i, o))}</ol>
          ${plan.parts.length > 1 ? html`<p class="xs faint">The stops apply in this order. If one fails, the set stops there: what was applied before it stays and can be restored.</p>` : nothing}
          ${restartsEngine(plan) ? whenBlock(s, sum.restarts, h, blocked && s.when.mode === 'at' ? blocked : null) : nothing}
          ${plan.changed ? html`<label class="rs-note small"><span>A note for the history (optional)</span><input class="st-input" data-in="note" maxlength="200" .value=${s.note} @input=${(e: Event) => h.note((e.target as HTMLInputElement).value)} /></label>` : nothing}`}
    ${signIn}
    <footer class="act-foot">
      ${s.second
        ? html`<button type="button" class="btn btn-ghost" data-act="sheet-back" ?disabled=${applying} @click=${h.back}>Back to the plan</button>
            <button type="button" class="btn rs-go-warn" data-act="sheet-apply-second" ?disabled=${applying} @click=${h.apply}>${applying ? 'Applying…' : 'Apply, and let the caches start cold'}</button>`
        : html`<button type="button" class="btn btn-ghost" data-act="sheet-close" ?disabled=${applying} @click=${h.close}>${plan.changed ? 'Not now' : 'Close'}</button>
            ${plan.changed ? html`<button type="button" class="btn btn-primary" data-act="sheet-apply" ?disabled=${applying || !!blocked} @click=${h.apply}>${applying ? 'Applying…' : plan.confirm_fingerprint ? 'Continue' : `Apply ${n} ${n === 1 ? 'change' : 'changes'}`}</button>` : nothing}`}
    </footer>`;
}

// ---- the result -----------------------------------------------------------------------------------

function resultStop(p: PartResult, o: Overview | null, v: RestartView, h: SheetHandlers): TemplateResult {
  const w = partStateWords(p);
  const sets = restoreOf(p);
  return html`<li class="rs-stop is-${w.tone}" data-part=${`${p.scope}/${p.target}`} data-state=${p.state}>
    <span class="rs-n" aria-hidden="true">${PART_GLYPH[p.state]}</span>
    <div class="rs-b">
      <div class="rs-h"><b>${placeOf(p, o)}</b><span class="rs-state" data-part-state>${w.text}</span></div>
      ${p.keys.length ? html`<p class="xs muted st-wrap"><span class="num">${p.keys.join(', ')}</span>${p.version_before && p.version_after ? html` <span>(version ${p.version_before} → ${p.version_after})</span>` : nothing}</p>` : nothing}
      ${p.error ? refusal({ type: p.error.type, message: p.error.message, facts: {} } as SettingsError, { quiet: true }) : nothing}
      ${p.restart ? restartCard(p.restart, v, h.cancelRestart) : nothing}
      ${p.state === 'applied' && sets.length
        ? html`<div class="rs-restore">${sets.map((c) => {
            const ver = (c[0] as { restore: string }).restore;
            return html`<button type="button" class="btn btn-sm" data-act="part-restore" data-version=${ver} @click=${() => h.restore(c)}>Restore ${sets.length > 1 ? ver : `version ${ver}`}…</button>`;
          })}<span class="xs muted">Shows what it brings back before it writes.</span></div>`
        : nothing}
    </div>
  </li>`;
}

function resultSheet(s: FlowState, o: Overview | null, v: RestartView, h: SheetHandlers): TemplateResult {
  const r = s.result!;
  const head = resultHead(r);
  const open = r.state === 'partial' || r.state === 'failed';
  return html`${sheetHead(head.title, r.change_set, head.text)}
    <ol class="rs" data-result=${r.state}>${r.parts.map((p) => resultStop(p, o, v, h))}</ol>
    ${open ? html`<p class="small" data-result-next>Fix what stopped it, then plan the same changes again: what already holds is left alone, the rest is planned anew.</p>` : nothing}
    <footer class="act-foot">
      <a class="btn btn-ghost" href=${`#/app/history?change_set=${encodeURIComponent(r.change_set)}`} data-act="sheet-history" @click=${h.close}>Show it in the history</a>
      ${open ? html`<button type="button" class="btn" data-act="sheet-again" @click=${h.again}>Plan it again</button>` : nothing}
      <button type="button" class="btn btn-primary" data-act="sheet-done" @click=${h.close}>Done</button>
    </footer>`;
}

/** The sheet for the flow's state: the plan, the second confirm or the result. `blocked`: why
 *  Apply cannot go now; `signIn`: the sign-in card after a 401. */
export function runSheet(s: FlowState, o: Overview | null, v: RestartView, blocked: string | null, h: SheetHandlers, signIn: TemplateResult | typeof nothing = nothing): TemplateResult | typeof nothing {
  if (s.phase === 'closed') return nothing;
  return s.phase === 'result' && s.result ? resultSheet(s, o, v, h) : planSheet(s, o, blocked, h, signIn);
}
