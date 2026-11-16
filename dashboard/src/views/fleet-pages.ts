// <qse-fleet-page>: TandemApp's new pages (VIS-242, concept §8), one chunk loaded when one of them
// opens. Overview, Hardware and Events & Alerts (VIS-248, views/monitor/) and Sparks with its
// topology and each Spark's page (VIS-250, views/fleet/) are built, and Add Spark (VIS-246,
// views/fleet/wizard.ts, a chunk of its own that loads when it opens), the setup wizard (VIS-278: Add
// engine, views/fleet/engine-wizard.ts, its own chunk too) and App (VIS-280, the settings area:
// views/settings/area.ts, a chunk of its own too, with OPS-234's controller host and its move in
// views/fleet/app-settings.ts), Health (SRV-835, views/health/area.ts, its own chunk) and Models (VIS-252: the
// deployments, Serve a model with its placement step, the weights matrix: views/fleet/models.ts,
// serve.ts, weights.ts); API still shows its head, what it will hold and the step that builds it.

import { html, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { Summary } from '../api/types';
import type { View } from '../lib/router';
import { LightElement, type Loadable } from '../ui/base';
import { icon } from '../ui/bits';
import '../styles/monitor.css';
import '../styles/sparks.css';
import './monitor/overview';
import './monitor/hardware';
import './monitor/events';
import './fleet/sparks';
import './fleet/node';
import './fleet/models';
import './fleet/serve';
import './fleet/weights';
import '../styles/models.css';

interface PageDef {
  eyebrow: string;
  title: string;
  text: string;
  step: number;
  /** today's page that already does part of it */
  link?: { href: string; label: string };
}

const PAGES: Partial<Record<View, PageDef>> = {
  api: {
    eyebrow: 'Fleet',
    title: 'Endpoint and keys',
    text: 'The one endpoint and its keys, the clients that use it, and the routes to deployments.',
    step: 6,
    link: { href: '#/api/backends', label: 'Backends' },
  },
};

/** The Add Spark wizard: its own chunk; the element upgrades in place once it has loaded. */
let wizardChunk: Promise<unknown> | null = null;
const loadWizard = () => (wizardChunk ??= import('./fleet/wizard'));
/** The setup wizard (VIS-278): Add engine, pairing, the model step, the first message; its own chunk */
let engineChunk: Promise<unknown> | null = null;
const loadEngineWizard = () => (engineChunk ??= import('./fleet/engine-wizard'));
/** Get started (SRV-749): the ways into a fleet and the connect-a-server form, its own chunk */
let startChunk: Promise<unknown> | null = null;
const loadStart = () => (startChunk ??= import('./fleet/start'));
/** The settings area (VIS-280): App with the controller's own page, a page a settings scope and
 *  History, with the run sheet and the restart card; its own chunk */
let appChunk: Promise<unknown> | null = null;
const loadApp = () => (appChunk ??= import('./settings/area'));
/** The health page (SRV-835): what is wrong, in words, and the fixes TandemApp runs itself; its own
 *  chunk, loaded when Monitor → Health opens */
let healthChunk: Promise<unknown> | null = null;
const loadHealth = () => (healthChunk ??= import('./health/area'));

@customElement('qse-fleet-page')
export class QseFleetPage extends LightElement {
  @property({ type: String }) view: View = 'overview';
  @property({ attribute: false }) path: string[] = [];
  /** the shell's summary: Overview's requests today */
  @property({ attribute: false }) summary: Loadable<Summary> | null = null;
  render() {
    if (this.view === 'overview') return html`<qse-overview .summary=${this.summary}></qse-overview>`;
    if (this.view === 'hardware') return html`<qse-hardware></qse-hardware>`;
    if (this.view === 'events') return html`<qse-events></qse-events>`;
    if (this.view === 'health') {
      void loadHealth();
      return html`<qse-health .path=${this.path}></qse-health>`;
    }
    if (this.view === 'add-engine') {
      void loadEngineWizard();
      return html`<qse-engine-wizard .path=${this.path}></qse-engine-wizard>`;
    }
    if (this.view === 'add-spark') {
      void loadWizard();
      return html`<qse-add-spark></qse-add-spark>`;
    }
    if (this.view === 'start') {
      void loadStart();
      return html`<qse-start></qse-start>`;
    }
    if (this.view === 'app') {
      void loadApp();
      return html`<qse-settings .path=${this.path}></qse-settings>`;
    }
    if (this.view === 'models') return this.path[0] === 'weights' ? html`<qse-weights></qse-weights>` : this.path[0] === 'place' ? html`<qse-serve></qse-serve>` : html`<qse-models></qse-models>`;
    if (this.view === 'sparks') {
      const p = this.path[0];
      return p && p !== 'topology' ? html`<qse-spark-node .nodeId=${p}></qse-spark-node>` : html`<qse-sparks .tab=${p === 'topology' ? 'topology' : 'list'}></qse-sparks>`;
    }
    const d = PAGES[this.view];
    if (!d) return nothing;
    return html`<header class="page-head" data-page=${this.view}>
        <div class="eyebrow">${d.eyebrow}</div>
        <h1>${d.title}</h1>
        <p class="page-sub">${d.text}</p>
      </header>
      <p class="page-soon">${icon('info')} <span>This page is built in step ${d.step} of TandemApp.${d.link ? html` Today: <a href=${d.link.href}>${d.link.label}</a>.` : nothing}</span></p>`;
  }
}
