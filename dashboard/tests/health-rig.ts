// What the health page's tests share (tests for SRV-835): the mock controller's health routes on a
// clock the test moves, reached through the page's own client (api/health.ts) so no HTTP is
// needed. Every answer is checked against its schema in contracts/fleet-v1 as it passes, every
// fix body against the body's schema before it goes, every path is kept, and every answer is kept
// as text (so a test can say that no secret is in any of them). `locked` names the rights a call
// is refused for with a 401, as a controller with an admin password and no session answers.
import { nothing, type TemplateResult } from 'lit';
import { MockFleet, type FleetScenario } from '../mock/fleet.ts';
import { rightsOf, type HealthScenario, type MockHealth, type Rights } from '../mock/health.ts';
import type { Send, Sent } from '../src/api/engines.ts';
import { healthApi, type HealthApi } from '../src/api/health.ts';
import { HealthFlow } from '../src/lib/health-flow.ts';
import { cardsOf, type Card, type Row } from '../src/lib/health.ts';
import { dialogView, healthView, type HealthHandlers } from '../src/views/health/view.ts';
import { text, valid } from './engine-rig.ts';

export const T0 = Date.parse('2026-11-16T09:00:00Z');

export interface HealthRig {
  fleet: MockFleet;
  mock: MockHealth;
  api: HealthApi;
  flow: HealthFlow;
  send: Send;
  /** every path asked so far, with its method */
  paths: string[];
  /** every fix body sent so far */
  bodies: unknown[];
  /** every answer so far, as text */
  seen: string[];
  /** the rights a call is answered 401 for (`read`: the reads and a check again) */
  locked: Set<Rights | 'read'>;
  /** nothing answers: the calls end with no status */
  offline: boolean;
  now(): number;
  wait(ms: number): void;
}

/** A controller in a health scenario, on the fleet4 mock (or another fleet), and the page's flow on it. */
export function rig(scenario: HealthScenario = 'mixed', o: { fleet?: FleetScenario } = {}): HealthRig {
  const fleet = new MockFleet({ scenario: o.fleet ?? 'fleet4', now: T0 / 1000 });
  fleet.health.load(scenario);
  let now = T0;
  const seen: string[] = [];
  const paths: string[] = [];
  const bodies: unknown[] = [];
  const r = { locked: new Set<Rights | 'read'>(), offline: false };
  const send: Send = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    paths.push(`${method} ${path}`);
    if (!p.startsWith('/v1/fleet/health')) throw new Error(`the health page asked ${method} ${path}: not a route of its own`);
    if (r.offline) return { status: 0, body: null };
    const fix = method === 'POST' && p.endsWith('/fix');
    if (fix) {
      valid('health-fix-body', body);
      bodies.push(body);
    }
    const needs = fix ? rightsOf(body) : 'dashboard';
    if (r.locked.has(needs === 'dashboard' ? 'read' : needs)) return { status: 401, body: { error: { type: 'unauthorized', message: 'a session or an admin token is required' } } };
    const a = fleet.handle(method, p, new URLSearchParams(qs), body ?? null, now);
    seen.push(JSON.stringify(a.body));
    if (a.status === 200) valid(fix ? 'health-fix' : p === '/v1/fleet/health' ? 'health' : 'health-target', a.body);
    else valid('engine-error', a.body);
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: a.status, body: JSON.parse(JSON.stringify(a.body)) };
  };
  const api = healthApi(send);
  const out: HealthRig = {
    fleet, mock: fleet.health, api, flow: new HealthFlow(api, () => undefined, () => now), send, paths, bodies, seen, now: () => now, wait: (ms) => void (now += ms),
    get locked() {
      return r.locked;
    },
    get offline() {
      return r.offline;
    },
    set offline(v: boolean) {
      r.offline = v;
    },
  };
  return out;
}

const noop = (): void => undefined;
export const HANDLERS: HealthHandlers = { refresh: noop, recheck: noop, press: noop, jump: noop, dismiss: noop, ref: noop, when: noop, go: noop, apply: noop, back: noop, close: noop };

/** the page as text; a command set as code reads with its backticks again */
export const page = (r: HealthRig, signIn: TemplateResult | typeof nothing = nothing): string => text(healthView(r.flow, HANDLERS, { signIn })).replace(/<\/?code>/g, '`');
export const dialog = (r: HealthRig): string => text(dialogView(r.flow.dialog, HANDLERS)).replace(/<\/?code>/g, '`');

export const cards = (r: HealthRig): Card[] => cardsOf(r.flow.view!);
export const card = (r: HealthRig, id: string): Card => {
  const c = cards(r).find((x) => x.id === id);
  if (!c) throw new Error(`no card ${id}`);
  return c;
};
export const row = (r: HealthRig, id: string, code: string): Row => {
  const x = card(r, id).rows.find((y) => y.finding.code === code);
  if (!x) throw new Error(`${id} has no finding ${code}: ${card(r, id).rows.map((y) => y.finding.code).join(', ') || 'none'}`);
  return x;
};
/** the codes a card lists, in its order */
export const codes = (r: HealthRig, id: string): string[] => card(r, id).rows.map((x) => x.finding.code);

/** one card's part of the page */
export function cardText(r: HealthRig, id: string): string {
  const all = page(r);
  const at = all.indexOf(`data-card=${id} `);
  if (at < 0) throw new Error(`the page shows no card ${id}`);
  const end = all.indexOf('data-card=', at + 10);
  return all.slice(at, end < 0 ? undefined : end);
}
/** one finding's row of a card */
export function rowText(r: HealthRig, id: string, code: string): string {
  const c = cardText(r, id);
  const at = c.indexOf(`data-finding=${code} `);
  if (at < 0) throw new Error(`${id} shows no finding ${code}`);
  const end = c.indexOf('data-finding=', at + 13);
  const fold = c.indexOf('data-checks', at);
  return c.slice(at, end < 0 ? (fold < 0 ? undefined : fold) : end);
}
/** the buttons a text draws: their data-act values */
export const acts = (t: string): string[] => [...t.matchAll(/data-act="?([\w-]+)/g)].map((m) => m[1]);
