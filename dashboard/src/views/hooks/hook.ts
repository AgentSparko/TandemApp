// One hook's page (VIS-282): how it stands now (called or not, its mode, its breaker, its calls and
// times), where it runs and how it is called (changed on the hooks' API: a conflict reads it again,
// a full hook's new endpoint outside the LAN asks first), its switches as settings rows (order,
// scope, failure policy, time limit, what it sees: staged, so they are planned and versioned), its
// signing secret (shown only as a hint; a rotation shows the new one once), the playground (the
// hook once against a stage's sample event, the answer explained, nothing applied or counted) and
// its recent calls, a shadow call with what it would have changed as field names and sizes. Removing
// it asks first. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import { SCOPE_KEYS, type HookCall, type HookMode, type Payload, type Policy, type StageTable } from '../../api/hooks';
import type { OneHook } from '../../lib/hooks-flow';
import { GRACES, KIND_WORDS, MODE_WORDS, PAYLOAD_WORDS, POLICY_WORDS, ROLE_WORDS, SCOPE_LABEL, at, breakerWords, callWords, defaultPolicy, diffRows, explainTest, ms, policyWords, scopeTexts, secretWords, stageTitle, timeoutAt, type HookSwitch, type ScopeKey, type ShownHook } from '../../lib/hooks';
import { icon } from '../../ui/bits';
import { back, cards, chip, confirmLeave, fact, field, hooksHref, refused, roleMark, secretOnce, textInput } from './bits';

export interface HookHandlers {
  stage(field: HookSwitch, value: unknown): void;
  type(field: 'priority' | 'timeout_ms' | `scope.${ScopeKey}`, raw: string): void;
  tidy(field: string): void;
  undo(field: HookSwitch): void;
  edit(): void;
  editField(p: { name?: string; stages?: string[]; endpoint?: string; options?: string }): void;
  save(confirmed?: boolean): void;
  cancelEdit(): void;
  play(p: { stage?: string; raw?: string }): void;
  resetEvent(): void;
  test(): void;
  secret(p: { step?: 'idle' | 'ask'; grace?: number }): void;
  rotate(): void;
  lang(l: 'python' | 'node'): void;
  askRemove(on: boolean): void;
  remove(): void;
  reload(): void;
}

export interface HookView {
  hook: ShownHook;
  table: StageTable;
  one: OneHook;
  writable: boolean;
  now: number;
  lang: 'python' | 'node';
  signIn: TemplateResult | typeof nothing;
}

/** how many rows of a diff show before the rest folds away */
const DIFF_FOLD = 6;
/** the newest calls that show at once, and how many more open below them */
const CALLS_SHOWN = 20;
const CALLS_MAX = 100;

function status(v: HookView, on: HookHandlers): TemplateResult {
  const h = v.hook;
  const m = h.metrics;
  const b = breakerWords(m.breaker);
  const limits = [...new Set(h.stages.map((s) => (v.table.stages[s] ? timeoutAt(h, v.table.stages[s], v.table) : null)).filter((x): x is number => x != null))];
  return html`<section class="panel hk-status" data-panel="status">
    <div class="hk-status-sw">
      <label class="switch st-switch"><input type="checkbox" role="switch" data-in="enabled" .checked=${h.enabled} ?disabled=${!v.writable} @change=${(e: Event) => on.stage('enabled', (e.target as HTMLInputElement).checked)} /><span>${h.enabled ? 'Called' : 'Not called at all'}</span></label>
      <div class="seg" role="group" aria-label="Mode">
        ${(['off', 'shadow', 'on'] as const).map((x: HookMode) => html`<button type="button" class="seg-btn ${h.mode === x ? 'is-on' : ''}" aria-pressed=${h.mode === x} data-mode-to=${x} ?disabled=${!v.writable} @click=${() => on.stage('mode', x)}>${MODE_WORDS[x].label}</button>`)}
      </div>
      ${h.staged.has('enabled') || h.staged.has('mode') ? html`<span class="st-mark" data-mark>changed</span>` : nothing}
      <p class="small muted hk-status-say" data-mode-words>${h.enabled ? MODE_WORDS[h.mode].text : 'Switched off: it is not called, whatever its mode.'}</p>
    </div>
    <dl class="hk-facts">
      ${fact('Breaker', html`${chip(b.tone, b.label.replace('breaker ', ''), { data: 'breaker' })}`, { sub: b.text, data: 'breaker' })}
      ${fact('Calls', html`<span class="num">${m.calls}</span>`, { sub: `${m.failures} failed, ${m.timeouts} timed out, ${m.skipped} skipped`, tone: m.failures ? 'warn' : undefined, data: 'calls' })}
      ${fact('Time', html`<span class="num">${m.p50_ms == null ? '–' : ms(m.p50_ms)}</span>`, { sub: m.p95_ms == null ? 'No call measured yet.' : `p50; p95 ${ms(m.p95_ms)}${limits.length ? `, limit ${limits.join(' / ')} ms` : ''}`, data: 'time' })}
      ${fact('What it did', html`<span class="hk-outcomes">${Object.keys(m.outcomes).length ? Object.entries(m.outcomes).sort((a, b) => b[1] - a[1]).map(([k, n]) => html`<span><span class="num">${n}</span> ${k.replace('_', ' ')}</span>`) : 'nothing yet'}</span>`, { data: 'outcomes' })}
    </dl>
  </section>`;
}

function where(v: HookView, on: HookHandlers): TemplateResult {
  const h = v.hook;
  const e = v.one.edit;
  if (e) {
    return html`<section class="panel" data-panel="where" data-editing>
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Where it runs and how it is called</h2><p class="panel-sub">Saved at once on the hooks’ API, not staged. Its kind and role are fixed: make a new hook for another.</p></div></header>
      <form class="hk-form" novalidate @submit=${(x: Event) => (x.preventDefault(), on.save(false))}>
        ${field('name', 'Name', textInput('name', e.name, (name) => on.editField({ name }), { bad: !!e.errors.name }), { error: e.errors.name })}
        <fieldset class="hk-fieldset" data-field="stages">
          <legend class="hk-label">Stops it runs at</legend>
          <div class="hk-checks">
            ${Object.entries(v.table.stages).map(([id, s]) => {
              const ok = s.roles.includes(h.role);
              return html`<label class="hk-check ${ok ? '' : 'is-off'}"><input type="checkbox" data-in="stage-${id}" .checked=${e.stages.includes(id)} ?disabled=${!ok} @change=${(x: Event) => on.editField({ stages: (x.target as HTMLInputElement).checked ? [...e.stages, id] : e.stages.filter((y) => y !== id) })} /><span>${stageTitle(id)}</span><span class="num xs muted">${id}</span>${ok ? nothing : html`<span class="xs muted">takes no ${h.role}</span>`}</label>`;
            })}
          </div>
          ${e.errors.stages ? html`<p class="st-err small" role="alert" data-invalid>${e.errors.stages}</p>` : nothing}
        </fieldset>
        ${h.kind === 'http'
          ? field('endpoint', 'The URL TandemApp calls', textInput('endpoint', e.endpoint, (endpoint) => on.editField({ endpoint }), { mono: true, bad: !!e.errors.endpoint, type: 'url' }), { error: e.errors.endpoint, wide: true, hint: h.payload === 'full' ? 'This hook gets the full payload: moving it takes pairing’s rights, and a place outside the LAN asks a second time.' : undefined })
          : field('options', `Options of the built-in ${h.target ?? ''} (JSON)`, html`<textarea id="hk-options" class="st-area code" rows="5" data-in="options" spellcheck="false" aria-invalid=${!!e.errors.options} .value=${e.options} @input=${(x: Event) => on.editField({ options: (x.target as HTMLTextAreaElement).value })}></textarea>`, { error: e.errors.options, wide: true })}
        ${e.refused ? refused(e.refused) : nothing}
        ${v.signIn}
        ${e.confirm
          ? confirmLeave(e.confirm, { busy: e.busy, go: 'Save, and send the full payload there', onGo: () => on.save(true), onBack: () => on.editField({}) })
          : html`<div class="st-acts"><button type="button" class="btn btn-ghost" data-act="edit-cancel" ?disabled=${e.busy} @click=${on.cancelEdit}>Cancel</button><button type="submit" class="btn btn-primary" data-act="edit-save" ?disabled=${e.busy}>${e.busy ? 'Saving…' : 'Save'}</button></div>`}
      </form>
    </section>`;
  }
  return html`<section class="panel" data-panel="where">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Where it runs and how it is called</h2></div><div class="panel-tools"><button type="button" class="btn btn-sm" data-act="edit" @click=${on.edit}>${icon('edit')} Change</button></div></header>
    ${v.one.notice ? html`<p class="note note-info small" role="status" data-notice>${icon('info')} ${v.one.notice}</p>` : nothing}
    <dl class="hk-kv">
      <div><dt>Runs at</dt><dd class="hk-chips">${h.stages.map((s) => html`<span class="st-chip is-quiet" title=${s} data-stage-chip=${s}>${stageTitle(s)}</span>`)}</dd></div>
      <div><dt>Role</dt><dd>${roleMark(h.role)} <span class="muted">${ROLE_WORDS[h.role].does}</span></dd></div>
      <div><dt>Called as</dt><dd>${KIND_WORDS[h.kind]?.label ?? h.kind}${h.kind === 'http' ? html`: <span class="num st-wrap" data-endpoint>${h.endpoint}</span> ${h.destination.lan ? chip('quiet', 'on the LAN', { data: 'lan' }) : chip('warn', 'outside the LAN', { data: 'lan' })}` : html`: <span class="num">${h.target}</span>${Object.keys(h.options).length ? html` <span class="num xs muted st-wrap">${JSON.stringify(h.options)}</span>` : nothing}`}</dd></div>
      <div><dt>Changed</dt><dd class="muted">${at(h.updated_at)}${h.updated_by ? ` by ${h.updated_by}` : ''}, version <span class="num">${h.version}</span></dd></div>
    </dl>
  </section>`;
}

/** one settings row: the label and its sentence, the control, Undo while it is staged */
function row(v: HookView, on: HookHandlers, key: HookSwitch, label: string, text: string, control: TemplateResult, o: { wide?: boolean } = {}): TemplateResult {
  const staged = v.hook.staged.has(key);
  return html`<div class="st-row ${staged ? 'is-staged' : ''} ${o.wide ? 'is-wide' : ''}" data-key=${`hooks.${v.hook.id}.${key}`} data-staged=${staged ? 'yes' : 'no'}>
    <div class="st-row-l"><span class="st-label">${label}</span>${staged ? html`<span class="st-mark" data-mark>changed</span>` : nothing}<p class="st-text small muted">${text}</p></div>
    <div class="st-row-c">${control}</div>
    <div class="st-row-a">${staged ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => on.undo(key)}>Undo</button>` : nothing}</div>
  </div>`;
}

function settings(v: HookView, on: HookHandlers): TemplateResult {
  const h = v.hook;
  const o = v.one;
  const specs = h.stages.map((s) => v.table.stages[s]).filter(Boolean);
  const defaults = [...new Set(specs.map((s) => policyWords(defaultPolicy(s, h.role))))].join(' or ');
  const sync = specs.some((s) => s.sync);
  const budgets = [...new Set(specs.map((s) => s.budget_ms).filter((x): x is number => x != null))];
  const texts = scopeTexts(h.scope);
  const g = h.grant;
  return html`<section class="panel st-group" data-panel="settings">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Settings</h2><p class="panel-sub">Staged like every other setting: nothing is written until you review and apply, and each change is a version you can restore.</p></div></header>
    ${v.writable ? nothing : html`<p class="note small" data-readonly>${icon('info')} The settings interface does not take changes here.</p>`}
    <div class="st-rows">
      ${row(v, on, 'priority', 'Priority', 'Its place among the hooks of its role at a stop: low runs first, ties by id.', html`${textInput('priority', o.text.priority ?? String(h.priority), (x) => on.type('priority', x), { mono: true, bad: !!o.invalid.priority, mode: 'numeric', blur: () => on.tidy('priority') })}${o.invalid.priority ? html`<p class="st-err small" role="alert" data-invalid>${o.invalid.priority}</p>` : nothing}`)}
      ${row(
        v,
        on,
        'scope',
        'Runs on',
        'The requests it sees. Patterns with *, commas between them. Empty: every request.',
        html`<div class="hk-grid">
            ${SCOPE_KEYS.map((k) => html`<label class="hk-mini"><span class="xs muted">${SCOPE_LABEL[k].label}</span>${textInput(`scope.${k}`, o.text[`scope.${k}`] ?? texts[k], (x) => on.type(`scope.${k}`, x), { placeholder: 'all', mono: true, bad: !!o.invalid[`scope.${k}`], blur: () => on.tidy(`scope.${k}`) })}</label>`)}
          </div>
          ${SCOPE_KEYS.map((k) => (o.invalid[`scope.${k}`] ? html`<p class="st-err small" role="alert" data-invalid>${o.invalid[`scope.${k}`]}</p>` : nothing))}`,
        { wide: true },
      )}
      ${row(
        v,
        on,
        'policy',
        'When it fails',
        sync ? 'No answer in time, an error, or an answer its stop does not allow.' : 'It runs beside the request: a failure is dropped and never reaches a client.',
        html`<div class="seg" role="group" aria-label="When it fails">
            ${([null, 'open', 'closed'] as (Policy | null)[]).map((p) => html`<button type="button" class="seg-btn ${h.policy === p ? 'is-on' : ''}" aria-pressed=${h.policy === p} data-policy-to=${p ?? 'default'} ?disabled=${!v.writable} @click=${() => on.stage('policy', p)}>${p === null ? 'default' : p}</button>`)}
          </div>
          <p class="xs muted" data-policy-words>${h.policy ? `${POLICY_WORDS[h.policy].label}. ${POLICY_WORDS[h.policy].text}` : `The stop’s default: ${defaults || 'fails open'}.`}</p>`,
      )}
      ${row(v, on, 'timeout_ms', 'Time limit (ms)', 'Its own limit for one call. No answer in time counts as a failure.', html`${textInput('timeout_ms', o.text.timeout_ms ?? (h.timeout_ms == null ? '' : String(h.timeout_ms)), (x) => on.type('timeout_ms', x), { placeholder: budgets.length ? `${budgets.join(' / ')} (the stop’s budget)` : `${v.table.async_timeout_ms}`, mono: true, bad: !!o.invalid.timeout_ms, mode: 'numeric', blur: () => on.tidy('timeout_ms') })}
        ${o.invalid.timeout_ms ? html`<p class="st-err small" role="alert" data-invalid>${o.invalid.timeout_ms}</p>` : html`<p class="xs muted">${budgets.length ? `At most its stop’s budget (${budgets.join(' / ')} ms), which the hooks there share: a longer limit is cut to it.` : `Empty: ${v.table.async_timeout_ms / 1000} s.`}</p>`}`)}
      ${row(
        v,
        on,
        'payload',
        'What it sees',
        'What leaves TandemApp with every call.',
        html`${cards<Payload>('payload', h.payload, v.table.payloads.map((p) => ({ id: p, label: PAYLOAD_WORDS[p].label, text: PAYLOAD_WORDS[p].sees, note: PAYLOAD_WORDS[p].never, off: v.writable ? undefined : 'Not changed from here.' })), (p) => on.stage('payload', p), { label: 'What it sees' })}
          ${h.staged.has('payload') && h.payload === 'full'
            ? html`<p class="rs-warn small" data-grant-note>${icon('warn')}<span>It would go to ${h.destination.host ?? 'this controller'}${h.destination.lan ? ', on the LAN' : ', outside the LAN: the review asks a second time'}. Granting it needs pairing’s rights.</span></p>`
            : nothing}
          ${g ? html`<p class="xs muted" data-grant>Granted by ${g.by}, ${at(g.at)}, for ${g.destination ?? 'this controller'}${g.lan ? ' on the LAN. Every call checks that the address it reached is still on the LAN.' : `, outside the LAN${g.confirmed_by ? `; confirmed by ${g.confirmed_by}` : ''}.`}</p>` : nothing}`,
        { wide: true },
      )}
    </div>
  </section>`;
}

function secret(v: HookView, on: HookHandlers): TemplateResult | typeof nothing {
  const h = v.hook;
  if (h.kind !== 'http') return nothing;
  const s = v.one.secret;
  return html`<section class="panel" data-panel="secret" data-secret-step=${s.step}>
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Signing secret</h2><p class="panel-sub" data-secret-words>${secretWords(h.secret, v.now)}</p></div>
      ${s.step === 'idle' ? html`<div class="panel-tools"><button type="button" class="btn btn-sm" data-act="secret-ask" @click=${() => on.secret({ step: 'ask' })}>Rotate the secret…</button></div>` : nothing}</header>
    ${s.step === 'ask' || s.step === 'busy'
      ? html`<div class="hk-rotate" data-rotate>
          <p class="small">A new secret is made and shown once. Your receiver must take it, or its calls stop checking out. How long may the old one still sign beside it?</p>
          ${cards('grace', String(s.grace), GRACES.map((x) => ({ id: String(x.s), label: x.label, text: x.text })), (x) => on.secret({ grace: Number(x) }), { label: 'How long the old secret still signs' })}
          ${s.refused ? refused(s.refused) : nothing}
          ${v.signIn}
          <div class="st-acts"><button type="button" class="btn btn-ghost" data-act="secret-cancel" ?disabled=${s.step === 'busy'} @click=${() => on.secret({ step: 'idle' })}>Not now</button><button type="button" class="btn btn-primary" data-act="secret-rotate" ?disabled=${s.step === 'busy'} @click=${on.rotate}>${s.step === 'busy' ? 'Rotating…' : 'Rotate, and show the new secret'}</button></div>
        </div>`
      : nothing}
    ${s.step === 'shown' && s.value
      ? html`${secretOnce(s.value, v.lang, { onLang: on.lang, note: 'This is the new secret.' })}
          <div class="st-acts"><button type="button" class="btn btn-primary" data-act="secret-saved" @click=${() => on.secret({ step: 'idle' })}>I saved it</button></div>`
      : nothing}
  </section>`;
}

function playground(v: HookView, on: HookHandlers): TemplateResult {
  const h = v.hook;
  const p = v.one.play;
  const spec = v.table.stages[p.stage];
  const x = p.result ? explainTest(p.result, spec ? timeoutAt(h, spec, v.table) : null) : null;
  return html`<section class="panel" id="hk-playground" data-panel="playground">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Try it</h2><p class="panel-sub">The hook is called once with a made-up event, whatever its mode, scope or breaker. Nothing is applied or counted, and nothing goes to an engine.</p></div></header>
    <div class="hk-play">
      <div class="hk-play-in">
        ${h.stages.length > 1
          ? field('play-stage', 'As if at', html`<select id="hk-play-stage" data-in="play-stage" @change=${(e: Event) => on.play({ stage: (e.target as HTMLSelectElement).value })}>${h.stages.map((s) => html`<option value=${s} ?selected=${s === p.stage}>${stageTitle(s)} (${s})</option>`)}</select>`)
          : html`<p class="small" data-play-stage>As if at <b>${stageTitle(p.stage)}</b> <span class="num xs muted">${p.stage}</span></p>`}
        ${field('event', 'The event it gets', html`<textarea id="hk-event" class="st-area code" rows="9" data-in="event" spellcheck="false" autocapitalize="off" aria-invalid=${!!p.invalid} aria-describedby="hk-event-e" .value=${p.raw} @input=${(e: Event) => on.play({ raw: (e.target as HTMLTextAreaElement).value })}></textarea>`, {
          error: p.invalid ?? undefined,
          hint: h.payload === 'meta' ? 'The stop’s sample event at the payload level meta. Change a value to try another case.' : `The stop’s sample facts. At its payload level (${h.payload}) the hook also gets the sample’s text as content.`,
          wide: true,
        })}
        <div class="st-acts">
          <button type="button" class="btn btn-primary" data-act="play-send" ?disabled=${p.busy || !!p.invalid} @click=${on.test}>${icon('play')} ${p.busy ? 'Calling…' : 'Send the test call'}</button>
          <button type="button" class="btn btn-ghost" data-act="play-reset" @click=${on.resetEvent}>Back to the sample</button>
        </div>
      </div>
      <div class="hk-play-out" aria-live="polite" data-play-result=${p.result?.outcome ?? (p.refused ? 'refused' : 'none')}>
        ${p.refused ? refused(p.refused) : nothing}
        ${x
          ? html`<div class="hk-explain is-${x.tone}" data-explain>
                <p class="hk-explain-t">${x.title}</p>
                ${x.lines.length ? html`<ul class="small">${x.lines.map((l) => html`<li>${l}</li>`)}</ul>` : nothing}
                <p class="xs muted">Nothing was applied or counted.</p>
              </div>
              ${p.result!.result ? html`<details class="st-doc"><summary class="small">Its answer, as it came</summary><pre class="hk-pre" data-raw-result><code>${JSON.stringify(p.result!.result, null, 2)}</code></pre></details>` : nothing}`
          : p.refused
            ? nothing
            : html`<p class="small muted" data-play-idle>The answer shows here: what the hook said, how long it took, and what that would do to a request.</p>`}
      </div>
    </div>
  </section>`;
}

/** what a shadow call would have changed: field names and sizes, the long ones folded away */
function diffOf(c: HookCall): TemplateResult | typeof nothing {
  const rows = diffRows(c.diff);
  if (!rows.length) return nothing;
  const line = (d: { field: string; text: string }) => html`<li><span class="num">${d.field}</span> <span class="muted">${d.text}</span></li>`;
  return html`<ul class="hk-diff xs" data-diff=${rows.length}>${rows.slice(0, DIFF_FOLD).map(line)}</ul>
    ${rows.length > DIFF_FOLD ? html`<details class="st-doc" data-diff-more><summary class="xs">and ${rows.length - DIFF_FOLD} more ${rows.length - DIFF_FOLD === 1 ? 'field' : 'fields'}</summary><ul class="hk-diff xs">${rows.slice(DIFF_FOLD).map(line)}</ul></details>` : nothing}`;
}

/** A hook's journal rows: when, where, how it ended and what it did. `trace`: the link to the
 *  call's request (not on the trace itself, where every row is that request's). */
export function callRows(calls: HookCall[], o: { stage?: boolean; trace?: boolean } = { stage: true, trace: true }): TemplateResult {
  return html`<ol class="hk-calls" data-calls=${calls.length}>
    ${calls.map((c) => {
      const w = callWords(c);
      return html`<li class="hk-call is-${w.tone}" data-outcome=${c.outcome} data-call-mode=${c.mode}>
        <span class="small muted hk-call-at">${at(c.ts)}</span>
        <span class="hk-call-what">
          ${chip(w.tone, w.outcome, { data: 'outcome' })}${c.mode === 'on' ? nothing : chip('quiet', c.mode, { data: 'mode' })}
          <span class="small" data-did>${w.did}</span>
          ${diffOf(c)}
        </span>
        ${o.stage ? html`<span class="xs muted" title=${c.stage}>${stageTitle(c.stage)}</span>` : nothing}
        <span class="num small hk-call-ms">${ms(c.duration_ms)}</span>
        ${o.trace === false ? nothing : c.request && c.mode !== 'test' ? html`<a class="btn btn-sm btn-ghost num hk-call-req" href=${hooksHref(['trace'], { request: c.request })} title="Show this request across every stop" data-act="call-trace">${c.request.slice(-8)}</a>` : html`<span></span>`}
      </li>`;
    })}
  </ol>`;
}

function calls(v: HookView, on: HookHandlers): TemplateResult {
  const o = v.one;
  return html`<section class="panel" data-panel="calls">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Recent calls</h2><p class="panel-sub">Newest first: ids, outcomes and sizes, never a payload. TandemApp keeps the last 500 calls of a hook in memory, since it started.</p></div><div class="panel-tools"><button type="button" class="btn btn-sm btn-ghost" data-act="calls-reload" @click=${on.reload}>${icon('refresh')} Read again</button></div></header>
    ${o.callsRefused ? refused(o.callsRefused, { quiet: true }) : nothing}
    ${!o.calls && !o.callsRefused ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:82%"></div><div class="skeleton-line" style="width:64%"></div></div>` : nothing}
    ${o.calls?.length === 0 ? html`<p class="small muted" data-calls-none>${v.hook.enabled && v.hook.mode !== 'off' ? 'No call yet. A request in its scope makes one; so does a test call above.' : 'No call yet: the hook is off.'}</p>` : nothing}
    ${o.calls?.length ? callRows(o.calls.slice(0, CALLS_SHOWN), { stage: v.hook.stages.length > 1, trace: true }) : nothing}
    ${o.calls && o.calls.length > CALLS_SHOWN
      ? html`<details class="st-doc" data-calls-more><summary class="small">${Math.min(o.calls.length, CALLS_MAX) - CALLS_SHOWN} older calls${o.calls.length > CALLS_MAX ? ` (of ${o.calls.length} kept)` : ''}</summary>${callRows(o.calls.slice(CALLS_SHOWN, CALLS_MAX), { stage: v.hook.stages.length > 1, trace: true })}</details>`
      : nothing}
  </section>`;
}

function removal(v: HookView, on: HookHandlers): TemplateResult {
  const r = v.one.remove;
  return html`<section class="panel hk-danger" data-panel="remove">
    ${r.step === 'idle'
      ? html`<div class="hk-danger-row"><div><h2 class="panel-title">Remove this hook</h2><p class="panel-sub">It stops being called at once. Its secret and its journal go with it.</p></div><button type="button" class="btn st-danger" data-act="remove-ask" @click=${() => on.askRemove(true)}>${icon('trash')} Remove…</button></div>`
      : html`<div role="alertdialog" aria-labelledby="hk-remove-t" data-remove-ask>
          <h2 class="panel-title" id="hk-remove-t">Remove ${v.hook.name}?</h2>
          <p class="small">It is taken out of ${v.hook.stages.length === 1 ? 'its stop' : `its ${v.hook.stages.length} stops`} now, not staged. This cannot be undone: a new hook gets a new secret.</p>
          ${r.refused ? refused(r.refused) : nothing}
          ${v.signIn}
          <div class="st-acts"><button type="button" class="btn btn-ghost" data-act="remove-cancel" ?disabled=${r.step === 'busy'} @click=${() => on.askRemove(false)}>Keep it</button><button type="button" class="btn hk-go-danger" data-act="remove-go" ?disabled=${r.step === 'busy'} @click=${on.remove}>${r.step === 'busy' ? 'Removing…' : 'Remove the hook'}</button></div>
        </div>`}
  </section>`;
}

/** The hook's page. */
export function hookView(v: HookView, on: HookHandlers): TemplateResult {
  const h = v.hook;
  return html`${back(hooksHref(), 'Pipeline')}
    <header class="hk-head" data-hook-page=${h.id}>
      <h2 class="hk-title">${h.name}</h2>
      <p class="small muted hk-head-meta">${roleMark(h.role)}<span class="num">${h.id}</span><span>${h.stages.map(stageTitle).join(', ')}</span></p>
    </header>
    ${v.one.refused ? refused(v.one.refused) : nothing}
    ${status(v, on)} ${where(v, on)} ${settings(v, on)} ${playground(v, on)} ${calls(v, on)} ${secret(v, on)} ${removal(v, on)}`;
}

/** A hook that is no longer there: removed on another screen, or a link that never was one. */
export function hookGone(id: string): TemplateResult {
  return html`${back(hooksHref(), 'Pipeline')}
    <section class="panel st-empty" data-hook-gone=${id}>
      <h2 class="panel-title">No hook ${id}</h2>
      <p class="small muted">It was removed, here or through the API. Its journal went with it.</p>
      <div class="st-acts"><a class="btn btn-primary" href=${hooksHref()}>Back to the pipeline</a></div>
    </section>`;
}
