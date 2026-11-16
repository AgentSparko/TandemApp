// A request's trace (VIS-282): one request across every stop it made, read out of the hooks'
// journals by its id. The stops in the path's order, at each the hooks that were called in the
// order they ran, with how each call ended, how long it took and, for a shadow call, what it would
// have changed as field names and sizes. The requests the journals still hold are listed to pick
// from, since nobody knows an id by heart. A replay is not something this TandemApp can do: the
// page says so, says that no body was kept, and offers the playground. It runs nothing.
// Templates only.

import { html, nothing, type TemplateResult } from 'lit';
import type { TraceState } from '../../lib/hooks-flow';
import { PAYLOAD_WORDS, at, ms, replayNote, stageTitle, type Trace } from '../../lib/hooks';
import { icon } from '../../ui/bits';
import { chip, hooksHref, refused, roleMark } from './bits';
import { callRows } from './hook';

export interface TraceHandlers {
  show(request: string): void;
  reload(): void;
  replay(on: boolean): void;
}

function replay(t: Trace, open: boolean, on: TraceHandlers): TemplateResult {
  const note = replayNote(t);
  return html`<section class="panel hk-replay" data-panel="replay" data-replay-available="false">
    <div class="hk-danger-row">
      <div><h2 class="panel-title">Replay</h2><p class="panel-sub">Run this request again through the hooks as they are set now, without sending it to an engine.</p></div>
      ${open ? nothing : html`<button type="button" class="btn" data-act="replay-start" aria-expanded="false" @click=${() => on.replay(true)}>Replay this request…</button>`}
    </div>
    ${open
      ? html`<div class="hk-replay-note" role="status" data-replay-note>
          <p class="st-refusal-t">${icon('info')} ${note.title}</p>
          <ul class="small">${note.lines.map((l) => html`<li>${l}</li>`)}</ul>
          ${note.playground.length
            ? html`<div class="hk-replay-try"><p class="small">Try a hook of this request in the playground:</p>
                <div class="st-acts">${note.playground.map((p) => html`<a class="btn btn-sm" href=${hooksHref(['hook', p.hook], { test: p.stage })} data-act="replay-playground" data-hook=${p.hook} data-stage=${p.stage}>${p.name} at ${stageTitle(p.stage)}</a>`)}</div></div>`
            : html`<p class="small muted">The hooks of this request are gone, so there is none to try.</p>`}
          <div class="st-acts"><button type="button" class="btn btn-ghost btn-sm" data-act="replay-close" @click=${() => on.replay(false)}>Close</button></div>
        </div>`
      : nothing}
  </section>`;
}

function traced(t: Trace, s: TraceState, on: TraceHandlers): TemplateResult {
  if (!t.calls)
    return html`<section class="panel st-empty" data-trace-none>
      <h2 class="panel-title">No hook call is kept for this request</h2>
      <p class="small"><span class="num st-wrap">${t.request}</span></p>
      <p class="small muted">Either no hook was in its scope, or it is too long ago: TandemApp keeps the last 500 calls of each hook in memory, and a restart of TandemApp empties them.</p>
    </section>`;
  return html`<section class="panel" data-trace=${t.request}>
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">Request <span class="num st-wrap">${t.request}</span></h2>
        <p class="panel-sub" data-trace-sum>${t.started ? `${at(t.started)}. ` : ''}${t.calls} ${t.calls === 1 ? 'call' : 'calls'} of ${t.hooks.length} ${t.hooks.length === 1 ? 'hook' : 'hooks'} at ${t.stages.length} ${t.stages.length === 1 ? 'stop' : 'stops'}. The hooks on its path took ${ms(t.path_ms)}.</p></div></header>
      ${t.refused ? html`<p class="rs-warn small" data-trace-refused>${icon('warn')}<span>${t.refused.name} refused this request at ${stageTitle(t.refused.stage)}${t.refused.reason ? `: ${t.refused.reason}` : ''}. The client got 403 hook_denied.</span></p>` : nothing}
      <ol class="hk-rail hk-trace">
        ${t.stages.map(
          (st) => html`<li class="hk-stop" data-trace-stage=${st.stage}>
            <span class="hk-n num" aria-hidden="true">${st.n}</span>
            <div class="hk-stop-b">
              <div class="hk-stop-h"><h3 class="hk-stop-t">${st.title}</h3><span class="num xs hk-stage-id">${st.stage}</span></div>
              <ol class="hk-tcalls">
                ${st.calls.map(
                  (c) => html`<li class="hk-tcall" data-trace-hook=${c.hook}>
                    <div class="hk-tcall-h">
                      <a class="hk-name" href=${hooksHref(['hook', c.hook])}>${c.name}</a>
                      ${c.role ? roleMark(c.role) : chip('quiet', 'removed since')}
                      ${c.payload ? html`<span class="xs muted" title=${PAYLOAD_WORDS[c.payload].sees}>saw: ${PAYLOAD_WORDS[c.payload].label.toLowerCase()}</span>` : nothing}
                    </div>
                    ${callRows([c], { stage: false, trace: false })}
                  </li>`,
                )}
              </ol>
            </div>
          </li>`,
        )}
      </ol>
    </section>
    ${replay(t, s.replay, on)}`;
}

/** The trace page: pick a request, or read the one that is open. */
export function traceView(s: TraceState, on: TraceHandlers): TemplateResult {
  return html`<section class="panel" data-panel="trace-pick">
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">One request, stop by stop</h2><p class="panel-sub">Every call the hooks made for a request, from their journals. The id is the one in the ledger and in the client’s answer.</p></div>
        <div class="panel-tools"><button type="button" class="btn btn-sm btn-ghost" data-act="trace-reload" ?disabled=${s.loading} @click=${on.reload}>${icon('refresh')} Read again</button></div></header>
      <form class="hk-trace-f" @submit=${(e: Event) => (e.preventDefault(), on.show(((e.currentTarget as HTMLFormElement).elements.namedItem('request') as HTMLInputElement).value.trim()))}>
        <label class="field hk-trace-in"><span>Request id</span><input name="request" class="num" data-in="trace-request" placeholder="chatcmpl-px-…" spellcheck="false" autocapitalize="off" autocomplete="off" .value=${s.request} /></label>
        <button type="submit" class="btn btn-primary" data-act="trace-show">Show its trace</button>
      </form>
      ${s.refused ? refused(s.refused) : nothing}
    </section>
    ${s.trace ? traced(s.trace, s, on) : nothing}
    <section class="panel" data-panel="trace-recent">
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">The requests the journals still hold</h2><p class="panel-sub">Newest first. Pick one to see its trace.</p></div></header>
      ${s.loading && !s.recent ? html`<div class="skeleton" aria-busy="true"><div class="skeleton-line" style="width:76%"></div><div class="skeleton-line" style="width:58%"></div></div>` : nothing}
      ${s.recent
        ? s.recent.length
          ? html`<ol class="hk-recent" data-recent=${s.recent.length}>
                ${s.recent.map(
                  (r) => html`<li><a class="hk-recent-r ${r.request === s.request ? 'is-on' : ''}" href=${hooksHref(['trace'], { request: r.request })} aria-current=${r.request === s.request ? 'true' : nothing} data-recent-request=${r.request}>
                    <span class="num">${r.request.slice(0, 12)}…${r.request.slice(-8)}</span>
                    <span class="small muted">${at(r.ts)}</span>
                    <span class="small muted">${r.calls} ${r.calls === 1 ? 'call' : 'calls'}, ${r.hooks} ${r.hooks === 1 ? 'hook' : 'hooks'}</span>
                    ${r.refused ? chip('warn', 'refused') : r.failed ? chip('err', 'a hook failed') : html`<span></span>`}
                  </a></li>`,
                )}
              </ol>`
          : html`<p class="small muted" data-recent-none>The journals hold no request yet. They fill as requests pass a hook that is on or in shadow.</p>`
        : nothing}
    </section>`;
}
