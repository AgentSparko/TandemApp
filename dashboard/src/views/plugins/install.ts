// Install a plugin (VIS-284): one page, the stops an install makes in their order. Where it comes
// from (a folder or a wheel on the controller's machine, a wheel uploaded from this computer, or a
// git URL at one full commit id); then, once the source was checked and before anything is
// installed: what the checks found (every check, each refusal in plain words with what to change
// and the controller's sentence as it came), the sha256 of its files, what it asks for (where it
// runs and each capability, the riskiest first) and what it adds. Only then Install. A refused
// source installs nothing and says so. An upgrade shows the version it replaces; an older version
// is asked for on purpose. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { Inspection, Plugin, Source } from '../../api/plugins';
import type { InstallState } from '../../lib/plugins-flow';
import { NEVER, SOURCE_WORDS, TRUST_WORDS, RISK_WORDS, bytes, capList, checkReport, contributions, inspectHead, installedWords, pluginsHref, problemOf, versionMove, type CheckRow, type SourceForm } from '../../lib/plugins';
import { flatFields } from '../../lib/schema-form';
import { icon } from '../../ui/bits';
import { back, cards, chip, field, textInput } from '../hooks/parts';
import { capRow, contribList, problem, refused, sha } from './bits';

export interface InstallHandlers {
  edit(p: Partial<SourceForm>): void;
  /** a wheel chosen on this computer: it goes to the controller, then it is checked */
  upload(file: File): void;
  /** check the source: nothing is installed */
  inspect(): void;
  downgrade(on: boolean): void;
  install(): void;
  /** on to the plugin's page */
  done(): void;
  again(): void;
}

export interface InstallView {
  state: InstallState;
  /** why Install cannot go now */
  blocked: string | null;
  /** the plugin an upgrade replaces, when it is known */
  replaces: Plugin | null;
  signIn: TemplateResult | typeof nothing;
}

/** A wheel from this computer: the file picker, then what the controller kept and until when. */
function uploadRow(s: InstallView['state'], on: InstallHandlers, busy: boolean): TemplateResult {
  const u = s.upload;
  const kept = u?.kept && s.form.path === u.kept.source.path ? u.kept : null;
  return html`<div class="hk-field is-wide plg-upload" data-field="upload" data-upload=${u?.busy ? 'busy' : kept ? 'kept' : u?.refused ? 'refused' : 'none'}>
    <label class="btn btn-ghost btn-sm" data-act="upload">
      ${icon('plus')} ${u?.busy ? `Uploading ${u.name}…` : 'Upload a wheel from this computer'}
      <input class="sr-only" type="file" accept=".whl" ?disabled=${busy || !!u?.busy}
        @change=${(e: Event) => {
          const el = e.target as HTMLInputElement;
          const f = el.files?.[0];
          el.value = '';
          if (f) on.upload(f);
        }} />
    </label>
    ${kept
      ? html`<p class="xs muted" data-upload-kept>Uploaded <b>${kept.name}</b>, ${bytes(kept.bytes)}. The controller keeps it until ${new Date(kept.expires_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}; install it before then.</p>`
      : html`<p class="xs muted">At most 32 MiB. The controller keeps it for an hour and checks it as any wheel.</p>`}
    ${u?.refused ? refused(u.refused) : nothing}
  </div>`;
}

const GLYPH: Record<CheckRow['state'], string> = { pass: '✓', refused: '✕', skipped: '–' };
const STATE_WORD: Record<CheckRow['state'], string> = { pass: 'passed', refused: 'refused', skipped: 'not checked' };

function sourceStep(v: InstallView, on: InstallHandlers): TemplateResult {
  const s = v.state;
  const f = s.form;
  const busy = s.step === 'inspecting' || s.step === 'installing';
  const e = s.errors;
  return html`<li class="hk-step" data-step="source">
    <span class="hk-n num" aria-hidden="true">1</span>
    <div class="hk-step-b">
      <h3 class="hk-step-t">Where it comes from</h3>
      ${cards<Source['type']>('source', f.type, (['folder', 'wheel', 'git'] as const).map((t) => ({ id: t, label: SOURCE_WORDS[t].label, text: SOURCE_WORDS[t].text })), (t) => on.edit({ type: t }), { label: 'Where the plugin comes from' })}
      ${f.type === 'git'
        ? html`${field('url', 'Repository', textInput('url', f.url, (x) => on.edit({ url: x }), { placeholder: 'https://example.com/you/plugin.git', mono: true, bad: !!e.url }), { error: e.url, hint: 'https, ssh or file. The controller’s machine fetches it, without a password prompt.', wide: true })}
            ${field('commit', 'Commit', textInput('commit', f.commit, (x) => on.edit({ commit: x }), { placeholder: '40 hex digits', mono: true, bad: !!e.commit }), { error: e.commit, hint: 'The full commit id. A branch or a tag can move, so neither is taken: git rev-parse HEAD prints it.', wide: true })}
            ${field('subdir', 'Folder in the repository (optional)', textInput('subdir', f.subdir, (x) => on.edit({ subdir: x }), { placeholder: 'plugins/hello', mono: true, bad: !!e.subdir }), { error: e.subdir, hint: 'Where plugin.json is, when it is not at the root.' })}`
        : field('path', f.type === 'wheel' ? 'Wheel file' : 'Folder', textInput('path', f.path, (x) => on.edit({ path: x }), { placeholder: f.type === 'wheel' ? '/srv/dist/hello_tool-0.1.0-py3-none-any.whl' : '/srv/plugins/hello-tool', mono: true, bad: !!e.path }), {
            error: e.path,
            hint: f.type === 'wheel' ? 'A full path on the machine TandemApp’s controller runs on, or upload the wheel below.' : 'A full path on the machine TandemApp’s controller runs on: the folder that holds plugin.json.',
            wide: true,
          })}
      ${f.type === 'wheel' ? uploadRow(s, on, busy) : nothing}
      ${s.refused && s.step === 'form' ? refused(s.refused) : nothing}
      ${s.step === 'form' || s.step === 'inspecting'
        ? html`<div class="st-acts">
              <button type="button" class="btn btn-primary" data-act="inspect" ?disabled=${busy} @click=${on.inspect}>${s.step === 'inspecting' ? 'Checking…' : 'Check it'}</button>
              <span class="xs muted">Nothing is installed and nothing in it runs: TandemApp reads its files and checks them.</span>
            </div>
            ${s.step === 'inspecting' ? html`<div class="skeleton" aria-busy="true" data-inspecting><div class="skeleton-line" style="width:68%"></div><div class="skeleton-line" style="width:52%"></div></div>` : nothing}`
        : nothing}
    </div>
  </li>`;
}

function checksStep(r: Inspection): TemplateResult {
  const rep = checkReport(r);
  const head = inspectHead(r);
  return html`<li class="hk-step" data-step="checks" data-ok=${r.ok}>
    <span class="hk-n num" aria-hidden="true">2</span>
    <div class="hk-step-b">
      <h3 class="hk-step-t">What the checks found</h3>
      <div class="hk-explain is-${head.tone === 'muted' ? 'warn' : head.tone}" role=${r.ok ? 'status' : 'alert'} data-inspect-head=${r.ok ? 'ok' : 'refused'}>
        <p class="hk-explain-t">${head.title}</p>
        <p class="small">${head.text}</p>
      </div>
      <ol class="plg-checks" data-checks data-passed=${rep.passed} data-refused=${rep.refused}>
        ${rep.rows.map(
          (c) => html`<li class="plg-check is-${c.state}" data-check=${c.group.id} data-check-state=${c.state}>
            <span class="plg-check-g" aria-hidden="true">${GLYPH[c.state]}</span>
            <div class="plg-check-b">
              <p class="plg-check-t">${c.group.title}<span class="sr-only">: ${STATE_WORD[c.state]}</span></p>
              ${c.state === 'refused' ? html`<ul class="plg-problems">${c.problems.map(problem)}</ul>` : html`<p class="xs muted">${c.note}</p>`}
            </div>
          </li>`,
        )}
      </ol>
      ${rep.other.length ? html`<ul class="plg-problems" data-problems-other>${rep.other.map(problem)}</ul>` : nothing}
      ${r.warnings.map((w) => html`<p class="rs-warn small" data-warning>${icon('warn')}<span>${w.charAt(0).toUpperCase()}${w.slice(1)}${/[.!?]$/.test(w) ? '' : '.'}</span></p>`)}
      ${sha(r.sha256)}
      <p class="xs muted plg-facts" data-tree>
        <span>${r.files} ${r.files === 1 ? 'file' : 'files'}, ${bytes(r.bytes)}</span>
        ${r.source.type === 'wheel' && r.source.archive_sha256 ? html`<span>sha256 of the wheel <span class="num">${r.source.archive_sha256.slice(0, 16)}…</span></span>` : nothing}
        ${r.source.type === 'git' ? html`<span>commit <span class="num">${r.source.commit}</span></span>` : nothing}
      </p>
      <p class="xs faint">Compare the sha256 with the one the author published. TandemApp records it and holds the files to it each time the plugin is turned on or started.</p>
    </div>
  </li>`;
}

function asksStep(r: Inspection): TemplateResult | typeof nothing {
  const m = r.manifest;
  if (!m || !r.ok) return nothing;
  const kind = m.kind ?? 'plugin';
  const trust = m.trust ?? 'sandboxed';
  const tw = TRUST_WORDS[trust];
  const caps = capList(r.capabilities.requested, r.capabilities.confirm);
  const twice = caps.filter((c) => c.confirm).length;
  return html`<li class="hk-step" data-step="asks">
    <span class="hk-n num" aria-hidden="true">3</span>
    <div class="hk-step-b">
      <h3 class="hk-step-t">What it asks for</h3>
      ${kind === 'engine-stats'
        ? html`<p class="small" data-level="recorded">Nothing: it is a package an engine loads itself to send its figures. TandemApp records it and never runs it.</p>`
        : html`<div class="plg-level is-${tw.risk}" data-level=${trust}>
              <p class="plg-cap-h"><b>Where it runs: ${tw.label.toLowerCase()}</b>${chip(RISK_WORDS[tw.risk].tone, RISK_WORDS[tw.risk].label, { data: 'risk' })}${trust === 'sandboxed' ? nothing : chip('quiet', 'asked twice', { data: 'confirm' })}</p>
              <p class="small muted">${tw.text}</p>
            </div>
            ${caps.length
              ? html`<ul class="plg-caps" data-caps=${caps.length}>${caps.map((c) => capRow(c))}</ul>
                  <p class="small" data-asks-note>Installing grants none of ${caps.length === 1 ? 'it' : 'these'}. You grant each one afterwards, on the plugin’s page${twice ? `; ${twice === 1 ? 'one' : twice} of them ${twice === 1 ? 'is' : 'are'} asked a second time` : ''}. Without a capability the plugin still runs, and the plugin host refuses what needs it.</p>`
              : html`<p class="small" data-caps="0">No capability: it sees no request, reaches no other machine and reads no setting.</p>`}
            ${m.secrets?.length ? html`<p class="small" data-secrets>It keeps ${m.secrets.length === 1 ? 'a secret' : `${m.secrets.length} secrets`} you store for it: ${m.secrets.map((s, i) => html`${i ? ', ' : ''}<code class="num">${s.name}</code>${s.description ? ` (${s.description})` : ''}`)}.</p>` : nothing}
            <p class="xs faint">${NEVER}</p>`}
    </div>
  </li>`;
}

function addsStep(r: Inspection): TemplateResult | typeof nothing {
  const m = r.manifest;
  if (!m || !r.ok) return nothing;
  const fields = r.settings_form ? flatFields(r.settings_form).filter((f) => f.control !== 'group') : [];
  return html`<li class="hk-step" data-step="adds">
    <span class="hk-n num" aria-hidden="true">4</span>
    <div class="hk-step-b">
      <h3 class="hk-step-t">What it adds</h3>
      ${m.description ? html`<p class="small" data-description>${m.description}</p>` : nothing}
      ${m.kind === 'engine-stats'
        ? html`<p class="small muted">Figures from inside ${m.engine_stats?.engine ?? 'an engine'}. It goes into the engine with <code>${m.engine_stats?.install ?? 'pip install'}</code>, not through this page.</p>`
        : contribList(contributions(m), { links: false })}
      ${r.settings_form?.fields.length ? html`<p class="small" data-settings-count>${r.settings_form.fields.length === 1 ? 'One setting' : `${r.settings_form.fields.length} settings`} of its own, drawn as a form on its page: ${r.settings_form.fields.map((f) => f.label).join(', ')}${fields.length > r.settings_form.fields.length ? ` (${fields.length} fields)` : ''}.</p>` : nothing}
      <p class="xs muted plg-facts" data-author>
        ${m.author ? html`<span>By ${m.author}</span>` : nothing}${m.license ? html`<span>${m.license}</span>` : nothing}${m.tandemapp ? html`<span>for TandemApp <span class="num">${m.tandemapp}</span></span>` : nothing}
        ${m.entry?.command?.length ? html`<span>starts as <code class="num">${m.entry.command.join(' ')}</code></span>` : nothing}
      </p>
      <details class="st-doc" data-manifest><summary class="small">The manifest, as it is</summary><pre class="hk-pre"><code>${JSON.stringify(m, null, 2)}</code></pre></details>
    </div>
  </li>`;
}

function installStep(v: InstallView, on: InstallHandlers): TemplateResult {
  const s = v.state;
  const r = s.report!;
  const move = versionMove(r);
  const name = r.manifest?.name ? `${r.manifest.title || r.manifest.name} ${r.manifest.version ?? ''}`.trim() : 'it';
  const busy = s.step === 'installing';
  if (!r.ok)
    return html`<li class="hk-step" data-step="install" data-installable="false">
      <span class="hk-n num" aria-hidden="true">3</span>
      <div class="hk-step-b">
        <h3 class="hk-step-t">Nothing was installed</h3>
        <p class="small">Change what the checks name in the plugin, then check it again. To see the same refusals while you work: <code>python tools/plugin_check.py &lt;folder&gt;</code></p>
        <div class="st-acts"><button type="button" class="btn btn-primary" data-act="inspect-again" @click=${on.again}>${icon('refresh')} Check it again</button><a class="btn btn-ghost" href=${pluginsHref()} data-act="install-leave">Back to the plugins</a></div>
      </div>
    </li>`;
  return html`<li class="hk-step" data-step="install" data-installable=${v.blocked === null}>
    <span class="hk-n num" aria-hidden="true">5</span>
    <div class="hk-step-b">
      <h3 class="hk-step-t">${move === 'upgrade' ? 'Upgrade' : move === 'downgrade' ? 'Go back to an older version' : 'Install'}</h3>
      ${move === 'upgrade'
        ? html`<p class="small" data-move="upgrade">It replaces ${v.replaces ? v.replaces.title || v.replaces.name : 'the installed plugin'} <span class="num">${r.installed!.version}</span> with <span class="num">${r.manifest!.version}</span>. Settings that still fit, secrets it still declares and grants it still asks for are kept; the rest is dropped and listed. A capability that is new starts ungranted.</p>`
        : move === 'downgrade'
          ? html`<p class="small" data-move="downgrade">${icon('warn')} <span class="num">${r.manifest!.version}</span> is older than the installed <span class="num">${r.installed!.version}</span>. Settings and grants the older version does not know are dropped.</p>
              <label class="hk-check" data-downgrade><input type="checkbox" .checked=${s.downgrade} ?disabled=${busy} @change=${(e: Event) => on.downgrade((e.target as HTMLInputElement).checked)} /><span>Go back to ${r.manifest!.version}</span></label>`
          : move === 'same'
            ? html`<p class="small muted" data-move="same">${v.blocked}</p>`
            : html`<p class="small" data-move="new">It is installed turned off. Its code does not run until you turn it on, and nothing it asks for is granted by installing it.</p>`}
      ${s.refused ? html`${refused(s.refused)}${s.refused.facts.errors?.length ? html`<ul class="plg-problems">${s.refused.facts.errors.map((x) => problem(problemOf(x)))}</ul>` : nothing}` : nothing}
      ${v.signIn}
      <div class="st-acts">
        <a class="btn btn-ghost" href=${pluginsHref()} data-act="install-leave">Not now</a>
        <button type="button" class="btn btn-primary" data-act="install-go" ?disabled=${busy || v.blocked !== null} title=${v.blocked ?? nothing} @click=${on.install}>${busy ? 'Installing…' : move === 'upgrade' ? `Upgrade to ${r.manifest!.version}` : move === 'downgrade' ? `Install ${r.manifest!.version}` : `Install ${name}`}</button>
      </div>
    </div>
  </li>`;
}

function doneView(v: InstallView, on: InstallHandlers): TemplateResult {
  const d = v.state.done!;
  const w = installedWords(d);
  const p = d.plugin;
  const asks = p.capabilities.requested.length - p.capabilities.granted.length;
  return html`<section class="panel hk-created" data-installed=${p.id} data-action=${d.action}>
    <h2 class="panel-title">${icon('check')} ${w.title}</h2>
    <p class="small">${w.text}</p>
    ${w.kept.length ? html`<p class="xs muted st-wrap" data-kept>Kept: <span class="num">${w.kept.join(', ')}</span></p>` : nothing}
    ${w.dropped.length ? html`<p class="small st-wrap" data-dropped>${icon('warn')} Dropped: <span class="num">${w.dropped.join(', ')}</span></p>` : nothing}
    ${d.warnings.map((x) => html`<p class="rs-warn small" data-warning>${icon('warn')}<span>${x}</span></p>`)}
    <div class="st-acts">
      <button type="button" class="btn btn-primary" data-act="install-done" @click=${on.done}>${p.kind !== 'plugin' ? 'Show it' : p.enabled ? 'Open its page' : asks > 0 ? 'Set it up' : 'Open its page and turn it on'}</button>
      <a class="btn btn-ghost" href=${pluginsHref()} data-act="install-list">Back to the plugins</a>
    </div>
  </section>`;
}

/** The install page for the flow's state. */
export function installView(v: InstallView, on: InstallHandlers): TemplateResult {
  const s = v.state;
  const up = s.upgrade ? (v.replaces?.title || v.replaces?.name || s.upgrade) : null;
  const head = html`${back(up ? pluginsHref(['p', s.upgrade!]) : pluginsHref(), up ?? 'Plugins')}
    <header class="hk-head" data-install-page=${s.step}>
      <h2 class="hk-title">${up ? `Upgrade ${up}` : 'Install a plugin'}</h2>
      <p class="small muted">${up ? 'Point at the new version. It is checked first, and you see what changes before it replaces the one installed.' : 'A plugin is someone else’s code on your machines. TandemApp checks it first and shows what it asks for; you decide after that.'}</p>
    </header>`;
  if (s.step === 'done' && s.done) return html`${head}${doneView(v, on)}`;
  const r = s.report;
  return html`${head}
    <ol class="hk-steps plg-install" data-install-steps>
      ${sourceStep(v, on)}
      ${r ? html`${checksStep(r)}${asksStep(r)}${addsStep(r)}${installStep(v, on)}` : nothing}
    </ol>`;
}
