// What the settings tests share (tests for VIS-280, VIS-281): the mock controller's settings
// interface on a clock the test moves, reached through the page's own client (api/settings.ts) so
// no HTTP is needed; every answer is checked against its schema in contracts/settings-v1 as it
// passes, and collected so a test can say that no secret is in any of them.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { MockSettings, type PartSwitches } from '../mock/settings.ts';
import type { Send, Sent } from '../src/api/engines.ts';
import { settingsApi, type SettingsApi } from '../src/api/settings.ts';

const DIR = join(__dirname, '..', '..', 'contracts', 'settings-v1');
const ajv = new Ajv2020({ allErrors: true, strict: false, allowUnionTypes: true });
const schema: Record<string, ReturnType<typeof ajv.compile>> = {};
// the files name each other by file name: each is added under its own, without its `$id`
for (const f of readdirSync(DIR)) {
  if (!f.endsWith('.schema.json')) continue;
  const doc = JSON.parse(readFileSync(join(DIR, f), 'utf8'));
  delete doc.$id;
  ajv.addSchema(doc, f);
}
for (const f of readdirSync(DIR)) if (f.endsWith('.schema.json')) schema[f.replace('.schema.json', '')] = ajv.getSchema(f)!;

/** every error type of the contract with its HTTP status */
export const ERRORS: Record<string, number> = JSON.parse(readFileSync(join(DIR, 'scopes.json'), 'utf8')).errors;
export const ROUTES: { method: string; path: string }[] = JSON.parse(readFileSync(join(DIR, 'scopes.json'), 'utf8')).routes;

export function valid(name: string, data: unknown): void {
  if (!schema[name]) throw new Error(`no schema ${name}`);
  if (!schema[name](data)) throw new Error(`${name}: ${ajv.errorsText(schema[name].errors, { separator: '\n' })}\n${JSON.stringify(data).slice(0, 600)}`);
}

/** the schema of a 200 answer, by its route */
export function schemaOf(method: string, path: string, body: unknown): string {
  const p = path.replace(/^\/v1\/settings/, '').split('?')[0];
  if (p === '') return 'overview';
  if (p === '/audit') return 'audit';
  if (p === '/restarts') return 'restarts';
  if (p.startsWith('/restarts/')) return 'restart';
  if (p.endsWith('/schema')) return 'schema';
  if (p.endsWith('/versions')) return 'versions';
  if (p.endsWith('/plan')) return 'plan';
  if (p.endsWith('/apply')) return 'result';
  // a restore without its confirm answers the plan, with it the result
  if (p.endsWith('/restore')) return 'state' in (body as object) ? 'result' : 'plan';
  return method === 'GET' ? 'values' : 'error';
}

export const T0 = Date.parse('2026-11-16T09:00:00Z');
/** the two secrets a test types: neither may come back in any answer */
export const SECRETS = ['sk-test-0123456789abcdef', 'hook-token-fedcba9876543210'];

export interface Rig {
  mock: MockSettings;
  parts: PartSwitches;
  api: SettingsApi;
  /** one raw call, as the page's client sends it */
  send: Send;
  /** every answer so far, as text */
  seen: string[];
  now(): number;
  /** the clock moves on */
  wait(ms: number): void;
}

/** A controller with the mock's settings; `seeded` false is the first run, with no engine host. */
export function rig(o: { seeded?: boolean; engines?: () => { id: string; alias: string }[] } = {}): Rig {
  const parts: PartSwitches = { on: { chat: false, conductor: false }, switched: { chat: false, conductor: false } };
  const mock = new MockSettings({ parts, seeded: o.seeded ?? true, engines: o.engines, now: T0 });
  let now = T0;
  const seen: string[] = [];
  const send: Send = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    const r = mock.handle(method, p, new URLSearchParams(qs), body ?? null, now);
    seen.push(JSON.stringify(r.body));
    if (r.status === 200) valid(schemaOf(method, path, r.body), r.body);
    else {
      valid('error', r.body);
      const type = (r.body as { error: { type: string } }).error.type;
      if (ERRORS[type] !== r.status) throw new Error(`${type} answered ${r.status}, the contract says ${ERRORS[type]}`);
    }
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
  };
  return { mock, parts, api: settingsApi(send), send, seen, now: () => now, wait: (ms) => void (now += ms) };
}
