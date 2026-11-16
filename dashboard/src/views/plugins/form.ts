// A plugin's settings as a form (VIS-284), drawn from the fields the form generator made of its
// JSON schema (lib/schema-form.ts): a row a property, in the settings pages' own rows. Each field
// gets the control it names: a switch, a number or text field, a choice of a few values, a select,
// a fixed value shown, a group of fields, a list (a few named values as checkboxes, anything else
// item by item) and the raw JSON editor where the generator fell back to it, with its reason.
// Under a field: what is wrong with it in a sentence, its limits and its default. A property is
// one key of the settings interface, so it is staged whole, marked, and can be taken back or
// handed back to its default. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import { blankOf, getAt, isSet, limitsOf, type Step } from '../../lib/plugins';
import type { Form, FormField } from '../../lib/schema-form';
import { shown } from '../../lib/settings-form';
import { icon } from '../../ui/bits';

export interface FormHandlers {
  /** a text field's content changed */
  type(path: Step[], f: FormField, raw: string): void;
  /** it lost the focus */
  tidy(path: Step[]): void;
  /** a switch, a choice, a list's item added (a value) or taken out (undefined) */
  put(path: Step[], value: unknown): void;
  /** hand the property back to its default */
  reset(prop: string): void;
  /** take the staged change back */
  undo(prop: string): void;
}

export interface FormState {
  form: Form;
  /** a property's value as the form shows it */
  value(prop: string): unknown;
  textAt(path: Step[], f: FormField): string;
  errorAt(path: Step[]): string | undefined;
  staged(prop: string): 'set' | 'unset' | null;
  /** the property is set (not its default) */
  isSet(prop: string): boolean;
  writable: boolean;
}

const idOf = (path: Step[]): string => `plg-f-${path.join('.')}`;
const same = (a: unknown, b: unknown): boolean => JSON.stringify(a) === JSON.stringify(b);

function control(f: FormField, path: Step[], s: FormState, on: FormHandlers, label: string): TemplateResult {
  const id = idOf(path);
  const value = getAt(s.value(String(path[0])), path.slice(1));
  const bad = !!s.errorAt(path);
  const off = !s.writable || f.read_only;
  switch (f.control) {
    case 'switch':
      return html`<label class="switch st-switch"><input type="checkbox" role="switch" id=${id} data-in=${f.key} .checked=${value === true} ?disabled=${off} @change=${(e: Event) => on.put(path, (e.target as HTMLInputElement).checked)} /><span data-switch-state>${value === true ? 'On' : 'Off'}</span></label>`;
    case 'number':
    case 'text':
      return html`<input id=${id} data-in=${f.key} class="st-input ${f.control === 'number' ? 'num is-num' : ''}" type="text" inputmode=${f.control === 'number' ? (f.type === 'integer' ? 'numeric' : 'decimal') : 'text'} spellcheck="false" autocapitalize="off" autocomplete="off"
        aria-invalid=${bad} aria-describedby="${id}-d" ?readonly=${off} placeholder=${f.nullable ? 'not set' : ''} .value=${s.textAt(path, f)} @input=${(e: Event) => on.type(path, f, (e.target as HTMLInputElement).value)} @blur=${() => on.tidy(path)} />`;
    case 'textarea':
    case 'json': {
      const text = s.textAt(path, f);
      return html`<textarea id=${id} data-in=${f.key} class="st-area ${f.control === 'json' ? 'code' : ''}" rows=${Math.min(14, Math.max(3, text.split('\n').length + 1))} spellcheck="false" autocapitalize="off"
        aria-invalid=${bad} aria-describedby="${id}-d" ?readonly=${off} .value=${text} @input=${(e: Event) => on.type(path, f, (e.target as HTMLTextAreaElement).value)} @blur=${() => on.tidy(path)}></textarea>`;
    }
    case 'choice':
      return html`<div class="seg" role="group" aria-label=${label} id=${id}>
        ${(f.options ?? []).map((o) => html`<button type="button" class="seg-btn ${same(o.value, value) ? 'is-on' : ''}" aria-pressed=${same(o.value, value)} data-value=${o.label} ?disabled=${off} @click=${() => on.put(path, o.value)}>${o.label}</button>`)}
        ${f.nullable ? html`<button type="button" class="seg-btn ${value === null || value === undefined ? 'is-on' : ''}" aria-pressed=${value === null || value === undefined} data-value="" ?disabled=${off} @click=${() => on.put(path, null)}>not set</button>` : nothing}
      </div>`;
    case 'select':
      return html`<select id=${id} data-in=${f.key} ?disabled=${off} aria-invalid=${bad} @change=${(e: Event) => {
        const i = Number((e.target as HTMLSelectElement).value);
        on.put(path, i < 0 ? null : f.options![i].value);
      }}>
        ${f.nullable || value === undefined || value === null ? html`<option value="-1" ?selected=${value === null || value === undefined}>not set</option>` : nothing}
        ${(f.options ?? []).map((o, i) => html`<option value=${i} ?selected=${same(o.value, value)}>${o.label}</option>`)}
      </select>`;
    case 'const':
      return html`<p class="small" id=${id} data-const><code class="num">${shown(f.default)}</code> <span class="muted">fixed by the plugin</span></p>`;
    case 'secret':
      return html`<p class="small muted" id=${id} data-secret-field>A secret is not a setting: it is stored under Secrets, and never shown.</p>`;
    case 'group':
      return html`<fieldset class="plg-group" id=${id} data-group=${f.key}>
        <legend class="sr-only">${label}</legend>
        ${(f.fields ?? []).map((sub) => inner(sub, [...path, sub.path[sub.path.length - 1]], s, on))}
      </fieldset>`;
    case 'list':
      return list(f, path, Array.isArray(value) ? value : [], s, on, label, off);
    default:
      return html`<p class="small muted">${shown(value)}</p>`;
  }
}

function list(f: FormField, path: Step[], items: unknown[], s: FormState, on: FormHandlers, label: string, off: boolean): TemplateResult {
  const item = f.item!;
  if (isSet(f))
    return html`<div class="hk-checks" role="group" aria-label=${label} id=${idOf(path)} data-set=${f.key}>
      ${item.options!.map((o) => {
        const has = items.some((x) => same(x, o.value));
        // the options' own order is kept, whatever the order of the clicks
        const next = (to: boolean) => item.options!.filter((x) => (same(x.value, o.value) ? to : items.some((y) => same(y, x.value)))).map((x) => x.value);
        return html`<label class="hk-check ${off ? 'is-off' : ''}" data-option=${o.label}><input type="checkbox" .checked=${has} ?disabled=${off} @change=${(e: Event) => on.put(path, next((e.target as HTMLInputElement).checked))} /><span>${o.label}</span></label>`;
      })}
    </div>`;
  const full = f.max_items !== undefined && items.length >= f.max_items;
  return html`<div class="plg-items" id=${idOf(path)} data-list=${f.key}>
    ${items.length
      ? html`<ol class="plg-items-l">
          ${items.map((_, i) => {
            const at = [...path, i];
            const err = s.errorAt(at);
            return html`<li class="plg-item-r" data-item=${i}>
              <div class="plg-item-c">${control(item, at, s, on, `${label}, item ${i + 1}`)}${err && item.control !== 'group' ? html`<p class="st-err small" role="alert" data-invalid>${err}</p>` : nothing}</div>
              ${off ? nothing : html`<button type="button" class="btn btn-sm btn-ghost st-danger" aria-label=${`Remove item ${i + 1} of ${label}`} data-act="item-remove" @click=${() => on.put(at, undefined)}>${icon('trash')}</button>`}
            </li>`;
          })}
        </ol>`
      : html`<p class="small muted" data-list-none>None.</p>`}
    ${off ? nothing : html`<div><button type="button" class="btn btn-sm" data-act="item-add" ?disabled=${full} @click=${() => on.put([...path, items.length], blankOf(item))}>${icon('plus')} Add ${items.length ? 'another' : 'one'}</button></div>`}
  </div>`;
}

/** what stands under a control: what is wrong with it, else its limits and its default */
function under(f: FormField, path: Step[], s: FormState): TemplateResult {
  const err = s.errorAt(path);
  const limits = limitsOf(f);
  const hints = [limits, f.has_default && f.control !== 'const' && f.control !== 'group' && f.default !== null && f.default !== '' ? `default ${shown(f.default, 48)}` : null, f.control === 'json' && f.why ? f.why : null].filter((x): x is string => !!x);
  return html`<div class="st-under" id="${idOf(path)}-d">
    ${err ? html`<p class="st-err small" role="alert" data-invalid>${err}</p>` : nothing}
    ${hints.length ? html`<p class="st-hints xs">${hints.map((h) => html`<span class="st-hint">${h.charAt(0).toUpperCase()}${h.slice(1)}</span>`)}</p>` : nothing}
  </div>`;
}

/** a field inside a group: its label over its control */
function inner(f: FormField, path: Step[], s: FormState, on: FormHandlers): TemplateResult {
  return html`<div class="plg-sub ${s.errorAt(path) ? 'is-bad' : ''}" data-field=${f.key} data-control=${f.control}>
    <label class="hk-label" for=${idOf(path)}>${f.label}${f.required ? html`<span class="xs muted"> (needed)</span>` : nothing}</label>
    ${f.description ? html`<p class="xs muted">${sentence(f.description)}</p>` : nothing}
    ${control(f, path, s, on, f.label)}
    ${under(f, path, s)}
  </div>`;
}

const sentence = (t: string): string => `${t.charAt(0).toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? '' : '.'}`;
const WIDE = new Set(['group', 'list', 'json', 'textarea']);

/** One property's row. */
function row(f: FormField, s: FormState, on: FormHandlers): TemplateResult {
  const prop = f.path[0];
  const path: Step[] = [prop];
  const staged = s.staged(prop);
  const wide = WIDE.has(f.control) && !isSet(f);
  return html`<div class="st-row ${staged ? 'is-staged' : ''} ${wide ? 'is-wide' : ''}" data-key=${`settings.${prop}`} data-staged=${staged ?? 'no'} data-control=${f.control} data-field=${f.key}>
    <div class="st-row-l">
      <label class="st-label" id="${idOf(path)}-l" for=${idOf(path)}>${f.label}</label>
      ${staged ? html`<span class="st-mark" data-mark>${staged === 'unset' ? 'goes back' : 'changed'}</span>` : nothing}
      ${f.required ? html`<span class="xs muted" data-required>needed</span>` : nothing}
      ${f.description ? html`<p class="st-text small muted">${sentence(f.description)}</p>` : nothing}
    </div>
    <div class="st-row-c">${control(f, path, s, on, f.label)}${under(f, path, s)}</div>
    <div class="st-row-a">
      ${staged
        ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => on.undo(prop)}>Undo</button>`
        : s.writable && s.isSet(prop) && (f.has_default || !f.required)
          ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="reset" @click=${() => on.reset(prop)}>Use the default</button>`
          : nothing}
    </div>
  </div>`;
}

/** The form: a row a property, then the places where the generator fell back to JSON. */
export function settingsForm(s: FormState, on: FormHandlers): TemplateResult {
  return html`<div class="st-rows plg-form" data-settings-form=${s.form.fields.length}>${s.form.fields.map((f) => row(f, s, on))}</div>
    ${s.form.notes.length ? html`<p class="xs muted st-wrap" data-form-notes>Edited as JSON: ${s.form.notes.join('; ')}.</p>` : nothing}`;
}

