// Add a hook (VIS-282): one page, five steps in the order the questions come up: where it runs,
// what it does there and how TandemApp calls it, which requests it sees, what happens when it
// fails, and what it may see (the three payload levels, each with what leaves TandemApp in plain
// words). Then a name and how it starts: in shadow unless told otherwise. What the controller
// refuses lands on its field. A full payload to a destination outside the LAN is asked a second
// time. After the hook is made, a webhook's secret shows once, with the code that checks a call's
// signature; a built-in has none. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import { SCOPE_KEYS, type Payload, type Policy, type Role, type StageTable } from '../../api/hooks';
import type { AddState } from '../../lib/hooks-flow';
import { BUILTINS, KIND_WORDS, MODE_WORDS, PAYLOAD_WORDS, POLICY_WORDS, ROLE_WORDS, SCOPE_LABEL, builtinsFor, defaultPolicy, mayWords, policyWords, rolesAt, stageOwner, stageTitle, type HookForm } from '../../lib/hooks';
import { sentence } from '../../lib/settings-form';
import { icon } from '../../ui/bits';
import { back, cards, confirmLeave, field, hooksHref, refused, roleMark, secretOnce, textInput } from './bits';

export interface AddHandlers {
  edit(p: Partial<HookForm>): void;
  /** show the stops again, to choose another */
  pick(): void;
  create(confirmed?: boolean): void;
  /** leave the second question without sending */
  unconfirm(): void;
  lang(l: 'python' | 'node'): void;
  /** the secret is saved (or there is none): on to the hook's page, or its playground */
  done(test: boolean): void;
}

export interface AddView {
  state: AddState;
  table: StageTable;
  builtins: string[];
  /** how many hooks sit at the chosen stage already */
  here: number;
  lang: 'python' | 'node';
  signIn: TemplateResult | typeof nothing;
}

const step = (n: number, title: string, sub: string | null, body: TemplateResult, id: string): TemplateResult =>
  html`<li class="hk-step" data-step=${id}>
    <span class="hk-n num" aria-hidden="true">${n}</span>
    <div class="hk-step-b"><h3 class="hk-step-t">${title}</h3>${sub ? html`<p class="small muted">${sub}</p>` : nothing}${body}</div>
  </li>`;

function stagePick(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  const chosen = v.table.stages[f.stage];
  // a stop is chosen: it stands alone, and the list comes back when another is wanted
  if (chosen && !v.state.picking)
    return html`<div class="hk-stage is-on is-chosen ${chosen.wired ? '' : 'is-later'}" data-stage-chosen=${f.stage}>
      <span class="hk-stage-n num" aria-hidden="true">${Object.keys(v.table.stages).indexOf(f.stage) + 1}</span>
      <span class="hk-stage-t"><b>${stageTitle(f.stage)}</b><span class="num xs muted">${f.stage}</span></span>
      <span class="xs muted hk-stage-w">${sentence(chosen.when)} A hook may ${mayWords(chosen).join(', ')}.${chosen.wired ? '' : ` Not called by this TandemApp yet: ${stageOwner(f.stage)} will call it.`}</span>
      <button type="button" class="btn btn-sm" data-act="stage-change" @click=${on.pick}>Pick another stop</button>
    </div>`;
  return html`<div class="hk-stages" role="radiogroup" aria-label="Where it runs">
      ${Object.entries(v.table.stages).map(
        ([id, s], i) => html`<label class="hk-stage ${f.stage === id ? 'is-on' : ''} ${s.wired ? '' : 'is-later'}" data-stage-pick=${id}>
          <input type="radio" name="hk-stage" .checked=${f.stage === id} @change=${() => on.edit({ stage: id })} />
          <span class="hk-stage-n num" aria-hidden="true">${i + 1}</span>
          <span class="hk-stage-t"><b>${stageTitle(id)}</b><span class="num xs muted">${id}</span></span>
          <span class="xs muted hk-stage-w">${sentence(s.when)} A hook may ${mayWords(s).join(', ')}.${s.wired ? '' : ` Not called by this TandemApp yet: ${stageOwner(id)} will call it.`}</span>
        </label>`,
      )}
    </div>
    ${v.state.errors.stages ? html`<p class="st-err small" role="alert" data-invalid>${v.state.errors.stages}</p>` : nothing}`;
}

function whatStep(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  const e = v.state.errors;
  const roles = f.stage ? rolesAt(v.table, f.stage) : [];
  const fit = builtinsFor(v.builtins, f.role, f.stage);
  const later = v.table.kinds.filter((k) => !v.table.kinds_available.includes(k));
  const b = BUILTINS[f.target];
  return html`${f.stage
      ? cards<Role>('role', f.role, roles.map((r) => ({ id: r, label: ROLE_WORDS[r].label, text: ROLE_WORDS[r].does })), (role) => on.edit({ role }), { label: 'What it does there' })
      : html`<p class="small muted" data-need-stage>Pick where it runs first: a stop takes only some roles.</p>`}
    ${e.role ? html`<p class="st-err small" role="alert" data-invalid>${e.role}</p>` : nothing}
    <h3 class="hk-sub">How TandemApp calls it</h3>
    ${cards<'http' | 'builtin'>(
      'kind',
      f.kind,
      [
        { id: 'http', label: KIND_WORDS.http.label, text: KIND_WORDS.http.text },
        { id: 'builtin', label: KIND_WORDS.builtin.label, text: KIND_WORDS.builtin.text, off: f.role && !fit.length ? `No built-in is a ${f.role} at this stop.` : undefined },
      ],
      (kind) => on.edit({ kind }),
      { label: 'How TandemApp calls it' },
    )}
    ${later.length ? html`<p class="xs muted" data-kinds-later>${later.map((k) => KIND_WORDS[k]?.label ?? k).join(', ')}: not in this version. A plugin’s own hooks join the pipeline while the plugin runs.</p>` : nothing}
    ${f.kind === 'http'
      ? field('endpoint', 'The URL TandemApp calls', textInput('endpoint', f.endpoint, (endpoint) => on.edit({ endpoint }), { placeholder: 'http://hooks.lan:9300/', mono: true, bad: !!e.endpoint, type: 'url' }), { error: e.endpoint, hint: 'One signed POST per call. It answers 200 with a result, or 204 to change nothing.', wide: true })
      : html`${field(
            'target',
            'The built-in',
            html`<select id="hk-target" data-in="target" aria-invalid=${!!e.target} @change=${(x: Event) => on.edit({ target: (x.target as HTMLSelectElement).value, options: {} })}>
              <option value="" ?selected=${!f.target}>${fit.length ? 'Pick one' : 'None fits this role and stop'}</option>
              ${fit.map((n) => html`<option value=${n} ?selected=${f.target === n}>${n}</option>`)}
            </select>`,
            { error: e.target, hint: b?.text },
          )}
          ${(b?.options ?? []).map((o) =>
            field(
              `options.${o.key}`,
              o.label,
              textInput(`options.${o.key}`, f.options[o.key] ?? '', (x) => on.edit({ options: { ...f.options, [o.key]: x } }), { placeholder: o.hint, mono: !!o.json, bad: !!e[`options.${o.key}`] }),
              { error: e[`options.${o.key}`], wide: !!o.json },
            ),
          )}`}`;
}

function scopeStep(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  return html`<div class="hk-grid">
      ${SCOPE_KEYS.map((k) => field(`scope.${k}`, SCOPE_LABEL[k].label, textInput(`scope.${k}`, f.scope[k], (x) => on.edit({ scope: { ...f.scope, [k]: x } }), { placeholder: `all (for example ${SCOPE_LABEL[k].hint})`, mono: true })))}
    </div>
    ${v.state.errors.scope ? html`<p class="st-err small" role="alert" data-invalid>${v.state.errors.scope}</p>` : nothing}
    <p class="xs muted">Patterns with <code>*</code>, commas between them. An empty field matches everything; a request must match every field that is filled.</p>`;
}

function failStep(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  const spec = v.table.stages[f.stage];
  const def = spec && f.role ? defaultPolicy(spec, f.role) : null;
  const off = spec && !spec.sync;
  return html`${off
      ? html`<p class="small muted" data-policy-async>This stop runs beside the request: a failure there is dropped and never reaches a client. An observer is called once more after a failure.</p>`
      : cards<Policy | 'default'>(
          'policy',
          f.policy || 'default',
          [
            { id: 'default', label: 'The stop’s default', text: def ? `Here: ${policyWords(def)}. ${def === 'closed' ? 'A guard that cannot answer refuses the request unless you say otherwise.' : 'The request goes on without the hook.'}` : 'Guards refuse the request, every other hook is skipped.' },
            { id: 'open', label: POLICY_WORDS.open.label, text: POLICY_WORDS.open.text },
            { id: 'closed', label: POLICY_WORDS.closed.label, text: POLICY_WORDS.closed.text },
          ],
          (p) => on.edit({ policy: p === 'default' ? '' : p }),
          { label: 'When it fails' },
        )}
    ${field('timeout_ms', 'Its own time limit (ms)', textInput('timeout_ms', f.timeout, (timeout) => on.edit({ timeout }), { placeholder: spec?.budget_ms != null ? `${spec.budget_ms} (the stop’s budget)` : `${v.table.async_timeout_ms}`, mono: true, bad: !!v.state.errors.timeout_ms, mode: 'numeric' }), {
      error: v.state.errors.timeout_ms,
      hint: spec?.budget_ms != null ? `Empty: the stop’s budget of ${spec.budget_ms} ms, which its hooks share. No answer in time counts as a failure.` : `Empty: ${v.table.async_timeout_ms / 1000} s. No answer in time counts as a failure.`,
    })}
    <p class="xs muted">Five failures in a row open the hook’s breaker: it is skipped for 30 s, then one probe call decides.</p>`;
}

function seesStep(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  const content = v.table.stages[f.stage]?.content ?? [];
  return html`${cards<Payload>(
      'payload',
      f.payload,
      v.table.payloads.map((p) => ({ id: p, label: PAYLOAD_WORDS[p].label, text: PAYLOAD_WORDS[p].sees, note: PAYLOAD_WORDS[p].never })),
      (payload) => on.edit({ payload }),
      { label: 'What it sees' },
    )}
    ${f.role === 'transform' && content.length && f.payload !== 'full'
      ? html`<p class="note small" data-content-note>${icon('info')} At this level it can change the parameters only. Changing ${content.join(', ')} takes the full payload.</p>`
      : nothing}`;
}

function nameStep(v: AddView, on: AddHandlers): TemplateResult {
  const f = v.state.form;
  const e = v.state.errors;
  return html`<div class="hk-grid">
      ${field('name', 'Name', textInput('name', f.name, (name) => on.edit({ name }), { placeholder: 'Temperature cap', bad: !!e.name }), { error: e.name, hint: 'Its id is made from the name.' })}
      ${field('priority', 'Priority', textInput('priority', f.priority, (priority) => on.edit({ priority }), { mono: true, bad: !!e.priority, mode: 'numeric' }), { error: e.priority, hint: `Low runs first within its role.${v.here ? ` ${v.here === 1 ? 'One hook sits' : `${v.here} hooks sit`} at this stop already; the order can be changed on the pipeline.` : ''}` })}
    </div>
    ${cards(
      'mode',
      f.mode,
      (['shadow', 'on', 'off'] as const).map((m) => ({ id: m, label: m === 'shadow' ? 'Start in shadow' : m === 'on' ? 'Start on' : 'Start off', text: MODE_WORDS[m].text })),
      (mode) => on.edit({ mode }),
      { label: 'How it starts' },
    )}`;
}

function created(v: AddView, on: AddHandlers): TemplateResult {
  const c = v.state.created!;
  const h = c.hook;
  return html`<section class="panel hk-created" role="status" data-created=${h.id}>
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">${icon('check')} ${h.name} is added</h2><p class="panel-sub hk-head-meta">${roleMark(h.role)}<span>at ${h.stages.map(stageTitle).join(', ')}</span><span>${MODE_WORDS[h.mode].text}</span></p></div></header>
    ${c.secret ? secretOnce(c.secret, v.lang, { onLang: on.lang, note: 'Your receiver needs it to check that a call comes from this TandemApp.' }) : html`<p class="small muted" data-no-secret>A built-in runs inside TandemApp: there is no secret to keep.</p>`}
    <div class="st-acts">
      <button type="button" class="btn btn-primary" data-act="created-test" @click=${() => on.done(true)}>${c.secret ? 'I saved it. Try the hook' : 'Try the hook'}</button>
      <button type="button" class="btn" data-act="created-done" @click=${() => on.done(false)}>${c.secret ? 'I saved it. Open its page' : 'Open its page'}</button>
    </div>
  </section>`;
}

/** The add-hook page for the flow's state: the form, the second question, or what was made. */
export function addView(v: AddView, on: AddHandlers): TemplateResult {
  const s = v.state;
  if (s.created) return created(v, on);
  const n = Object.keys(s.errors).length;
  return html`${back(hooksHref(), 'Pipeline')}
    <header class="hk-head"><h2 class="hk-title">Add a hook</h2><p class="small muted">Five questions, top to bottom. Nothing is called until the hook is added.</p></header>
    <form class="hk-form" novalidate @submit=${(e: Event) => (e.preventDefault(), on.create(false))} data-add-form>
      <ol class="hk-steps">
        ${step(1, 'Where it runs', 'The stops of a request’s path, in order. It can run at more stops later, from its page.', stagePick(v, on), 'stage')}
        ${step(2, 'What it does there', null, whatStep(v, on), 'what')}
        ${step(3, 'Which requests it runs on', 'Leave everything empty to run on every request.', scopeStep(v, on), 'scope')}
        ${step(4, 'When it fails', 'No answer in time, an error, or an answer the stop does not allow.', failStep(v, on), 'fail')}
        ${step(5, 'What it sees', 'What leaves TandemApp with every call. Start with the least it needs.', seesStep(v, on), 'sees')}
      </ol>
      <section class="panel hk-finish" data-step="name">${nameStep(v, on)}</section>
      ${s.refused ? refused(s.refused) : nothing}
      ${v.signIn}
      ${s.confirm
        ? confirmLeave(s.confirm, { busy: s.busy, go: 'Add it, and send the full payload', onGo: () => on.create(true), onBack: on.unconfirm })
        : html`<div class="hk-form-foot">
            ${n ? html`<p class="small err-ink" role="status" data-form-invalid>${n === 1 ? 'One thing needs' : `${n} things need`} a correction above.</p>` : html`<p class="small muted">A webhook’s signing secret is shown once, right after this.</p>`}
            <div class="st-acts"><a class="btn btn-ghost" href=${hooksHref()} data-act="add-cancel">Cancel</a><button type="submit" class="btn btn-primary" data-act="add-create" ?disabled=${s.busy}>${s.busy ? 'Adding…' : 'Add the hook'}</button></div>
          </div>`}
    </form>`;
}
