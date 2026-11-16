// What the plugins tests share (tests for VIS-284, the matrix of VIS-285): the mock controller's
// plugin registry behind its two faces (the plugins' API and the settings interface's plugins
// scope) on a clock the test moves, reached through the pages' own clients so no HTTP is needed.
// Every answer is checked against its schema in contracts/plugin-v1 or contracts/settings-v1 as it
// passes, every path is kept (so a test can say that nothing was installed), and every answer is
// kept as text (so a test can say that no secret is in any of them).
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { PLUGIN_ERRORS, type MockPlugins } from '../mock/plugins.ts';
import { MockSettings, type PartSwitches } from '../mock/settings.ts';
import type { Send, Sent } from '../src/api/engines.ts';
import { pluginsApi, type PluginSend, type PluginsApi } from '../src/api/plugins.ts';
import { settingsApi, type SettingsApi } from '../src/api/settings.ts';
import { PluginsFlow, type Probe } from '../src/lib/plugins-flow.ts';
import { Draft } from '../src/lib/settings-draft.ts';
import { ChangeSetFlow } from '../src/lib/settings-flow.ts';
import { CONTRACTS } from './engine-rig.ts';
import { schemaOf as settingsSchema, valid as settingsValid } from './settings-rig.ts';

const DIR = join(CONTRACTS, 'plugin-v1');
const ajv = new Ajv2020({ allErrors: true, strict: false, allowUnionTypes: true });
const schema: Record<string, ReturnType<typeof ajv.compile>> = {};
// the files name each other by file name: each is added under its own, without its `$id`, and
// compiled once all of them are there
const names = readdirSync(DIR).filter((f) => f.endsWith('.schema.json'));
for (const f of names) {
  const doc = JSON.parse(readFileSync(join(DIR, f), 'utf8'));
  delete doc.$id;
  ajv.addSchema(doc, f);
}
for (const f of names) schema[f.replace('.schema.json', '')] = ajv.getSchema(f)!;

/** the contract's own tables: every route, every error type with its status, every check code */
export const API: { routes: { method: string; path: string; schema: string }[]; errors: Record<string, number>; checks: Record<string, string> } = JSON.parse(readFileSync(join(DIR, 'api.json'), 'utf8'));

export function valid(name: string, data: unknown): void {
  if (!schema[name]) throw new Error(`no schema ${name}`);
  if (!schema[name](data)) throw new Error(`${name}: ${ajv.errorsText(schema[name].errors, { separator: '\n' })}\n${JSON.stringify(data).slice(0, 600)}`);
}

/** the schema of a 2xx answer, by its route (contracts/plugin-v1/api.json) */
export function schemaOf(method: string, path: string): string {
  const sub = path.split('?')[0].replace(/^\/v1\/fleet\/plugins/, '').split('/').filter(Boolean);
  const shape = `${method} /v1/fleet/plugins${sub.length ? (sub.length === 1 && (sub[0] === 'inspect' || (sub[0] === 'upload' && method === 'POST')) ? `/${sub[0]}` : `/{id}${sub[1] ? `/${sub[1]}` : ''}`) : ''}`;
  const route = API.routes.find((r) => `${r.method} ${r.path}` === shape);
  if (!route) throw new Error(`${shape}: not a route of plugin-v1`);
  return route.schema.replace('.schema.json', '');
}

export const T0 = Date.parse('2026-11-16T09:00:00Z');

/** the routes of plugin-v1 the mock does not answer: the trust routes (SEC-18), no page asks them yet */
export const NOT_MOCKED = ['GET /v1/fleet/plugins/{id}/trust', 'POST /v1/fleet/plugins/{id}/trust', 'GET /v1/fleet/plugin-trust', 'POST /v1/fleet/plugin-trust/keys', 'DELETE /v1/fleet/plugin-trust/keys/{key}', 'POST /v1/fleet/plugin-trust/policy'];

/** the bytes of a wheel as far as an upload looks: a zip's first four */
export const wheelBytes = (n = 64): Uint8Array => Uint8Array.from({ length: n }, (_, i) => [0x50, 0x4b, 3, 4][i] ?? i % 251);

export interface PluginsRig {
  mock: MockSettings;
  reg: MockPlugins;
  api: PluginsApi;
  settings: SettingsApi;
  send: PluginSend;
  probe: Probe;
  /** every path asked so far, with its method */
  paths: string[];
  /** every answer so far, as text */
  seen: string[];
  now(): number;
  wait(ms: number): void;
}

/** A controller whose registry holds hello-tool (`plugins: false`: nothing; `'demo'`: the demo set). */
export function rig(o: { plugins?: boolean | 'demo' } = {}): PluginsRig {
  const parts: PartSwitches = { on: { chat: false, conductor: false }, switched: { chat: false, conductor: false } };
  const mock = new MockSettings({ parts, plugins: o.plugins ?? true, now: T0 });
  let now = T0;
  const seen: string[] = [];
  const paths: string[] = [];
  const send: PluginSend = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    paths.push(`${method} ${path}`);
    if (p.startsWith('/v1/settings')) {
      const r = mock.handle(method, p, new URLSearchParams(qs), body ?? null, now);
      seen.push(JSON.stringify(r.body));
      settingsValid(r.status === 200 ? settingsSchema(method, path, r.body) : 'error', r.body);
      return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
    }
    if (!p.startsWith('/v1/fleet/plugins')) throw new Error(`the plugins pages asked ${method} ${path}: not a route of theirs`);
    const r = mock.plugins.handle(method, p.split('/').slice(3), new URLSearchParams(qs), body ?? null, now);
    seen.push(JSON.stringify(r.body));
    if (r.status >= 200 && r.status < 300) valid(schemaOf(method, path), r.body);
    else {
      valid('error', r.body);
      const type = (r.body as { error: { type: string } }).error.type;
      if (PLUGIN_ERRORS[type] !== r.status) throw new Error(`${type} answered ${r.status}, the API says ${PLUGIN_ERRORS[type]}`);
    }
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
  };
  // what a panel's address answers, as the mock server serves it
  const probe: Probe = async (url) => {
    const [id, name] = url.replace('/plugin-panels/', '').split('/');
    return mock.plugins.panelFile(id, name, now) === null ? { status: 404, type: 'application/json' } : { status: 200, type: 'text/html; charset=utf-8' };
  };
  return { mock, reg: mock.plugins, api: pluginsApi(send), settings: settingsApi(send as Send), send, probe, paths, seen, now: () => now, wait: (ms) => void (now += ms) };
}

export interface Pages {
  r: PluginsRig;
  draft: Draft;
  flow: PluginsFlow;
  /** the settings area's own review: plan, confirm, apply */
  sheet: ChangeSetFlow;
}

/** The plugins pages as the element holds them: the flow on the settings area's draft, and the
 *  area's change-set flow that applies what it stages. */
export function pages(r: PluginsRig = rig()): Pages {
  const d = new Draft();
  return { r, draft: d, flow: new PluginsFlow(r.api, r.settings, d, () => undefined, r.now, r.probe), sheet: new ChangeSetFlow(r.settings, () => undefined, r.now) };
}

/** Review what is staged and apply it, the second confirm included; then the page reads again. */
export async function applyStaged(p: Pages): Promise<void> {
  await p.sheet.review(p.draft.changes());
  await p.sheet.apply();
  if (p.sheet.state.second) await p.sheet.apply();
  if (p.sheet.state.result?.state === 'applied') {
    p.draft.clear();
    p.flow.synced();
  }
  if (p.flow.one) await p.flow.read();
}

/** Install from a folder, as the page does it: check first, then install. */
export async function install(p: Pages, path: string, type: 'folder' | 'wheel' = 'folder'): Promise<string> {
  p.flow.startInstall();
  p.flow.editSource({ type, path });
  if (!(await p.flow.inspect())) throw new Error(`the check of ${path} did not answer a report: ${p.flow.install?.refused?.message}`);
  const done = await p.flow.commit();
  if (!done) throw new Error(`${path} was not installed: ${p.flow.install?.refused?.message ?? p.flow.installBlocked()}`);
  p.flow.closeInstall();
  return done.plugin.id;
}
