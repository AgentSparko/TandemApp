// One plugin's page (VIS-284). From the top: what it still waits for when it is not set up; its
// state as the plugin host tells it, with the switch (staged), start, stop and restart (at once),
// its restarts, its last error, what was not registered and what was refused; what it adds, with
// where TandemApp shows each (a panel opens from there); its settings as a form drawn from its schema; its
// secrets (set, replaced or removed, never shown); its trust (where it runs, its signature); what
// it may do (each capability with its grant); its log; its files, where they came from and whether they still
// are the ones installed, with the way to a newer version; and its removal, which the controller
// refuses while a hook names it. Every switch, setting, secret and grant is staged into the
// settings area's draft: nothing is written until the review. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { PluginFull } from '../../api/plugins';
import { KEY, TAILS, type OneState } from '../../lib/plugins-flow';
import { at, NEVER, TRUST_WORDS, RISK_WORDS, capList, contributions, controls, integrityWords, nameOf, pluginsHref, setupOf, signatureWords, sourceWords, stateWords, type Step } from '../../lib/plugins';
import type { FormField } from '../../lib/schema-form';
import { icon } from '../../ui/bits';
import { back, cards, chip, copyBtn, fact } from '../hooks/parts';
import { capRow, contribList, refused, sha, stateMark } from './bits';
import { settingsForm } from './form';

export interface PluginHandlers {
  enable(on: boolean): void;
  /** stage on, every capability it asks for, and the host when it asks for it */
  setUp(): void;
  act(what: 'start' | 'stop' | 'restart' | 'verify'): void;
  grant(capability: string, on: boolean): void;
  trust(to: 'sandboxed' | 'host'): void;
  /** take a staged key back */
  undo(key: string): void;
  type(path: Step[], f: FormField, raw: string): void;
  tidy(path: Step[]): void;
  put(path: Step[], value: unknown): void;
  reset(prop: string): void;
  secretField(name: string, open: boolean): void;
  secretType(name: string, raw: string): void;
  secretRemove(name: string): void;
  logs(tail?: number): void;
  follow(on: boolean): void;
  askRemove(on: boolean): void;
  remove(): void;
}

/** What the page reads of the flow besides its state: what shows with the staged changes applied. */
export interface PluginShown {
  enabled: boolean;
  granted(capability: string): boolean;
  trust: 'sandboxed' | 'host' | 'engine';
  staged(key: string): 'set' | 'unset' | null;
  value(prop: string): unknown;
  textAt(path: Step[], f: FormField): string;
  errorAt(path: Step[]): string | undefined;
  isSet(prop: string): boolean;
}

export interface PluginView {
  one: OneState;
  shown: PluginShown;
  /** the settings interface takes changes of this plugin */
  writable: boolean;
  now: number;
  signIn: TemplateResult | typeof nothing;
}

const ms = (t: number | null | undefined): number | null => (t == null ? null : t < 1e12 ? t * 1000 : t);

function setup(v: PluginView, on: PluginHandlers): TemplateResult | typeof nothing {
  const p = v.one.plugin!;
  const s = setupOf(p);
  // one the plugin host gave up on has its own words, in its state
  if (!s.needed || v.shown.staged(KEY.enabled) || !v.writable || v.one.status?.state === 'failed') return nothing;
  const n = s.grants.length + (s.trust ? 1 : 0);
  return html`<section class="plg-setup" data-setup>
    <div>
      <h3>${nameOf(p)} is turned off</h3>
      <p class="small">Its code does not run and it adds nothing. ${s.text}${s.secrets.length ? ' Store its secrets below before it starts.' : ''}</p>
    </div>
    <div class="st-acts">
      <button type="button" class="btn btn-primary" data-act="set-up" @click=${on.setUp}>${n ? 'Turn it on with what it asks for…' : 'Turn it on…'}</button>
      <span class="xs muted">This stages ${n ? `the switch and ${n === 1 ? 'one grant' : `${n} grants`}` : 'the switch'}. You review ${n ? 'each one' : 'it'} before anything is written${n ? ', and can take any of them back below' : ''}.</span>
    </div>
  </section>`;
}

function status(v: PluginView, on: PluginHandlers): TemplateResult {
  const o = v.one;
  const p = o.plugin!;
  const s = o.status;
  const w = s ? stateWords(s, v.now) : null;
  const can = controls(p, s);
  const staged = v.shown.staged(KEY.enabled);
  const busy = o.act.busy;
  const since = ms(s?.started_at);
  const btn = (what: 'start' | 'stop' | 'restart', label: string, doing: string, glyph: string, why: string | null) =>
    html`<button type="button" class="btn btn-sm" data-act=${what} ?disabled=${why !== null || busy !== null} title=${why ?? nothing} @click=${() => on.act(what)}>${icon(glyph)} ${busy === what ? doing : label}</button>`;
  return html`<section class="panel hk-status ${staged ? 'is-staged' : ''}" data-panel="status" data-state=${s?.state ?? 'unread'}>
    <div class="hk-status-sw">
      <label class="switch st-switch"><input type="checkbox" role="switch" data-in="enabled" .checked=${v.shown.enabled} ?disabled=${!v.writable} @change=${(e: Event) => on.enable((e.target as HTMLInputElement).checked)} /><span data-switch-state>${v.shown.enabled ? 'On' : 'Off'}</span></label>
      ${staged ? html`<span class="st-mark" data-mark>changed</span><button type="button" class="btn btn-sm btn-ghost" data-act="undo-enabled" @click=${() => on.undo(KEY.enabled)}>Undo</button>` : nothing}
      ${w ? stateMark(w, { title: false }) : nothing}
      <span class="plg-controls" role="group" aria-label="Run it">
        ${btn('start', 'Start', 'Starting…', 'play', can.start)}${btn('stop', 'Stop', 'Stopping…', 'stop', can.stop)}${btn('restart', 'Restart', 'Restarting…', 'refresh', can.restart)}
      </span>
      <p class="small hk-status-say" data-state-words>${staged ? (v.shown.enabled ? 'Staged: the plugin host may start it once the change is applied. Turning a plugin on takes pairing’s rights.' : 'Staged: its process ends once the change is applied, and what it adds goes with it.') : (w?.text ?? (o.statusRefused ? '' : 'Reading its state…'))}</p>
    </div>
    ${o.statusRefused ? refused(o.statusRefused, { quiet: true }) : nothing}
    ${o.act.refused && o.act.busy === null ? refused(o.act.refused) : nothing}
    ${s
      ? html`<dl class="hk-facts" data-facts>
            ${fact('Restarts', html`<span class="num">${s.restarts}</span>`, { data: 'restarts', sub: 'since the plugin host started' })}
            ${fact('Failures', html`<span class="num">${s.failures_last_hour}</span>`, { data: 'failures', tone: s.failures_last_hour ? 'warn' : undefined, sub: 'in the last hour; five turn it off' })}
            ${fact('Runs as', s.runtime ? html`${s.runtime === 'host' ? 'a host process' : s.runtime === 'docker' ? 'a container' : 'a pod'}` : '—', { data: 'runtime', sub: s.where ?? (s.state === 'running' ? undefined : 'not running') })}
            ${fact('Since', since ? at(since) : '—', { data: 'since', sub: s.version ? `version ${s.version}` : undefined })}
          </dl>
          ${s.last_error
            ? html`<div class="hk-explain is-${s.state === 'running' ? 'warn' : 'err'}" data-last-error>
                <p class="hk-explain-t">${s.state === 'running' ? 'Its last error, before it ran again' : 'Its last error'}</p>
                <pre class="plg-err"><code>${s.last_error}</code></pre>
              </div>`
            : nothing}
          ${s.notes.length
            ? html`<div class="hk-explain is-warn" data-notes>
                <p class="hk-explain-t">Not registered</p>
                <ul class="small">${s.notes.map((n) => html`<li>${n.charAt(0).toUpperCase()}${n.slice(1)}${/[.!?]$/.test(n) ? '' : '.'}</li>`)}</ul>
              </div>`
            : nothing}
          ${s.refusals.length
            ? html`<details class="st-doc" data-refusals=${s.refusals.length}>
                <summary class="small">${s.refusals.length === 1 ? 'One thing it tried and was refused' : `${s.refusals.length} things it tried and was refused`}</summary>
                <ol class="plg-journal">${[...s.refusals].reverse().map((r) => html`<li><span class="small muted">${at(ms(r.ts))}</span><span class="small">${r.what.charAt(0).toUpperCase()}${r.what.slice(1)}</span><code class="num xs">${r.capability}</code></li>`)}</ol>
                <p class="xs muted">The plugin host checks each capability where it is used. A refusal means the plugin asked for something it was not granted: grant it below if it should have it.</p>
              </details>`
            : nothing}`
      : nothing}
  </section>`;
}

function adds(v: PluginView): TemplateResult {
  const p = v.one.plugin!;
  const items = contributions(p.manifest, p.id, v.one.status);
  return html`<section class="panel" data-panel="adds">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">What it adds</h2><p class="panel-sub">Each is registered while the plugin runs and gone when it stops.</p></div></header>
    ${contribList(items)}
    ${p.references.length
      ? html`<div class="plg-refs" data-references=${p.references.length}>
          <h3 class="hk-sub">${p.references.length === 1 ? 'One hook names it' : `${p.references.length} hooks name it`}</h3>
          <p class="xs muted">A hook of kind plugin takes a contributed hook’s place, with a priority, a scope and a mode of its own.</p>
          <ul class="plg-rows">${p.references.map((h) => html`<li class="plg-row" data-ref=${h.id}><span class="plg-row-b"><span class="plg-row-h"><b>${h.name}</b>${h.enabled ? nothing : chip('quiet', 'off')}</span><span class="num xs muted">${h.id}</span></span><a class="btn btn-sm btn-ghost" href=${`#/app/hooks/hook/${encodeURIComponent(h.id)}`} data-act="ref-open">Open the hook${icon('right')}</a></li>`)}</ul>
        </div>`
      : nothing}
  </section>`;
}

function settings(v: PluginView, on: PluginHandlers): TemplateResult | typeof nothing {
  const p = v.one.plugin!;
  const form = p.settings_form;
  if (!form?.fields.length) return nothing;
  return html`<section class="panel st-group" data-panel="settings">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Settings</h2><p class="panel-sub">${form.description ?? 'Drawn from the plugin’s own schema.'} A change is staged with the rest; the plugin starts again with the new values once it is applied.</p></div></header>
    ${settingsForm(
      { form, value: v.shown.value, textAt: v.shown.textAt, errorAt: v.shown.errorAt, staged: (prop) => v.shown.staged(KEY.setting(prop)), isSet: v.shown.isSet, writable: v.writable },
      { type: on.type, tidy: on.tidy, put: on.put, reset: on.reset, undo: (prop) => on.undo(KEY.setting(prop)) },
    )}
  </section>`;
}

function secrets(v: PluginView, on: PluginHandlers): TemplateResult | typeof nothing {
  const p = v.one.plugin!;
  const declared = p.manifest.secrets ?? [];
  if (!declared.length) return nothing;
  return html`<section class="panel st-group" data-panel="secrets">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Secrets</h2><p class="panel-sub">Stored sealed and never shown again, here or in any answer. The plugin gets a secret only while the capability for it is granted.</p></div></header>
    <div class="st-rows">
      ${declared.map((d) => {
        const key = KEY.secret(d.name);
        const hint = p.secrets[d.name];
        const staged = v.shown.staged(key);
        const open = d.name in v.one.secretOpen;
        const cap = `secrets:${d.name}`;
        const state = staged === 'set' ? 'A new value is staged. It is sent when the change set is applied.' : staged === 'unset' ? 'The stored value is removed when the change set is applied.' : hint?.set ? `A value is stored${hint.last4 ? `, ending in ${hint.last4}` : ''}${hint.sha256_6 ? ` (its SHA-256 starts ${hint.sha256_6})` : ''}${hint.set_at ? `, since ${at(hint.set_at)}` : ''}. It is never shown.` : 'No value is stored.';
        return html`<div class="st-row ${staged ? 'is-staged' : ''}" data-key=${key} data-staged=${staged ?? 'no'} data-control="secret">
          <div class="st-row-l">
            <label class="st-label num" for="plg-s-${d.name}">${d.name}</label>
            ${staged ? html`<span class="st-mark" data-mark>${staged === 'unset' ? 'goes' : 'changed'}</span>` : nothing}
            ${d.description ? html`<p class="st-text small muted">${d.description.charAt(0).toUpperCase()}${d.description.slice(1)}${/[.!?]$/.test(d.description) ? '' : '.'}</p>` : nothing}
          </div>
          <div class="st-row-c">
            <div class="st-secret" data-secret=${staged ?? (hint?.set ? 'stored' : 'none')}>
              <p class="small" id="plg-s-${d.name}-s">${state}</p>
              ${open
                ? html`<div class="st-secret-in"><input id="plg-s-${d.name}" data-in=${key} class="st-input num" type="password" autocomplete="new-password" spellcheck="false" aria-describedby="plg-s-${d.name}-s" placeholder="Paste or type the value" .value=${v.one.secretOpen[d.name] ?? ''} @input=${(e: Event) => on.secretType(d.name, (e.target as HTMLInputElement).value)} />
                    ${staged === 'set' ? nothing : html`<button type="button" class="btn btn-sm btn-ghost" data-act="secret-close" @click=${() => on.secretField(d.name, false)}>Cancel</button>`}</div>`
                : v.writable && staged !== 'unset'
                  ? html`<div class="st-secret-acts"><button type="button" class="btn btn-sm" data-act="secret-set" @click=${() => on.secretField(d.name, true)}>${hint?.set ? 'Replace the value…' : 'Set a value…'}</button></div>`
                  : nothing}
            </div>
            ${p.capabilities.requested.includes(cap) && !v.shown.granted(cap) ? html`<p class="st-hints xs" data-secret-ungranted><span class="st-hint">The plugin does not get it yet: <code class="num">${cap}</code> is not granted.</span></p>` : nothing}
          </div>
          <div class="st-row-a">
            ${staged
              ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => on.undo(key)}>Undo</button>`
              : hint?.set && v.writable
                ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="secret-remove" @click=${() => on.secretRemove(d.name)}>Remove the value</button>`
                : nothing}
          </div>
        </div>`;
      })}
    </div>
  </section>`;
}

/** Trust: where its code runs, and whose code it is. Its own panel, so what trust comes to hold
 *  (whose signature counts, a level an engine host allows) has its place. */
function trust(v: PluginView, on: PluginHandlers): TemplateResult {
  const p = v.one.plugin!;
  const asked = p.trust.requested;
  const tw = TRUST_WORDS[asked];
  const tstaged = v.shown.staged(KEY.trust);
  const sig = signatureWords(p.signature);
  return html`<section class="panel" data-panel="trust">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Trust</h2><p class="panel-sub">Where its code runs, and whose code it is.</p></div></header>
    <div class="plg-level is-${tw.risk} ${tstaged ? 'is-staged' : ''}" data-trust-asked=${asked} data-trust=${v.shown.trust}>
      <p class="plg-cap-h"><b>Where it runs: ${tw.label.toLowerCase()}</b>${chip(RISK_WORDS[tw.risk].tone, RISK_WORDS[tw.risk].label, { data: 'risk' })}${tstaged ? html`<span class="st-mark" data-mark>changed</span><button type="button" class="btn btn-sm btn-ghost" data-act="undo-trust" @click=${() => on.undo(KEY.trust)}>Undo</button>` : nothing}</p>
      <p class="small muted">${tw.text}</p>
      ${asked === 'host'
        ? html`${cards<'sandboxed' | 'host'>(
              'trust',
              v.shown.trust === 'host' ? 'host' : 'sandboxed',
              [
                { id: 'sandboxed', label: 'Do not allow it', text: 'It stays installed and does not start.', off: v.writable ? undefined : 'Not changed from here.' },
                { id: 'host', label: 'Allow it on the host', text: 'It runs with everything the app’s user can read and write. The review asks a second time.', off: v.writable ? undefined : 'Not changed from here.' },
              ],
              (t) => on.trust(t),
              { label: 'Where it may run' },
            )}`
        : nothing}
    </div>
    <p class="small plg-facts plg-trust-sig" data-trust-signature>${chip(sig.tone, sig.label, { data: 'signature' })}<span class="muted">${sig.text}</span></p>
  </section>`;
}

function may(v: PluginView, on: PluginHandlers): TemplateResult {
  const p = v.one.plugin!;
  const caps = capList(p.capabilities.requested);
  return html`<section class="panel" data-panel="may">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">What it may do</h2><p class="panel-sub">A plugin gets nothing by default. It asks; you grant, one capability at a time, and can take each back. Granting takes pairing’s rights.</p></div></header>
    ${caps.length
      ? html`<ul class="plg-caps" data-caps=${caps.length}>
          ${caps.map((c) => {
            const key = KEY.cap(c.capability);
            const staged = v.shown.staged(key);
            const onNow = v.shown.granted(c.capability);
            const g = p.capabilities.grants[c.capability];
            return capRow(c, {
              staged: !!staged,
              under: html`${staged
                ? html`<p class="xs" data-grant-staged><span class="st-mark" data-mark>${onNow ? 'grant staged' : 'taken back'}</span> ${onNow && c.confirm ? 'The review asks a second time before it is granted.' : ''}</p>`
                : g
                  ? html`<p class="xs muted" data-grant>Granted by ${g.by ?? 'the admin'}, ${at(g.at)}${g.confirmed_by ? `; confirmed a second time by ${g.confirmed_by}` : ''}.</p>`
                  : nothing}`,
              end: html`<span class="plg-cap-e">
                <label class="switch st-switch"><input type="checkbox" role="switch" data-in=${key} aria-label=${`${c.title}: granted`} .checked=${onNow} ?disabled=${!v.writable} @change=${(e: Event) => on.grant(c.capability, (e.target as HTMLInputElement).checked)} /><span data-switch-state>${onNow ? 'Granted' : 'Not granted'}</span></label>
                ${staged ? html`<button type="button" class="btn btn-sm btn-ghost" data-act="undo" @click=${() => on.undo(key)}>Undo</button>` : nothing}
              </span>`,
            });
          })}
        </ul>`
      : html`<p class="small muted" data-caps="0">It asks for no capability: it sees no request, reaches no other machine and reads no setting.</p>`}
    <p class="xs faint">${NEVER}</p>
  </section>`;
}

function log(v: PluginView, on: PluginHandlers): TemplateResult {
  const l = v.one.logs;
  const text = (l.lines ?? []).join('\n');
  return html`<section class="panel" data-panel="log">
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Log</h2><p class="panel-sub">What it wrote to stderr and what the plugin host said about it. Every secret’s value reads <code>[secret NAME]</code>.</p></div>
      <div class="panel-tools plg-log-tools">
        <label class="small plg-tail"><span class="sr-only">How many lines</span><select data-in="tail" @change=${(e: Event) => on.logs(Number((e.target as HTMLSelectElement).value))}>${TAILS.map((n) => html`<option value=${n} ?selected=${n === l.tail}>last ${n} lines</option>`)}</select></label>
        <label class="hk-check small" data-follow><input type="checkbox" .checked=${l.follow} @change=${(e: Event) => on.follow((e.target as HTMLInputElement).checked)} /><span>Keep reading</span></label>
        <button type="button" class="btn btn-sm btn-ghost" data-act="log-reload" ?disabled=${l.busy} @click=${() => on.logs()}>${icon('refresh')} ${l.busy ? 'Reading…' : 'Read again'}</button>
        ${l.lines?.length ? copyBtn(text, 'Copy the log') : nothing}
      </div></header>
    ${l.refused ? refused(l.refused, { quiet: true }) : nothing}
    ${l.lines === null && !l.refused ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:82%"></div><div class="skeleton-line" style="width:64%"></div></div>` : nothing}
    ${l.lines?.length === 0 ? html`<p class="small muted" data-log-none>Nothing yet. A line shows here once the plugin has started.</p>` : nothing}
    ${l.lines?.length ? html`<pre class="plg-log" tabindex="0" aria-label="The plugin’s log" data-log=${l.lines.length}><code>${l.lines.map((x) => html`<span class=${/error|traceback|exception|refused|gave up|exited with [1-9]/i.test(x) ? 'plg-log-e' : x.includes('[host]') ? 'plg-log-h' : ''}>${x}\n</span>`)}</code></pre><p class="xs faint">${l.at ? `Read ${at(l.at)}.` : ''} Newest last. The file is <code>logs/plugins/${v.one.id}.log</code> in the controller’s state folder.</p>` : nothing}
  </section>`;
}

function files(v: PluginView, on: PluginHandlers): TemplateResult {
  const o = v.one;
  const p = o.plugin!;
  const src = sourceWords(p.source);
  const sig = signatureWords(p.signature);
  const checked = o.act.verified;
  const integ = integrityWords(checked ? { integrity: checked.integrity, sha256: p.sha256 } : p);
  const m = p.manifest;
  return html`<section class="panel" data-panel="files" data-integrity=${checked?.integrity ?? p.integrity}>
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Its files</h2><p class="panel-sub">Where it came from, and whether what is on disk is still what was installed.</p></div>
      <div class="panel-tools"><a class="btn btn-sm" href=${pluginsHref(['install'], { upgrade: p.id })} data-act="upgrade">Install another version…</a></div></header>
    <dl class="hk-kv" data-source=${p.source.type}>
      <div><dt>From</dt><dd>${src.kind} <span class="num st-wrap">${src.where}</span>${p.source.type === 'git' ? html`<br /><span class="xs muted">commit</span> <span class="num xs st-wrap">${src.pin}</span>` : p.source.type === 'wheel' && src.pin ? html`<br /><span class="xs muted">sha256 of the wheel</span> <span class="num xs st-wrap">${src.pin}</span>` : nothing}</dd></div>
      <div><dt>Version</dt><dd><span class="num">${p.version}</span>${m.author ? html` <span class="muted">by ${m.author}</span>` : nothing}${m.license ? html` <span class="xs muted">${m.license}</span>` : nothing}${m.homepage ? html` <a class="xs" href=${m.homepage} target="_blank" rel="noopener noreferrer">its page${icon('external')}</a>` : nothing}</dd></div>
      <div><dt>Installed</dt><dd>${at(p.installed_at)}${p.installed_by ? ` by ${p.installed_by}` : ''}${p.updated_at !== p.installed_at ? html`<br /><span class="xs muted">last changed ${at(p.updated_at)}${p.updated_by ? ` by ${p.updated_by}` : ''}</span>` : nothing}</dd></div>
      <div><dt>Signature</dt><dd>${chip(sig.tone, sig.label, { data: 'signature' })} <span class="small muted">${sig.text}</span></dd></div>
      <div><dt>On disk</dt><dd data-integrity-words>${chip(integ.tone, integ.label, { data: 'integrity' })} <span class="small muted">${integ.text}${p.checked_at && !checked ? ` Last checked ${at(p.checked_at)}.` : checked ? ' Checked just now.' : ''}</span>
        <div class="st-acts"><button type="button" class="btn btn-sm" data-act="verify" ?disabled=${o.act.busy !== null} @click=${() => on.act('verify')}>${o.act.busy === 'verify' ? 'Checking…' : 'Check the files now'}</button></div></dd></div>
    </dl>
    ${sha(p.sha256, 'sha256 recorded at the install')}
    ${checked && checked.integrity === 'mismatch' && checked.sha256 ? sha(checked.sha256, 'sha256 of the files now') : nothing}
    ${p.kind === 'engine-stats' ? html`<p class="small" data-engine-stats>${icon('info')} An engine loads this package itself to send its figures${m.engine_stats?.install ? html`: <code>${m.engine_stats.install}</code> on the engine’s machine` : ''}. TandemApp records it here and does not run it.</p>` : nothing}
    <details class="st-doc" data-manifest><summary class="small">The manifest, as it is</summary><pre class="hk-pre"><code>${JSON.stringify(m, null, 2)}</code></pre></details>
  </section>`;
}

function removal(v: PluginView, on: PluginHandlers): TemplateResult {
  const p = v.one.plugin!;
  const r = v.one.remove;
  const refs = p.references;
  const hooks = (label: string) => html`<ul class="plg-rows" data-remove-refs=${refs.length}>${refs.map((h) => html`<li class="plg-row"><span class="plg-row-b"><span class="plg-row-h"><b>${h.name}</b>${h.enabled ? nothing : chip('quiet', 'off')}</span><span class="num xs muted">${h.id}</span></span><a class="btn btn-sm" href=${`#/app/hooks/hook/${encodeURIComponent(h.id)}`} data-act="ref-open">${label}${icon('right')}</a></li>`)}</ul>`;
  return html`<section class="panel hk-danger" data-panel="remove" data-removable=${refs.length === 0}>
    ${r.step === 'idle'
      ? html`<div class="hk-danger-row"><div><h2 class="panel-title">Remove this plugin</h2><p class="panel-sub">${refs.length ? `It cannot go yet: ${refs.length === 1 ? 'a hook names' : `${refs.length} hooks name`} it. Remove ${refs.length === 1 ? 'that hook' : 'those hooks'} first, or point ${refs.length === 1 ? 'it' : 'them'} at something else.` : 'Its process ends first. Its folder, its settings, its secrets and its grants go with it.'}</p></div><button type="button" class="btn st-danger" data-act="remove-ask" @click=${() => on.askRemove(true)}>${icon('trash')} Remove…</button></div>
          ${refs.length ? hooks('Open the hook') : nothing}`
      : html`<div role="alertdialog" aria-labelledby="plg-remove-t" data-remove-ask>
          <h2 class="panel-title" id="plg-remove-t">Remove ${nameOf(p)}?</h2>
          <p class="small">It is removed now, not staged: its process ends, then its folder, its settings, its secrets and its grants go. This cannot be undone; installing it again starts from nothing.</p>
          ${r.refused ? html`${refused(r.refused)}${r.refused.type === 'referenced' && refs.length ? hooks('Open the hook') : nothing}` : nothing}
          ${v.signIn}
          <div class="st-acts"><button type="button" class="btn btn-ghost" data-act="remove-cancel" ?disabled=${r.step === 'busy'} @click=${() => on.askRemove(false)}>Keep it</button><button type="button" class="btn hk-go-danger" data-act="remove-go" ?disabled=${r.step === 'busy'} @click=${on.remove}>${r.step === 'busy' ? 'Removing…' : r.refused?.type === 'referenced' ? 'Try again' : 'Remove the plugin'}</button></div>
        </div>`}
  </section>`;
}

/** The plugin's page. */
export function pluginView(v: PluginView, on: PluginHandlers): TemplateResult {
  const p: PluginFull = v.one.plugin!;
  const run = p.kind === 'plugin';
  return html`${back(pluginsHref(), 'Plugins')}
    <header class="hk-head" data-plugin-page=${p.id} data-kind=${p.kind}>
      <h2 class="hk-title">${nameOf(p)}</h2>
      <p class="small muted hk-head-meta"><span class="num">${p.id}</span><span class="num">${p.version}</span>${run ? nothing : chip('quiet', 'engine stats', { data: 'kind' })}</p>
      ${p.description ? html`<p class="small plg-desc">${p.description}</p>` : nothing}
    </header>
    ${v.one.refused ? refused(v.one.refused) : nothing}
    ${v.writable ? nothing : html`<p class="note small" data-readonly>${icon('info')} The settings interface does not take changes of this plugin here, so its switches are shown as they are.</p>`}
    ${p.integrity === 'ok' ? nothing : html`<div class="hk-explain is-err" role="alert" data-integrity-alert><p class="hk-explain-t">${integrityWords(p).text}</p><p class="small">Install it again from its source: Its files, below.</p></div>`}
    ${run ? html`${setup(v, on)}${status(v, on)}${adds(v)}${settings(v, on)}${secrets(v, on)}${trust(v, on)}${may(v, on)}${log(v, on)}` : nothing}
    ${files(v, on)} ${removal(v, on)}`;
}

/** A plugin that is no longer there: removed on another screen, or a link that never was one. */
export function pluginGone(id: string): TemplateResult {
  return html`${back(pluginsHref(), 'Plugins')}
    <section class="panel st-empty" data-plugin-gone=${id}>
      <h2 class="panel-title">No plugin ${id}</h2>
      <p class="small muted">It was removed, here or through the API. Its settings, secrets and grants went with it.</p>
      <div class="st-acts"><a class="btn btn-primary" href=${pluginsHref()}>Back to the plugins</a></div>
    </section>`;
}
