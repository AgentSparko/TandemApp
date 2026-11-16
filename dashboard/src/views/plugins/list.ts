// The installed plugins (VIS-284): one row a plugin with its name and version, what kind it is,
// where it came from, its trust level, its signature, what it adds and its state as the plugin
// host tells it. A row whose files changed since the install says so. Above the rows: the
// versions this TandemApp reads, and Install. Below: how a plugin of your own starts. Templates
// only.

import { html, nothing, type TemplateResult } from 'lit';
import type { Plugin, PluginList, Run } from '../../api/plugins';
import { contribLine, integrityWords, listState, nameOf, pluginsHref, shortSha, signatureWords, sourceWords, trustLine } from '../../lib/plugins';
import { icon } from '../../ui/bits';
import { chip } from '../hooks/parts';
import { stateMark } from './bits';

export interface ListView {
  info: PluginList;
  statuses: Record<string, Run | null>;
  /** how many changes are staged for a plugin */
  staged: (id: string) => number;
  now: number;
}

function row(p: Plugin, v: ListView): TemplateResult {
  const src = sourceWords(p.source);
  const sig = signatureWords(p.signature);
  const trust = trustLine(p.trust);
  const state = listState(p, v.statuses[p.id], v.now);
  const n = v.staged(p.id);
  const waits = p.kind === 'plugin' ? p.capabilities.requested.length - p.capabilities.granted.length : 0;
  return html`<li class="plg-item ${n ? 'is-staged' : ''} ${p.enabled || p.kind !== 'plugin' ? '' : 'is-off'}" data-plugin=${p.id} data-kind=${p.kind} data-enabled=${p.enabled}>
    <a class="plg-item-a" href=${pluginsHref(['p', p.id])} data-act="plugin-open">
      <span class="plg-item-h">
        <b class="plg-item-n">${nameOf(p)}</b>
        <span class="num small" data-version>${p.version}</span>
        ${p.kind === 'engine-stats' ? chip('quiet', 'engine stats', { title: 'A package an engine loads to send its figures. TandemApp records it and does not run it.', data: 'kind' }) : chip('quiet', 'plugin', { data: 'kind' })}
        ${n ? html`<span class="st-count" data-staged-count>${n} staged</span>` : nothing}
      </span>
      ${p.description ? html`<span class="small muted plg-item-d">${p.description}</span>` : nothing}
      <span class="xs muted plg-item-m">
        <span data-source=${p.source.type}>${src.kind} <span class="num">${src.where}</span>${p.source.type === 'git' ? html` at <span class="num">${shortSha(src.pin)}</span>` : nothing}</span>
        ${p.kind === 'plugin' ? html`<span data-contributes>${contribLine(p.contributes)}</span>` : nothing}
      </span>
    </a>
    <span class="plg-item-s">
      ${stateMark(state)}
      ${p.kind === 'plugin' ? chip(trust.tone, trust.text, { data: 'trust' }) : nothing}
      ${chip(sig.tone, sig.label, { title: sig.text, data: 'signature' })}
      ${p.integrity === 'ok' ? nothing : chip('err', integrityWords(p).label, { title: integrityWords(p).text, data: 'integrity' })}
      ${waits > 0 ? chip('warn', `${waits} not granted`, { title: 'Capabilities it asks for that are not granted. It runs without them.', data: 'ungranted' }) : nothing}
    </span>
  </li>`;
}

/** how a plugin of your own starts: the three lines a first one takes */
function author(): TemplateResult {
  return html`<details class="st-keys" data-plugins-author>
    <summary class="small">Write your own</summary>
    <div class="plg-author small">
      <p>A plugin is a folder with a manifest, <code>plugin.json</code>, and a program that speaks MCP over stdio. It can add tools, hooks, decision providers, backends, panels and skills. The smallest one is <code>contracts/plugin-v1/examples/hello-tool</code>: a manifest and one file of Python.</p>
      <ol>
        <li>Copy the example and change its <code>name</code>, its <code>entry</code> and what it <code>contributes</code>.</li>
        <li>Check it the way an install does, without installing it: <code>python tools/plugin_check.py path/to/your-plugin</code></li>
        <li>Install it here from its folder. It starts turned off, with nothing granted.</li>
      </ol>
      <p class="muted">Everything a manifest takes, the capabilities and the protocol are in <code>docs/plugins.md</code>.</p>
    </div>
  </details>`;
}

/** `install`: the button to the install page (the empty list has its own) */
export function listHead(info: PluginList | null, install = true): TemplateResult {
  return html`<nav class="hk-nav plg-nav" aria-label="Plugins">
    <p class="small muted plg-reads" data-reads>${info ? html`This TandemApp is <span class="num">${info.tandemapp}</span> and reads plugin API <span class="num">${info.plugin_api}</span> and hooks API <span class="num">${info.hooks_api}</span>.` : nothing}</p>
    ${install ? html`<a class="btn btn-primary" href=${pluginsHref(['install'])} data-act="plugin-install">${icon('plus')} Install a plugin</a>` : nothing}
  </nav>`;
}

export function listView(v: ListView): TemplateResult {
  const ps = v.info.plugins;
  if (!ps.length)
    return html`${listHead(v.info, false)}
      <section class="panel st-empty" data-plugins-none>
        <h2 class="panel-title">No plugin yet</h2>
        <p class="small muted">A plugin adds to TandemApp without changing it: a tool for Chat, a hook on a request’s way, a decision provider, a backend, a panel of its own. Install one from a folder, a wheel or a git commit. TandemApp checks it first and shows what it asks for; nothing in it runs until you turn it on.</p>
        <div class="st-acts"><a class="btn btn-primary" href=${pluginsHref(['install'])} data-act="plugin-install-first">${icon('plus')} Install a plugin</a></div>
      </section>
      ${author()}`;
  return html`${listHead(v.info)}
    <ul class="plg-list" data-plugins=${ps.length}>${ps.map((p) => row(p, v))}</ul>
    ${author()}`;
}
