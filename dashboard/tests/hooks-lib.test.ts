// Tests for VIS-282 (the matrix of VIS-283): the hooks pages' logic and words by themselves, without
// a controller and without a browser. The request's path out of the stage table, the order hooks
// run in and what a move costs in priorities, the time budgets, what is staged, scopes, a new
// hook's form, the playground's sample events and its answers, a trace out of journals, the replay
// note, every refusal, and the decision providers' counterparts.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { Bucket, Hook, HookCall, HookTest, Provider, ProviderCall, Report } from '../src/api/hooks.ts';
import { BUILTINS, PRIORITY_MAX, PRIORITY_MIN, SAMPLE_EVENTS, VERIFY, bodyOf, breakerWords, builtinsFor, callWords, capUse, checkForm, decisionWords, defaultPolicy, diffRows, explainTest, hookErrorWords, hookKey, kindWords, mayWords, movePriorities, newForm, parseEvent, parsePatterns, pipeline, recentRequests, replayNote, rolesAt, runOrder, scopeError, scopeOf, scopeWords, secretWords, stageSwitch, stageTitle, timeoutAt, traceOf, withStaged } from '../src/lib/hooks.ts';
import { adapterHooks, apiKeyKey, cellWords, checkProvider, conflicts, explainProviderTest, fallbackReasons, fallbackWords, healthWords, keyWords, mappingOf, modeWords, newProviderForm, probeWords, providerBody, providerCallWords, reportRows, reportWords, stageKey, stageProvider, takenBy, whoAnswers, withStagedProviders } from '../src/lib/providers.ts';
import { Draft } from '../src/lib/settings-draft.ts';
import { EXAMPLES, STAGE_TABLE as T } from './hooks-rig.ts';

const metrics = (o: Partial<Hook['metrics']> = {}): Hook['metrics'] => ({ calls: 0, failures: 0, timeouts: 0, skipped: 0, outcomes: {}, p50_ms: null, p95_ms: null, breaker: { state: 'closed', consecutive_failures: 0, open_for_s: 0, opened: 0 }, ...o });
const hook = (id: string, o: Partial<Hook> = {}): Hook => ({
  id, name: id, kind: 'http', stages: ['request.before_engine'], role: 'transform', priority: 100, mode: 'on', scope: {}, policy: null, timeout_ms: null, payload: 'meta', endpoint: `http://hooks.lan:9300/${id}`, target: null, options: {}, enabled: true,
  created_at: 1, updated_at: 1, updated_by: 'admin', version: 1, grant: null, policy_effective: {}, secret: { set: true, last4: 'abcd', sha256_6: '0a1b2c', rotated_at: null, previous_until: null }, destination: { host: 'hooks.lan', lan: true }, metrics: metrics(), ...o,
});
const call = (o: Partial<HookCall> = {}): HookCall => ({ ts: 1000, stage: 'request.before_engine', request: 'r-1', mode: 'on', outcome: 'ok', action: 'continue', duration_ms: 10, breaker: 'closed', ...o });

describe('the stages', () => {
  it('every stage of the contract has a name in plain words, and a stage this page does not know keeps its id', () => {
    for (const id of Object.keys(T.stages)) expect(stageTitle(id), id).not.toBe(id);
    expect(Object.keys(T.stages)).toHaveLength(15);
    expect(stageTitle('future.stage')).toBe('future.stage');
    expect(mayWords(T.stages['request.received'])).toEqual(['change it', 'refuse it']);
    expect(mayWords(T.stages['request.error'])).toEqual(['choose a fallback']);
    expect(mayWords({ may: ['later'] })).toEqual(['later']);
  });

  it('the playground’s sample events are the contract’s own examples, stage by stage', () => {
    for (const id of Object.keys(T.stages)) {
      const example = JSON.parse(readFileSync(join(EXAMPLES, `${id}.meta.json`), 'utf8'));
      expect(SAMPLE_EVENTS[id], id).toEqual(example.event);
    }
    expect(Object.keys(SAMPLE_EVENTS).sort()).toEqual(Object.keys(T.stages).sort());
  });

  it('a hook’s default failure policy: guards refuse, the rest follow the stage, async stages drop', () => {
    expect(defaultPolicy(T.stages['request.received'], 'guard')).toBe('closed');
    expect(defaultPolicy(T.stages['request.received'], 'transform')).toBe('open');
    expect(defaultPolicy(T.stages['request.finished'], 'observer')).toBe('drop');
    expect(timeoutAt({ timeout_ms: null }, T.stages['request.received'], T)).toBe(150);
    expect(timeoutAt({ timeout_ms: 900 }, T.stages['request.received'], T)).toBe(150);
    expect(timeoutAt({ timeout_ms: 40 }, T.stages['request.received'], T)).toBe(40);
    expect(timeoutAt({ timeout_ms: null }, T.stages['request.finished'], T)).toBe(T.async_timeout_ms);
  });
});

describe('the order hooks run in', () => {
  it('within a role: priority, low first, ties by id', () => {
    expect(runOrder([hook('b', { priority: 10 }), hook('a', { priority: 10 }), hook('c', { priority: 5 })]).map((h) => h.id)).toEqual(['c', 'a', 'b']);
  });

  it('the path has every stage in the table’s order, the hooks at each by role then priority', () => {
    const hooks = withStaged([hook('obs', { role: 'observer', priority: 1 }), hook('t2', { priority: 20 }), hook('g', { role: 'guard', priority: 1 }), hook('t1', { priority: 10 }), hook('elsewhere', { stages: ['request.finished'], role: 'observer' })], undefined);
    const stops = pipeline(T, hooks);
    expect(stops.map((s) => s.stage)).toEqual(Object.keys(T.stages));
    expect(stops.map((s) => s.n)).toEqual(Array.from({ length: 15 }, (_, i) => i + 1));
    const stop = stops.find((s) => s.stage === 'request.before_engine')!;
    expect(stop.groups.map((g) => [g.role, g.hooks.map((h) => h.id)])).toEqual([['transform', ['t1', 't2']], ['guard', ['g']], ['observer', ['obs']]]);
    expect([stop.count, stops.find((s) => s.stage === 'request.finished')!.count, stops.find((s) => s.stage === 'route.select')!.count]).toEqual([4, 1, 0]);
  });

  it('a move sets one priority when a number fits between the new neighbours', () => {
    const order = [{ id: 'audit', priority: 50 }, { id: 'export', priority: 200 }, { id: 'zeta', priority: 300 }];
    expect(movePriorities(order, 1, 0)).toEqual({ export: 40 });
    expect(movePriorities(order, 0, 1)).toEqual({ audit: 250 });
    expect(movePriorities(order, 0, 2)).toEqual({ audit: 310 });
    expect(movePriorities(order, 2, 1)).toEqual({ zeta: 125 });
    expect(movePriorities(order, 1, 1)).toEqual({});
    expect(movePriorities(order, 5, 0)).toEqual({});
  });

  it('a move between neighbours with no room uses the tie by id, or numbers the group again', () => {
    // a and c share a priority: b fits between them by its id alone
    expect(movePriorities([{ id: 'a', priority: 10 }, { id: 'c', priority: 10 }, { id: 'b', priority: 30 }], 2, 1)).toEqual({ b: 10 });
    // one apart: the moved hook takes the upper one's priority when its id sorts after it
    expect(movePriorities([{ id: 'a', priority: 10 }, { id: 'c', priority: 11 }, { id: 'b', priority: 30 }], 2, 1)).toEqual({ b: 10 });
    // two at one priority: the moved hook goes between its new neighbours
    const tight = [{ id: 'm', priority: 10 }, { id: 'n', priority: 10 }, { id: 'a', priority: 30 }];
    const next = movePriorities(tight, 0, 1);
    const after = tight.map((h) => ({ ...h, priority: next[h.id] ?? h.priority }));
    expect(runOrder(after).map((h) => h.id)).toEqual(['n', 'm', 'a']);
    expect(next).toEqual({ m: 20 });
    // no number and no id fits (a whole group at one bound): it is numbered again in steps of
    // ten. Every result gives the order asked for, and stays inside the registry's bounds
    for (const [from, to] of [[0, 2], [2, 0], [1, 0], [1, 2]] as const) {
      for (const group of [tight, [{ id: 'x', priority: PRIORITY_MIN }, { id: 'y', priority: PRIORITY_MIN }, { id: 'z', priority: PRIORITY_MIN }], [{ id: 'x', priority: PRIORITY_MAX }, { id: 'y', priority: PRIORITY_MAX }, { id: 'z', priority: PRIORITY_MAX }]]) {
        const want = group.map((h) => h.id);
        want.splice(to, 0, want.splice(from, 1)[0]);
        const got = movePriorities(group, from, to);
        const moved = group.map((h) => ({ ...h, priority: got[h.id] ?? h.priority }));
        expect(runOrder(moved).map((h) => h.id), `${JSON.stringify(group)} ${from}->${to}`).toEqual(want);
        for (const p of Object.values(got)) expect(p >= PRIORITY_MIN && p <= PRIORITY_MAX).toBe(true);
      }
    }
  });
});

describe('the time budgets', () => {
  const on = (id: string, p95: number | null, o: Partial<Hook> = {}) => hook(id, { metrics: metrics({ p95_ms: p95, calls: p95 == null ? 0 : 20 }), ...o });

  it('a stage’s use adds up the p95 of the hooks on the request’s path: not a shadow, not an observer, not one that is off', () => {
    const hooks = withStaged([on('a', 40), on('b', 90), on('shadow', 500, { mode: 'shadow' }), on('watch', 500, { role: 'observer' }), on('off', 500, { enabled: false }), on('new', null)], undefined);
    const use = pipeline(T, hooks).find((s) => s.stage === 'request.before_engine')!.use!;
    expect([use.budget_ms, use.used_ms, use.unmeasured, use.over, use.parts.map((p) => p.id)]).toEqual([250, 130, 1, false, ['a', 'b']]);
    expect(pipeline(T, withStaged([on('late', 400)], undefined)).find((s) => s.stage === 'request.before_engine')!.use!.over).toBe(true);
    // off the path, or with nothing on it: no bar
    expect(pipeline(T, withStaged([on('w', 30, { role: 'observer', stages: ['request.finished'] })], undefined)).find((s) => s.stage === 'request.finished')!.use).toBeNull();
    expect(pipeline(T, withStaged([on('s', 30, { mode: 'shadow' })], undefined)).find((s) => s.stage === 'request.before_engine')!.use).toBeNull();
  });

  it('the cap before the first token counts the stages that count toward it', () => {
    const hooks = [on('rx', 100, { stages: ['request.received'], role: 'guard' }), on('be', 200), on('both', 50, { stages: ['request.received', 'response.after_engine'] }), on('tool', 400, { stages: ['tool.before_exec'], role: 'guard' })];
    const cap = capUse(T, hooks);
    // tool.before_exec is not before the first token; a hook at two stages counts at each
    expect([cap.budget_ms, cap.used_ms, cap.over]).toEqual([800, 400, false]);
    expect(capUse(T, [...hooks, on('slow', 450, { stages: ['response.after_engine'] })]).over).toBe(true);
    expect(capUse(T, []).parts).toEqual([]);
  });
});

describe('what is staged', () => {
  it('a switch equal to what holds is no change; nothing hands a key back; a staged one stands in on the page', () => {
    const d = new Draft();
    const h = hook('cap', { priority: 10, policy: 'closed', scope: { models: ['qwen*'] } });
    stageSwitch(d, h, 'priority', 10);
    stageSwitch(d, h, 'mode', 'on');
    expect(d.empty).toBe(true);
    stageSwitch(d, h, 'priority', 5);
    stageSwitch(d, h, 'policy', null);
    stageSwitch(d, h, 'scope', {});
    stageSwitch(d, h, 'timeout_ms', null);
    stageSwitch(d, h, 'payload', 'full');
    expect(d.changes()).toEqual([{ scope: 'hooks', target: 'controller', set: { 'hooks.cap.priority': 5, 'hooks.cap.payload': 'full' }, unset: ['hooks.cap.policy', 'hooks.cap.scope'] }]);
    const [shown] = withStaged([h], d.part('hooks', 'controller'));
    expect([shown.priority, shown.policy, shown.scope, shown.payload, [...shown.staged].sort()]).toEqual([5, null, {}, 'full', ['payload', 'policy', 'priority', 'scope']]);
    // back to what holds: the change is gone
    stageSwitch(d, h, 'priority', 10);
    stageSwitch(d, h, 'policy', 'closed');
    stageSwitch(d, h, 'scope', { models: ['qwen*'] });
    stageSwitch(d, h, 'payload', 'meta');
    expect(d.empty).toBe(true);
    expect(hookKey('cap', 'timeout_ms')).toBe('hooks.cap.timeout_ms');
  });
});

describe('scopes', () => {
  it('in words, typed, and what the registry refuses', () => {
    expect(scopeWords({})).toBe('every request');
    expect(scopeWords(null)).toBe('every request');
    expect(scopeWords({ models: ['big-*'], users: ['guest*', 'ci'] })).toBe('models big-*; users guest*, ci');
    expect(parsePatterns(' qwen*, big-*\nqwen* ,, ')).toEqual(['qwen*', 'big-*']);
    expect(scopeOf({ models: 'a, b', users: ' ', client_kinds: 'chat' })).toEqual({ models: ['a', 'b'], client_kinds: ['chat'] });
    expect(scopeError({ models: ['a'] })).toBeNull();
    expect(scopeError({ models: Array.from({ length: 65 }, (_, i) => `m${i}`) })).toBe('Models: at most 64 patterns.');
    expect(scopeError({ users: ['x'.repeat(129)] })).toBe('Users: a pattern is at most 128 characters.');
  });
});

describe('a new hook’s form', () => {
  it('starts in shadow at the stage asked for, with the role when the stage takes one only', () => {
    const f = newForm(T, 'stream.chunk');
    expect([f.stage, f.role, f.mode, f.payload, f.kind, f.priority]).toEqual(['stream.chunk', 'observer', 'shadow', 'meta', 'http', '100']);
    expect([newForm(T, 'request.received').role, newForm(T, 'nowhere').stage, newForm(T).stage]).toEqual(['', '', '']);
    expect(rolesAt(T, 'response.after_engine')).toEqual(['transform', 'guard', 'decider', 'observer']);
    expect(rolesAt(T, 'route.select')).toEqual(['decider', 'observer']);
  });

  it('every field’s error, and the body it becomes', () => {
    const empty = checkForm(newForm(T), T);
    expect(Object.keys(empty).sort()).toEqual(['endpoint', 'name', 'role', 'stages']);
    const f = { ...newForm(T, 'request.before_engine'), name: ' Temperature cap ', role: 'transform' as const, endpoint: 'hooks.lan:9300', priority: 'ten', timeout: '900' };
    expect(checkForm(f, T)).toEqual({ endpoint: 'A URL that starts with http:// or https://.', priority: `A whole number, ${PRIORITY_MIN} to ${PRIORITY_MAX}.`, timeout_ms: 'At most the stage’s budget, 250 ms.' });
    expect(checkForm({ ...f, role: 'decider' as const }, T).role).toBe('A decider cannot run at request.before_engine. It takes transform, guard, observer.');
    expect(checkForm({ ...f, name: 'x'.repeat(81) }, T).name).toBe('At most 80 characters.');
    expect(checkForm({ ...f, timeout: '0' }, T).timeout_ms).toContain('1 to 30000');
    const ok = { ...f, endpoint: ' http://hooks.lan:9300/ ', priority: '10', timeout: '120', policy: 'closed' as const, scope: { ...f.scope, models: 'qwen*, big-*' }, payload: 'redacted' as const };
    expect(checkForm(ok, T)).toEqual({});
    expect(bodyOf(ok)).toEqual({ name: 'Temperature cap', kind: 'http', stages: ['request.before_engine'], role: 'transform', priority: 10, mode: 'shadow', payload: 'redacted', scope: { models: ['qwen*', 'big-*'] }, policy: 'closed', timeout_ms: 120, endpoint: 'http://hooks.lan:9300/' });
  });

  it('a built-in: the ones that fit the role and the stage, their options checked', () => {
    const names = ['deny', 'fallback', 'noop', 'route', 'set', 'custom'];
    expect(builtinsFor(names, 'guard', 'request.received')).toEqual(['deny', 'custom']);
    expect(builtinsFor(names, 'decider', 'route.select')).toEqual(['route', 'custom']);
    expect(builtinsFor(names, 'decider', 'request.error')).toEqual(['fallback', 'custom']);
    expect(builtinsFor(names, 'decider', 'response.after_engine')).toEqual(['custom']);
    expect(Object.keys(BUILTINS).sort()).toEqual(['deny', 'fallback', 'noop', 'route', 'set']);
    const f = { ...newForm(T, 'request.before_engine'), name: 'Cap', role: 'transform' as const, kind: 'builtin' as const };
    expect(checkForm(f, T).target).toBe('Pick the built-in.');
    expect(checkForm({ ...f, target: 'set' }, T)).toEqual({ 'options.set': 'Needed.' });
    expect(checkForm({ ...f, target: 'set', options: { set: '[1]' } }, T)).toEqual({ 'options.set': 'An object: { … }.' });
    expect(checkForm({ ...f, target: 'set', options: { set: '{"temperature":' } }, T)['options.set']).toMatch(/^Not valid JSON yet/);
    expect(bodyOf({ ...f, target: 'set', options: { set: '{"temperature": 0.3}' } })).toMatchObject({ kind: 'builtin', target: 'set', options: { set: { temperature: 0.3 } } });
    const deny = { ...newForm(T, 'request.received'), name: 'No', role: 'guard' as const, kind: 'builtin' as const, target: 'deny', options: { reason: ' ask the admin ' } };
    expect([checkForm(deny, T), bodyOf(deny).options, 'endpoint' in bodyOf(deny)]).toEqual([{}, { reason: 'ask the admin' }, false]);
  });
});

describe('the playground', () => {
  it('the event typed: the sample itself sends nothing, a change is sent, a non-object is refused', () => {
    expect(parseEvent('stream.chunk', JSON.stringify(SAMPLE_EVENTS['stream.chunk'], null, 2))).toEqual({ ok: true });
    expect(parseEvent('stream.chunk', '  ')).toEqual({ ok: true });
    expect(parseEvent('stream.chunk', '{"index": 9}')).toEqual({ ok: true, event: { index: 9 } });
    expect(parseEvent('stream.chunk', '[1]')).toEqual({ ok: false, error: 'An event is an object: { … }.' });
    expect((parseEvent('stream.chunk', '{') as { error: string }).error).toMatch(/^Not valid JSON yet/);
  });

  it('a test call’s answer, explained for every way it can end', () => {
    const t = (o: Partial<HookTest>): HookTest => ({ hook: 'cap', stage: 'request.before_engine', outcome: 'ok', duration_ms: 12.4, result: { action: 'continue' }, error: null, ...o });
    expect(explainTest(t({}))).toMatchObject({ tone: 'ok', title: 'It answered in 12 ms and would change nothing.' });
    expect(explainTest(t({ result: null }))).toMatchObject({ title: 'It answered in 12 ms and would change nothing.' });
    const mod = explainTest(t({ result: { action: 'modify', patch: { temperature: 0.3, stop: null } }, diff: { temperature: { op: 'replace', bytes: 3 }, stop: { op: 'remove' } } }));
    expect([mod.title, mod.lines]).toEqual(['It answered in 12 ms and would change 2 fields.', ['stop: removed', 'temperature: replaced, 3 B']]);
    expect(explainTest(t({ result: { action: 'modify', patch: { temperature: 0.3 } } })).lines).toEqual(['temperature: set']);
    const deny = explainTest(t({ result: { action: 'deny', reason: 'too big' } }));
    expect([deny.tone, deny.title, deny.lines[0]]).toEqual(['warn', 'It answered in 12 ms and would refuse the request.', 'The client would read: too big']);
    expect(explainTest(t({ result: { action: 'decide', decision: { backend: 'cloud', model: 'big-1' } } })).lines).toEqual(['backend cloud, model big-1']);
    expect(explainTest(t({ outcome: 'timeout', result: null, error: 'no answer within 250 ms' }), 250)).toMatchObject({ tone: 'err', title: 'No answer within its 250 ms.' });
    expect(explainTest(t({ outcome: 'error', result: null, error: 'the hook answered HTTP 500' })).lines[0]).toBe('the hook answered HTTP 500');
    expect(explainTest(t({ outcome: 'invalid', result: null, error: 'a guard may not answer modify' })).title).toBe('It answered, but not with something this stage allows.');
    expect(explainTest(t({ outcome: 'redaction_failed', result: null })).title).toBe('The redaction failed, so the hook was not called.');
    expect(explainTest(t({ outcome: 'breaker_open', result: null })).title).toBe('The call ended as “breaker_open”.');
  });
});

describe('a journal’s rows', () => {
  it('a diff is field names, what happens to each and its size, never a value', () => {
    expect(diffRows({ messages: { op: 'replace', bytes: 2048 }, temperature: { op: 'add', bytes: 3 }, stop: { op: 'remove' } })).toEqual([{ field: 'messages', text: 'replaced, 2.0 KB' }, { field: 'stop', text: 'removed' }, { field: 'temperature', text: 'added, 3 B' }]);
    expect(diffRows({ deny: true })).toEqual([]);
    expect(diffRows(undefined)).toEqual([]);
    expect(decisionWords({ fallback: { backend: 'cloud', model: 'm' } })).toBe('fallback backend cloud, model m');
    expect(decisionWords({ retry: true })).toBe('retry');
  });

  it('a call in a line: applied when on, “would” in shadow, the reason when it failed', () => {
    expect(callWords(call({ action: 'modify', fields: ['temperature'] }))).toEqual({ tone: 'ok', outcome: 'answered', did: 'changed temperature' });
    expect(callWords(call({ mode: 'shadow', action: 'modify', diff: { temperature: { op: 'replace', bytes: 3 } } }))).toEqual({ tone: 'ok', outcome: 'answered', did: 'would change temperature' });
    expect(callWords(call({ mode: 'shadow', action: 'deny', reason: 'a phone number', diff: { deny: true } })).did).toBe('would refuse: a phone number');
    expect(callWords(call({ action: 'deny', reason: 'ask the admin' }))).toEqual({ tone: 'warn', outcome: 'answered', did: 'refused: ask the admin' });
    expect(callWords(call({ action: 'decide', decision: { backend: 'cloud' } })).did).toBe('decided: backend cloud');
    expect(callWords(call({ outcome: 'timeout', action: null, error: 'no answer within 200 ms' }))).toEqual({ tone: 'err', outcome: 'timed out', did: 'no answer within 200 ms' });
    expect(callWords(call({ outcome: 'breaker_open', action: null }))).toEqual({ tone: 'warn', outcome: 'skipped, breaker open', did: '' });
    expect(callWords(call({ outcome: 'refused', action: 'modify', fields: ['choices'], error: 'the answer went out as a stream: nothing applied' })).did).toBe('would change choices (the answer went out as a stream: nothing applied)');
  });

  it('a breaker in words', () => {
    expect(breakerWords({ state: 'closed', consecutive_failures: 0, open_for_s: 0, opened: 0 })).toMatchObject({ tone: 'ok', label: 'breaker closed' });
    expect(breakerWords({ state: 'closed', consecutive_failures: 3, open_for_s: 0, opened: 0 }).text).toContain('3 in a row failed');
    expect(breakerWords({ state: 'open', consecutive_failures: 5, open_for_s: 12.4, opened: 1 })).toMatchObject({ tone: 'err', label: 'breaker open', text: 'It failed too often and is skipped for 13 s more. Then one probe call decides.' });
    expect(breakerWords({ state: 'half_open', consecutive_failures: 5, open_for_s: 0, opened: 1 })).toMatchObject({ tone: 'live', label: 'breaker probing' });
  });
});

describe('a request’s trace', () => {
  const hooks = [hook('guard', { role: 'guard', stages: ['request.received'], priority: 10, kind: 'builtin', target: 'deny' }), hook('cap', { priority: 10, payload: 'redacted' }), hook('audit', { role: 'observer', stages: ['request.finished'], priority: 50 }), hook('export', { role: 'observer', stages: ['request.finished'], priority: 200, mode: 'shadow' })];
  const journals: Record<string, HookCall[]> = {
    audit: [call({ ts: 1090, stage: 'request.finished', duration_ms: 9 }), call({ request: 'r-2', ts: 2000, stage: 'request.finished' })],
    export: [call({ ts: 1090, stage: 'request.finished', mode: 'shadow', duration_ms: 48 })],
    cap: [call({ ts: 1010, action: 'modify', fields: ['temperature'], duration_ms: 12 }), call({ request: 'test-abc', mode: 'test', ts: 3000 })],
    guard: [call({ ts: 1000, stage: 'request.received', duration_ms: 0.2 }), call({ request: 'r-3', ts: 2500, stage: 'request.received', action: 'deny', reason: 'no' }), call({ request: 'r-2', ts: 1990, stage: 'request.received', outcome: 'error', action: null })],
  };

  it('one request across the stages in the path’s order, each call with its hook, time and result', () => {
    const t = traceOf('r-1', journals, hooks, T);
    expect(t.stages.map((s) => [s.n, s.stage, s.calls.map((c) => c.hook)])).toEqual([[1, 'request.received', ['guard']], [3, 'request.before_engine', ['cap']], [11, 'request.finished', ['audit', 'export']]]);
    expect([t.calls, t.hooks.sort(), t.path_ms, t.started, t.refused]).toEqual([4, ['audit', 'cap', 'export', 'guard'], 12.2, 1000, null]);
    expect(t.stages[1].calls[0]).toMatchObject({ name: 'cap', role: 'transform', payload: 'redacted', duration_ms: 12 });
    // the same instant: the order the hooks run in now decides, so a new priority shows
    const moved = hooks.map((h) => (h.id === 'export' ? { ...h, priority: 40 } : h));
    expect(traceOf('r-1', journals, moved, T).stages[2].calls.map((c) => c.hook)).toEqual(['export', 'audit']);
    expect(traceOf('r-3', journals, hooks, T).refused).toMatchObject({ hook: 'guard', reason: 'no' });
    expect(traceOf('nothing', journals, hooks, T)).toMatchObject({ calls: 0, stages: [], started: null });
  });

  it('the requests the journals still hold, newest first; a test call is none', () => {
    expect(recentRequests(journals)).toEqual([
      { request: 'r-3', ts: 2500, calls: 1, hooks: 1, failed: false, refused: true },
      { request: 'r-2', ts: 2000, calls: 2, hooks: 2, failed: true, refused: false },
      { request: 'r-1', ts: 1090, calls: 4, hooks: 4, failed: false, refused: false },
    ]);
    expect(recentRequests(journals, 1)).toHaveLength(1);
  });

  it('the replay note: not available, the body was not kept, the playground instead', () => {
    const text = replayNote(traceOf('r-1', journals, hooks, T));
    expect([text.available, text.title]).toEqual([false, 'This request cannot be replayed']);
    expect(text.lines[0]).toContain('Some of its hooks got the text, but the journal holds ids, outcomes and sizes');
    expect(text.playground).toEqual([{ hook: 'guard', name: 'guard', stage: 'request.received' }, { hook: 'cap', name: 'cap', stage: 'request.before_engine' }, { hook: 'audit', name: 'audit', stage: 'request.finished' }, { hook: 'export', name: 'export', stage: 'request.finished' }]);
    const meta = replayNote(traceOf('r-1', journals, hooks.map((h) => ({ ...h, payload: 'meta' as const })), T));
    expect(meta.lines[0]).toBe('The body of this request was not kept. Its hooks ran at the payload level meta: they got ids, sizes and counts, never the text.');
    expect(meta.lines[1]).toContain('no replay yet');
    // a hook removed since has no page to try it on
    expect(replayNote(traceOf('r-1', journals, [], T)).playground).toEqual([]);
  });
});

describe('words', () => {
  it('a hook’s kind, its secret, the code that checks a signature', () => {
    expect(kindWords(hook('a'))).toBe('webhook to hooks.lan');
    expect(kindWords(hook('a', { kind: 'builtin', target: 'deny', destination: { host: null, lan: true } }))).toBe('built-in deny');
    expect(secretWords(null, 0)).toBe('No secret is stored.');
    expect(secretWords({ set: true, last4: 'wxyz', sha256_6: 'abc123', rotated_at: 1, previous_until: null }, 5)).toBe('A secret is stored, ending in wxyz. It is never shown again.');
    expect(secretWords({ set: true, last4: 'wxyz', sha256_6: 'abc123', rotated_at: 1, previous_until: 3_600_000 }, 0)).toContain('The previous one still signs beside it for 60 min.');
    for (const l of ['python', 'node'] as const) for (const s of ['X-Tandem-Timestamp', 'X-Tandem-Signature', 'v1=', '300', 'sha256']) expect(VERIFY[l].toLowerCase(), `${l} ${s}`).toContain(s.toLowerCase());
  });

  it('every refusal of the hooks’ API is a sentence with what to do', () => {
    const e = (type: string, facts = {}, message = 'the controller’s words') => hookErrorWords({ type, message, facts });
    expect(e('confirm_needed', { destination: 'archive.example.com' })).toMatchObject({ title: 'Prompts and answers would leave the LAN.', fix: 'Confirm it to grant the full payload to archive.example.com.' });
    expect(e('conflict').fix).toBe('This is how it stands now. Make your change again.');
    expect(e('invalid', { field: 'scope.models' })).toMatchObject({ title: 'Which requests is not accepted.', detail: 'the controller’s words' });
    expect(e('invalid').title).toBe('A value is not accepted.');
    expect(e('kind_unavailable').fix).toBe('Use a webhook or a built-in.');
    expect(e('unauthorized').title).toBe('This needs TandemApp’s admin password.');
    expect(e('question_taken').title).toBe('Another provider answers one of these questions already.');
    for (const t of ['offline', 'exists', 'no_secret', 'not_found', 'unavailable', 'bad_request']) expect(e(t).title.length, t).toBeGreaterThan(10);
    expect(e('something_new', {}, 'its own sentence').title).toBe('its own sentence');
  });
});

// ---- decision providers ----------------------------------------------------------------------------

const prov = (id: string, o: Partial<Provider> = {}): Provider => ({
  id, name: id, kind: 'systemone-http', endpoint: 'http://decider.lan:8720', hook_id: null, model: 'auto', timeout_ms: 400, questions: { classify: { model: null, endpoint: null, timeout_ms: null, key: { key_set: false, last4: null, sha256_6: null, set_at: null } } },
  mode: 'shadow', ab_percent: 0, sticky: 'session', enabled: true, key: { key_set: true, last4: '0003', sha256_6: 'abc123', set_at: 1 }, created_at: 1, updated_at: 1, updated_by: 'admin', version: 1, breakers: {}, ...o,
});
const QS = { classify: { what: 'which class' }, judge: { what: 'a failure' }, memory_gate: { what: 'worth a note' } };

describe('decision providers', () => {
  it('who answers each question, who only watches, and two that would answer the same', () => {
    const list = [prov('classes', { mode: 'on', questions: { classify: prov('x').questions.classify, judge: prov('x').questions.classify } }), prov('own', { questions: { classify: prov('x').questions.classify } }), prov('ab', { mode: 'ab', ab_percent: 30, questions: { memory_gate: prov('x').questions.classify } }), prov('off', { mode: 'on', enabled: false, questions: { memory_gate: prov('x').questions.classify } })];
    expect(whoAnswers(QS, list)).toEqual([
      { id: 'classify', what: 'which class', answers: { id: 'classes', name: 'classes', words: 'always' }, watching: [{ id: 'own', name: 'own' }] },
      { id: 'judge', what: 'a failure', answers: { id: 'classes', name: 'classes', words: 'always' }, watching: [] },
      { id: 'memory_gate', what: 'worth a note', answers: { id: 'ab', name: 'ab', words: 'for 30 % of sessions' }, watching: [] },
    ]);
    expect([takenBy('classify', list, null), takenBy('classify', list, 'classes'), takenBy('judge', list, 'own')]).toEqual(['classes', null, 'classes']);
    expect(conflicts(list)).toEqual([]);
    expect(conflicts([...list, prov('second', { mode: 'ab', questions: { classify: prov('x').questions.classify } })])).toEqual([{ question: 'classify', names: ['classes', 'second'] }]);
    expect([modeWords(list[0]), modeWords(list[1]), modeWords(list[2]), modeWords(list[3])]).toEqual(['answers', 'watches in shadow', 'answers 30 % by session', 'switched off']);
  });

  it('health and probes in words', () => {
    expect(healthWords({ status: 'ok' })).toMatchObject({ tone: 'ok', label: 'ok' });
    expect(healthWords({ status: 'failing' }).tone).toBe('warn');
    expect(healthWords({ status: 'down' }).tone).toBe('err');
    expect(healthWords({ status: 'off' })).toMatchObject({ tone: 'muted', label: 'off' });
    expect(healthWords(null).label).toBe('not probed');
    expect(probeWords({ up: true, ms: 21.4, age_s: 4 })).toEqual({ tone: 'ok', text: 'ready in 21 ms, checked 4 s ago' });
    expect(probeWords({ up: false, error: 'unreachable', age_s: 0.2 })).toEqual({ tone: 'err', text: 'not ready: not reachable, checked just now' });
    expect(probeWords({ up: false, age_s: 200 }).text).toBe('not ready, checked 3 min ago');
  });

  it('every fallback reason of the docs has its words; the reasons among calls, the most frequent first', () => {
    for (const r of ['timeout', 'unreachable', 'unknown_label', 'breaker_open', 'provider_down', 'hook_off', 'no_answer', 'out_of_scope', 'replay_miss']) expect(fallbackWords(r).title, r).not.toBe(r);
    expect(fallbackWords('http_401')).toEqual({ title: 'HTTP 401', text: 'The endpoint refused the key. Set the right API key.' });
    expect(fallbackWords('http_502').text).toBe('The endpoint answered the error 502.');
    expect(fallbackWords('later')).toEqual({ title: 'later', text: 'The built-in answer was used.' });
    const c = (o: Partial<ProviderCall>): ProviderCall => ({ provider: 'p', kind: 'systemone-http', question: 'classify', mode: 'on', arm: null, used: 'builtin', label: 'chat', answer: null, endpoint: null, builtin: null, agree: null, fallback: null, key: null, limits: null, request: null, session: null, ts_ms: 1, ...o });
    const rows = fallbackReasons([c({ fallback: 'timeout', ts_ms: 5 }), c({ fallback: 'http_401', question: 'judge', ts_ms: 9 }), c({ fallback: 'timeout', question: 'judge', ts_ms: 7 }), c({ used: 'provider' })]);
    expect(rows.map((r) => [r.reason, r.n, r.questions, r.last])).toEqual([['timeout', 2, ['classify', 'judge'], 7], ['http_401', 1, ['judge'], 9]]);
    expect(fallbackReasons([c({})])).toEqual([]);
    expect(providerCallWords(c({ fallback: 'timeout' }))).toEqual({ tone: 'err', text: 'fell back to the built-in: no answer in time' });
    expect(providerCallWords(c({ mode: 'ab', arm: 'a' })).text).toBe('built-in answered “chat” (its A/B side)');
    expect(providerCallWords(c({ mode: 'shadow', agree: true })).text).toBe('agreed with the conductor on “chat”');
    expect(providerCallWords(c({ mode: 'shadow', agree: false, answer: { label: 'code_edit', confidence: 0.9, probs: {}, model: null, ms: 1, error: null } })).text).toBe('said “code_edit”, the conductor decided “chat”');
    expect(providerCallWords(c({ used: 'provider' })).text).toBe('answered “chat”');
  });

  it('the report: a row a question, the arms that have calls side by side', () => {
    const b = (o: Partial<Bucket>): Bucket => ({ calls: 40, answered: 40, failures: 0, fallbacks: 0, compared: 0, agreed: 0, agreement_pct: null, p50_ms: 21, p95_ms: 34.5, labels: { chat: 30, code_edit: 10 }, ...o });
    const report: Report = { contract_version: '1.0', since_ms: 0, hours: 24, source: 'memory', providers: { p: { classify: { shadow: b({ compared: 40, agreed: 37, agreement_pct: 92.5 }), ab_b: b({ calls: 12, answered: 10, failures: 2, fallbacks: 2 }), ab_a: b({ calls: 28, answered: 0 }) }, judge: { shadow: b({ calls: 3, answered: 3 }) } } } };
    const { arms, rows } = reportRows(report, 'p');
    expect(arms).toEqual(['shadow', 'ab_b', 'ab_a']);
    expect(rows.map((r) => [r.question, r.calls, r.cells.map((c) => (c.bucket ? c.bucket.calls : null))])).toEqual([['classify', 80, [40, 12, 28]], ['judge', 3, [3, null, null]]]);
    expect(reportRows(report, 'nobody')).toEqual({ arms: [], rows: [] });
    expect(cellWords(report.providers.p.classify.shadow!, 'shadow')).toEqual({ calls: '40 calls', answered: '100 % answered', agreement: '92.5 % agree (37 of 40)', time: 'p50 21 ms, p95 35 ms', trouble: null, top: 'mostly “chat” (75 %)' });
    expect(cellWords(report.providers.p.classify.ab_b!, 'ab_b')).toMatchObject({ answered: '83 % answered', agreement: null, trouble: '2 failed, 2 fell back to the built-in' });
    expect(cellWords(report.providers.p.classify.ab_a!, 'ab_a').answered).toBe('answered by the built-in');
    expect(cellWords(b({ calls: 1, p50_ms: null, labels: {} }), 'on')).toMatchObject({ calls: '1 call', time: 'no time measured', top: null });
    expect(reportWords({ hours: 24, source: 'memory' })).toContain('from this controller’s memory');
    expect(reportWords({ hours: 168, source: 'journal' })).toBe('The last 7 days, from the conductor’s journal.');
    expect(reportWords({ hours: 1, source: 'journal' })).toBe('The last hour, from the conductor’s journal.');
  });

  it('a provider’s switches, mapping and keys as settings keys', () => {
    const d = new Draft();
    const p = prov('classes', { mode: 'on', questions: { classify: prov('x').questions.classify, judge: { model: 'judge-v2', endpoint: null, timeout_ms: null, key: { key_set: true, last4: 'wxyz', sha256_6: 'abc123', set_at: 1 } } } });
    expect(mappingOf(p.questions)).toEqual({ classify: {}, judge: { model: 'judge-v2' } });
    stageProvider(d, p, 'mode', 'on');
    stageProvider(d, p, 'questions', { judge: { model: 'judge-v2' }, classify: {} });
    expect(d.empty).toBe(true);
    stageProvider(d, p, 'mode', 'ab');
    stageProvider(d, p, 'ab_percent', 30);
    stageProvider(d, p, 'questions', { classify: { timeout_ms: 250 }, memory_gate: {} });
    stageKey(d, p, null, 'sk-new-key');
    stageKey(d, p, 'judge', null);
    stageKey(d, p, 'classify', null);
    expect(d.changes()).toEqual([{ scope: 'hooks', target: 'controller', set: { 'providers.classes.mode': 'ab', 'providers.classes.ab_percent': 30, 'providers.classes.questions': { classify: { timeout_ms: 250 }, memory_gate: {} }, 'providers.classes.api_key': 'sk-new-key' }, unset: ['providers.classes.questions.judge.api_key'] }]);
    const [shown] = withStagedProviders([p], d.part('hooks', 'controller'));
    expect([shown.mode, shown.ab_percent, Object.keys(shown.questions), shown.fresh, shown.keys, [...shown.staged].sort()]).toEqual(['ab', 30, ['classify', 'memory_gate'], ['memory_gate'], { '*': 'set', judge: 'unset' }, ['ab_percent', 'mode', 'questions']]);
    expect(shown.questions.classify).toEqual({ model: null, endpoint: null, timeout_ms: 250, key: p.questions.classify.key });
    expect([apiKeyKey('a', null), apiKeyKey('a', 'judge')]).toEqual(['providers.a.api_key', 'providers.a.questions.judge.api_key']);
    expect([keyWords(p.key, undefined), keyWords(p.key, 'set'), keyWords(p.key, 'unset'), keyWords(null, undefined)]).toEqual(['A key is stored, ending in 0003. It is never shown.', 'A new key is staged. It is sent when the change set is applied.', 'The stored key is removed when the change set is applied.', 'No key is stored.']);
  });

  it('a new provider’s form: its checks per kind and the body it becomes', () => {
    const f = newProviderForm();
    expect([f.kind, f.mode, f.model, f.timeout]).toEqual(['systemone-http', 'shadow', 'auto', '400']);
    expect(Object.keys(checkProvider(f, [])).sort()).toEqual(['endpoint', 'name', 'questions']);
    const ok = { ...f, name: ' My decider ', endpoint: 'http://decider.example:8720', api_key: 'sk-1', questions: ['classify'] };
    expect(checkProvider(ok, [])).toEqual({});
    expect(providerBody(ok)).toEqual({ name: 'My decider', kind: 'systemone-http', model: 'auto', timeout_ms: 400, questions: { classify: {} }, mode: 'shadow', endpoint: 'http://decider.example:8720', api_key: 'sk-1' });
    expect(checkProvider({ ...ok, api_key: 'a b', timeout: '9000', model: 'two words' }, [])).toEqual({ api_key: 'A key has no spaces.', timeout_ms: 'A whole number of milliseconds, 1 to 5000.', model: 'A model id, or auto.' });
    // on or A/B: a question another provider answers already is said before the controller says it
    const taken = [prov('classes', { mode: 'on' })];
    expect(checkProvider({ ...ok, mode: 'on' }, taken).questions).toContain('classify is answered by classes');
    expect(checkProvider({ ...ok, mode: 'shadow' }, taken)).toEqual({});
    expect(checkProvider({ ...ok, mode: 'ab', ab_percent: '130' }, []).ab_percent).toBe('A share from 0 to 100.');
    expect(providerBody({ ...ok, mode: 'ab', ab_percent: '25', sticky: 'user' })).toMatchObject({ mode: 'ab', ab_percent: 25, sticky: 'user' });
    const viaHook = { ...f, name: 'Adapter', kind: 'hook' as const, questions: ['packages'] };
    expect(checkProvider(viaHook, []).hook_id).toBe('Pick the hook that answers.');
    expect(providerBody({ ...viaHook, hook_id: 'adapter' })).toEqual({ name: 'Adapter', kind: 'hook', model: 'auto', timeout_ms: 400, questions: { packages: {} }, mode: 'shadow', hook_id: 'adapter' });
    expect('endpoint' in providerBody({ ...f, name: 'Own', kind: 'builtin', questions: ['judge'] })).toBe(false);
    expect(adapterHooks([hook('a', { role: 'decider', stages: ['decision.ask'] }), hook('b', { role: 'decider', stages: ['route.select'] }), hook('c', { role: 'observer', stages: ['decision.ask'] })]).map((h) => h.id)).toEqual(['a']);
  });

  it('a test call’s answer, explained', () => {
    const ok = explainProviderTest({ provider: 'p', question: 'classify', ok: true, endpoint: 'http://decider.lan:8720', ms: 21.4, answer: { label: 'code_edit', confidence: 0.93, probs: { chat: 0.07, code_edit: 0.93 }, model: 'my-decider', ms: 21.4, error: null }, error: null });
    expect([ok.tone, ok.title, ok.probs.map((p) => p.label)]).toEqual(['ok', 'It answered “code_edit” in 21 ms, 93 % sure.', ['code_edit', 'chat']]);
    expect(ok.lines).toEqual(['Model my-decider', 'At http://decider.lan:8720', 'At the decider’s default threshold of 80 % this answer would be taken.']);
    expect(explainProviderTest({ provider: 'p', question: 'classify', ok: true, endpoint: null, ms: 9, answer: { label: 'chat', confidence: 0.6, probs: {}, model: null, ms: 9, error: null }, error: null }).lines).toEqual(['Below the decider’s default threshold of 80 %: the next stage would be asked.']);
    const bad = explainProviderTest({ provider: 'p', question: 'classify', ok: false, endpoint: 'x', ms: 400, answer: null, error: 'http_401' });
    expect([bad.tone, bad.title, bad.lines[0]]).toEqual(['err', 'It did not answer classify: http 401.', 'The endpoint refused the key. Set the right API key.']);
  });
});
