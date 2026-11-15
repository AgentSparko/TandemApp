// What the Add engine tests share (tests for VIS-278): the mock controller on a clock the
// test moves, reached through the page's own client (api/engines.ts) so no HTTP is needed; every
// answer is checked against its schema in contracts/fleet-v1 as it passes; and a template's text
// without a DOM.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { nothing, type TemplateResult } from 'lit';
import { MockFleet, type FleetScenario } from '../mock/fleet.ts';
import { enginesApi, type EnginesApi, type Send, type Sent } from '../src/api/engines.ts';

export const CONTRACTS = join(__dirname, '..', '..', 'contracts');
const FDIR = join(CONTRACTS, 'fleet-v1');
// allowUnionTypes: one fleet schema says a field as a string or a list (engine-connect-key-body `from`)
const ajv = new Ajv2020({ allErrors: true, strict: true, allowUnionTypes: true });
const schema: Record<string, ReturnType<typeof ajv.compile>> = {};
// every schema first, then each one: a schema may $ref another (health-fix -> health-target)
for (const f of readdirSync(FDIR)) if (f.endsWith('.schema.json')) ajv.addSchema(JSON.parse(readFileSync(join(FDIR, f), 'utf8')), f);
for (const f of readdirSync(FDIR)) if (f.endsWith('.schema.json')) schema[f.replace('.schema.json', '')] = ajv.getSchema(f)!;

export function valid(name: string, data: unknown): void {
  if (!schema[name]) throw new Error(`no schema ${name}`);
  if (!schema[name](data)) throw new Error(`${name}: ${ajv.errorsText(schema[name].errors, { separator: '\n' })}\n${JSON.stringify(data).slice(0, 600)}`);
}

/** the schema of a 200 answer, by its route */
export function schemaOf(method: string, path: string): string {
  const p = path.replace(/^\/v1\/fleet\/engines/, '').split('?')[0];
  if (p === '') return 'engines';
  if (p === '/discover') return 'engine-discover';
  if (p === '/probe') return 'engine-probe';
  if (p === '/pair/start') return 'engine-pair-start';
  if (p === '/pair/abandon') return 'engine-pair-abandon';
  if (p === '/pair/code' || p === '/connect/password/confirm' || p === '/connect/key/check') return 'engine-paired';
  if (p === '/connect/password/plan') return 'engine-connect-plan';
  if (p === '/connect/key/new') return 'engine-connect-key';
  if (p.endsWith('/models/pull')) return 'engine-pull';
  if (p.endsWith('/models/progress')) return 'engine-progress';
  if (p.endsWith('/models')) return 'engine-models';
  if (p.endsWith('/mode') && method === 'POST') return 'engine-mode';
  return 'engine';
}

export const T0 = Date.parse('2026-11-15T09:00:00Z');

export interface Rig {
  fleet: MockFleet;
  api: EnginesApi;
  /** one raw call, as the page's client sends it */
  send: Send;
  /** every answer so far, as text: nothing in it may be a secret */
  seen: string[];
  now(): number;
  /** the clock moves on */
  wait(ms: number): void;
}

/** A controller with no member yet (the first run), unless another scenario is named. */
export function rig(scenario: FleetScenario = 'empty'): Rig {
  const fleet = new MockFleet({ scenario, now: T0 / 1000 });
  let now = T0;
  const seen: string[] = [];
  const send: Send = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    const r = fleet.handle(method, p, new URLSearchParams(qs), body ?? null, now);
    seen.push(JSON.stringify(r.body));
    if (r.status === 200) valid(schemaOf(method, path), r.body);
    else valid('engine-error', r.body);
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: r.status, body: JSON.parse(JSON.stringify(r.body)) };
  };
  return { fleet, api: enginesApi(send), send, seen, now: () => now, wait: (ms) => void (now += ms) };
}

/** The text and attribute values a template renders, flattened (no DOM in these tests); a
 *  handler renders as nothing. */
export function text(t: unknown): string {
  if (t == null || t === nothing || typeof t === 'function' || typeof t === 'boolean') return typeof t === 'boolean' ? String(t) : '';
  if (Array.isArray(t)) return t.map(text).join('');
  const r = t as TemplateResult;
  if (r && Array.isArray(r.strings)) return r.strings.map((s, i) => s + (i < r.values.length ? text(r.values[i]) : '')).join('');
  return String(t);
}
