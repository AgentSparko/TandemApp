// The decision providers' pages (VIS-282). The conductor asks small typed questions all the time; a
// provider is who answers. The list starts from the questions: who answers each, who only watches.
// Then every provider with its health and its mode (shadow, A/B with its share and what the split
// sticks to, on), and the agreement report with the arms side by side: on, shadow, and the two
// sides of an A/B. A provider's page holds its health with each endpoint's probe, the questions it
// answers with their endpoint, model, time limit and key (a key shows as its last four characters,
// never more), a test call, why the built-in answered instead, and its calls. The switches,
// mapping and keys are settings keys, staged; name, endpoint, model and time limit are saved on
// the providers' API. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { Arm, Health, Hook, ProviderKind, ProviderList, ProviderMode, Report, Sticky } from '../../api/hooks';
import type { AddProvider, OneProvider } from '../../lib/hooks-flow';
import { at, breakerWords, ms } from '../../lib/hooks';
import { ARM_WORDS, KIND_WORDS, MODE_WORDS, STICKY_WORDS, adapterHooks, cellWords, conflicts, explainProviderTest, fallbackReasons, healthWords, keyWords, modeWords, probeWords, providerCallWords, reportRows, reportWords, takenBy, whoAnswers, type ProviderForm, type ProviderSwitch, type ShownProvider } from '../../lib/providers';
import { icon } from '../../ui/bits';
import { back, cards, chip, confirmLeave, field, hooksHref, providersHref, refused, textInput } from './bits';

export interface ProvidersHandlers {
  stage(id: string, field: ProviderSwitch, value: unknown): void;
  hours(h: number): void;
}

export interface ProviderHandlers extends ProvidersHandlers {
  share(raw: string): void;
  undo(field: ProviderSwitch): void;
  question(q: string, on: boolean): void;
  map(q: string, field: 'model' | 'endpoint' | 'timeout_ms', raw: string): void;
  tidy(field: string): void;
  keyField(q: string, open: boolean): void;
  key(q: string, raw: string): void;
  removeKey(q: string): void;
  undoKey(q: string): void;
  probe(): void;
  edit(): void;
  editField(p: Partial<Record<'name' | 'endpoint' | 'model' | 'timeout' | 'hook_id', string>>): void;
  save(): void;
  cancelEdit(): void;
  setTest(p: { question?: string; text?: string }): void;
  test(): void;
  reload(): void;
  askRemove(on: boolean): void;
  remove(): void;
}

const MODES: ProviderMode[] = ['shadow', 'ab', 'on'];
const HOURS = [
  { h: 1, label: '1 hour' },
  { h: 24, label: '24 hours' },
  { h: 168, label: '7 days' },
];

function modeSeg(p: ShownProvider, writable: boolean, on: (m: ProviderMode) => void): TemplateResult {
  return html`<div class="seg" role="group" aria-label=${`Mode of ${p.name}`}>
    ${MODES.map((m) => html`<button type="button" class="seg-btn ${p.mode === m ? 'is-on' : ''}" aria-pressed=${p.mode === m} title=${MODE_WORDS[m].text} data-mode-to=${m} ?disabled=${!writable} @click=${() => on(m)}>${MODE_WORDS[m].label}</button>`)}
  </div>`;
}

function healthChip(h: Health | undefined, enabled: boolean): TemplateResult {
  const w = healthWords(h ?? (enabled ? null : { status: 'off' }));
  return chip(w.tone, w.label, { title: w.text, data: 'health' });
}

function conflictNote(list: ShownProvider[]): TemplateResult | typeof nothing {
  const c = conflicts(list);
  if (!c.length) return nothing;
  return html`<div class="rs-warn small" role="alert" data-conflict>${icon('warn')}<div>${c.map((x) => html`<p>${x.names.join(' and ')} would both answer <span class="num">${x.question}</span>.</p>`)}<p>One provider answers a question, on or in A/B. Put the other in shadow before you apply, or the review refuses it.</p></div></div>`;
}

// ---- the report -----------------------------------------------------------------------------------

/** One provider's report: a row a question, its arms side by side. */
export function reportTable(report: Report, pid: string): TemplateResult {
  const { arms, rows } = reportRows(report, pid);
  if (!rows.length || !arms.length) return html`<p class="small muted" data-report-none=${pid}>No call in this time.</p>`;
  return html`<div class="hk-report" style="--arms:${arms.length}" data-report=${pid} data-arms=${arms.join(' ')}>
    <div class="hk-report-h" aria-hidden="true"><span>Question</span>${arms.map((a: Arm) => html`<span title=${ARM_WORDS[a].text}>${ARM_WORDS[a].label}</span>`)}</div>
    ${rows.map(
      (r) => html`<div class="hk-report-r" data-report-question=${r.question}>
        <span class="hk-report-q"><b class="num">${r.question}</b><span class="xs muted">${r.calls} ${r.calls === 1 ? 'call' : 'calls'}</span></span>
        ${r.cells.map((c) => {
          if (!c.bucket) return html`<span class="hk-report-c is-none" data-arm=${c.arm}><span class="hk-arm-l xs">${ARM_WORDS[c.arm].label}</span><span class="xs muted">no call</span></span>`;
          const w = cellWords(c.bucket, c.arm);
          return html`<span class="hk-report-c" data-arm=${c.arm} data-agreement=${c.bucket.agreement_pct ?? ''}>
            <span class="hk-arm-l xs">${ARM_WORDS[c.arm].label}</span>
            <b class="hk-report-n">${w.agreement ?? w.answered}</b>
            <span class="xs muted">${w.agreement ? `${w.calls}, ${w.answered}` : w.calls}</span>
            <span class="xs muted num">${w.time}</span>
            ${w.trouble ? html`<span class="xs warn-ink" data-trouble>${w.trouble}</span>` : nothing}
            ${w.top ? html`<span class="xs muted">${w.top}</span>` : nothing}
          </span>`;
        })}
      </div>`,
    )}
  </div>`;
}

function reportPanel(report: Report | null, err: TemplateResult | typeof nothing, hours: number, ids: { id: string; name: string }[], on: ProvidersHandlers, single = false): TemplateResult {
  return html`<section class="panel" data-panel="report">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">${single ? 'How it did' : 'Agreement report'}</h2><p class="panel-sub">${report ? reportWords(report) : 'From the conductor’s journal.'} Agreement is measured in shadow: there the provider’s answer is compared with the conductor’s own decision.</p></div>
      <div class="panel-tools"><div class="seg hk-hours" role="group" aria-label="Time span">${HOURS.map((x) => html`<button type="button" class="seg-btn ${hours === x.h ? 'is-on' : ''}" aria-pressed=${hours === x.h} data-hours=${x.h} @click=${() => on.hours(x.h)}>${x.label}</button>`)}</div></div></header>
    ${err}
    ${!report && err === nothing ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:80%"></div><div class="skeleton-line" style="width:62%"></div></div>` : nothing}
    ${report ? ids.map((p) => html`${single ? nothing : html`<h3 class="hk-sub"><a href=${providersHref(['p', p.id])}>${p.name}</a></h3>`}${reportTable(report, p.id)}`) : nothing}
    ${report && !ids.length ? html`<p class="small muted">No provider yet.</p>` : nothing}
  </section>`;
}

// ---- the list -------------------------------------------------------------------------------------

export interface ProvidersView {
  info: ProviderList;
  list: ShownProvider[];
  health: Record<string, Health>;
  report: Report | null;
  reportRefused: TemplateResult | typeof nothing;
  hours: number;
  writable: boolean;
}

export function providersView(v: ProvidersView, on: ProvidersHandlers): TemplateResult {
  const who = whoAnswers(v.info.questions, v.list);
  return html`${v.info.replay ? html`<p class="note note-info small" role="status" data-replay-on>${icon('info')} Recorded answers are being replayed (TANDEM_DECISION_REPLAY): no provider is called, and a question with no recorded answer falls back to the built-in.</p>` : nothing}
    ${conflictNote(v.list)}
    <section class="panel" data-panel="questions">
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Who answers what</h2><p class="panel-sub">The conductor asks these questions around every request. Each has a fixed set of answers; whoever answers does it in tens of milliseconds.</p></div></header>
      <ol class="hk-who">
        ${who.map(
          (q) => html`<li class="hk-who-r ${q.answers ? 'is-yours' : ''}" data-question=${q.id} data-answers=${q.answers?.id ?? 'builtin'}>
            <span class="hk-who-q"><b class="num">${q.id}</b><span class="xs muted">${q.what}</span></span>
            <span class="hk-who-a">${q.answers ? html`<a href=${providersHref(['p', q.answers.id])}>${q.answers.name}</a> <span class="xs muted">${q.answers.words}</span>` : html`<span class="muted">the built-in decider</span>`}</span>
            <span class="hk-who-w xs">${q.watching.length ? html`<span class="muted">watching in shadow:</span> ${q.watching.map((w, i) => html`${i ? ', ' : ''}<a href=${providersHref(['p', w.id])}>${w.name}</a>`)}` : nothing}</span>
          </li>`,
        )}
      </ol>
    </section>
    ${v.list.length
      ? html`<section class="panel" data-panel="providers">
          <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Providers</h2><p class="panel-sub">Start one in shadow, read its agreement below, then let it answer a share, then everything.</p></div></header>
          <ol class="hk-provs">
            ${v.list.map((p) => {
              const h = v.health[p.id];
              const probe = h?.endpoints[0]?.probe;
              return html`<li class="hk-prov is-${p.enabled ? p.mode : 'off'} ${p.staged.size || Object.keys(p.keys).length ? 'is-staged' : ''}" data-provider=${p.id} data-mode=${p.mode} data-enabled=${p.enabled} data-health=${h?.status ?? ''}>
                <div class="hk-row-b">
                  <div class="hk-row-h"><a class="hk-name" href=${providersHref(['p', p.id])}>${p.name}</a>${healthChip(h, p.enabled)}${p.staged.size || Object.keys(p.keys).length ? html`<span class="st-mark" data-mark>changed</span>` : nothing}</div>
                  <p class="hk-meta xs">
                    <span data-fact="kind">${KIND_WORDS[p.kind].label.toLowerCase()}</span>
                    <span data-fact="mode">${modeWords(p)}</span>
                    <span data-fact="questions">${Object.keys(p.questions).join(', ')}</span>
                    ${probe ? html`<span data-fact="probe">${probeWords(probe).text}</span>` : nothing}
                  </p>
                </div>
                <div class="hk-prov-c">
                  <label class="switch"><input type="checkbox" role="switch" data-in="enabled" .checked=${p.enabled} ?disabled=${!v.writable} @change=${(e: Event) => on.stage(p.id, 'enabled', (e.target as HTMLInputElement).checked)} /><span class="sr-only">${p.name} is asked</span></label>
                  ${modeSeg(p, v.writable, (m) => on.stage(p.id, 'mode', m))}
                </div>
              </li>`;
            })}
          </ol>
        </section>`
      : html`<section class="panel st-empty" data-providers-none>
          <h2 class="panel-title">No provider yet</h2>
          <p class="small">The conductor’s own decider answers every question. Put your own model in its place for the questions you pick: a service that speaks the systemone wire, or a hook you wrote for any other API.</p>
          <p class="small muted">A new provider starts in shadow, so nothing changes until its answers look right.</p>
          <div class="st-acts"><a class="btn btn-primary" href=${providersHref(['new'])} data-act="provider-first">${icon('plus')} Add the first provider</a></div>
        </section>`}
    ${v.list.length ? reportPanel(v.report, v.reportRefused, v.hours, v.list, on) : nothing}`;
}

// ---- one provider ---------------------------------------------------------------------------------

export interface ProviderView {
  provider: ShownProvider;
  info: ProviderList;
  list: ShownProvider[];
  health: Health | undefined;
  report: Report | null;
  reportRefused: TemplateResult | typeof nothing;
  hours: number;
  one: OneProvider;
  /** the hooks a hook provider may name */
  hooks: Pick<Hook, 'id' | 'name' | 'role' | 'stages'>[];
  writable: boolean;
  signIn: TemplateResult | typeof nothing;
}

function providerStatus(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const p = v.provider;
  const o = v.one;
  const staged = ['enabled', 'mode', 'ab_percent', 'sticky'].some((k) => p.staged.has(k as ProviderSwitch));
  return html`<section class="panel hk-status ${staged ? 'is-staged' : ''}" data-panel="status">
    <div class="hk-status-sw">
      <label class="switch st-switch"><input type="checkbox" role="switch" data-in="enabled" .checked=${p.enabled} ?disabled=${!v.writable} @change=${(e: Event) => on.stage(p.id, 'enabled', (e.target as HTMLInputElement).checked)} /><span>${p.enabled ? 'Asked' : 'Not asked at all'}</span></label>
      ${modeSeg(p, v.writable, (m) => on.stage(p.id, 'mode', m))}
      ${staged ? html`<span class="st-mark" data-mark>changed</span>` : nothing}
      <p class="small muted hk-status-say" data-mode-words>${p.enabled ? MODE_WORDS[p.mode].text : 'Switched off: the conductor decides as without it.'}</p>
    </div>
    ${p.mode === 'ab'
      ? html`<div class="hk-ab" data-ab>
          ${field('ab_percent', 'Share it answers (%)', textInput('ab_percent', o.text.ab_percent ?? String(p.ab_percent), on.share, { mono: true, bad: !!o.invalid.ab_percent, mode: 'numeric', blur: () => on.tidy('ab_percent') }), { error: o.invalid.ab_percent, hint: `${p.ab_percent} of 100 get the provider’s answers, the rest the built-in.` })}
          <div class="hk-field" data-field="sticky"><span class="hk-label" id="hk-sticky-l">The split sticks to</span>
            <div class="seg" role="group" aria-labelledby="hk-sticky-l">${v.info.sticky.map((s: Sticky) => html`<button type="button" class="seg-btn ${p.sticky === s ? 'is-on' : ''}" aria-pressed=${p.sticky === s} data-sticky-to=${s} ?disabled=${!v.writable} @click=${() => on.stage(p.id, 'sticky', s)}>${s}</button>`)}</div>
            <p class="xs muted">${STICKY_WORDS[p.sticky].charAt(0).toUpperCase()}${STICKY_WORDS[p.sticky].slice(1)} stays on its side. When there is none, the next one is taken: session, user, request.</p>
          </div>
        </div>`
      : nothing}
    ${conflictNote(v.list)}
  </section>`;
}

function healthPanel(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const h = v.health;
  const w = healthWords(h ?? (v.provider.enabled ? null : { status: 'off' }));
  return html`<section class="panel" data-panel="health" data-health=${h?.status ?? ''}>
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Health ${chip(w.tone, w.label, { data: 'health' })}</h2><p class="panel-sub" data-health-words>${w.text}</p></div>
      <div class="panel-tools"><button type="button" class="btn btn-sm" data-act="probe" ?disabled=${v.one.probing} @click=${on.probe}>${icon('refresh')} ${v.one.probing ? 'Probing…' : 'Probe now'}</button></div></header>
    ${h
      ? html`<ol class="hk-eps">
            ${h.endpoints.map((e) => {
              const p = e.probe ? probeWords(e.probe) : { tone: 'muted' as const, text: 'not probed yet' };
              const b = breakerWords(e.breaker);
              return html`<li class="hk-ep" data-endpoint=${e.endpoint} data-up=${e.probe?.up ?? ''}>
                <span class="num st-wrap">${e.endpoint}</span>
                <span class="xs muted">${e.questions.join(', ')}</span>
                <span class="hk-ep-s">${chip(p.tone, !e.probe ? 'not probed' : e.probe.up ? 'ready' : 'not ready', { data: 'probe' })}<span class="xs muted" data-probe-words>${p.text}</span>${chip(b.tone, b.label, { title: b.text, data: 'breaker' })}</span>
              </li>`;
            })}
          </ol>
          <p class="xs muted" data-last-call>${h.last_call ? `Last call: ${h.last_call.question}, ${at(h.last_call.ts_ms)}, ${h.last_call.error ? `failed (${h.last_call.error})` : `answered in ${ms(h.last_call.ms)}`}.` : 'No call yet.'} A probe reads /readyz and /v1/models; its answer is kept for 10 s.</p>`
      : html`<p class="small muted">Its health was not read yet.</p>`}
  </section>`;
}

function keyControl(v: ProviderView, on: ProviderHandlers, q: string, label: string): TemplateResult {
  const p = v.provider;
  const o = v.one;
  const hint = q === '*' ? p.key : p.questions[q]?.key;
  const staged = p.keys[q];
  const open = o.keyOpen.includes(q);
  if (q !== '*' && p.fresh.includes(q)) return html`<p class="xs muted" data-key-later=${q}>Its own key can be set once this question is applied: a key belongs to a question the provider answers already.</p>`;
  return html`<div class="st-secret" data-key=${q} data-key-state=${staged ?? (hint?.key_set ? 'stored' : 'none')}>
    <p class="small" id="hk-key-${q}-s">${label}: ${keyWords(hint, staged)}</p>
    ${open
      ? html`<div class="st-secret-in"><input class="st-input num" type="password" autocomplete="new-password" spellcheck="false" data-in="key-${q}" aria-label=${label} aria-describedby="hk-key-${q}-s" placeholder="Paste or type the key" .value=${o.text[`key.${q}`] ?? ''} @input=${(e: Event) => on.key(q, (e.target as HTMLInputElement).value)} />
          <button type="button" class="btn btn-sm btn-ghost" data-act="key-close" @click=${() => on.keyField(q, false)}>Cancel</button></div>`
      : html`<div class="st-secret-acts">
          ${staged ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="key-undo" @click=${() => on.undoKey(q)}>Undo</button>` : nothing}
          ${staged === 'unset' ? nothing : html`<button type="button" class="btn btn-sm" data-act="key-set" ?disabled=${!v.writable} @click=${() => on.keyField(q, true)}>${hint?.key_set ? 'Replace the key…' : 'Set a key…'}</button>`}
          ${hint?.key_set && !staged ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="key-remove" ?disabled=${!v.writable} @click=${() => on.removeKey(q)}>Remove the key</button>` : nothing}
        </div>`}
  </div>`;
}

function questionsPanel(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const p = v.provider;
  const o = v.one;
  const http = p.kind === 'systemone-http';
  const staged = p.staged.has('questions');
  return html`<section class="panel ${staged ? 'is-staged-panel' : ''}" data-panel="mapping">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Questions it answers ${staged ? html`<span class="st-mark" data-mark>changed</span>` : nothing}</h2><p class="panel-sub">Each question takes the provider’s own ${http ? 'endpoint, model, time limit and key' : 'model and time limit'} unless it names its own. Staged like every other setting.</p></div>
      ${staged ? html`<div class="panel-tools"><button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => on.undo('questions')}>Undo</button></div>` : nothing}</header>
    <ol class="hk-qs">
      ${Object.entries(v.info.questions).map(([q, info]) => {
        const m = p.questions[q];
        const taken = m && p.mode !== 'shadow' && p.enabled ? takenBy(q, v.list, p.id) : null;
        const f = (k: 'model' | 'endpoint' | 'timeout_ms') => o.text[`${q}.${k}`] ?? (m?.[k] == null ? '' : String(m[k]));
        return html`<li class="hk-q ${m ? 'is-on' : ''}" data-question=${q} data-answered=${!!m}>
          <label class="hk-q-h"><input type="checkbox" data-in="q-${q}" .checked=${!!m} ?disabled=${!v.writable || (!!m && Object.keys(p.questions).length <= 1)} @change=${(e: Event) => on.question(q, (e.target as HTMLInputElement).checked)} /><b class="num">${q}</b><span class="xs muted">${info.what}</span></label>
          ${m
            ? html`<div class="hk-q-b">
                ${taken ? html`<p class="rs-warn small" data-taken>${icon('warn')}<span>${taken} answers ${q} already. One provider answers a question, on or in A/B.</span></p>` : nothing}
                <div class="hk-grid">
                  ${http ? html`<label class="hk-mini"><span class="xs muted">Endpoint</span>${textInput(`${q}.endpoint`, f('endpoint'), (x) => on.map(q, 'endpoint', x), { placeholder: p.endpoint ?? '', mono: true, bad: !!o.invalid[`${q}.endpoint`], blur: () => on.tidy(`${q}.endpoint`) })}</label>` : nothing}
                  <label class="hk-mini"><span class="xs muted">Model</span>${textInput(`${q}.model`, f('model'), (x) => on.map(q, 'model', x), { placeholder: p.model, mono: true, bad: !!o.invalid[`${q}.model`], blur: () => on.tidy(`${q}.model`) })}</label>
                  <label class="hk-mini"><span class="xs muted">Time limit (ms)</span>${textInput(`${q}.timeout_ms`, f('timeout_ms'), (x) => on.map(q, 'timeout_ms', x), { placeholder: String(p.timeout_ms), mono: true, mode: 'numeric', bad: !!o.invalid[`${q}.timeout_ms`], blur: () => on.tidy(`${q}.timeout_ms`) })}</label>
                </div>
                ${(['endpoint', 'model', 'timeout_ms'] as const).map((k) => (o.invalid[`${q}.${k}`] ? html`<p class="st-err small" role="alert" data-invalid>${o.invalid[`${q}.${k}`]}</p>` : nothing))}
                ${http ? keyControl(v, on, q, 'Its own key') : nothing}
              </div>`
            : nothing}
        </li>`;
      })}
    </ol>
    <p class="xs faint">Empty fields take the provider’s own. A question’s key goes only to that question’s endpoint.</p>
  </section>`;
}

function connection(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const p = v.provider;
  const e = v.one.edit;
  const fit = adapterHooks(v.hooks);
  if (e)
    return html`<section class="panel" data-panel="connection" data-editing>
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Connection</h2><p class="panel-sub">Saved at once on the providers’ API, not staged. Its kind is fixed.</p></div></header>
      <form class="hk-form" novalidate @submit=${(x: Event) => (x.preventDefault(), on.save())}>
        <div class="hk-grid">
          ${field('name', 'Name', textInput('name', e.name, (name) => on.editField({ name }), { bad: !!e.errors.name }), { error: e.errors.name })}
          ${p.kind === 'systemone-http' ? field('endpoint', 'Endpoint', textInput('endpoint', e.endpoint, (endpoint) => on.editField({ endpoint }), { mono: true, bad: !!e.errors.endpoint, type: 'url' }), { error: e.errors.endpoint }) : nothing}
          ${p.kind === 'hook'
            ? field('hook_id', 'The hook that answers', html`<select id="hk-hook_id" data-in="hook_id" @change=${(x: Event) => on.editField({ hook_id: (x.target as HTMLSelectElement).value })}>${fit.map((h) => html`<option value=${h.id} ?selected=${h.id === e.hook_id}>${h.name}</option>`)}</select>`, { error: e.errors.hook_id })
            : nothing}
          ${field('model', 'Model', textInput('model', e.model, (model) => on.editField({ model }), { mono: true, bad: !!e.errors.model }), { error: e.errors.model, hint: 'auto: the service picks.' })}
          ${field('timeout_ms', 'Time limit (ms)', textInput('timeout_ms', e.timeout, (timeout) => on.editField({ timeout }), { mono: true, mode: 'numeric', bad: !!e.errors.timeout_ms }), { error: e.errors.timeout_ms, hint: 'At most the stage’s: 400 ms for the router’s questions.' })}
        </div>
        ${e.refused ? refused(e.refused) : nothing}
        ${v.signIn}
        <div class="st-acts"><button type="button" class="btn btn-ghost" data-act="edit-cancel" ?disabled=${e.busy} @click=${on.cancelEdit}>Cancel</button><button type="submit" class="btn btn-primary" data-act="edit-save" ?disabled=${e.busy}>${e.busy ? 'Saving…' : 'Save'}</button></div>
      </form>
    </section>`;
  const hook = p.hook_id ? v.hooks.find((h) => h.id === p.hook_id) : null;
  return html`<section class="panel" data-panel="connection">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Connection</h2><p class="panel-sub">${KIND_WORDS[p.kind].text}</p></div><div class="panel-tools"><button type="button" class="btn btn-sm" data-act="edit" @click=${on.edit}>${icon('edit')} Change</button></div></header>
    ${v.one.notice ? html`<p class="note note-info small" role="status" data-notice>${icon('info')} ${v.one.notice}</p>` : nothing}
    <dl class="hk-kv">
      ${p.kind === 'systemone-http' ? html`<div><dt>Endpoint</dt><dd class="num st-wrap" data-endpoint>${p.endpoint}</dd></div>` : nothing}
      ${p.kind === 'hook' ? html`<div><dt>Hook</dt><dd>${hook ? html`<a href=${hooksHref(['hook', hook.id])}>${hook.name}</a>` : html`<span class="num">${p.hook_id}</span> <span class="err-ink">(no such hook)</span>`} <span class="xs muted">Keep the hook’s own mode on: this provider’s mode decides shadow and A/B.</span></dd></div>` : nothing}
      <div><dt>Model</dt><dd class="num">${p.model}</dd></div>
      <div><dt>Time limit</dt><dd><span class="num">${p.timeout_ms}</span> ms</dd></div>
      <div><dt>Changed</dt><dd class="muted">${at(p.updated_at)}${p.updated_by ? ` by ${p.updated_by}` : ''}, version <span class="num">${p.version}</span></dd></div>
    </dl>
    ${p.kind === 'systemone-http' ? html`<div class="hk-own-key">${keyControl(v, on, '*', 'API key')}<p class="xs muted">Sent as the bearer, to its own endpoint only. Stored sealed; a move of the controller leaves it behind.</p></div>` : nothing}
  </section>`;
}

function testPanel(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const t = v.one.test;
  const qs = Object.keys(v.provider.questions);
  const x = t.result ? explainProviderTest(t.result) : null;
  return html`<section class="panel" data-panel="test">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Try it</h2><p class="panel-sub">One live call with a sample question. Nothing is counted or journaled, and no decision is changed.</p></div></header>
    <div class="hk-play">
      <div class="hk-play-in">
        ${field('test-question', 'Question', html`<select id="hk-test-question" data-in="test-question" @change=${(e: Event) => on.setTest({ question: (e.target as HTMLSelectElement).value })}>${qs.map((q) => html`<option value=${q} ?selected=${q === t.question}>${q}</option>`)}</select>`)}
        ${field('test-text', 'The text it is about (optional)', html`<textarea id="hk-test-text" class="st-area" rows="3" data-in="test-text" maxlength="8000" placeholder="Empty: the question’s own sample text" .value=${t.text} @input=${(e: Event) => on.setTest({ text: (e.target as HTMLTextAreaElement).value })}></textarea>`, { wide: true })}
        <div class="st-acts"><button type="button" class="btn btn-primary" data-act="test-send" ?disabled=${t.busy || !qs.length} @click=${on.test}>${icon('play')} ${t.busy ? 'Asking…' : 'Ask it once'}</button></div>
      </div>
      <div class="hk-play-out" aria-live="polite" data-test-result=${t.result ? (t.result.ok ? 'ok' : 'failed') : t.refused ? 'refused' : 'none'}>
        ${t.refused ? refused(t.refused) : nothing}
        ${x
          ? html`<div class="hk-explain is-${x.tone}" data-explain>
              <p class="hk-explain-t">${x.title}</p>
              ${x.probs.length ? html`<ul class="hk-probs">${x.probs.map((p) => html`<li><span class="num">${p.label}</span><span class="hk-prob"><span style="width:${(p.p * 100).toFixed(1)}%"></span></span><span class="num xs">${(p.p * 100).toFixed(0)} %</span></li>`)}</ul>` : nothing}
              <ul class="small">${x.lines.map((l) => html`<li>${l}</li>`)}</ul>
            </div>`
          : t.refused
            ? nothing
            : html`<p class="small muted">Its answer shows here: the option it picked, how sure it is and how long it took.</p>`}
      </div>
    </div>
  </section>`;
}

function fallbacks(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const o = v.one;
  const rows = o.calls ? fallbackReasons(o.calls) : [];
  return html`<section class="panel" data-panel="fallbacks">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Why the built-in answered instead</h2><p class="panel-sub">Among its recent calls. Whenever the provider cannot answer, the conductor decides as without it.</p></div><div class="panel-tools"><button type="button" class="btn btn-sm btn-ghost" data-act="calls-reload" @click=${on.reload}>${icon('refresh')} Read again</button></div></header>
    ${o.callsRefused ? refused(o.callsRefused, { quiet: true }) : nothing}
    ${o.calls && !rows.length ? html`<p class="small muted" data-fallbacks-none>No fallback among its last ${o.calls.length} ${o.calls.length === 1 ? 'call' : 'calls'}.</p>` : nothing}
    ${rows.length
      ? html`<ol class="hk-falls" data-fallbacks=${rows.length}>${rows.map((r) => html`<li class="hk-fall" data-reason=${r.reason}><b class="num hk-fall-n">${r.n}×</b><span><b>${r.title}</b> <span class="num xs muted">${r.reason}</span><br /><span class="small muted">${r.text} For ${r.questions.join(', ')}; last ${at(r.last)}.</span></span></li>`)}</ol>`
      : nothing}
    ${o.calls?.length
      ? html`<details class="st-doc" data-provider-calls><summary class="small">Its last ${Math.min(40, o.calls.length)} calls</summary>
          <ol class="hk-calls">${o.calls.slice(0, 40).map((c) => {
            const w = providerCallWords(c);
            return html`<li class="hk-call is-${w.tone}" data-used=${c.used}><span class="small muted hk-call-at">${at(c.ts_ms)}</span><span class="hk-call-what"><span class="num small">${c.question}</span> <span class="small">${w.text}</span></span><span class="xs muted">${c.mode}${c.arm ? ` ${c.arm}` : ''}</span><span class="num small hk-call-ms">${c.answer ? ms(c.answer.ms) : ''}</span><span></span></li>`;
          })}</ol></details>`
      : nothing}
  </section>`;
}

export function providerView(v: ProviderView, on: ProviderHandlers): TemplateResult {
  const p = v.provider;
  const r = v.one.remove;
  return html`${back(providersHref(), 'Decision providers')}
    <header class="hk-head" data-provider-page=${p.id}><h2 class="hk-title">${p.name}</h2><p class="small muted hk-head-meta"><span>${KIND_WORDS[p.kind].label}</span><span class="num">${p.id}</span><span>${modeWords(p)}</span></p></header>
    ${v.one.refused ? refused(v.one.refused) : nothing}
    ${v.writable ? nothing : html`<p class="note small" data-readonly>${icon('info')} The settings interface does not take changes here.</p>`}
    ${providerStatus(v, on)} ${healthPanel(v, on)} ${questionsPanel(v, on)} ${connection(v, on)} ${testPanel(v, on)}
    ${reportPanel(v.report, v.reportRefused, v.hours, [p], on, true)}
    ${fallbacks(v, on)}
    <section class="panel hk-danger" data-panel="remove">
      ${r.step === 'idle'
        ? html`<div class="hk-danger-row"><div><h2 class="panel-title">Remove this provider</h2><p class="panel-sub">The conductor’s own decider answers its questions again, at once. Its keys go with it.</p></div><button type="button" class="btn st-danger" data-act="remove-ask" @click=${() => on.askRemove(true)}>${icon('trash')} Remove…</button></div>`
        : html`<div role="alertdialog" aria-labelledby="hk-remove-t" data-remove-ask>
            <h2 class="panel-title" id="hk-remove-t">Remove ${p.name}?</h2>
            <p class="small">It is removed now, not staged. Its stored keys are deleted; type them again if you add it back.</p>
            ${r.refused ? refused(r.refused) : nothing}
            ${v.signIn}
            <div class="st-acts"><button type="button" class="btn btn-ghost" data-act="remove-cancel" ?disabled=${r.step === 'busy'} @click=${() => on.askRemove(false)}>Keep it</button><button type="button" class="btn hk-go-danger" data-act="remove-go" ?disabled=${r.step === 'busy'} @click=${on.remove}>${r.step === 'busy' ? 'Removing…' : 'Remove the provider'}</button></div>
          </div>`}
    </section>`;
}

export function providerGone(id: string): TemplateResult {
  return html`${back(providersHref(), 'Decision providers')}
    <section class="panel st-empty" data-provider-gone=${id}><h2 class="panel-title">No provider ${id}</h2><p class="small muted">It was removed, here or through the API.</p><div class="st-acts"><a class="btn btn-primary" href=${providersHref()}>Back to the providers</a></div></section>`;
}

// ---- a new provider -------------------------------------------------------------------------------

export interface AddProviderHandlers {
  edit(p: Partial<ProviderForm>): void;
  /** `confirmed`: the second confirm for an endpoint outside the LAN was given */
  create(confirmed?: boolean): void;
  unconfirm(): void;
}

export interface AddProviderView {
  state: AddProvider;
  info: ProviderList;
  list: ShownProvider[];
  hooks: Pick<Hook, 'id' | 'name' | 'role' | 'stages'>[];
  signIn: TemplateResult | typeof nothing;
}

const pstep = (n: number, title: string, sub: string | null, body: TemplateResult, id: string): TemplateResult =>
  html`<li class="hk-step" data-step=${id}><span class="hk-n num" aria-hidden="true">${n}</span><div class="hk-step-b"><h3 class="hk-step-t">${title}</h3>${sub ? html`<p class="small muted">${sub}</p>` : nothing}${body}</div></li>`;

export function addProviderView(v: AddProviderView, on: AddProviderHandlers): TemplateResult {
  const s = v.state;
  const f = s.form;
  const e = s.errors;
  const fit = adapterHooks(v.hooks);
  const n = Object.keys(e).length;
  const kinds: ProviderKind[] = ['systemone-http', 'hook', 'builtin'];
  const connect =
    f.kind === 'systemone-http'
      ? html`<div class="hk-grid">
            ${field('endpoint', 'Where it answers', textInput('endpoint', f.endpoint, (endpoint) => on.edit({ endpoint }), { placeholder: 'http://decider.example:8720', mono: true, bad: !!e.endpoint, type: 'url' }), { error: e.endpoint, hint: 'TandemApp calls POST /v1/systemone there, and GET /readyz to see whether it is up.' })}
            ${field('api_key', 'API key (optional)', html`<input id="hk-api_key" class="st-input num" type="password" autocomplete="new-password" spellcheck="false" data-in="api_key" aria-invalid=${!!e.api_key} placeholder="Sent as the bearer" .value=${f.api_key} @input=${(x: Event) => on.edit({ api_key: (x.target as HTMLInputElement).value })} />`, { error: e.api_key, hint: 'Write-only: stored sealed, shown afterwards as its last four characters.' })}
          </div>
          <p class="xs muted">No service yet? <code>python tools/systemone_example.py --port 8720</code> is a whole provider in the standard library: run it, point this at it, then put your model in its place.</p>`
      : f.kind === 'hook'
        ? fit.length
          ? field('hook_id', 'The hook that answers', html`<select id="hk-hook_id" data-in="hook_id" aria-invalid=${!!e.hook_id} @change=${(x: Event) => on.edit({ hook_id: (x.target as HTMLSelectElement).value })}><option value="" ?selected=${!f.hook_id}>Pick one</option>${fit.map((h) => html`<option value=${h.id} ?selected=${h.id === f.hook_id}>${h.name}</option>`)}</select>`, { error: e.hook_id, hint: 'A decider at the stop “The conductor asks a question”. It gets the question and answers with an option id.' })
          : html`<div class="note small" data-no-adapter>${icon('info')} No hook can answer yet. Add a decider at the stop “The conductor asks a question” first, then come back. <a href=${hooksHref(['new'], { stage: 'decision.ask' })}>Add that hook</a>${e.hook_id ? html`<p class="st-err" role="alert" data-invalid>${e.hook_id}</p>` : nothing}</div>`
        : html`<p class="small muted" data-builtin-note>Nothing to connect: it is the conductor’s own decider. Name a model for a question on its page after it is added.</p>`;
  return html`${back(providersHref(), 'Decision providers')}
    <header class="hk-head"><h2 class="hk-title">Add a decision provider</h2><p class="small muted">Put your own model in the place of the conductor’s decider, for the questions you pick.</p></header>
    <form class="hk-form" novalidate @submit=${(x: Event) => (x.preventDefault(), on.create())} data-add-provider>
      <ol class="hk-steps">
        ${pstep(1, 'Who answers', null, cards<ProviderKind>('kind', f.kind, kinds.filter((k) => v.info.kinds.includes(k)).map((k) => ({ id: k, label: KIND_WORDS[k].label, text: KIND_WORDS[k].text })), (kind) => on.edit({ kind }), { label: 'Who answers' }), 'kind')}
        ${pstep(
          2,
          'How TandemApp reaches it',
          null,
          html`${connect}
            <div class="hk-grid">
              ${field('model', 'Model', textInput('model', f.model, (model) => on.edit({ model }), { mono: true, bad: !!e.model }), { error: e.model, hint: 'auto: the provider picks.' })}
              ${field('timeout_ms', 'Time limit (ms)', textInput('timeout_ms', f.timeout, (timeout) => on.edit({ timeout }), { mono: true, mode: 'numeric', bad: !!e.timeout_ms }), { error: e.timeout_ms, hint: 'No answer in time: the built-in answers.' })}
            </div>`,
          'connect',
        )}
        ${pstep(
          3,
          'Which questions it answers',
          'Each can get its own endpoint, model and key later.',
          html`<div class="hk-checks" data-field="questions">
              ${Object.entries(v.info.questions).map(([q, info]) => {
                const taken = takenBy(q, v.list, null);
                return html`<label class="hk-check"><input type="checkbox" data-in="q-${q}" .checked=${f.questions.includes(q)} @change=${(x: Event) => on.edit({ questions: (x.target as HTMLInputElement).checked ? [...f.questions, q] : f.questions.filter((y) => y !== q) })} /><b class="num">${q}</b><span class="xs muted">${info.what}${taken ? `. ${taken} answers it now.` : ''}</span></label>`;
              })}
            </div>
            ${e.questions ? html`<p class="st-err small" role="alert" data-invalid>${e.questions}</p>` : nothing}`,
          'questions',
        )}
        ${pstep(
          4,
          'How it starts',
          null,
          html`${cards<ProviderMode>('mode', f.mode, MODES.map((m) => ({ id: m, label: m === 'shadow' ? 'In shadow' : MODE_WORDS[m].label, text: MODE_WORDS[m].text })), (mode) => on.edit({ mode }), { label: 'How it starts' })}
            ${f.mode === 'ab'
              ? html`<div class="hk-grid">
                  ${field('ab_percent', 'Share it answers (%)', textInput('ab_percent', f.ab_percent, (ab_percent) => on.edit({ ab_percent }), { mono: true, mode: 'numeric', bad: !!e.ab_percent }), { error: e.ab_percent })}
                  <div class="hk-field" data-field="sticky"><span class="hk-label" id="hk-new-sticky">The split sticks to</span><div class="seg" role="group" aria-labelledby="hk-new-sticky">${v.info.sticky.map((x) => html`<button type="button" class="seg-btn ${f.sticky === x ? 'is-on' : ''}" aria-pressed=${f.sticky === x} data-sticky-to=${x} @click=${() => on.edit({ sticky: x })}>${x}</button>`)}</div></div>
                </div>`
              : nothing}`,
          'mode',
        )}
      </ol>
      <section class="panel hk-finish" data-step="name">${field('name', 'Name', textInput('name', f.name, (name) => on.edit({ name }), { placeholder: 'My decider', bad: !!e.name }), { error: e.name, hint: 'Its id is made from the name.' })}</section>
      ${s.refused ? refused(s.refused) : nothing}
      ${v.signIn}
      ${s.confirm
        ? confirmLeave(s.confirm, { busy: s.busy, go: 'Add it, and send the text there', onGo: () => on.create(true), onBack: on.unconfirm, provider: true })
        : html`<div class="hk-form-foot">
            ${n ? html`<p class="small err-ink" role="status" data-form-invalid>${n === 1 ? 'One thing needs' : `${n} things need`} a correction above.</p>` : html`<p class="small muted">It is added at once. Its switches can be changed afterwards, staged like every setting.</p>`}
            <div class="st-acts"><a class="btn btn-ghost" href=${providersHref()} data-act="add-cancel">Cancel</a><button type="submit" class="btn btn-primary" data-act="add-create" ?disabled=${s.busy}>${s.busy ? 'Adding…' : 'Add the provider'}</button></div>
          </div>`}
    </form>`;
}
