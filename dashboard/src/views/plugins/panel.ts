// A plugin's panel (VIS-284): the plugin's own page in a sandboxed frame. The frame's sandbox
// lets it run scripts and forms and nothing else (never the page's origin), and the controller
// serves it with a policy that gives it an origin of its own, so it reads no cookie, storage or
// route of TandemApp's. It talks to this page with postMessage; the element (area.ts) relays each
// call to the controller's bridge route, only the methods this plugin was given, and posts the
// answer back. A panel that cannot be shown is a finding with what to do, never an empty frame.
// Under the frame: what the bridge offers this panel, and what it asked for. Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { PanelState } from '../../lib/plugins-flow';
import { at, pluginsHref } from '../../lib/plugins';
import { icon } from '../../ui/bits';
import { back, chip } from '../hooks/parts';

export interface PanelHandlers {
  reload(): void;
}

/** the frame's sandbox: what the controller names, cut down to the two words a panel may have */
export function sandboxOf(asked: string): string {
  const ok = new Set(['allow-scripts', 'allow-forms']);
  return asked.split(/\s+/).filter((t) => ok.has(t)).join(' ');
}

const METHOD: Record<string, string> = {
  'plugin.info': 'read its own id, version and grants',
  'plugin.status': 'read its own state',
  'tools.call': 'call its own tools that only read',
  'settings.read': 'read its own settings',
  'engine.control': 'control the engine',
};

/** `base`: where the controller answers (the page's own origin when empty). */
export function panelView(s: PanelState, base: string, on: PanelHandlers): TemplateResult {
  const name = s.plugin ? s.plugin.title || s.plugin.name : s.id;
  const title = s.panel?.title ?? s.plugin?.manifest.contributes?.panels?.find((p) => p.name === s.name)?.title ?? s.name;
  const refusedCalls = s.recent.filter((r) => !r.ok);
  return html`${back(pluginsHref(['p', s.id]), name)}
    <header class="hk-head plg-panel-head" data-panel-page=${s.name} data-panel-step=${s.step}>
      <h2 class="hk-title">${title}</h2>
      <p class="small muted hk-head-meta"><span>A panel of ${name}</span>${chip('quiet', 'sandboxed frame', { title: 'It runs scripts in an origin of its own: no cookie, no storage and no route of TandemApp’s.' })}</p>
    </header>
    ${s.step === 'reading' ? html`<div class="skeleton plg-frame-wait" aria-busy="true" data-panel-reading></div>` : nothing}
    ${s.step === 'failed' && s.finding
      ? html`<section class="panel" data-panel-finding>
          <div class="hk-explain is-err" role="alert">
            <p class="hk-explain-t">${s.finding.title}</p>
            <p class="small">${s.finding.fix}</p>
            ${s.finding.detail ? html`<p class="xs muted st-wrap" data-verbatim>${s.finding.detail}</p>` : nothing}
          </div>
          <div class="st-acts"><button type="button" class="btn btn-primary" data-act="panel-reload" @click=${on.reload}>${icon('refresh')} Load it again</button><a class="btn btn-ghost" href=${pluginsHref(['p', s.id])} data-act="panel-plugin">Open the plugin’s page</a></div>
        </section>`
      : nothing}
    ${s.step === 'ready' && s.panel
      ? html`<div class="plg-frame-w" data-panel-ready>
            <iframe class="plg-frame" data-panel-frame=${s.panel.name} title=${`${title}, a panel of ${name}`} sandbox=${sandboxOf(s.panel.sandbox)} referrerpolicy="no-referrer" src=${`${base}${s.panel.url}${s.load > 1 ? `?load=${s.load}` : ''}`}></iframe>
          </div>
          <div class="plg-bridge" data-bridge data-calls=${s.calls}>
            <p class="small"><b>What this panel may ask TandemApp:</b> ${s.panel.bridge.methods.length ? s.panel.bridge.methods.map((m, i) => html`${i ? ', ' : ''}${METHOD[m] ?? m} <code class="num xs">${m}</code>`) : 'nothing'}. Anything else is refused.</p>
            <p class="xs muted" data-bridge-count>${s.calls ? `${s.calls} ${s.calls === 1 ? 'call' : 'calls'} so far${refusedCalls.length ? `, ${refusedCalls.length} of the newest refused` : ''}.` : 'It has not asked for anything yet.'}</p>
            ${s.recent.length
              ? html`<details class="st-doc" data-bridge-recent><summary class="small">The newest calls</summary>
                  <ol class="plg-journal">${s.recent.map((r) => html`<li data-call-ok=${r.ok}><span class="small muted">${at(r.at)}</span><code class="num xs">${r.method}</code>${r.ok ? chip('ok', 'answered') : html`<span class="small">${chip('err', 'refused')} ${r.why}</span>`}</li>`)}</ol>
                </details>`
              : nothing}
            <div class="st-acts"><button type="button" class="btn btn-sm btn-ghost" data-act="panel-reload" @click=${on.reload}>${icon('refresh')} Load it again</button></div>
          </div>`
      : nothing}`;
}
