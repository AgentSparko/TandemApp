// OPS-288: What runs here (lib/app-parts.ts: the switches' rows, the connected things and what each
// action changes) and the mock's /v1/fleet/app/parts (mock/appsettings.ts): bare at the start, a
// plan with its confirm, the switch, a stale confirm refused, a bad body refused. Every mock answer
// validates against fleet-v1.

import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { describe, expect, it } from 'vitest';
import { MockFleet } from '../mock/fleet.ts';
import type { AppParts, PartPlan } from '../src/api/fleet.ts';
import { backendChanges, backendRows, doneWords, engineModeChanges, isParts, mcpChanges, partRows, planTitle } from '../src/lib/app-parts.ts';

const FDIR = join(__dirname, '..', '..', 'contracts', 'fleet-v1');
const ajv = new Ajv2020({ allErrors: true, strict: true, allowUnionTypes: true });
const schema: Record<string, ReturnType<typeof ajv.compile>> = {};
for (const f of readdirSync(FDIR)) if (f.endsWith('.schema.json')) ajv.addSchema(JSON.parse(readFileSync(join(FDIR, f), 'utf8')), f);
for (const f of readdirSync(FDIR)) if (f.endsWith('.schema.json')) schema[f.replace('.schema.json', '')] = ajv.getSchema(f)!;
function valid<T>(name: string, data: T): T {
  if (!schema[name](data)) throw new Error(`${name}: ${ajv.errorsText(schema[name].errors, { separator: '\n' })}`);
  return data;
}
const T0 = Date.parse('2026-11-15T09:00:00Z');
const q = () => new URLSearchParams();

const BARE: AppParts = {
  contract_version: '1.0',
  chat: { on: false, switched: false, install: 'off', runner: null, running: null, sandbox: null, note: 'Chat is off: no chatd runs here and the Chat tab says so.' },
  conductor: { on: false, switched: false, install: 'off', dir: null },
  engines: [],
  backends: [],
  mcp: { where: 'chat', available: false },
};

describe('the switches in words', () => {
  it('a bare TandemApp: Chat and the conductor off, as installed, each with its switch-on button', () => {
    valid('app-parts', BARE);
    const rows = partRows(BARE);
    expect(rows.map((r) => [r.id, r.on, r.badge, r.tone, r.action])).toEqual([
      ['chat', false, '○ Off', 'muted', 'Switch Chat on…'],
      ['conductor', false, '○ Off', 'muted', 'Switch the conductor on…'],
    ]);
    expect(rows[0].source).toBe('As installed (TANDEM_CHAT=off)');
    expect(rows[1].source).toBe('As installed (TANDEM_CONDUCTOR=off)');
    expect(rows[0].detail).toContain('the Chat tab says Chat is off');
  });

  it('Chat on without a sandbox runtime says so; on with a socket is plain on; external names the other host', () => {
    const none = partRows({ ...BARE, chat: { on: true, switched: true, install: 'off', runner: 'own', running: true, sandbox: 'none', note: 'No sandbox runtime on this host: chats run without their sandbox tools (files, shell, browser).' } })[0];
    expect([none.tone, none.action, none.source]).toEqual(['warn', 'Switch Chat off…', 'Set on this page']);
    expect(none.detail).toContain('without their sandbox tools');
    const sock = partRows({ ...BARE, chat: { on: true, switched: false, install: 'own', runner: 'own', running: true, sandbox: 'socket', note: null } })[0];
    expect(sock.tone).toBe('ok');
    const k8s = partRows({ ...BARE, chat: { on: true, switched: false, install: 'own', runner: 'own', running: true, sandbox: 'kubernetes', note: null } })[0];
    expect(k8s.detail).toContain('pod');
    const down = partRows({ ...BARE, chat: { on: true, switched: false, install: 'own', runner: 'own', running: false, sandbox: 'socket', note: null } })[0];
    expect([down.tone, down.detail]).toEqual(['warn', 'On, but chatd is not running. The health page says why.']);
    const ext = partRows({ ...BARE, chat: { on: true, switched: false, install: 'external', runner: 'external', running: null, sandbox: null, note: 'Chat is another host’s chatd (http://127.0.0.1:8016).' } })[0];
    expect(ext.detail).toContain('another host');
    const cond = partRows({ ...BARE, conductor: { on: true, switched: true, install: 'off', dir: '/data/conductor' } })[1];
    expect([cond.badge, cond.detail, cond.source]).toEqual(['● On', 'On. Its documents are in /data/conductor.', 'Set on this page']);
  });

  it('the connected things and what each action changes', () => {
    const p: AppParts = { ...BARE, backends: [{ id: 'engine', label: 'TandemLLM engine', kind: 'tandemllm', enabled: true, env: true }, { id: 'cloud', label: null, kind: 'openai', enabled: false, env: false }] };
    expect(backendRows(p).map((b) => [b.id, b.name, b.acts])).toEqual([
      ['engine', 'TandemLLM engine', []],
      ['cloud', 'cloud', ['enable', 'remove']],
    ]);
    expect(backendChanges({ name: 'cloud' }, 'remove')[0]).toContain('stored key are deleted');
    expect(backendChanges({ name: 'cloud' }, 'disable')[1]).toContain('Enable brings it back');
    expect(mcpChanges({ name: 'tools' }, 'remove')[0]).toContain('lose its tools');
    expect(engineModeChanges('box', 'engine')).toEqual(['box serves its models bare: no conductor inside the engine.', 'box restarts once, when it is idle.']);
  });
});

describe('the mock: plan, confirm, switch', () => {
  it('starts bare, plans, switches with the confirm, refuses a stale one and a bad body', () => {
    const m = new MockFleet({ seed: 42, scenario: 'gold3', now: T0 / 1000 });
    const get = () => valid('app-parts', m.handle('GET', '/v1/fleet/app/parts', q(), null, T0).body as AppParts);
    expect(get()).toEqual(BARE);
    const post = (part: string, body: unknown) => m.handle('POST', `/v1/fleet/app/parts/${part}`, q(), body, T0);
    // the plan changes nothing
    const plan = valid('app-part-plan', post('chat', valid('app-part-body', { on: true })).body as PartPlan);
    expect([plan.changed, plan.needs_confirm, typeof plan.confirm]).toEqual([true, true, 'string']);
    expect(planTitle(plan)).toBe('Switch Chat on');
    expect(get().chat.on).toBe(false);
    // a wrong confirm: 409
    const wrong = post('chat', { on: true, confirm: 'nope' });
    expect([wrong.status, (wrong.body as { error: { type: string } }).error.type]).toEqual([409, 'stale_plan']);
    // the plan's confirm switches it
    const done = post('chat', { on: true, confirm: plan.confirm });
    expect(done.status).toBe(200);
    const p = valid('app-parts', done.body as AppParts);
    expect([isParts(p), isParts(plan)]).toEqual([true, false]);
    expect([p.chat.on, p.chat.switched, p.mcp.available]).toEqual([true, true, true]);
    expect(doneWords(p)).toBe('Chat is on: switched on.');
    // the old confirm is stale now; asking for the state that holds changes nothing
    expect(post('chat', { on: false, confirm: plan.confirm }).status).toBe(409);
    const same = valid('app-part-plan', post('chat', { on: true }).body as PartPlan);
    expect([same.changed, same.confirm]).toEqual([false, null]);
    // bad bodies and parts
    expect(post('chat', { on: 'yes' }).status).toBe(400);
    expect(post('chat', { on: true, force: true }).status).toBe(400);
    expect(post('memory', { on: true }).status).toBe(404);
    // the conductor, the same way
    const c = valid('app-part-plan', post('conductor', { on: true }).body as PartPlan);
    expect(valid('app-parts', post('conductor', { on: true, confirm: c.confirm }).body as AppParts).conductor.on).toBe(true);
  });
});
