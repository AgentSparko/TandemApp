// Tests for SRV-835: the mock controller's health routes (mock/health.ts) answer as the contract
// says (contracts/fleet-v1 health*.schema.json, engine-codes.json): every finding code with its
// severity and action, every action, the kept answers and their limits, the fixes and their
// refusals, and the rights each fix needs. The rig checks every answer against its schema.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { ENGINE_CODES } from '../mock/enginecodes.ts';
import { MOCK_ENGINES } from '../mock/enginehosts.ts';
import { API_SPEAKS, CACHE_S, FIXES_PER_MIN, HEALTH_ACTIONS, HEALTH_FINDINGS, HEALTH_SCENARIOS, MIN_GAP_S, START_S, UPGRADE_S, healthFinding, rightsOf, type HealthScenario } from '../mock/health.ts';
import { EngineError, enginesApi, type EngineFinding, type Send } from '../src/api/engines.ts';
import type { ActionId, HealthTarget, HealthView } from '../src/api/health.ts';
import { CONTRACTS } from './engine-rig.ts';
import { rig, type HealthRig } from './health-rig.ts';

async function refused(p: Promise<unknown>): Promise<EngineFinding & { status: number }> {
  try {
    await p;
  } catch (e) {
    if (e instanceof EngineError) return { ...e.finding, status: e.status };
    throw e;
  }
  throw new Error('the call was not refused');
}
const codesOf = (t: HealthTarget): string[] => t.findings.map((f) => f.code);
const engine = (v: HealthView, id: string): HealthTarget => v.engines.find((e) => e.id === id)!;

describe('the tables', () => {
  const contract = JSON.parse(readFileSync(join(CONTRACTS, 'fleet-v1', 'engine-codes.json'), 'utf8')) as {
    findings: Record<string, { severity: string; action: string | null }>;
    actions: Record<string, { label: string; changes: boolean; confirm: boolean; route: string }>;
  };
  it('each finding’s severity and action are the contract’s', () => {
    expect(Object.fromEntries(Object.entries(HEALTH_FINDINGS).map(([k, [severity, action]]) => [k, { severity, action }]))).toEqual(contract.findings);
    for (const code of Object.keys(HEALTH_FINDINGS)) expect(ENGINE_CODES[code], code).toBeTruthy();
  });
  it('each action’s label, whether it changes something, whether it asks first and its rights are the contract’s', () => {
    expect(Object.fromEntries(Object.entries(HEALTH_ACTIONS).map(([k, [label, changes, confirm, route]]) => [k, { label, changes, confirm, route }]))).toEqual(contract.actions);
  });
  it('a finding is the code’s sentence and fix filled from its facts, never with a brace left', () => {
    for (const code of Object.keys(HEALTH_FINDINGS)) {
      const f = healthFinding(code);
      expect(f.sentence + f.fix, code).not.toMatch(/[{}]/);
      expect(f.sentence.length, code).toBeGreaterThan(10);
    }
    const f = healthFinding('disk_low', { path: '/data', free_gb: 3.2, need_gb: 5 });
    expect([f.sentence, f.fix, f.severity, f.action]).toEqual(['Only 3.2 GB are free at /data.', 'Free some space there: TandemApp wants 5 GB.', 'warning', null]);
    expect(healthFinding('disk_low', {}, null, { severity: 'error' }).severity).toBe('error');
    expect(healthFinding('engine_down').action).toEqual({ id: 'start', label: 'Start', changes: true, confirm: false });
    expect(healthFinding('engine_too_old').action).toEqual({ id: 'upgrade', label: 'Upgrade', changes: true, confirm: true });
  });
  it('the rights a fix needs: a read’s for a check again and the guides', () => {
    const of = (a: string) => rightsOf({ action: a });
    expect([of('retry'), of('re_pair'), of('pick_model'), of('add_engine')]).toEqual(['dashboard', 'dashboard', 'dashboard', 'dashboard']);
    expect([of('start'), of('rotate_token'), of('upgrade'), of('restart_chatd')]).toEqual(['setup', 'fleet_trust', 'fleet_trust', 'conductor_write']);
    expect([rightsOf(null), rightsOf({ action: 'nope' }), rightsOf('start')]).toEqual(['dashboard', 'dashboard', 'dashboard']);
  });
});

describe('what the scenarios find', () => {
  it('every finding code is made by a scenario, and every answer fits its schema', async () => {
    const seen = new Set<string>();
    for (const s of HEALTH_SCENARIOS) {
      const r = rig(s, { fleet: s === 'first' ? 'empty' : 'fleet4' });
      const v = await r.api.all();
      for (const t of [v.controller, ...v.engines]) for (const f of t.findings) seen.add(f.code);
      for (const p of v.pairings) seen.add(p.finding.code);
      for (const t of v.engines) expect((await r.api.target(t.id)).id).toBe(t.id);
    }
    expect([...seen].sort()).toEqual(Object.keys(HEALTH_FINDINGS).sort());
  });
  it('the answer names every action’s button, the setup flag and the worst state', async () => {
    const v = await rig('mixed').api.all();
    expect(v.state).toBe('error');
    expect(v.setup).toEqual({ needs_setup: false, engines: 4, nodes: 4, backends: 0, first_page: null });
    expect(Object.keys(v.actions).sort()).toEqual(Object.keys(HEALTH_ACTIONS).sort());
    expect(v.actions.retry).toEqual({ id: 'retry', label: 'Check again', changes: false, confirm: false });
    expect((await rig('ok').api.all()).state).toBe('ok');
    const first = await rig('first', { fleet: 'empty' }).api.all();
    expect([first.state, first.setup.needs_setup, first.setup.first_page, codesOf(first.controller)]).toEqual(['ok', true, 'add_engine', ['no_engine']]);
  });
  it('an engine host’s checks run in order, and one that fails makes the ones behind it moot', async () => {
    const v = await rig('all').api.all();
    const checks = (id: string) => engine(v, id).checks.map((k) => `${k.check}:${k.ok}`);
    expect(checks('spark-far')).toEqual(['ssh:false', 'host_key:null', 'engine:null', 'version:null', 'model:null', 'http:null', 'token:null']);
    expect(checks('spark-stopped')).toEqual(['ssh:true', 'host_key:true', 'engine:false', 'version:true', 'model:null', 'http:null', 'token:null']);
    expect(checks('spark-walled')).toEqual(['ssh:true', 'host_key:true', 'engine:true', 'version:true', 'model:true', 'http:false', 'token:null']);
    expect(checks('gpu-box')).toEqual(['ssh:true', 'host_key:true', 'engine:true', 'version:true', 'model:true', 'http:true', 'token:false']);
    // SSH down: nothing the engine would say is known
    const far = engine(v, 'spark-far');
    expect([far.engine_state, far.engine_version, far.model, codesOf(far)]).toEqual([null, null, null, ['ssh_unreachable']]);
    expect(far.findings[0].detail).toContain('its HTTP port 8000 does not answer either');
    expect(far.findings[0].facts).toEqual({ host: '192.0.2.101', port: 22, user: 'spark' });
    // SSH down while the engine serves: a warning, the control alone is cut off
    expect([engine(v, 'spark-serving').findings[0].severity, engine(v, 'spark-serving').state]).toEqual(['warning', 'warning']);
  });
  it('a changed host key carries both fingerprints, and its token is never tried', async () => {
    const v = await rig('all').api.all();
    const moved = engine(v, 'spark-moved');
    const f = moved.findings[0];
    expect([f.code, f.action?.id, f.action?.changes]).toEqual(['host_key_changed', 're_pair', false]);
    expect([f.facts.pinned, f.facts.offered].every((x) => /^SHA256:[A-Za-z0-9+/]{43}$/.test(x ?? ''))).toBe(true);
    expect(f.facts.pinned).not.toBe(f.facts.offered);
    expect(f.sentence).toContain(f.facts.offered);
    expect(moved.checks.find((k) => k.check === 'host_key')).toEqual({ check: 'host_key', ok: false, code: 'host_key_changed' });
    expect(moved.checks.find((k) => k.check === 'token')!.ok).toBeNull();
  });
  it('the versions: an older minor may be upgraded, another major has no button', async () => {
    const v = await rig('all').api.all();
    const old = engine(v, 'spark-old').findings[0];
    expect([old.code, old.severity, old.action?.id, old.detail]).toEqual(['engine_too_old', 'warning', 'upgrade', 'engine API 1.7, TandemApp is made for 1.9; control 1.0, TandemApp is made for 1.1']);
    const next = engine(v, 'spark-next').findings[0];
    expect([next.code, next.action, next.facts.engine_api, next.facts.speaks]).toEqual(['engine_api_unsupported', null, '2.0', API_SPEAKS]);
  });
  it('the controller: Chat off checks nothing of it; no socket leaves the memory cgroup unchecked; little disk is a warning, almost none an error', async () => {
    const off = (await rig('first', { fleet: 'empty' }).api.all()).controller;
    expect([off.chat, off.sandbox_socket, off.checks.slice(0, 3).map((k) => k.ok)]).toEqual(['off', null, [null, null, null]]);
    const mixed = (await rig('mixed').api.all()).controller;
    expect(codesOf(mixed)).toEqual(['sandbox_off', 'disk_low', 'backend_down', 'plugin_failed']);
    expect(mixed.checks.find((k) => k.check === 'memory_cgroup')!.ok).toBeNull();
    expect(mixed.findings[1].severity).toBe('warning');
    const all = (await rig('all').api.all()).controller;
    expect(all.findings.find((f) => f.code === 'disk_low')!.severity).toBe('error');
    expect(all.findings.find((f) => f.code === 'chatd_down')!.detail).toBe('the watcher gave up for this hour');
    const ext = (await rig('external').api.all()).controller;
    expect([ext.chat, codesOf(ext), ext.findings[0].facts.address]).toEqual(['external', ['chat_unreachable'], '192.0.2.60:8016']);
    expect(codesOf((await rig('silent').api.all()).controller)).toEqual(['chatd_silent', 'sandbox_unreachable']);
  });
  it('the refused pairing codes of the last ten minutes, then no more', async () => {
    const r = rig('all');
    const v = await r.api.all();
    expect(v.pairings.map((p) => p.finding.code)).toEqual(['wrong_code', 'code_spent', 'no_window', 'host_key_mismatch', 'rate_limited']);
    expect(v.pairings[0]).toMatchObject({ address: '192.0.2.84', port: 8000 });
    r.wait(700_000);
    expect((await r.api.all()).pairings).toEqual([]);
  });
  it('an engine host paired through Add engine is checked too, and ends the first run', async () => {
    const r = rig('first', { fleet: 'empty' });
    expect((await r.api.all()).setup.needs_setup).toBe(true);
    const send: Send = async (method, path, body) => {
      const [p, qs = ''] = path.split('?');
      const a = r.fleet.handle(method, p, new URLSearchParams(qs), body ?? null, r.now());
      return { status: a.status, body: JSON.parse(JSON.stringify(a.body)) };
    };
    const eng = enginesApi(send);
    const paired = await eng.pairCode((await eng.pairStart({ address: MOCK_ENGINES.desk })).pairing, MOCK_ENGINES.code);
    r.wait((CACHE_S + 1) * 1000);
    const v = await r.api.all();
    expect([v.setup.needs_setup, v.setup.engines, v.engines.map((e) => e.id)]).toEqual([false, 1, [paired.engine!.id]]);
    // it serves no model yet: the page opens the model step for it
    expect(v.engines[0].findings.map((f) => [f.code, f.action?.id])).toEqual([['no_model', 'pick_model']]);
    expect(codesOf(v.controller)).toEqual([]);
  });
});

describe('kept answers and their limits', () => {
  it(`an answer is kept ${CACHE_S} s and says its age; fresh checks again, but not within ${MIN_GAP_S} s of the last check`, async () => {
    const r = rig('ok');
    const a = await r.api.target('spark-desk');
    expect([a.cached, a.age_s, a.limited]).toEqual([false, 0, false]);
    r.wait(4000);
    const b = await r.api.target('spark-desk');
    expect([b.cached, b.age_s, b.limited, b.checked_at]).toEqual([true, 4, false, a.checked_at]);
    const c = await r.api.target('spark-desk', true);
    expect([c.cached, c.limited, c.checked_at]).toEqual([false, false, a.checked_at + 4]);
    r.wait(1000);
    const d = await r.api.target('spark-desk', true);
    expect([d.cached, d.limited, d.age_s]).toEqual([true, true, 1]);
    r.wait((CACHE_S + 1) * 1000);
    expect((await r.api.target('spark-desk')).cached).toBe(false);
    // the whole answer follows the same rule, target by target
    r.wait(1000);
    const all = await r.api.all(true);
    expect([all.controller.limited, all.engines[0].limited, all.engines[1].limited]).toEqual([false, true, false]);
  });
  it('a target that is not there is refused in words', async () => {
    const r = rig('ok');
    expect(await refused(r.api.target('nope'))).toMatchObject({ code: 'not_found', status: 404 });
    expect(await refused(r.api.fix('nope', { action: 'retry' }))).toMatchObject({ code: 'not_found', status: 404 });
  });
});

describe('the fixes', () => {
  const fix = (r: HealthRig, id: string, action: ActionId, o: Record<string, unknown> = {}) => r.api.fix(id, { action, ...o });

  it('an action runs only when the target’s findings offer it; a check again always', async () => {
    const r = rig('mixed');
    const no = await refused(fix(r, 'spark-desk', 'start'));
    expect([no.code, no.status, no.message, no.detail]).toEqual(['not_offered', 409, 'spark-desk does not need that fix now.', 'it offers retry']);
    const again = await fix(r, 'spark-desk', 'retry');
    expect([again.done, again.cleared, again.finding, again.guide, again.job, again.target.cached]).toEqual([true, true, null, null, null, false]);
    expect((await refused(fix(r, 'controller', 'restart_chatd'))).detail).toBe('it offers retry');
  });
  it('a body with another field, another action or another when is refused', async () => {
    const r = rig('mixed');
    const bad = async (body: unknown) => {
      const a = await r.send('POST', '/v1/fleet/health/spark-rack/fix', body).catch((e: Error) => ({ status: -1, body: e.message }));
      return [a.status, typeof a.body === 'string' ? a.body.slice(0, 40) : (a.body as { error: { code: string; detail: string } }).error.detail];
    };
    // the page's own client never sends these: the body's schema refuses them before they go
    expect((await bad({ action: 'start', force: true }))[0]).toBe(-1);
    expect((await bad({ action: 'connect_anyway' }))[0]).toBe(-1);
    const raw = (body: unknown) => r.fleet.handle('POST', '/v1/fleet/health/spark-rack/fix', new URLSearchParams(), body, r.now());
    expect([raw({ action: 'start', force: true }).status, raw({ action: 'connect_anyway' }).status, raw({ action: 'start', when: 'later' }).status, raw({}).status, raw(null).status]).toEqual([400, 400, 400, 400, 400]);
    expect((raw({ action: 'connect_anyway' }).body as { error: { detail: string } }).error.detail).toContain('action is one of retry, start');
  });
  it(`a target takes ${FIXES_PER_MIN} fixes a minute, then answers 429 until the minute is over`, async () => {
    const r = rig('all');
    for (let i = 0; i < FIXES_PER_MIN; i++) expect((await fix(r, 'spark-far', 'retry')).cleared).toBe(false);
    const no = await refused(fix(r, 'spark-far', 'retry'));
    expect([no.code, no.status, no.message, no.fix]).toEqual(['too_many_fixes', 429, 'spark-far had too many fixes in the last minute.', 'Wait a minute, then try again.']);
    // another target has its own count
    expect((await fix(r, 'spark-odd', 'retry')).done).toBe(true);
    r.wait(61_000);
    expect((await fix(r, 'spark-far', 'retry')).done).toBe(true);
  });
  it(`Start: a job, the engine loads for ${START_S} s and then serves`, async () => {
    const r = rig('mixed');
    const d = await fix(r, 'spark-rack', 'start');
    expect([d.done, d.cleared, d.finding, d.job?.kind, d.job?.state]).toEqual([true, true, null, 'start', 'acting']);
    expect([d.target.engine_state, codesOf(d.target), d.target.state]).toEqual(['loading', ['engine_loading'], 'ok']);
    r.wait((START_S + 1) * 1000);
    const t = await r.api.target('spark-rack', true);
    expect([t.engine_state, codesOf(t), t.model]).toEqual(['healthy', [], 'Qwen3.8-Flash-Next']);
  });
  it('Start that fails: the finding stays, with the new reason', async () => {
    const r = rig('mixed');
    r.mock.host('spark-rack').startFails = true;
    const before = (await r.api.target('spark-rack')).findings[0];
    expect(before.detail).toBe('it was stopped on purpose');
    const d = await fix(r, 'spark-rack', 'start');
    expect([d.cleared, d.job?.state, d.finding?.code, d.finding?.detail, d.finding?.facts.state]).toEqual([false, 'failed', 'engine_down', 'it ended with an error; its log says why', 'failed']);
  });
  it('Rotate: the new token is stored and only its hint comes back', async () => {
    const r = rig('mixed');
    const was = (await r.api.target('gpu-box')).http!.token.last4;
    const d = await fix(r, 'gpu-box', 'rotate_token');
    expect([d.cleared, d.job, Object.keys(d.result!).sort()]).toEqual([true, null, ['hint', 'rotated', 'stored']]);
    const hint = d.result!.hint as { last4: string };
    expect(Object.keys(hint).sort()).toEqual(['last4', 'rotated_at', 'set', 'sha256_6']);
    expect([hint.last4 !== was, d.target.http!.token.last4]).toEqual([true, hint.last4]);
    expect(codesOf(d.target)).toEqual([]);
  });
  it('Upgrade is two calls: a plan with its confirm, then the apply with it', async () => {
    const r = rig('mixed');
    // no release named and none set on the controller
    const no = await refused(fix(r, 'spark-attic', 'upgrade'));
    expect([no.code, no.status, no.detail]).toEqual(['bad_request', 400, 'ref is the engine release to upgrade to (a tag such as v0.16.0), or set TANDEM_ENGINE_REF']);
    const plan = await fix(r, 'spark-attic', 'upgrade', { ref: 'v0.16.0' });
    expect([plan.done, plan.needs_confirm, plan.job, plan.cleared, plan.finding?.code]).toEqual([false, true, null, false, 'engine_too_old']);
    const p = plan.result!.plan!;
    expect([plan.result!.ref, p.from?.version, p.to?.version, p.to?.ref, p.venv]).toEqual(['v0.16.0', '0.15.2', '0.16.0', 'v0.16.0', 'synced']);
    expect(p.confirm).toMatch(/^[0-9a-f]{16}$/);
    expect(p.steps!.length).toBeGreaterThan(2);
    // a confirm that is not the plan's
    expect(await refused(fix(r, 'spark-attic', 'upgrade', { ref: 'v0.16.0', confirm: '0000000000000000' }))).toMatchObject({ code: 'ctl_failed', status: 502 });
    expect((await refused(fix(r, 'spark-attic', 'upgrade', { ref: 'v0.16.0', confirm: 'nope' }))).code).toBe('bad_request');
    const d = await fix(r, 'spark-attic', 'upgrade', { ref: 'v0.16.0', confirm: p.confirm, when: 'now' });
    expect([d.done, d.needs_confirm, d.job?.kind, d.job?.state, d.job?.when?.mode, d.cleared]).toEqual([true, false, 'upgrade', 'acting', 'now', false]);
    r.wait((UPGRADE_S + 1) * 1000);
    const t = await r.api.target('spark-attic', true);
    expect([t.engine_api, t.engine_version, codesOf(t)]).toEqual([API_SPEAKS, '0.16.0', ['engine_loading']]);
    r.wait(10_000);
    expect(codesOf(await r.api.target('spark-attic', true))).toEqual([]);
  });
  it('the release this TandemApp names is used when the call names none', async () => {
    const r = rig('mixed');
    r.mock.engineRef = 'v0.16.1';
    const plan = await fix(r, 'spark-attic', 'upgrade');
    expect([plan.needs_confirm, plan.result!.ref, plan.result!.plan!.to?.version]).toEqual([true, 'v0.16.1', '0.16.1']);
  });
  it('Restart chatd waits for its job; a check again probes the backends that are down', async () => {
    const r = rig('all');
    const d = await fix(r, 'controller', 'restart_chatd');
    expect([d.done, d.cleared, d.job?.state, d.job?.action]).toEqual([true, true, 'done', 'restart']);
    const m = rig('mixed');
    const again = await fix(m, 'controller', 'retry');
    expect([again.cleared, codesOf(again.target)]).toEqual([true, ['sandbox_off', 'disk_low', 'plugin_failed']]);
    // a restart that does not bring it back
    const f = rig('all');
    f.mock.controller.restartFails = true;
    const bad = await fix(f, 'controller', 'restart_chatd');
    expect([bad.done, bad.cleared, bad.job?.state, bad.finding?.code]).toEqual([false, false, 'failed', 'chatd_down']);
  });
  it('the guides change nothing and say how', async () => {
    const r = rig('all');
    const pair = await fix(r, 'spark-moved', 're_pair');
    expect([pair.done, pair.cleared, pair.job, pair.guide!.open, pair.guide!.engine, pair.guide!.address]).toEqual([true, false, null, 'add_engine', 'spark-moved', '192.0.2.103']);
    expect([pair.guide!.pinned, pair.guide!.offered].every((x) => /^SHA256:/.test(x ?? ''))).toBe(true);
    expect(pair.guide!.steps).toHaveLength(3);
    expect(pair.guide!.steps[0]).toContain('`tandem pair`');
    const model = await fix(r, 'spark-empty', 'pick_model');
    expect([model.guide!.open, model.guide!.engine, model.guide!.steps.length]).toEqual(['model', 'spark-empty', 1]);
    const f = rig('first', { fleet: 'empty' });
    const add = await fix(f, 'controller', 'add_engine');
    expect([add.guide!.open, add.guide!.engine, add.guide!.steps.length]).toEqual(['add_engine', null, 2]);
  });
  it('a fix over SSH needs SSH: the control’s own refusal comes back; a switch makes any fix refuse', async () => {
    const r = rig('mixed');
    r.mock.host('spark-rack');
    // the engine is down and SSH goes away between the read and the fix
    await r.api.target('spark-rack');
    r.mock.hosts.find((h) => h.id === 'spark-rack')!.ssh = 'unreachable';
    const no = await refused(fix(r, 'spark-rack', 'start'));
    expect([no.code, no.status, no.host, no.port]).toEqual(['ssh_unreachable', 502, '192.0.2.81', 22]);
    const m = rig('mixed');
    m.mock.fail('rotate_token', 'busy');
    expect(await refused(fix(m, 'gpu-box', 'rotate_token'))).toMatchObject({ code: 'busy', status: 409 });
    m.mock.fail('rotate_token', null);
    expect((await fix(m, 'gpu-box', 'rotate_token')).cleared).toBe(true);
  });
  it('a condition that passes by itself: SSH back on the next check, an engine that finished loading', async () => {
    const r = rig('all');
    Object.assign(r.mock.host('spark-far'), { sshBackAfter: 1, http: 'ok' });
    expect(codesOf(await r.api.target('spark-far'))).toEqual(['ssh_unreachable']);
    const d = await fix(r, 'spark-far', 'retry');
    expect([d.cleared, codesOf(d.target)]).toEqual([true, []]);
    r.mock.host('spark-loading').readyAt = r.now() / 1000 + 30;
    expect(codesOf(await r.api.target('spark-loading'))).toEqual(['engine_loading']);
    r.wait(31_000);
    expect(codesOf(await r.api.target('spark-loading'))).toEqual([]);
  });
  it('the mock’s switches: a scenario, a host’s condition, the controller’s, a release', () => {
    const r = rig('ok');
    expect(r.mock.control({ scenario: 'mixed' })).toMatchObject({ scenario: 'mixed', hosts: ['spark-desk', 'spark-rack', 'gpu-box', 'spark-attic'] });
    r.mock.control({ host: { id: 'spark-desk', engine: 'down' }, controller: { freeGb: 0.2 }, ref: 'v0.16.0', fail: 'start:busy' });
    expect([r.mock.host('spark-desk').engine, r.mock.controller.freeGb, r.mock.engineRef]).toEqual(['down', 0.2, 'v0.16.0']);
    expect(r.mock.control({ fail: 'start:' })).toMatchObject({ fail: {} });
    expect(() => r.mock.control({ scenario: 'nope' })).toThrow(/scenario is one of/);
    expect(() => r.mock.control({ fail: 'connect:busy' })).toThrow(/fail is <action>:<code>/);
    for (const s of HEALTH_SCENARIOS) expect(r.mock.control({ scenario: s as HealthScenario }).scenario).toBe(s);
  });
});
