// The pipeline (VIS-282): a request's path as the stops it makes, in the order it passes them, with
// the hooks that sit at each stop in the order they run. The path is the run sheet's rail drawn
// long: a numbered stop a stage, what a hook may do there, the time budget the stage's hooks share
// and how much of it they use. A hook's row says what the list must say of it (its role and kind,
// the requests it runs on, its priority, what happens when it fails, its breaker) and carries the
// one control that matters here: off, shadow or on. A hook in shadow is drawn dashed, as the copy
// it runs on; one that is off is dimmed. Order is by priority within a role, so a move changes
// priorities: arrows on every row, and dragging where a pointer can drag. Every change is staged
// as a settings key. Templates only.

import { html, nothing, svg, type TemplateResult } from 'lit';
import type { HookMode, Role, StageTable } from '../../api/hooks';
import { MODE_WORDS, ROLE_WORDS, breakerWords, defaultPolicy, kindWords, mayWords, ms, policyWords, scopeWords, stageOwner, type ShownHook, type Stop, type Use } from '../../lib/hooks';
import { sentence } from '../../lib/settings-form';
import { icon } from '../../ui/bits';
import { chip, hooksHref, roleMark, useBar } from './bits';

export interface PipelineHandlers {
  mode(id: string, mode: HookMode): void;
  /** move the hook at `from` to `to` within its role at a stage */
  move(stage: string, role: Role, from: number, to: number): void;
  drag(d: Drag | null): void;
}

/** the row being dragged: reordering stays within its role at its stage */
export interface Drag {
  stage: string;
  role: Role;
  from: number;
}

export interface PipelineView {
  table: StageTable;
  stops: Stop[];
  cap: Use;
  /** the settings interface takes changes here */
  writable: boolean;
  drag: Drag | null;
}

const grip = svg`<svg class="hk-grip" viewBox="0 0 10 16" width="10" height="16" aria-hidden="true"><g fill="currentColor"><circle cx="2.5" cy="3" r="1.3"/><circle cx="7.5" cy="3" r="1.3"/><circle cx="2.5" cy="8" r="1.3"/><circle cx="7.5" cy="8" r="1.3"/><circle cx="2.5" cy="13" r="1.3"/><circle cx="7.5" cy="13" r="1.3"/></g></svg>`;

/** the list's one control: off when the hook is not called at all, whatever its mode says */
const shownMode = (h: ShownHook): HookMode => (h.enabled ? h.mode : 'off');

function modeSeg(h: ShownHook, v: PipelineView, on: PipelineHandlers): TemplateResult {
  const now = shownMode(h);
  return html`<div class="seg hk-mode" role="group" aria-label=${`Mode of ${h.name}`}>
    ${(['off', 'shadow', 'on'] as const).map((m) => html`<button type="button" class="seg-btn ${now === m ? 'is-on' : ''}" aria-pressed=${now === m} title=${MODE_WORDS[m].text} data-mode-to=${m} ?disabled=${!v.writable} @click=${() => on.mode(h.id, m)}>${MODE_WORDS[m].label}</button>`)}
  </div>`;
}

function hookRow(h: ShownHook, stop: Stop, role: Role, i: number, n: number, v: PipelineView, on: PipelineHandlers, group: ShownHook[]): TemplateResult {
  const mode = shownMode(h);
  const b = breakerWords(h.metrics.breaker);
  const policy = h.policy_effective[stop.stage] ?? h.policy ?? defaultPolicy(stop.spec, h.role);
  const staged = h.staged.size > 0;
  const movable = n > 1 && v.writable;
  const target = v.drag && v.drag.stage === stop.stage && v.drag.role === role && v.drag.from !== i;
  return html`<li class="hk-row is-${mode} ${staged ? 'is-staged' : ''} ${v.drag && v.drag.stage === stop.stage && v.drag.role === role && v.drag.from === i ? 'is-dragged' : ''}" data-hook=${h.id} data-mode=${mode} data-priority=${h.priority} data-breaker=${h.metrics.breaker.state}
    draggable=${movable ? 'true' : 'false'}
    @dragstart=${(e: DragEvent) => {
      if (!movable) return;
      e.dataTransfer?.setData('text/plain', h.id);
      if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
      on.drag({ stage: stop.stage, role, from: i });
    }}
    @dragend=${() => on.drag(null)}
    @dragover=${(e: DragEvent) => target && e.preventDefault()}
    @drop=${(e: DragEvent) => {
      if (!target) return;
      e.preventDefault();
      on.move(stop.stage, role, v.drag!.from, i);
      on.drag(null);
    }}>
    ${n > 1
      ? html`<div class="hk-order">
          ${grip}
          <button type="button" class="btn btn-sm btn-ghost" aria-label=${i > 0 ? `Move ${h.name} up: it then runs before ${group[i - 1].name}` : `${h.name} runs first already`} data-act="move-up" ?disabled=${i === 0 || !v.writable} @click=${() => on.move(stop.stage, role, i, i - 1)}>↑</button>
          <button type="button" class="btn btn-sm btn-ghost" aria-label=${i < n - 1 ? `Move ${h.name} down: it then runs after ${group[i + 1].name}` : `${h.name} runs last already`} data-act="move-down" ?disabled=${i === n - 1 || !v.writable} @click=${() => on.move(stop.stage, role, i, i + 1)}>↓</button>
        </div>`
      : nothing}
    <div class="hk-row-b">
      <div class="hk-row-h">
        <a class="hk-name" href=${hooksHref(['hook', h.id])}>${h.name}</a>
        ${staged ? html`<span class="st-mark" data-mark>changed</span>` : nothing}
        ${h.payload === 'full' ? chip('warn', 'sees everything', { title: 'It gets every prompt and answer as it is.', data: 'payload' }) : nothing}
      </div>
      <p class="hk-meta xs">
        <span data-fact="kind">${kindWords(h)}</span>
        <span data-fact="scope">runs on ${scopeWords(h.scope)}</span>
        <span class=${h.staged.has('priority') ? 'hk-staged-ink' : ''} data-fact="priority">priority <span class="num">${h.priority}</span></span>
        <span data-fact="policy">${policyWords(policy)}</span>
        ${h.metrics.p95_ms != null ? html`<span data-fact="p95">p95 <span class="num">${ms(h.metrics.p95_ms)}</span></span>` : nothing}
        ${chip(b.tone, b.label, { title: b.text, data: 'breaker' })}
      </p>
    </div>
    ${modeSeg(h, v, on)}
  </li>`;
}

function stopView(s: Stop, v: PipelineView, on: PipelineHandlers): TemplateResult {
  const spec = s.spec;
  const budget = spec.budget_ms != null ? `${spec.budget_ms} ms shared` : 'beside the request';
  return html`<li class="hk-stop ${s.count ? '' : 'is-empty'} ${spec.wired ? '' : 'is-later'}" data-stage=${s.stage} data-hooks=${s.count} data-wired=${spec.wired}>
    <span class="hk-n num" aria-hidden="true">${s.n}</span>
    <div class="hk-stop-b">
      <div class="hk-stop-h">
        <h3 class="hk-stop-t">${s.title}</h3>
        <span class="num xs hk-stage-id">${s.stage}</span>
        <a class="btn btn-sm btn-ghost hk-add" href=${hooksHref(['new'], { stage: s.stage })} aria-label=${`Add a hook at ${s.title}`} data-act="add-here">${icon('plus')}<span>Add a hook here</span></a>
      </div>
      <p class="hk-stop-facts xs">
        <span>${sentence(spec.when)}</span>
        <span data-fact="may">A hook may ${mayWords(spec).join(', ')}.</span>
        <span data-fact="budget" title=${spec.sync ? 'The stage’s hooks share this time per request.' : 'Its hooks run off the request’s path: they cannot slow it down.'}>${budget}</span>
        ${spec.wired ? nothing : chip('quiet', 'not called yet', { title: `This TandemApp does not call this stage yet: ${stageOwner(s.stage)} will. A hook can be set up for it now.`, data: 'later' })}
      </p>
      ${s.use
        ? html`<div class="hk-stop-use">${useBar(s.use, { label: `Time the hooks at ${s.title} use` })}<span class="xs ${s.use.over ? 'err-ink' : 'muted'}" data-stage-use><span class="num">${ms(s.use.used_ms)}</span> of ${s.use.budget_ms} ms${s.use.over ? ': over the budget, the last hooks get no time' : ''}</span></div>`
        : nothing}
      ${s.groups.map(
        (g) => html`<div class="hk-group" data-role-group=${g.role}>
          <p class="hk-group-h xs">${roleMark(g.role)}<span class="muted">${g.hooks.length > 1 ? ROLE_WORDS[g.role].runs : ROLE_WORDS[g.role].does}</span></p>
          <ol class="hk-rows">${g.hooks.map((h, i) => hookRow(h, s, g.role, i, g.hooks.length, v, on, g.hooks))}</ol>
        </div>`,
      )}
    </div>
  </li>`;
}

function capPanel(v: PipelineView): TemplateResult {
  const c = v.cap;
  const any = c.parts.length + c.unmeasured > 0;
  return html`<section class="panel hk-cap" data-cap data-over=${c.over}>
    <div class="hk-cap-h">
      <div><h2 class="panel-title">Before the first token</h2><p class="panel-sub">The hooks on a request’s path share ${c.budget_ms} ms before the first token reaches the client.</p></div>
      ${any ? html`<p class="hk-cap-n"><span class="num">${Math.round(c.used_ms)}</span><span class="small muted">of ${c.budget_ms} ms</span></p>` : nothing}
    </div>
    ${any
      ? html`${useBar(c, { label: 'Time used before the first token' })}
          <p class="small muted" data-cap-words>Each hook’s p95, added up over the stages that count.${c.unmeasured ? ` ${c.unmeasured === 1 ? 'One hook has' : `${c.unmeasured} hooks have`} no call measured yet.` : ''} ${c.over ? 'This is over the cap: a hook with no time left is not called, and its failure policy applies.' : 'A hook with no time left is not called, and its failure policy applies.'}</p>`
      : html`<p class="small muted" data-cap-none>No hook runs on a request’s path. TandemApp relays every request byte for byte, as it does without hooks.</p>`}
  </section>`;
}

/** The request's path with its hooks. */
export function pipelineView(v: PipelineView, on: PipelineHandlers): TemplateResult {
  const total = new Set(v.stops.flatMap((s) => s.groups.flatMap((g) => g.hooks.map((h) => h.id)))).size;
  return html`${capPanel(v)}
    ${total
      ? nothing
      : html`<section class="panel st-empty" data-hooks-none>
          <h2 class="panel-title">No hooks yet</h2>
          <p class="small">A hook is your code at a point of a request’s way through TandemApp: a webhook TandemApp calls, or a built-in. It can watch, change, refuse or decide, as its stop allows.</p>
          <p class="small muted">Pick a stop below and add one there. A new hook starts in shadow: it sees real requests and nothing it answers is applied until you switch it on.</p>
          <div class="st-acts"><a class="btn btn-primary" href=${hooksHref(['new'])} data-act="hook-first">${icon('plus')} Add the first hook</a></div>
        </section>`}
    <section class="panel hk-path" data-pipeline>
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">A request’s path</h2><p class="panel-sub">The ${v.stops.length} stops a request can make, in order. Within a stop, transforms run first, then guards, then deciders; observers watch beside the path. Within a role the lower priority runs first.</p></div></header>
      <ol class="hk-rail">${v.stops.map((s) => stopView(s, v, on))}</ol>
      ${v.writable ? nothing : html`<p class="note small" data-readonly>${icon('info')} The settings interface does not take changes here, so the order and the modes are shown as they are.</p>`}
      <p class="xs faint">A move changes priorities, and a hook has one priority: it moves at every stop it runs at. Nothing is written until you review and apply.</p>
    </section>`;
}
