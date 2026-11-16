// What was (VIS-280): a scope's versions, newest first, each with what differs from now and a
// restore that is staged like any other change (so it is planned and confirmed before it writes),
// and the audit rows of the settings, one change set's alone when it is named. A conductor
// document's version carries no values: its restore's plan shows the diff. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { AuditRow, KeyValue, Overview, Scope, SettingsError, Version, VersionsView } from '../../api/settings';
import { shown, versionDiff } from '../../lib/settings-form';
import { auditWords } from '../../lib/settings-words';
import { refusal } from './sheet';

/** `14 Nov, 09:00` in the reader's own time */
export const when = (iso: string | null): string => (iso ? new Date(iso).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'time not kept');

export interface VersionsState {
  scope: Scope;
  target: string;
  /** null until the panel was opened and read */
  view: VersionsView | null;
  error: SettingsError | null;
  /** the version whose difference from now shows */
  compare: string | null;
  values: KeyValue[];
  secrets: Set<string>;
  /** the version a restore is staged for */
  staged: string | null;
  writable: boolean;
}

export interface VersionsHandlers {
  /** the panel opened: read the versions */
  open(): void;
  compare(id: string | null): void;
  restore(id: string): void;
  /** take the staged restore back */
  undo(): void;
}

function versionRow(v: Version, s: VersionsState, h: VersionsHandlers): TemplateResult {
  const diff = s.compare === v.id ? versionDiff(s.scope, v, s.values, s.secrets) : null;
  const staged = s.staged === v.id;
  return html`<li class="st-ver ${v.current ? 'is-now' : ''} ${staged ? 'is-staged' : ''}" data-version=${v.id}>
    <div class="st-ver-h">
      <b class="num">${/^\d+$/.test(v.id) ? `v${v.id}` : v.id}</b>
      ${v.current ? html`<span class="st-chip is-ok" data-current>holds now</span>` : nothing}
      ${v.good && !v.current ? html`<span class="st-chip is-quiet" data-good>last good start</span>` : nothing}
      <span class="small muted">${when(v.at)}${v.who ? ` by ${v.who}` : ''}</span>
    </div>
    ${v.note ? html`<p class="small st-wrap">${v.note}</p>` : nothing}
    ${v.keys.length ? html`<p class="xs muted st-wrap">Changed: <span class="num">${v.keys.slice(0, 6).join(', ')}</span>${v.keys.length > 6 ? ` and ${v.keys.length - 6} more` : ''}</p>` : nothing}
    <div class="st-ver-a">
      ${v.values && !v.current ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="version-compare" aria-expanded=${s.compare === v.id} @click=${() => h.compare(s.compare === v.id ? null : v.id)}>${s.compare === v.id ? 'Hide the difference' : 'Compare with now'}</button>` : nothing}
      ${v.change_set ? html`<a class="btn btn-sm btn-ghost num" href=${`#/app/history?change_set=${encodeURIComponent(v.change_set)}`} data-act="version-history">${v.change_set}</a>` : nothing}
      ${v.current || !s.writable
        ? nothing
        : staged
          ? html`<span class="st-mark">staged</span><button type="button" class="btn btn-sm btn-ghost" data-act="version-undo" @click=${h.undo}>Undo</button>`
          : html`<button type="button" class="btn btn-sm" data-act="version-restore" @click=${() => h.restore(v.id)}>Restore…</button>`}
    </div>
    ${diff
      ? diff.length
        ? html`<ul class="rs-changes st-ver-diff" data-version-diff>${diff.map((d) => html`<li class="rs-change"><span class="rs-key num">${d.key}</span><span class="rs-val"><span class="rs-from" title="now">${shown(d.now, 48)}</span><span class="rs-arrow" aria-hidden="true">→</span><span class="sr-only"> would become </span><span class="rs-to" title="in this version">${shown(d.then, 48)}</span></span></li>`)}</ul><p class="xs faint">Now on the left, this version on the right. Secrets are not kept in versions.</p>`
        : html`<p class="small muted" data-version-same>Nothing differs: this version holds what holds now.</p>`
      : nothing}
  </li>`;
}

/** A scope's versions, behind a fold that reads them when it opens. */
export function versionsPanel(s: VersionsState, h: VersionsHandlers): TemplateResult {
  const list = s.view?.versions ?? null;
  return html`<details class="panel st-fold st-versions" data-versions=${`${s.scope}/${s.target}`} @toggle=${(e: Event) => (e.target as HTMLDetailsElement).open && h.open()}>
    <summary class="st-fold-h"><span class="panel-title">Versions${list ? html`<span class="st-count">${list.length}</span>` : nothing}</span><span class="panel-sub">Every change here is kept. A restore writes a new version equal to an old one; nothing is ever deleted.</span></summary>
    ${s.error ? refusal(s.error, { quiet: true }) : nothing}
    ${!list && !s.error ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:60%"></div><div class="skeleton-line" style="width:44%"></div></div>` : nothing}
    ${list?.length === 0 ? html`<p class="small muted" data-versions-none>No version yet. The first change made here keeps one of what was before it.</p>` : nothing}
    ${list?.length ? html`<ol class="st-vers">${list.map((v) => versionRow(v, s, h))}</ol>${s.scope === 'conductor' ? html`<p class="xs faint">A document’s restore shows its difference in the plan, before anything is written.</p>` : nothing}` : nothing}
  </details>`;
}

/** The audit rows, newest first; one change set's when `filter` names it. */
export function auditList(rows: AuditRow[] | null, error: SettingsError | null, o: Overview | null, filter: string, h: { filter(cs: string): void }): TemplateResult {
  return html`<section class="panel" data-panel="audit">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">What changed</h2><p class="panel-sub">Every part applied or restored, every change set and every restart’s end: who, when, which keys and how it ended. A value is never kept here.</p></div></header>
    <form class="st-audit-f" @submit=${(e: Event) => (e.preventDefault(), h.filter(((e.currentTarget as HTMLFormElement).elements.namedItem('cs') as HTMLInputElement).value.trim()))}>
      <label class="field"><span>Change set</span><input name="cs" class="num" data-in="audit-filter" placeholder="cs-…" spellcheck="false" autocapitalize="off" .value=${filter} /></label>
      <button type="submit" class="btn btn-sm" data-act="audit-filter">Show</button>
      ${filter ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="audit-all" @click=${() => h.filter('')}>Show everything</button>` : nothing}
    </form>
    ${error ? refusal(error, { quiet: true }) : nothing}
    ${!rows && !error ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:80%"></div><div class="skeleton-line" style="width:62%"></div></div>` : nothing}
    ${rows?.length === 0 ? html`<p class="small muted" data-audit-none>${filter ? html`Nothing is recorded for <span class="num">${filter}</span>.` : 'Nothing was changed through these settings yet.'}</p>` : nothing}
    ${rows?.length
      ? html`<ol class="st-audit" data-audit>${rows.map((r) => {
          const w = auditWords(r, o);
          return html`<li class="st-audit-r" data-action=${r.action} data-result=${r.result ?? ''}>
            <span class="small muted st-audit-at">${when(r.at)}</span>
            <span class="st-audit-what"><b>${w.action}</b>${w.where ? html` <span>${w.where}</span>` : nothing}${r.keys.length ? html` <span class="xs muted num st-wrap">${r.keys.slice(0, 8).join(', ')}${r.keys.length > 8 ? ` +${r.keys.length - 8}` : ''}</span>` : nothing}</span>
            <span class="st-chip is-${w.tone}">${r.result ?? 'recorded'}</span>
            <span class="small muted">${r.who || 'unknown'}</span>
            ${r.change_set ? html`<button type="button" class="btn btn-sm btn-ghost num" data-act="audit-cs" title="Show this change set alone" @click=${() => h.filter(r.change_set!)}>${r.change_set}</button>` : html`<span></span>`}
          </li>`;
        })}</ol>`
      : nothing}
  </section>`;
}
