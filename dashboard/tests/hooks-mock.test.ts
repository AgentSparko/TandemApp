// Tests for VIS-282 (the matrix of VIS-283): the mock's hook registry behind its three faces, held
// to the contracts (the rig checks every answer against its schema): the hooks' API with every
// refusal, the decision providers' API with every refusal, and the settings interface's hooks
// scope as the same registry seen another way. And the stand-ins the pages' states need: a request
// walked through the stages, a breaker that opens and probes, the providers' journal and report.

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { MockFleet } from '../mock/fleet.ts';
import { HOOK_ERRORS, QUESTIONS, hostIsLan } from '../mock/hooks.ts';
import { HookError, type Hook, type HookCalls, type HookList, type Provider, type ProviderList } from '../src/api/hooks.ts';
import type { Plan, SchemaView, ValuesView } from '../src/api/settings.ts';
import { EXAMPLES, STAGE_TABLE, T0, rig } from './hooks-rig.ts';

const refusal = async (p: Promise<unknown>): Promise<[number, string, HookError['facts']]> => {
  try {
    await p;
  } catch (e) {
    const x = e as HookError;
    return [x.status, x.type, x.facts];
  }
  throw new Error('it was not refused');
};
const HTTP = { name: 'Temperature cap', kind: 'http' as const, endpoint: 'http://hooks.lan:9300/cap', role: 'transform' as const, stages: ['request.before_engine'] };

describe('the hooks’ API in the mock', () => {
  it('lists the hooks with what the list must say of each, and the stage table as the contract has it', async () => {
    const r = rig();
    const list = await r.hooks.list();
    expect(list.hooks.map((h) => [h.id, h.kind, h.role, h.stages, h.priority, h.mode, h.metrics.breaker.state])).toEqual([
      ['audit', 'http', 'observer', ['request.finished'], 50, 'on', 'closed'],
      ['export', 'http', 'observer', ['response.after_engine', 'request.finished'], 200, 'shadow', 'closed'],
      ['guard', 'builtin', 'guard', ['request.received'], 10, 'on', 'closed'],
    ]);
    expect([list.kinds_available, list.builtins]).toEqual([['http', 'builtin', 'plugin'], ['deny', 'fallback', 'noop', 'route', 'set']]);
    const by = Object.fromEntries(list.hooks.map((h) => [h.id, h]));
    expect([by.audit.destination, by.export.destination, by.guard.destination]).toEqual([{ host: 'audit.lan', lan: true }, { host: 'archive.example.com', lan: false }, { host: null, lan: true }]);
    expect([by.guard.policy_effective, by.audit.policy_effective, by.export.policy_effective]).toEqual([{ 'request.received': 'closed' }, { 'request.finished': 'drop' }, { 'response.after_engine': 'open', 'request.finished': 'open' }]);
    // a secret shows as its hint only; a built-in has none
    expect([by.audit.secret, by.guard.secret]).toEqual([{ set: true, last4: '0001', sha256_6: expect.stringMatching(/^[0-9a-f]{6}$/), rotated_at: null, previous_until: null }, null]);
    // the stage table as the contract has it, and each stage's sample event as its examples hold it (SRV-849)
    const { samples, ...table } = await r.hooks.stages();
    expect(table).toEqual(STAGE_TABLE);
    for (const st of Object.keys(STAGE_TABLE.stages)) {
      expect(samples![st].event, st).toEqual(JSON.parse(readFileSync(join(EXAMPLES, `${st}.meta.json`), 'utf8')).event);
      const full = join(EXAMPLES, `${st}.full.json`);
      expect(samples![st].content, st).toEqual(existsSync(full) ? JSON.parse(readFileSync(full, 'utf8')).event.content : null);
    }
    expect(r.seen.join('')).not.toContain('hks_mock');
  });

  it('a hook is made, changed, tested, read and removed; a webhook’s secret is answered once', async () => {
    const r = rig();
    const made = await r.hooks.create({ ...HTTP, scope: { models: ['qwen*'] }, priority: 10 });
    expect([made.hook.id, made.hook.mode, made.hook.payload, made.hook.version, made.hook.updated_by]).toEqual(['temperature-cap', 'on', 'meta', 1, 'admin']);
    expect(made.secret).toMatch(/^hks_/);
    expect(made.hook.secret).toMatchObject({ set: true, last4: made.secret!.slice(-4) });
    r.seen.length = 0;
    expect((await r.hooks.get('temperature-cap')).name).toBe('Temperature cap');
    const changed = await r.hooks.patch('temperature-cap', { name: 'Cap', stages: ['request.received', 'request.before_engine'] });
    expect([changed.name, changed.stages, changed.version]).toEqual(['Cap', ['request.received', 'request.before_engine'], 2]);
    // nothing new in a patch is no change
    expect((await r.hooks.patch('temperature-cap', { name: 'Cap' })).version).toBe(2);
    r.reg.hooks['temperature-cap'].answer = { action: 'modify', patch: { temperature: 0.3, seed: 7 } };
    const test = await r.hooks.test('temperature-cap', 'request.before_engine');
    // the answer holds the event the hook was sent: the stage's sample, at its payload (meta: no text)
    expect(test.event).toEqual((await r.hooks.stages()).samples!['request.before_engine'].event);
    expect([test.outcome, test.result, test.error, test.diff]).toEqual(['ok', { action: 'modify', patch: { temperature: 0.3, seed: 7 } }, null, { temperature: { op: 'replace', bytes: 3 }, seed: { op: 'add', bytes: 1 } }]);
    // a test is journaled as a test and counts for nothing
    const calls = await r.hooks.calls('temperature-cap');
    expect([calls.calls.map((c) => [c.mode, c.outcome, c.action, c.fields]), calls.metrics.calls]).toEqual([[['test', 'ok', 'modify', ['seed', 'temperature']]], 0]);
    expect((await r.hooks.test('temperature-cap', 'request.before_engine', { params: { temperature: 1.5 } })).event).toMatchObject({ params: { temperature: 1.5 } });
    // no later answer holds the secret
    expect(r.seen.join('')).not.toContain(made.secret!);
    await r.hooks.remove('temperature-cap');
    expect(await refusal(r.hooks.get('temperature-cap'))).toEqual([404, 'not_found', { code: 'not_found' }]);
    // a second one with the same name gets the next free id; a built-in has no secret
    const again = await r.hooks.create(HTTP);
    const twin = await r.hooks.create(HTTP);
    expect([again.hook.id, twin.hook.id]).toEqual(['temperature-cap', 'temperature-cap-2']);
    const deny = await r.hooks.create({ name: 'No', kind: 'builtin', target: 'deny', role: 'guard', stages: ['request.received'], options: { reason: 'no' } });
    expect([deny.secret, deny.hook.secret, deny.hook.endpoint]).toEqual([undefined, null, null]);
  });

  it('refuses what the registry refuses, each on its field', async () => {
    const r = rig();
    const bad = async (doc: object) => {
      const [status, type, facts] = await refusal(r.hooks.create({ ...HTTP, ...doc } as never));
      return [status, type, facts.field];
    };
    expect(await bad({ name: ' ' })).toEqual([422, 'invalid', 'name']);
    expect(await bad({ kind: 'mcp' })).toEqual([422, 'kind_unavailable', 'kind']);
    expect(await bad({ kind: 'plugin' })).toEqual([422, 'invalid', 'endpoint']);
    expect(await bad({ kind: 'plugin', endpoint: null, target: 'audit/peek' })).toEqual([422, 'invalid', 'target']);
    expect(await bad({ role: 'decider' })).toEqual([422, 'invalid', 'stages']);
    expect(await bad({ stages: ['nowhere'] })).toEqual([422, 'invalid', 'stages']);
    expect(await bad({ stages: [] })).toEqual([422, 'invalid', 'stages']);
    expect(await bad({ priority: 1e7 })).toEqual([422, 'invalid', 'priority']);
    expect(await bad({ timeout_ms: 0 })).toEqual([422, 'invalid', 'timeout_ms']);
    expect(await bad({ payload: 'everything' })).toEqual([422, 'invalid', 'payload']);
    expect(await bad({ scope: { colours: ['red'] } })).toEqual([422, 'invalid', 'scope']);
    expect(await bad({ scope: { models: 'qwen*' } })).toEqual([422, 'invalid', 'scope.models']);
    expect(await bad({ endpoint: 'ftp://hooks.lan' })).toEqual([422, 'invalid', 'endpoint']);
    expect(await bad({ endpoint: 'http://user:pw@hooks.lan/' })).toEqual([422, 'invalid', 'endpoint']);
    expect(await bad({ target: 'deny' })).toEqual([422, 'invalid', 'target']);
    expect(await bad({ kind: 'builtin', endpoint: null, target: 'explode' })).toEqual([422, 'invalid', 'target']);
    expect(await bad({ colour: 'red' })).toEqual([422, 'invalid', 'colour']);
    expect(await bad({ id: 'audit' })).toEqual([409, 'exists', 'id']);
    expect(await refusal(r.hooks.patch('audit', { kind: 'builtin' } as never))).toMatchObject([422, 'invalid', { field: 'kind' }]);
    expect(await refusal(r.hooks.test('audit', 'request.received'))).toMatchObject([422, 'invalid', { field: 'stage' }]);
    expect(await refusal(r.hooks.test('audit', 'nowhere'))).toMatchObject([422, 'invalid', { field: 'stage' }]);
    expect(await refusal(r.hooks.rotate('guard'))).toMatchObject([409, 'no_secret', {}]);
    expect(await refusal(r.hooks.rotate('audit', 100000))).toMatchObject([422, 'invalid', { field: 'grace_s' }]);
    expect(await refusal(r.hooks.remove('nobody'))).toMatchObject([404, 'not_found', {}]);
    expect((await r.send('GET', '/v1/fleet/hooks/audit/nothing')).status).toBe(404);
    expect((await r.send('POST', '/v1/fleet/hooks', [] as never)).status).toBe(400);
  });

  it('the full payload: pairing’s rights, and outside the LAN a confirm before anything is stored', async () => {
    const r = rig();
    const away = { ...HTTP, name: 'Archive', endpoint: 'https://archive.example.com/in', payload: 'full' as const };
    r.reg.control({ trust: false });
    expect(await refusal(r.hooks.create(away))).toMatchObject([403, 'trust_required', {}]);
    expect(await refusal(r.hooks.rotate('audit'))).toMatchObject([403, 'trust_required', {}]);
    r.reg.control({ trust: true });
    const [status, type, facts] = await refusal(r.hooks.create(away));
    expect([status, type, facts.field, facts.destination, facts.lan, facts.confirm?.length]).toEqual([409, 'confirm_needed', 'payload', 'archive.example.com', false, 32]);
    expect((await r.hooks.list()).hooks.some((h) => h.name === 'Archive')).toBe(false);
    expect(await refusal(r.hooks.create({ ...away, confirm: 'not-the-token' }))).toMatchObject([409, 'confirm_needed', {}]);
    const made = await r.hooks.create({ ...away, confirm: facts.confirm });
    expect(made.hook.grant).toEqual({ level: 'full', by: 'admin', at: T0, destination: 'archive.example.com', lan: false, confirmed_by: 'admin', confirmed_at: T0 });
    // on the LAN no second question; the grant says who and when
    const near = await r.hooks.create({ ...HTTP, name: 'Near', payload: 'full' });
    expect(near.hook.grant).toMatchObject({ destination: 'hooks.lan', lan: true, confirmed_by: null });
    // moving a full hook to another place outside the LAN asks again; the same place does not
    const [, moved, where] = await refusal(r.hooks.patch('near', { endpoint: 'https://other.example.org/x' }));
    expect([moved, where.destination]).toEqual(['confirm_needed', 'other.example.org']);
    expect((await r.hooks.patch('near', { endpoint: 'https://other.example.org/x', confirm: where.confirm })).grant).toMatchObject({ destination: 'other.example.org', lan: false });
    expect((await r.hooks.patch('near', { endpoint: 'https://other.example.org/y' })).version).toBe(3);
    expect([hostIsLan('192.168.1.7'), hostIsLan('10.0.0.1'), hostIsLan('100.72.1.1'), hostIsLan('printer'), hostIsLan('nas.home.arpa'), hostIsLan('8.8.8.8'), hostIsLan('example.com'), hostIsLan(null)]).toEqual([true, true, true, true, true, false, false, false]);
  });

  it('a hook that changed on another screen: the patch is refused and nothing of it is written', async () => {
    const r = rig();
    r.reg.control({ race: 'audit' });
    expect(await refusal(r.hooks.patch('audit', { name: 'Mine' }))).toMatchObject([409, 'conflict', {}]);
    const now = await r.hooks.get('audit');
    expect([now.name, now.version, now.updated_by]).toEqual(['Audit log', 2, 'another screen']);
    expect((await r.hooks.patch('audit', { name: 'Mine' })).name).toBe('Mine');
  });

  it('a secret’s rotation answers the new one once; the old one signs beside it for the grace', async () => {
    const r = rig();
    const before = (await r.hooks.get('audit')).secret!;
    const out = await r.hooks.rotate('audit', 3600);
    expect(out.secret).toMatch(/^hks_/);
    expect(out.hook.secret).toMatchObject({ set: true, last4: out.secret.slice(-4), rotated_at: T0, previous_until: T0 + 3_600_000 });
    expect(out.hook.secret!.sha256_6).not.toBe(before.sha256_6);
    r.wait(3_600_001);
    expect((await r.hooks.get('audit')).secret!.previous_until).toBeNull();
    expect((await r.hooks.rotate('audit')).hook.secret!.previous_until).toBeNull();
  });

  it('every route can be made to refuse with every error the API has', async () => {
    const r = rig();
    const routes: Record<string, () => Promise<unknown>> = {
      list: () => r.hooks.list(), stages: () => r.hooks.stages(), create: () => r.hooks.create(HTTP), get: () => r.hooks.get('audit'), patch: () => r.hooks.patch('audit', { name: 'x' }), delete: () => r.hooks.remove('audit'),
      test: () => r.hooks.test('audit'), calls: () => r.hooks.calls('audit'), secret: () => r.hooks.rotate('audit'),
      'p-list': () => r.providers.list(), 'p-create': () => r.providers.create({ name: 'x', kind: 'builtin', questions: { judge: {} } }), 'p-get': () => r.providers.get('own'), 'p-patch': () => r.providers.patch('own', { name: 'x' }), 'p-delete': () => r.providers.remove('own'),
      'p-test': () => r.providers.test('own'), 'p-health': () => r.providers.health('own'), 'p-calls': () => r.providers.calls('own'), 'p-report': () => r.providers.report(),
    };
    for (const [route, go] of Object.entries(routes)) {
      for (const [type, status] of Object.entries(HOOK_ERRORS)) {
        r.reg.control({ fail: `${route}:${type}` });
        const got = await refusal(go());
        expect([got[0], got[1]], `${route} ${type}`).toEqual([status, type]);
        r.reg.control({ fail: null });
      }
    }
    expect(() => r.reg.control({ fail: 'list:teapot' })).toThrow('no hooks error teapot');
  });
});

describe('a request through the stages, in the mock', () => {
  it('the hooks in scope run by role, then priority; a shadow call keeps what it would have changed', async () => {
    const r = rig();
    await r.hooks.create({ ...HTTP, priority: 20 });
    await r.hooks.create({ ...HTTP, name: 'Second cap', priority: 5, mode: 'shadow' });
    await r.hooks.create({ name: 'Watch', kind: 'builtin', target: 'noop', role: 'observer', stages: ['request.before_engine'], priority: 1 });
    r.reg.hooks['second-cap'].answer = { action: 'modify', patch: { temperature: 0.123456, stop: null } };
    const id = r.reg.request(r.now());
    const journal = async (hook: string) => (await r.hooks.calls(hook)).calls.filter((c) => c.request === id);
    const at = async (hook: string) => (await journal(hook))[0];
    // transforms before the observer whatever its priority; within the transforms the lower first
    const order = (await Promise.all(['second-cap', 'temperature-cap', 'watch'].map(async (h) => [h, (await at(h)).ts] as const))).sort((a, b) => a[1] - b[1]).map((x) => x[0]);
    expect(order).toEqual(['second-cap', 'temperature-cap', 'watch']);
    expect(await at('second-cap')).toMatchObject({ mode: 'shadow', outcome: 'ok', action: 'modify', fields: ['stop', 'temperature'], diff: { temperature: { op: 'replace', bytes: 8 }, stop: { op: 'remove' } } });
    // the guard is out of this request's scope; the two observers at the end both ran
    expect([(await journal('guard')).length, (await journal('audit')).map((c) => c.stage), (await journal('export')).map((c) => c.stage)]).toEqual([0, ['request.finished'], ['request.finished', 'response.after_engine']]);
    // a journal holds field names and sizes, never a value
    expect(JSON.stringify(await r.hooks.calls('second-cap'))).not.toContain('0.123456');
  });

  it('one request’s trace: every hook’s calls of it, in the order the request met them, each with its hook’s name (SRV-849)', async () => {
    const r = rig();
    await r.hooks.create({ ...HTTP, priority: 20 });
    await r.hooks.create({ name: 'Watch', kind: 'builtin', target: 'noop', role: 'observer', stages: ['request.before_engine'], priority: 1 });
    const id = r.reg.request(r.now());
    r.reg.request(r.now() + 1000);
    const t = await r.hooks.trace(id);
    expect(t.request).toBe(id);
    expect(t.calls.map((c) => [c.stage, c.hook, c.name])).toEqual([
      ['request.before_engine', 'temperature-cap', 'Temperature cap'],
      ['request.before_engine', 'watch', 'Watch'],
      ['response.after_engine', 'export', 'Export to the archive'],
      ['request.finished', 'audit', 'Audit log'],
      ['request.finished', 'export', 'Export to the archive'],
    ]);
    // the same calls the hooks' own journals hold for it, and no other request's
    const each = (await Promise.all(['temperature-cap', 'watch', 'export', 'audit'].map((h) => r.hooks.calls(h)))).flatMap((x) => x.calls).filter((c) => c.request === id);
    expect(t.calls.length).toBe(each.length);
    expect((await r.hooks.trace('chatcmpl-none')).calls).toEqual([]);
    const none = await r.send('GET', '/v1/fleet/hooks/calls');
    expect([none.status, (none.body as { error: { type: string } }).error.type]).toEqual([400, 'bad_request']);
    // `stages` and `calls` are routes: no hook takes either id
    expect(await refusal(r.hooks.create({ ...HTTP, id: 'calls' } as never))).toMatchObject([422, 'invalid', { field: 'id' }]);
    expect((await r.hooks.create({ ...HTTP, name: 'Calls' })).hook.id).toBe('calls-2');
  });

  it('a guard’s deny ends the path: only the ledger’s stage still runs', async () => {
    const r = rig();
    await r.hooks.create(HTTP);
    const id = r.reg.request(r.now(), { model: 'big-1', user: 'guest-7' });
    const calls = async (h: string) => (await r.hooks.calls(h)).calls.filter((c) => c.request === id);
    expect((await calls('guard'))[0]).toMatchObject({ stage: 'request.received', action: 'deny', reason: 'ask the admin for this model', mode: 'on' });
    expect([(await calls('temperature-cap')).length, (await calls('audit')).length, (await calls('export')).map((c) => c.stage)]).toEqual([0, 1, ['request.finished']]);
  });

  it('five failures in a row open a hook’s breaker for 30 s; then one probe closes it or opens it again', async () => {
    const r = rig();
    await r.hooks.create(HTTP);
    r.reg.control({ behave: { 'temperature-cap': 'timeout' } });
    for (let i = 0; i < 5; i++) r.reg.request(r.now());
    let h = await r.hooks.get('temperature-cap');
    expect([h.metrics.breaker.state, h.metrics.breaker.opened, h.metrics.failures, h.metrics.timeouts, h.metrics.breaker.open_for_s > 29]).toEqual(['open', 1, 5, 5, true]);
    r.reg.request(r.now());
    h = await r.hooks.get('temperature-cap');
    expect([h.metrics.skipped, h.metrics.calls, (await r.hooks.calls('temperature-cap', 1)).calls[0].outcome]).toEqual([1, 5, 'breaker_open']);
    r.wait(31_000);
    expect((await r.hooks.get('temperature-cap')).metrics.breaker.state).toBe('half_open');
    r.reg.request(r.now());
    expect((await r.hooks.get('temperature-cap')).metrics.breaker).toMatchObject({ state: 'open', opened: 2 });
    r.wait(31_000);
    r.reg.control({ behave: { 'temperature-cap': 'ok' } });
    r.reg.request(r.now());
    h = await r.hooks.get('temperature-cap');
    expect([h.metrics.breaker.state, h.metrics.breaker.consecutive_failures, h.metrics.p95_ms != null]).toEqual(['closed', 0, true]);
    // an error and an answer the stage does not allow fail too; a test call bypasses the breaker
    r.reg.control({ behave: { 'temperature-cap': 'error' } });
    expect((await r.hooks.test('temperature-cap')).outcome).toBe('error');
    r.reg.control({ behave: { 'temperature-cap': 'invalid' } });
    expect((await r.hooks.test('temperature-cap')).outcome).toBe('invalid');
    r.reg.hooks['temperature-cap'].behave = 'ok';
    r.reg.hooks['temperature-cap'].answer = { action: 'deny', reason: 'a transform may not' };
    expect(await r.hooks.test('temperature-cap')).toMatchObject({ outcome: 'invalid', result: null });
    expect((await r.hooks.get('temperature-cap')).metrics.calls).toBe(7);
  });

  it('a journal keeps the last 500 calls, newest first', async () => {
    const r = rig();
    for (let i = 0; i < 520; i++) r.reg.request(r.now() + i * 10);
    const c: HookCalls = await r.hooks.calls('audit', 500);
    expect([c.calls.length, c.metrics.calls, c.calls[0].ts > c.calls[499].ts]).toEqual([500, 520, true]);
    expect((await r.hooks.calls('audit', 3)).calls).toHaveLength(3);
  });
});

describe('one registry, three faces', () => {
  it('a hook made on its API is settings keys at once; a change applied as settings shows on the API', async () => {
    const r = rig();
    const keys = async () => (await r.settings.schema('hooks')).keys.map((k) => k.key);
    expect(await keys()).not.toContain('hooks.temperature-cap.priority');
    await r.hooks.create(HTTP);
    expect(await keys()).toContain('hooks.temperature-cap.priority');
    expect((await r.settings.values('hooks')).values.find((v) => v.key === 'hooks.temperature-cap.mode')).toMatchObject({ value: 'on', label: 'Temperature cap' });
    const changes = [{ scope: 'hooks' as const, set: { 'hooks.temperature-cap.priority': 5, 'hooks.audit.payload': 'full' } }];
    const plan: Plan = await r.settings.plan(changes);
    r.wait(1000);
    expect((await r.settings.apply(changes, { confirm: plan.confirm! })).state).toBe('applied');
    const cap = await r.hooks.get('temperature-cap');
    const audit = await r.hooks.get('audit');
    expect([cap.priority, cap.version, cap.updated_at, audit.payload]).toEqual([5, 2, T0 + 1000, 'full']);
    // the full payload granted through the settings is a grant on the hook, with who and when
    expect(audit.grant).toEqual({ level: 'full', by: 'admin', at: T0 + 1000, destination: 'audit.lan', lan: true, confirmed_by: null, confirmed_at: null });
    await r.hooks.remove('temperature-cap');
    expect(await keys()).not.toContain('hooks.temperature-cap.priority');
    // a provider's key set through the settings shows as its hint on the providers' API
    const key = [{ scope: 'hooks' as const, set: { 'providers.classes.questions.judge.api_key': 'sk-judge-9876' } }];
    await r.settings.apply(key, { confirm: (await r.settings.plan(key)).confirm! });
    const classes = await r.providers.get('classes');
    expect([classes.questions.judge.key, classes.version]).toEqual([{ key_set: true, last4: '9876', sha256_6: expect.stringMatching(/^[0-9a-f]{6}$/), set_at: T0 + 1000 }, 2]);
    expect(r.seen.join('')).not.toContain('sk-judge-9876');
  });

  it('with nothing registered the scope has no key and both lists are empty; the mock fleet answers both APIs', async () => {
    const r = rig({ hooks: false });
    expect([(await r.hooks.list()).hooks, (await r.providers.list()).providers, (await r.settings.schema('hooks')).keys]).toEqual([[], [], []]);
    const fleet = new MockFleet({ now: T0 / 1000 });
    const hooks = fleet.handle('GET', '/v1/fleet/hooks', new URLSearchParams(), null, T0).body as HookList;
    const provs = fleet.handle('GET', '/v1/fleet/decision-providers', new URLSearchParams(), null, T0).body as ProviderList;
    expect(hooks.hooks.map((h) => h.id)).toEqual(['audit', 'classes-adapter', 'export', 'guard', 'pii-guard', 'temperature-cap', 'vision-route']);
    expect(provs.providers.map((p) => [p.id, p.kind, p.mode])).toEqual([['adapter', 'hook', 'shadow'], ['classes', 'systemone-http', 'on'], ['intent', 'systemone-http', 'ab'], ['own', 'builtin', 'shadow']]);
    // the demo's traffic: figures, a breaker that is not closed, calls in the report
    const by = Object.fromEntries(hooks.hooks.map((h: Hook) => [h.id, h]));
    expect([by.audit.metrics.calls > 30, by['temperature-cap'].metrics.p95_ms != null, by['pii-guard'].metrics.breaker.state, by['classes-adapter'].metrics.calls > 0]).toEqual([true, true, 'half_open', true]);
    const keys = (fleet.settings.handle('GET', '/v1/settings/hooks/schema', new URLSearchParams(), null, T0).body as SchemaView).keys.map((k) => k.key);
    expect(keys).toContain('hooks.pii-guard.mode');
    expect((fleet.settings.handle('GET', '/v1/settings/hooks', new URLSearchParams(), null, T0).body as ValuesView).values.find((v) => v.key === 'providers.intent.ab_percent')!.value).toBe(30);
    expect(fleet.handle('GET', '/v1/fleet/hooks/stages', new URLSearchParams(), null, T0).body).toEqual({ ...STAGE_TABLE, samples: expect.any(Object) });
    expect(new MockFleet({ scenario: 'empty', now: T0 / 1000 }).handle('GET', '/v1/fleet/hooks', new URLSearchParams(), null, T0).body).toMatchObject({ hooks: [] });
  });
});

describe('the decision providers’ API in the mock', () => {
  it('lists the providers with their keys as hints, the questions, kinds, modes and sticky keys', async () => {
    const r = rig();
    const list = await r.providers.list();
    expect(list.providers.map((p) => [p.id, p.kind, p.mode, Object.keys(p.questions), p.key?.key_set ?? null])).toEqual([['classes', 'systemone-http', 'on', ['classify', 'judge'], true], ['own', 'builtin', 'shadow', ['memory_gate'], null]]);
    expect(list.providers[0].key).toMatchObject({ key_set: true, last4: '0003' });
    expect(list.providers[0].questions.judge).toEqual({ model: 'judge-v2', endpoint: null, timeout_ms: null, key: { key_set: false, last4: null, sha256_6: null, set_at: null } });
    expect('key' in list.providers[1].questions.memory_gate).toBe(false);
    expect([Object.keys(list.questions), list.kinds, list.modes, list.sticky, list.replay]).toEqual([Object.keys(QUESTIONS), ['builtin', 'systemone-http', 'hook'], ['on', 'shadow', 'ab'], ['session', 'user', 'request'], false]);
    expect(r.seen.join('')).not.toContain('sk-mock');
  });

  it('a provider is made, changed and removed; its keys are write-only', async () => {
    const r = rig();
    const made = await r.providers.create({ name: 'My decider', kind: 'systemone-http', endpoint: 'http://decider.example:8720', api_key: 'sk-my-decider-1234', questions: { memory_intent: { endpoint: 'http://other.example:8720', api_key: 'sk-other-5678', model: 'intent-v1' }, packages: {} } });
    expect([made.id, made.mode, made.model, made.timeout_ms, made.key?.last4, made.questions.memory_intent.key?.last4, made.questions.packages.key?.key_set]).toEqual(['my-decider', 'shadow', 'auto', 400, '1234', '5678', false]);
    expect(Object.keys(made.breakers).sort()).toEqual(['http://decider.example:8720', 'http://other.example:8720']);
    const changed = await r.providers.patch('my-decider', { name: 'Mine', model: 'm-2', timeout_ms: 250 });
    expect([changed.name, changed.model, changed.timeout_ms, changed.version, changed.key?.last4]).toEqual(['Mine', 'm-2', 250, 2, '1234']);
    expect(r.seen.join('')).not.toMatch(/sk-my-decider-1234|sk-other-5678/);
    await r.providers.remove('my-decider');
    expect(await refusal(r.providers.get('my-decider'))).toMatchObject([404, 'not_found', {}]);
    const viaHook = await r.hooks.create({ name: 'Adapter', kind: 'http', endpoint: 'http://hooks.lan:9300/ask', role: 'decider', stages: ['decision.ask'] });
    const p = await r.providers.create({ name: 'Through a hook', kind: 'hook', hook_id: viaHook.hook.id, questions: { packages: {} } });
    expect([p.kind, p.hook_id, p.endpoint, p.key, Object.keys(p.breakers)]).toEqual(['hook', 'adapter', null, null, ['hook:adapter']]);
  });

  it('refuses what the store refuses: a question taken, a hook that is no decider, a key where none goes', async () => {
    const r = rig();
    const bad = async (doc: object) => {
      const [status, type, facts] = await refusal(r.providers.create({ name: 'X', kind: 'systemone-http', endpoint: 'http://x.lan:1', questions: { packages: {} }, ...doc } as never));
      return [status, type, facts.field];
    };
    expect(await bad({ questions: { classify: {} }, mode: 'on' })).toEqual([409, 'question_taken', 'questions']);
    expect(await bad({ questions: { classify: {} }, mode: 'ab', ab_percent: 10 })).toEqual([409, 'question_taken', 'questions']);
    expect((await r.providers.create({ name: 'Watcher', kind: 'systemone-http', endpoint: 'http://x.lan:1', questions: { classify: {} } })).mode).toBe('shadow');
    expect(await bad({ questions: {} })).toEqual([422, 'invalid', 'questions']);
    expect(await bad({ questions: { weather: {} } })).toEqual([422, 'invalid', 'questions.weather']);
    expect(await bad({ endpoint: null })).toEqual([422, 'invalid', 'endpoint']);
    expect(await bad({ timeout_ms: 9000 })).toEqual([422, 'invalid', 'timeout_ms']);
    expect(await bad({ ab_percent: 101 })).toEqual([422, 'invalid', 'ab_percent']);
    expect(await bad({ api_key: 'a key' })).toEqual([422, 'invalid', 'api_key']);
    expect(await bad({ kind: 'builtin', endpoint: null, api_key: 'sk-1' })).toEqual([422, 'invalid', 'api_key']);
    expect(await bad({ kind: 'hook', endpoint: null, hook_id: 'audit' })).toEqual([422, 'invalid', 'hook_id']);
    expect(await bad({ kind: 'hook', endpoint: null, hook_id: 'nobody' })).toEqual([422, 'invalid', 'hook_id']);
    expect(await bad({ id: 'own' })).toEqual([409, 'exists', 'id']);
    expect(await refusal(r.providers.test('own', 'classify'))).toMatchObject([422, 'invalid', { field: 'question' }]);
    expect(await refusal(r.providers.report(9999))).toMatchObject([400, 'bad_request', {}]);
    r.reg.control({ race: 'own' });
    expect(await refusal(r.providers.patch('own', { name: 'Mine' }))).toMatchObject([409, 'conflict', {}]);
    expect((await r.providers.get('own')).updated_by).toBe('another screen');
  });

  it('health: ok, failing after a failed call, down when a probe fails, off when switched off', async () => {
    const r = rig();
    // the list carries the health from the last probes, probing nothing: none yet (SRV-851)
    const listed = async () => (await r.providers.list()).providers.find((p) => p.id === 'classes')!.health!;
    expect([(await listed()).status, (await listed()).endpoints[0].probe]).toEqual(['unknown', null]);
    let h = await r.providers.health('classes');
    expect([h.status, h.endpoints.map((e) => [e.endpoint, e.questions, e.probe!.up, e.breaker.state]), h.last_call]).toEqual(['ok', [['http://decider.lan:8720', ['classify', 'judge'], true, 'closed']], null]);
    // a probe is kept 10 s; force probes now
    r.wait(4000);
    expect([(await listed()).status, (await listed()).endpoints[0].probe!.age_s]).toEqual(['ok', 4]);
    expect((await r.providers.health('classes')).endpoints[0].probe!.age_s).toBe(4);
    expect((await r.providers.health('classes', true)).endpoints[0].probe!.age_s).toBe(0);
    r.reg.control({ provider: { classes: 'timeout' } });
    r.reg.ask(r.now(), { question: 'classify' });
    h = await r.providers.health('classes');
    expect([h.status, h.last_call]).toEqual(['failing', { ts_ms: r.now(), question: 'classify', error: 'timeout', ms: 400 }]);
    r.reg.control({ provider: { classes: 'down' } });
    expect((await r.providers.health('classes', true)).status).toBe('down');
    expect((await r.providers.health('classes', true)).endpoints[0].probe).toMatchObject({ up: false, error: 'unreachable' });
    expect((await listed()).status).toBe('down');
    r.reg.control({ provider: { classes: 'ok' } });
    await r.settings.apply([{ scope: 'hooks', set: { 'providers.classes.enabled': false } }], { confirm: (await r.settings.plan([{ scope: 'hooks', set: { 'providers.classes.enabled': false } }])).confirm! });
    expect((await r.providers.health('classes', true)).status).toBe('off');
    expect((await r.providers.get('own')).health).toBeUndefined();
    expect((await r.send('GET', '/v1/fleet/decision-providers/own?health=1')).body).toMatchObject({ provider: { health: { status: 'ok', endpoints: [{ endpoint: 'builtin' }] } } });
  });

  it('the journal and the report: on, shadow and both sides of an A/B, with every fallback counted', async () => {
    const r = rig();
    const set = async (sets: Record<string, unknown>) => {
      const c = [{ scope: 'hooks' as const, set: sets }];
      expect((await r.settings.apply(c, { confirm: (await r.settings.plan(c)).confirm! })).state).toBe('applied');
    };
    for (let i = 0; i < 40; i++) r.reg.ask(r.now() + i, { question: 'classify', session: `s-${i}` });
    for (let i = 0; i < 30; i++) r.reg.ask(r.now() + i, { question: 'memory_gate', session: `s-${i}` });
    await set({ 'providers.own.mode': 'ab', 'providers.own.ab_percent': 40 });
    for (let i = 0; i < 60; i++) r.reg.ask(r.now() + 100 + i, { question: 'memory_gate', session: `s-${i}` });
    r.reg.control({ provider: { classes: 'http_401' } });
    for (let i = 0; i < 3; i++) r.reg.ask(r.now() + 200 + i, { question: 'classify', session: 's-x' });
    r.wait(1000);
    const rep = await r.providers.report(24);
    const on = rep.providers.classes.classify.on!;
    expect([on.calls, on.answered, on.failures, on.fallbacks, on.agreement_pct, on.p50_ms! > 10]).toEqual([43, 40, 3, 3, null, true]);
    // each fallback counted by its reason (SRV-851)
    expect(on.fallback_reasons).toEqual({ http_401: 3 });
    const own = rep.providers.own.memory_gate;
    expect([own.shadow!.calls, own.shadow!.compared, own.shadow!.agreement_pct! > 70, own.ab_a!.calls + own.ab_b!.calls, own.ab_b!.calls > 10, own.ab_a!.answered]).toEqual([30, 30, true, 60, true, 0]);
    expect([rep.source, rep.hours, Object.keys(rep.providers).sort()]).toEqual(['memory', 24, ['classes', 'own']]);
    expect(Object.keys((await r.providers.report(24, 'own')).providers)).toEqual(['own']);
    // the calls: the fallback's reason, never the question's text
    const calls = (await r.providers.calls('classes', 5)).calls;
    expect(calls.slice(0, 3).map((c) => [c.used, c.fallback, c.answer?.error])).toEqual([['builtin', 'http_401', 'http_401'], ['builtin', 'http_401', 'http_401'], ['builtin', 'http_401', 'http_401']]);
    expect(calls[4]).toMatchObject({ used: 'provider', fallback: null, mode: 'on', arm: null });
    const ab = (await r.providers.calls('own', 60)).calls;
    expect([new Set(ab.map((c) => c.arm)).size, ab.filter((c) => c.arm === 'a').every((c) => c.answer === null && c.used === 'builtin')]).toEqual([2, true]);
    // out of the time span: nothing
    r.wait(2 * 3_600_000);
    expect((await r.providers.report(1)).providers).toEqual({});
    // recorded answers in the place of every provider
    r.reg.control({ replay: true });
    r.reg.ask(r.now(), { question: 'classify', session: 's-1' });
    const replayed = (await r.providers.calls('classes', 1)).calls[0];
    expect([(await r.providers.list()).replay, replayed.answer?.error, replayed.fallback, replayed.used]).toEqual([true, 'replay_miss', 'replay_miss', 'builtin']);
  });

  it('a test call asks once and counts for nothing', async () => {
    const r = rig();
    const ok = await r.providers.test('classes', 'judge', 'The build failed with exit code 2.');
    expect([ok.ok, ok.question, ok.endpoint, ok.answer?.model, ok.answer!.confidence > 0.7, ok.error]).toEqual([true, 'judge', 'http://decider.lan:8720', 'judge-v2', true, null]);
    expect((await r.providers.test('classes')).question).toBe('classify');
    r.reg.control({ provider: { classes: 'unreachable' } });
    expect(await r.providers.test('classes')).toMatchObject({ ok: false, answer: null, error: 'unreachable' });
    expect((await r.providers.calls('classes')).calls).toEqual([]);
    const p: Provider = await r.providers.get('classes');
    expect(p.breakers['http://decider.lan:8720'].consecutive_failures).toBe(0);
  });
});
