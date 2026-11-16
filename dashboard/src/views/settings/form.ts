// A scope's form (VIS-280): one row a key, drawn from its field (lib/settings-form.ts). The label
// and the scope's own sentence on the left; the control its type asks for; under it what is wrong
// with the text typed, why the key is locked (the variable that sets it), where its value comes
// from and what a change costs (a restart, the disk cache's fingerprint). A staged row is marked
// and can be taken back; a key that can go back to what stands below it has a button for that. A
// secret is set or removed and never shown. Templates only, so they render in a test without a
// browser; the page (area.ts) holds the state.

import { html, nothing, svg, type TemplateResult } from 'lit';
import { MODE_KEY, MODE_WORDS, asRoutes, sentence, textOf, type Field, type Group, type RouteRule } from '../../lib/settings-form';
import { icon } from '../../ui/bits';

export interface FormHandlers {
  /** a text control's content changed */
  edit(f: Field, raw: string): void;
  /** the control lost the focus: a valid text is tidied */
  blur(f: Field): void;
  /** a switch, a choice or the routing rules: the new value */
  set(f: Field, value: unknown): void;
  /** hand the key back to what stands below it */
  unset(f: Field): void;
  /** take the staged change back */
  undo(f: Field): void;
  /** open or close a secret's field */
  secret(f: Field, open: boolean): void;
  /** a group was opened or closed */
  toggle(group: string, open: boolean): void;
}

export interface FormView {
  /** what is typed in a field while it is being edited */
  raw: Map<string, string>;
  /** what is wrong with it */
  invalid: Map<string, string>;
  /** the secrets whose field is open */
  secrets: Set<string>;
  /** the groups that are open */
  open: Set<string>;
  /** the backends a routing rule may name */
  backends: string[];
}

export const lock = svg`<svg class="icon st-lock-g" viewBox="0 0 20 20" width="14" height="14" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="9" width="11" height="7.5" rx="1.5"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9"/></g></svg>`;

const isOn = (v: unknown): boolean => v === true || v === 'on';

function switchControl(f: Field, h: FormHandlers): TemplateResult {
  const on = isOn(f.value);
  return html`<label class="switch st-switch"><input type="checkbox" role="switch" id=${f.id} data-in=${f.key} .checked=${on} ?disabled=${f.locked}
      @change=${(e: Event) => {
        const to = (e.target as HTMLInputElement).checked;
        h.set(f, f.spec.type === 'boolean' ? to : to ? 'on' : 'off');
      }} /><span data-switch-state>${on ? 'On' : 'Off'}</span></label>`;
}

function choiceControl(f: Field, h: FormHandlers): TemplateResult {
  const values = f.spec.values ?? [];
  if (f.key === MODE_KEY && f.scope === 'engine')
    return html`<div class="st-modes" role="radiogroup" aria-labelledby="${f.id}-l">
      ${values.map((v) => {
        const w = MODE_WORDS[v] ?? { label: v, text: '' };
        return html`<label class="st-mode ${f.value === v ? 'is-on' : ''}" data-mode=${v}><input type="radio" name=${f.id} .checked=${f.value === v} ?disabled=${f.locked} @change=${() => h.set(f, v)} /><b>${w.label}</b><span class="small muted">${w.text}</span></label>`;
      })}
    </div>`;
  return html`<div class="seg" role="group" aria-labelledby="${f.id}-l">
    ${values.map((v) => html`<button type="button" class="seg-btn ${f.value === v ? 'is-on' : ''}" aria-pressed=${f.value === v} data-value=${v} ?disabled=${f.locked} @click=${() => h.set(f, v)}>${v}</button>`)}
  </div>`;
}

function textControl(f: Field, v: FormView, h: FormHandlers): TemplateResult {
  const text = v.raw.get(f.id) ?? textOf(f.control, f.value);
  const bad = v.invalid.has(f.id);
  const on = { input: (e: Event) => h.edit(f, (e.target as HTMLInputElement).value), blur: () => h.blur(f) };
  if (f.control === 'lines' || f.control === 'json')
    return html`<textarea id=${f.id} data-in=${f.key} class="st-area ${f.control === 'json' ? 'code' : ''}" rows=${f.control === 'json' ? Math.min(18, Math.max(5, text.split('\n').length + 1)) : Math.min(8, Math.max(2, text.split('\n').length + 1))} spellcheck="false" autocapitalize="off"
      aria-invalid=${bad} aria-describedby="${f.id}-d" ?readonly=${f.locked} .value=${text} @input=${on.input} @blur=${on.blur}></textarea>`;
  const mono = f.control === 'number' || f.scope === 'engine' || f.spec.type === 'path' || f.spec.type === 'module' || f.spec.type === 'cidrs';
  return html`<input id=${f.id} data-in=${f.key} class="st-input ${mono ? 'num' : ''} ${f.control === 'number' ? 'is-num' : ''}" type="text" inputmode=${f.control === 'number' ? (f.spec.type === 'integer' ? 'numeric' : 'decimal') : 'text'} spellcheck="false" autocapitalize="off" autocomplete="off"
    aria-invalid=${bad} aria-describedby="${f.id}-d" ?readonly=${f.locked} placeholder=${f.spec.nullable ? 'not set' : ''} .value=${text} @input=${on.input} @blur=${on.blur} />`;
}

function selectControl(f: Field, h: FormHandlers): TemplateResult {
  return html`<select id=${f.id} data-in=${f.key} ?disabled=${f.locked} @change=${(e: Event) => h.set(f, (e.target as HTMLSelectElement).value)}>
    ${(f.spec.values ?? []).map((v) => html`<option value=${v} ?selected=${f.value === v}>${v}</option>`)}
  </select>`;
}

/** A secret: whether one is stored, a field to type a new one, and its removal. The stored value
 *  is never here; the typed one stays in this page's memory until it is applied. */
function secretControl(f: Field, v: FormView, h: FormHandlers): TemplateResult {
  const stored = !!f.held?.set;
  const open = v.secrets.has(f.id);
  const state = f.staged === 'set' ? 'A new key is staged. It is sent when the change set is applied.' : f.staged === 'unset' ? 'The stored key is removed when the change set is applied.' : stored ? `A key is stored${f.held?.hint ? ` (its SHA-256 starts ${f.held.hint})` : ''}. It is never shown.` : 'No key is stored.';
  return html`<div class="st-secret" data-secret=${f.staged ?? (stored ? 'stored' : 'none')}>
    <p class="small" id="${f.id}-s">${state}</p>
    ${open
      ? html`<div class="st-secret-in"><input id=${f.id} data-in=${f.key} class="st-input num" type="password" autocomplete="new-password" spellcheck="false" aria-describedby="${f.id}-s" placeholder="Paste or type the key" .value=${v.raw.get(f.id) ?? ''} @input=${(e: Event) => h.edit(f, (e.target as HTMLInputElement).value)} />
          ${f.staged === 'set' ? nothing : html`<button type="button" class="btn btn-sm btn-ghost" data-act="secret-close" @click=${() => h.secret(f, false)}>Cancel</button>`}</div>`
      : f.locked
        ? nothing
        : html`<div class="st-secret-acts"><button type="button" class="btn btn-sm" data-act="secret-set" @click=${() => h.secret(f, true)}>${stored ? 'Replace the key…' : 'Set a key…'}</button></div>`}
  </div>`;
}

/** The routing rules in order. A rule the environment sets keeps its place and its value. */
function routesControl(f: Field, v: FormView, h: FormHandlers): TemplateResult {
  const rules = asRoutes(f.value);
  const put = (next: RouteRule[]) => h.set(f, next);
  const patch = (i: number, p: Partial<RouteRule>) => put(rules.map((r, j) => (j === i ? { ...r, ...p } : r)));
  const swap = (i: number, j: number) => {
    const next = [...rules];
    [next[i], next[j]] = [next[j], next[i]];
    put(next);
  };
  // a new rule goes before the environment's last ones, which keep their place
  let end = rules.length;
  while (end > 0 && rules[end - 1].variable) end--;
  const free = (i: number) => i >= 0 && i < rules.length && !rules[i].variable;
  return html`<ol class="st-routes" data-routes>
      ${rules.map((r, i) => {
        const fixed = !!r.variable || f.locked;
        return html`<li class="st-route ${r.variable ? 'is-env' : ''}" data-route=${i}>
          <span class="st-route-n num" aria-hidden="true">${i + 1}</span>
          <label class="st-route-f"><span class="sr-only">Rule ${i + 1}: model ids like</span><input class="st-input num" data-in="pattern" spellcheck="false" autocapitalize="off" placeholder="model-*" ?readonly=${fixed} .value=${r.pattern} @change=${(e: Event) => patch(i, { pattern: (e.target as HTMLInputElement).value.trim() })} /></label>
          <label class="st-route-f"><span class="sr-only">go to the backend</span><select data-in="backend" ?disabled=${fixed} @change=${(e: Event) => patch(i, { backend: (e.target as HTMLSelectElement).value })}>${[...new Set([...v.backends, r.backend])].map((b) => html`<option value=${b} ?selected=${b === r.backend}>${b}</option>`)}</select></label>
          <label class="switch"><input type="checkbox" role="switch" data-in="enabled" .checked=${r.enabled} ?disabled=${fixed} @change=${(e: Event) => patch(i, { enabled: (e.target as HTMLInputElement).checked })} /><span class="sr-only">Rule ${i + 1} is on</span></label>
          ${r.variable
            ? html`<span class="st-route-env small muted">${lock} set by <code>${r.variable}</code></span>`
            : f.locked
              ? nothing
              : html`<span class="st-route-acts">
                  <button type="button" class="btn btn-sm btn-ghost" aria-label=${`Move rule ${i + 1} up`} ?disabled=${!free(i - 1)} @click=${() => swap(i, i - 1)}>↑</button>
                  <button type="button" class="btn btn-sm btn-ghost" aria-label=${`Move rule ${i + 1} down`} ?disabled=${!free(i + 1)} @click=${() => swap(i, i + 1)}>↓</button>
                  <button type="button" class="btn btn-sm btn-ghost st-danger" aria-label=${`Remove rule ${i + 1}`} data-act="route-remove" @click=${() => put(rules.filter((_, j) => j !== i))}>${icon('trash')}</button>
                </span>`}
        </li>`;
      })}
    </ol>
    ${rules.length ? nothing : html`<p class="small muted" data-routes-none>No rule: every model id goes to the backend that lists it.</p>`}
    ${f.locked ? nothing : html`<div><button type="button" class="btn btn-sm" data-act="route-add" ?disabled=${!v.backends.length} @click=${() => put([...rules.slice(0, end), { pattern: '', backend: v.backends[0], enabled: true }, ...rules.slice(end)])}>${icon('plus')} Add a rule</button></div>`}
    <p class="xs faint">The first rule that matches a model id decides. A pattern takes <code>*</code> for any run of characters.</p>`;
}

function control(f: Field, v: FormView, h: FormHandlers): TemplateResult {
  switch (f.control) {
    case 'switch':
      return switchControl(f, h);
    case 'choice':
      return choiceControl(f, h);
    case 'select':
      return selectControl(f, h);
    case 'secret':
      return secretControl(f, v, h);
    case 'routes':
      return routesControl(f, v, h);
    default:
      return textControl(f, v, h);
  }
}

/** One key's row. */
export function fieldRow(f: Field, v: FormView, h: FormHandlers): TemplateResult {
  const bad = v.invalid.get(f.id);
  const doc = f.control === 'json' && f.spec.type === 'document';
  // the routing rules are their panel's only row: the panel's head names them
  const solo = f.control === 'routes';
  const body = doc
    ? html`<details class="st-doc" ?open=${v.open.has(f.id)} @toggle=${(e: Event) => h.toggle(f.id, (e.target as HTMLDetailsElement).open)}><summary class="small">${f.locked ? 'Show the document' : 'Edit as JSON'}</summary>${v.open.has(f.id) ? control(f, v, h) : nothing}</details>`
    : control(f, v, h);
  return html`<div class="st-row ${f.staged ? 'is-staged' : ''} ${f.locked ? 'is-locked' : ''} ${f.control === 'routes' || f.control === 'json' || f.key === MODE_KEY ? 'is-wide' : ''}" data-key=${f.key} data-staged=${f.staged ?? 'no'} data-control=${f.control}>
    <div class="st-row-l ${solo ? 'sr-only' : ''}">
      <label class="st-label ${f.mono ? 'num' : ''}" id="${f.id}-l" for=${f.id}>${f.label}</label>
      ${f.staged ? html`<span class="st-mark" data-mark>${f.staged === 'unset' ? 'goes back' : 'changed'}</span>` : nothing}
      ${f.text && !solo ? html`<p class="st-text small muted">${sentence(f.text)}</p>` : nothing}
    </div>
    <div class="st-row-c">
      ${body}
      <div class="st-under" id="${f.id}-d">
        ${bad ? html`<p class="st-err small" role="alert" data-invalid>${bad}</p>` : nothing}
        ${f.why ? html`<p class="st-lock small" data-locked>${lock}<span>${f.why}</span></p>` : nothing}
        ${f.hints.length || f.chips.length
          ? html`<p class="st-hints xs">${f.hints.map((x) => html`<span class="st-hint">${x}</span>`)}${f.chips.map((c) => html`<span class="st-chip is-${c.tone}" title=${c.title ?? nothing} data-chip=${c.tone}>${c.text}</span>`)}</p>`
          : nothing}
      </div>
    </div>
    <div class="st-row-a">
      ${f.staged
        ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => h.undo(f)}>Undo</button>`
        : f.reset && !f.locked
          ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="reset" @click=${() => h.unset(f)}>${f.reset}</button>`
          : nothing}
    </div>
  </div>`;
}

/** A group of rows: a panel; `fold`: one that opens and closes (a backend, the keys set elsewhere). */
export function groupPanel(g: Group, v: FormView, h: FormHandlers, o: { fold?: boolean } = {}): TemplateResult {
  const staged = g.fields.filter((f) => f.staged).length;
  const mark = staged ? html`<span class="st-count" data-staged-count>${staged} staged</span>` : nothing;
  const rows = html`<div class="st-rows">${g.fields.map((f) => fieldRow(f, v, h))}</div>`;
  if (o.fold)
    return html`<details class="panel st-group st-fold" data-group=${g.id} ?open=${v.open.has(g.id)} @toggle=${(e: Event) => e.target === e.currentTarget && h.toggle(g.id, (e.target as HTMLDetailsElement).open)}>
      <summary class="st-fold-h"><span class="panel-title">${g.title}${mark}</span>${g.sub ? html`<span class="panel-sub">${g.sub}</span>` : nothing}</summary>
      ${v.open.has(g.id) ? rows : nothing}
    </details>`;
  return html`<section class="panel st-group" data-group=${g.id}>
    ${g.title ? html`<header class="panel-head"><div class="panel-titles"><h2 class="panel-title">${g.title}${mark}</h2>${g.sub ? html`<p class="panel-sub">${g.sub}</p>` : nothing}</div></header>` : nothing}
    ${rows}
  </section>`;
}
