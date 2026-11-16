// <qse-plugins>: the Plugins pages of the settings area (VIS-284), a chunk of its own that loads
// when Settings → Plugins opens. Below #/app/plugins:
//
//   (nothing)                  the installed plugins (list.ts)
//   install[?upgrade=<id>]     install one, or another version of one (install.ts)
//   p/<id>                     one plugin: state, what it adds, settings, secrets, grants, log (plugin.ts)
//   p/<id>/panel/<name>        one of its panels in a sandboxed frame (panel.ts)
//
// The state is one flow free of the DOM (lib/plugins-flow.ts); this element reads the route, binds
// the templates' handlers to it and keeps what is read fresh. Every switch, setting, secret and
// grant is staged into the settings area's own draft, so its bar, its review sheet and its
// versions serve these pages too. What is written on the plugins' API itself (a plugin installed,
// upgraded or removed) is told to the settings area, which reads its plugins scope again.
//
// The page's side of a panel's bridge is here: a message from the frame that shows (and from no
// other window) goes to the flow, which relays the call, and the answer is posted back to that
// frame. The frame has an origin of its own, so the answer is posted without naming one.

import { html, nothing, type PropertyValues, type TemplateResult } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { apiBase } from '../../api/client';
import { plugins as api, type PluginError } from '../../api/plugins';
import { settings as settingsApi, type Target } from '../../api/settings';
import { PluginsFlow, type Probe } from '../../lib/plugins-flow';
import { PLUGINS_SCOPE, pluginsHref } from '../../lib/plugins';
import { parseHash } from '../../lib/router';
import { draft } from '../../lib/settings-draft';
import { LightElement } from '../../ui/base';
import { icon } from '../../ui/bits';
import { signInCard } from '../monitor/bits';
import { checkSession } from '../monitor/data';
import { refused } from './bits';
import { installView } from './install';
import { listHead, listView } from './list';
import { panelView } from './panel';
import { pluginGone, pluginView } from './plugin';
import '../../styles/hooks.css';
import '../../styles/plugins.css';

/** how often a plugin's state is read again while its page shows, in seconds */
const FRESH_S = 5;

const probe: Probe = async (url) => {
  try {
    const r = await fetch(apiBase() + url, { credentials: 'same-origin', cache: 'no-store' });
    return { status: r.status, type: r.headers.get('content-type') ?? '' };
  } catch {
    return { status: 0, type: '' };
  }
};

@customElement('qse-plugins')
export class QsePlugins extends LightElement {
  /** the route below #/app/plugins */
  @property({ attribute: false }) path: string[] = [];
  /** the plugins scope's targets, as the settings interface lists them */
  @property({ attribute: false }) targets: Target[] = [];
  /** what the settings area last read of the open plugin's keys: when it changes, a change was applied */
  @property({ attribute: false }) stamp: unknown = null;

  private flow = new PluginsFlow(api, settingsApi, draft, () => this.requestUpdate(), () => Date.now(), probe);
  private asked = false;
  private at = '';
  private secs = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private unsub: (() => void) | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    this.unsub = draft.on(() => {
      this.flow.synced();
      this.requestUpdate();
    });
    window.addEventListener('qse-signed-in', this.onSignedIn);
    window.addEventListener('message', this.onMessage);
    this.timer = setInterval(() => this.tick(), 1000);
  }
  disconnectedCallback(): void {
    this.unsub?.();
    window.removeEventListener('qse-signed-in', this.onSignedIn);
    window.removeEventListener('message', this.onMessage);
    if (this.timer) clearInterval(this.timer);
    this.flow.closePanel();
    super.disconnectedCallback();
  }
  protected willUpdate(changed: PropertyValues): void {
    // the settings area hands down a new path with every render of its own, so the two are told apart
    if (changed.has('path')) this.enter();
    if (changed.has('stamp') && changed.get('stamp') != null && this.path[0] === 'p' && this.path.length === 2) void this.flow.read();
  }

  private onSignedIn = (): void => {
    this.asked = false;
    this.requestUpdate();
  };

  /** A panel's bridge call: only from the frame that shows; the answer goes back to that frame. */
  private onMessage = (e: MessageEvent): void => {
    const frame = this.querySelector<HTMLIFrameElement>('iframe[data-panel-frame]');
    const from = frame?.contentWindow;
    if (!from || e.source !== from) return;
    void this.flow.relay(e.data).then((reply) => {
      // the frame may have been loaded anew or left meanwhile: the answer goes to no other
      if (reply && this.querySelector<HTMLIFrameElement>('iframe[data-panel-frame]')?.contentWindow === from) from.postMessage(reply, '*');
    });
  };

  /** every few seconds, while the tab shows: the list's states, or the open plugin's */
  private tick(): void {
    if (document.visibilityState !== 'visible' || ++this.secs % FRESH_S) return;
    const p = this.path;
    if (!p.length) void this.flow.load();
    else if (p[0] === 'p' && p.length === 2) void this.flow.refresh();
  }

  /** The route changed: read what its page shows. */
  private enter(): void {
    const p = this.path;
    const q = parseHash().params;
    const key = `${p.join('/')}?${q.toString()}`;
    if (key === this.at) return;
    const was = this.at;
    this.at = key;
    if (was.includes('/panel/')) this.flow.closePanel();
    // an install that ended does not come back with the page
    if (was.startsWith('install') && p[0] !== 'install' && this.flow.install?.step === 'done') this.flow.closeInstall();
    if (p[0] === 'install') {
      const up = q.get('upgrade');
      void (this.flow.info ? Promise.resolve() : this.flow.load()).then(() => {
        const s = this.flow.install;
        if (s && s.step !== 'done' && s.upgrade === (up ?? null)) return;
        const from = up ? this.flow.info?.plugins.find((x) => x.id === up) : undefined;
        this.flow.startInstall(from ? { id: from.id, source: from.source } : null);
      });
    } else if (p[0] === 'p' && p[1] && p[2] === 'panel' && p[3]) void this.flow.openPanel(p[1], p[3]);
    else if (p[0] === 'p' && p[1]) void this.flow.open(p[1]);
    else void this.flow.load();
  }

  /** the settings area reads its plugins scope again: a plugin was installed, upgraded or removed */
  private told(): void {
    this.dispatchEvent(new CustomEvent('qse-plugins-changed', { bubbles: true }));
  }

  /** the sign-in, where a refusal asks for it */
  private signIn(e: PluginError | null | undefined, what: string): TemplateResult | typeof nothing {
    if (e?.type !== 'unauthorized') return nothing;
    if (!this.asked) {
      this.asked = true;
      void checkSession(true);
    }
    return signInCard(what);
  }

  private unreadable(e: PluginError | null, retry: () => void, list = false): TemplateResult {
    // the list's own route answers 404 only on a controller from before plugins
    if (list && e?.status === 404)
      return html`<section class="panel st-empty" data-plugins-unsupported><h2 class="panel-title">This controller does not run plugins yet</h2><p class="small muted">It answers none of the plugins’ API. Update TandemApp on the controller’s machine.</p></section>`;
    return e
      ? html`<section class="panel" data-plugins-unread>${refused(e)}${this.signIn(e, 'The plugins')}<div class="st-acts"><button type="button" class="btn btn-sm" data-act="reload" @click=${retry}>${icon('refresh')} Try again</button></div></section>`
      : html`<div class="skeleton" style="height:240px" aria-busy="true"></div>`;
  }

  private writable(id: string): boolean {
    // a plugin installed a moment ago is not in the list the area holds yet: its keys are its own
    return this.targets.find((t) => t.id === id)?.writable ?? true;
  }

  private listPage(): TemplateResult {
    const f = this.flow;
    if (!f.info) return html`${f.refused ? nothing : listHead(null)}${this.unreadable(f.refused, () => void f.load(), true)}`;
    return html`${f.refused ? refused(f.refused, { quiet: true }) : nothing}${listView({ info: f.info, statuses: f.statuses, staged: (id) => draft.count(PLUGINS_SCOPE, id), now: f.now })}`;
  }

  private installPage(): TemplateResult {
    const f = this.flow;
    const s = f.install;
    if (!s) return this.unreadable(f.refused, () => void f.load().then(() => f.startInstall()));
    const id = s.report?.manifest?.name ?? s.upgrade;
    return installView(
      { state: s, blocked: f.installBlocked(), replaces: f.info?.plugins.find((x) => x.id === id) ?? null, signIn: this.signIn(s.refused, 'Installing a plugin') },
      {
        edit: (p) => f.editSource(p),
        upload: (file) => void f.uploadWheel({ name: file.name, size: file.size, bytes: file }),
        inspect: () => void f.inspect(),
        again: () => void f.inspect(),
        downgrade: (on) => f.setDowngrade(on),
        install: () =>
          void f.commit().then((done) => {
            if (!done) return;
            this.told();
            void f.load();
          }),
        done: () => {
          const pid = s.done?.plugin.id;
          f.closeInstall();
          location.hash = pid ? pluginsHref(['p', pid]) : pluginsHref();
        },
      },
    );
  }

  private pluginPage(id: string): TemplateResult {
    const f = this.flow;
    const o = f.one;
    if (o?.id === id && o.missing) return pluginGone(id);
    if (!o || o.id !== id || !o.plugin) return this.unreadable(o?.refused ?? null, () => void f.open(id));
    const staged = (key: string): 'set' | 'unset' | null => draft.staged(PLUGINS_SCOPE, id, key)?.kind ?? null;
    return pluginView(
      {
        one: o,
        shown: { enabled: f.enabled, granted: (c) => f.granted(c), trust: f.trust, staged, value: (prop) => f.value(prop), textAt: (path, field) => f.textAt(path, field), errorAt: (path) => f.errorAt(path), isSet: (prop) => f.isSet(prop) },
        writable: this.writable(id),
        now: f.now,
        signIn: this.signIn(o.remove.refused ?? o.act.refused, 'Removing a plugin'),
      },
      {
        enable: (on) => f.setEnabled(on),
        setUp: () => f.setUp(),
        act: (what) => void f.act(what),
        grant: (c, on) => f.grant(c, on),
        trust: (t) => f.setTrust(t),
        undo: (key) => f.undo(key),
        type: (path, field, raw) => f.type(path, field, raw),
        tidy: (path) => f.tidy(path),
        put: (path, value) => f.put(path, value),
        reset: (prop) => f.reset(prop),
        secretField: (name, open) => {
          f.secretField(name, open);
          if (open) void this.updateComplete.then(() => this.querySelector<HTMLElement>(`[id="plg-s-${name}"]`)?.focus());
        },
        secretType: (name, raw) => f.secretType(name, raw),
        secretRemove: (name) => f.secretRemove(name),
        logs: (tail) => void f.readLogs(tail),
        follow: (on) => f.followLogs(on),
        askRemove: (on) => f.askRemove(on),
        remove: () =>
          void f.remove().then((ok) => {
            if (!ok) return;
            this.told();
            location.hash = pluginsHref();
          }),
      },
    );
  }

  private panelPage(id: string, name: string): TemplateResult {
    const s = this.flow.panel;
    if (!s || s.id !== id || s.name !== name) return html`<div class="skeleton" style="height:240px" aria-busy="true"></div>`;
    return panelView(s, apiBase(), { reload: () => void this.flow.openPanel(id, name) });
  }

  render() {
    const p = this.path;
    if (p[0] === 'install') return this.installPage();
    if (p[0] === 'p' && p[1] && p[2] === 'panel' && p[3]) return this.panelPage(p[1], p[3]);
    if (p[0] === 'p' && p[1]) return this.pluginPage(p[1]);
    return this.listPage();
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-plugins': QsePlugins;
  }
}
