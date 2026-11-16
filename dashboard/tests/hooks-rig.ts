// What the hooks tests share (tests for VIS-282, the matrix of VIS-283): the mock controller's hook
// registry behind its three faces (the hooks' API, the decision providers' API and the settings
// interface's hooks scope) on a clock the test moves, reached through the pages' own clients so no
// HTTP is needed. Every answer is checked against its schema in contracts/hooks-v1 or
// contracts/conductor-v1 as it passes, every path is kept (so a test can say that nothing went to
// an engine), and every answer is kept as text (so a test can say that no secret is in any of them
// but the two that make one).
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { HOOK_ERRORS, type MockHooks } from '../mock/hooks.ts';
import { MockSettings, type PartSwitches } from '../mock/settings.ts';
import type { Send, Sent } from '../src/api/engines.ts';
import { hooksApi, providersApi, type HookSend, type HooksApi, type ProvidersApi, type StageTable } from '../src/api/hooks.ts';
import { settingsApi, type SettingsApi } from '../src/api/settings.ts';
import { HooksFlow, ProvidersFlow } from '../src/lib/hooks-flow.ts';
import { Draft } from '../src/lib/settings-draft.ts';
import { ChangeSetFlow } from '../src/lib/settings-flow.ts';
import { CONTRACTS } from './engine-rig.ts';
import { schemaOf as settingsSchema, valid as settingsValid } from './settings-rig.ts';

const ajv = new Ajv2020({ allErrors: true, strict: false, allowUnionTypes: true });
const schema: Record<string, ReturnType<typeof ajv.compile>> = {};
const names: string[] = [];
// the files name each other by file name: each is added under its own, without its `$id`, and
// compiled once all of them are there
for (const dir of ['hooks-v1', 'conductor-v1']) {
  for (const f of readdirSync(join(CONTRACTS, dir))) {
    if (!f.endsWith('.schema.json') || (dir === 'conductor-v1' && !f.startsWith('decision-provider'))) continue;
    const doc = JSON.parse(readFileSync(join(CONTRACTS, dir, f), 'utf8'));
    delete doc.$id;
    ajv.addSchema(doc, f);
    names.push(f);
  }
}
for (const f of names) schema[f.replace('.schema.json', '')] = ajv.getSchema(f)!;
// the providers' refusals: conductor-v1's error schema, which names `code` and `field` (SRV-851)
schema['conductor-error'] = ajv.compile(JSON.parse(readFileSync(join(CONTRACTS, 'conductor-v1', 'error.schema.json'), 'utf8')));

export const STAGE_TABLE: StageTable = JSON.parse(readFileSync(join(CONTRACTS, 'hooks-v1', 'stages.json'), 'utf8'));
export const EXAMPLES = join(CONTRACTS, 'hooks-v1', 'examples');

export function valid(name: string, data: unknown): void {
  if (!schema[name]) throw new Error(`no schema ${name}`);
  if (!schema[name](data)) throw new Error(`${name}: ${ajv.errorsText(schema[name].errors, { separator: '\n' })}\n${JSON.stringify(data).slice(0, 600)}`);
}

/** the schema of a 2xx answer, by its route; null where the contract has none (the stage table, a delete) */
export function schemaOf(method: string, path: string): string | null {
  const [p] = path.split('?');
  const hook = p.startsWith('/v1/fleet/hooks');
  const sub = p.replace(/^\/v1\/fleet\/(hooks|decision-providers)/, '').split('/').filter(Boolean);
  if (method === 'DELETE') return null;
  if (hook) {
    if (!sub.length) return method === 'GET' ? 'hooks' : 'hook-created';
    if (sub[0] === 'stages') return null;
    if (sub[0] === 'calls') return 'hook-trace';
    if (sub[1] === 'test') return 'hook-test';
    if (sub[1] === 'calls') return 'hook-calls';
    if (sub[1] === 'secret') return 'hook-created';
    return 'hook-one';
  }
  if (!sub.length) return method === 'GET' ? 'decision-providers' : 'decision-provider';
  if (sub[0] === 'report') return 'decision-provider-report';
  if (sub[1] === 'test') return 'decision-provider-test';
  if (sub[1] === 'health') return 'decision-provider-health';
  if (sub[1] === 'calls') return 'decision-provider-calls';
  return 'decision-provider';
}

export const T0 = Date.parse('2026-11-16T09:00:00Z');

export interface HooksRig {
  mock: MockSettings;
  reg: MockHooks;
  hooks: HooksApi;
  providers: ProvidersApi;
  settings: SettingsApi;
  send: HookSend;
  /** every path asked so far, with its method */
  paths: string[];
  /** every answer so far, as text */
  seen: string[];
  now(): number;
  wait(ms: number): void;
}

/** A controller whose registry holds the three sample hooks and the two sample providers
 *  (`hooks: false`: nothing; `'demo'`: the demo set with its traffic). */
export function rig(o: { hooks?: boolean | 'demo' } = {}): HooksRig {
  const parts: PartSwitches = { on: { chat: false, conductor: false }, switched: { chat: false, conductor: false } };
  const mock = new MockSettings({ parts, hooks: o.hooks ?? true, now: T0 });
  let now = T0;
  const seen: string[] = [];
  const paths: string[] = [];
  const send: HookSend = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    paths.push(`${method} ${path}`);
    if (p.startsWith('/v1/settings')) {
      const r = mock.handle(method, p, new URLSearchParams(qs), body ?? null, now);
      seen.push(JSON.stringify(r.body));
      settingsValid(r.status === 200 ? settingsSchema(method, path, r.body) : 'error', r.body);
      return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
    }
    if (!p.startsWith('/v1/fleet/hooks') && !p.startsWith('/v1/fleet/decision-providers')) throw new Error(`the hooks pages asked ${method} ${path}: not a route of theirs`);
    const r = mock.registry.handle(method, p.split('/').slice(3), new URLSearchParams(qs), body ?? null, now);
    seen.push(JSON.stringify(r.body));
    if (r.status >= 200 && r.status < 300) {
      const name = schemaOf(method, path);
      if (name) valid(name, r.body);
    } else {
      const e = (r.body as { error: { type: string; message: string } }).error;
      // the hooks' refusals have their schema, the providers' conductor-v1's (SRV-851)
      valid(p.startsWith('/v1/fleet/hooks') ? 'error' : 'conductor-error', r.body);
      if (HOOK_ERRORS[e.type] !== r.status) throw new Error(`${e.type} answered ${r.status}, the API says ${HOOK_ERRORS[e.type]}`);
    }
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
  };
  return { mock, reg: mock.registry, hooks: hooksApi(send), providers: providersApi(send), settings: settingsApi(send as Send), send, paths, seen, now: () => now, wait: (ms) => void (now += ms) };
}

export interface Pages {
  r: HooksRig;
  draft: Draft;
  flow: HooksFlow;
  prov: ProvidersFlow;
  /** the settings area's own review: plan, confirm, apply */
  sheet: ChangeSetFlow;
}

/** The hooks pages as the element holds them: the two flows on one draft, and the settings area's
 *  change-set flow that applies what they stage. */
export function pages(r: HooksRig = rig()): Pages {
  const d = new Draft();
  return { r, draft: d, flow: new HooksFlow(r.hooks, d, () => undefined, r.now), prov: new ProvidersFlow(r.providers, d), sheet: new ChangeSetFlow(r.settings, () => undefined, r.now) };
}

/** Review what is staged and apply it, the second confirm included; then the pages read again. */
export async function applyStaged(p: Pages): Promise<void> {
  await p.sheet.review(p.draft.changes());
  await p.sheet.apply();
  if (p.sheet.state.second) await p.sheet.apply();
  if (p.sheet.state.result?.state === 'applied') p.draft.clear();
  await p.flow.load();
  if (p.prov.info) await p.prov.load();
}
