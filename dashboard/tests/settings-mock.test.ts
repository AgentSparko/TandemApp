// Tests for VIS-280: the mock of the settings interface (mock/settings.ts) answers every route of
// contracts/settings-v1 in the contract's shapes (the rig checks each answer against its schema and
// each refusal's status against scopes.json), refuses what the controller refuses, never answers a
// secret, applies a change set in order and stops at the first failure, and moves a restart along
// the clock: scheduled, draining (the gateway answers 503 with Retry-After), restarting, warming,
// done; failed with the version restored; timeout; cancelled.

import { describe, expect, it } from 'vitest';
import { SETTINGS_ERRORS } from '../mock/settings.ts';
import { MockFleet } from '../mock/fleet.ts';
import type { Sent } from '../src/api/engines.ts';
import type { AuditRow, Overview, Plan, Restart, Result, SchemaView, ValuesView, VersionsView } from '../src/api/settings.ts';
import { ERRORS, ROUTES, SECRETS, T0, rig, valid, type Rig } from './settings-rig.ts';

const get = (r: Rig, path = '') => r.send('GET', `/v1/settings${path}`);
const post = (r: Rig, path: string, body: unknown) => r.send('POST', `/v1/settings${path}`, body);
const refused = (s: Sent): [number, string] => [s.status, (s.body as { error: { type: string } }).error.type];
const ok = <T>(s: Sent): T => {
  if (s.status !== 200) throw new Error(`${s.status}: ${JSON.stringify(s.body)}`);
  return s.body as T;
};
const valueOf = async (r: Rig, scope: string, key: string, target?: string) => ok<ValuesView>(await get(r, `/${scope}${target ? `?target=${target}` : ''}`)).values.find((v) => v.key === key)!;
/** plan a change set and apply it with the plan's confirms */
async function change(r: Rig, changes: unknown[], extra: Record<string, unknown> = {}): Promise<Result> {
  const plan = ok<Plan>(await post(r, '/plan', { changes }));
  return ok<Result>(await post(r, '/apply', { changes, confirm: plan.confirm, ...(plan.confirm_fingerprint ? { confirm_fingerprint: plan.confirm_fingerprint } : {}), ...extra }));
}
const QUEUE = [{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '32' } }];

describe('the mock answers the contract', () => {
  it('the same error table as the contract, and every route of it', async () => {
    expect(SETTINGS_ERRORS).toEqual(ERRORS);
    expect(ROUTES.map((x) => `${x.method} ${x.path}`)).toEqual([
      'GET /v1/settings', 'GET /v1/settings/{scope}/schema', 'GET /v1/settings/{scope}', 'GET /v1/settings/{scope}/versions', 'POST /v1/settings/{scope}/plan', 'POST /v1/settings/{scope}/apply',
      'POST /v1/settings/{scope}/restore', 'POST /v1/settings/plan', 'POST /v1/settings/apply', 'GET /v1/settings/restarts', 'GET /v1/settings/restarts/{id}', 'POST /v1/settings/restarts/{id}/cancel', 'GET /v1/settings/audit',
    ]);
    const r = rig();
    const o = ok<Overview>(await get(r));
    expect(o.order).toEqual(['app', 'backends', 'conductor', 'engine', 'hooks']);
    expect(o.scopes.find((s) => s.scope === 'engine')!.targets).toEqual([
      { id: 'eh-desk', label: 'spark-desk', writable: true, why: null, transport: 'ssh' },
      { id: 'http:bench', label: 'bench', writable: false, why: 'reached over HTTP only: change its settings on its own host (tandem remote settings), or pair it over SSH', transport: 'http' },
    ]);
    // the three reads of every scope and target
    for (const s of o.scopes)
      for (const t of s.targets) {
        const q = s.scope === 'engine' ? `?target=${encodeURIComponent(t.id)}` : '';
        const sch = ok<SchemaView>(await get(r, `/${s.scope}/schema${q}`));
        const vals = ok<ValuesView>(await get(r, `/${s.scope}${q}`));
        expect(vals.values.map((v) => v.key).sort(), `${s.scope}/${t.id}`).toEqual(sch.keys.map((k) => k.key).sort());
        // a secret's value is never answered
        for (const k of sch.keys.filter((x) => x.secret)) expect(vals.values.find((v) => v.key === k.key)!.value, k.key).toBeNull();
        if (t.writable) ok<VersionsView>(await get(r, `/${s.scope}/versions${q}`));
      }
    // one scope's own plan, apply and restore
    const plan = ok<Plan>(await post(r, '/app/plan', { set: { conductor: 'on' } }));
    expect([plan.parts.length, plan.changed, plan.restart, plan.restart_needed, plan.confirm_fingerprint]).toEqual([1, true, 'none', false, null]);
    const res = ok<Result>(await post(r, '/app/apply', { set: { conductor: 'on' }, confirm: plan.confirm }));
    expect([res.state, res.parts[0].state, res.parts[0].version_before, res.parts[0].version_after, res.parts[0].restore]).toEqual(['applied', 'applied', '1', '2', [{ scope: 'app', target: 'controller', version: '1' }]]);
    const back = ok<Plan>(await post(r, '/app/restore', { version: '1' }));
    expect([back.parts[0].kind, back.parts[0].version, back.parts[0].changes.map((c) => [c.key, c.from, c.to])]).toEqual(['restore', '1', [['conductor', 'on', 'off']]]);
    expect(ok<Result>(await post(r, '/app/restore', { version: '1', confirm: back.confirm })).parts[0].kind).toBe('restore');
    expect((await valueOf(r, 'app', 'conductor')).value).toBe('off');
    ok(await get(r, '/restarts'));
    ok(await get(r, '/audit'));
  });

  it('a part’s refusal names its scope and target besides the key', async () => {
    const r = rig();
    const facts = (x: { body: unknown }) => {
      const e = (x.body as { error: { type: string; scope?: string; target?: string; key?: string } }).error;
      return [e.type, e.scope, e.target, e.key];
    };
    expect(facts(await post(r, '/plan', { changes: [...QUEUE.map((c) => ({ ...c, set: { MAX_QUEUE: 'many' } }))] }))).toEqual(['bad_value', 'engine', 'eh-desk', 'MAX_QUEUE']);
    expect(facts(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { PORT: '9000' } }] }))).toEqual(['locked', 'engine', 'eh-desk', 'PORT']);
    expect(facts(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { NOPE_KEY: '1' } }] }))).toEqual(['unknown_key', 'engine', 'eh-desk', 'NOPE_KEY']);
    expect(facts(await post(r, '/plan', { changes: [...QUEUE, { scope: 'backends', set: { 'backends.cloud.max_inflight': 0 } }] }))).toEqual(['bad_value', 'backends', 'controller', 'backends.cloud.max_inflight']);
    expect(facts(await post(r, '/plan', { changes: [{ scope: 'conductor', set: { router: { default: 'main' } } }] }))).toEqual(['invalid_document', 'conductor', 'controller', 'router']);
  });

  it('an unset of a key that is unset already is no change; the backends say set or unset truthfully', async () => {
    const r = rig();
    const inflight = await valueOf(r, 'backends', 'backends.engine.max_inflight');
    expect([inflight.value, inflight.set]).toEqual([null, false]);
    expect((await valueOf(r, 'backends', 'backends.cloud.max_inflight')).set).toBe(true);
    for (const c of [
      { scope: 'backends', unset: ['backends.engine.max_inflight'] },
      { scope: 'backends', set: { 'backends.engine.host.url': null } },
      { scope: 'app', unset: ['chat'] },
      { scope: 'engine', target: 'eh-desk', unset: ['QWEN38_PREFIX_CHUNK'] },
    ]) {
      const plan = ok<Plan>(await post(r, '/plan', { changes: [c] }));
      expect([plan.changed, plan.parts[0].changes], JSON.stringify(c)).toEqual([false, []]);
      expect(ok<Result>(await post(r, '/apply', { changes: [c] })).state).toBe('unchanged');
    }
    // beside a set, the unset is dropped and the set plans
    const both = ok<Plan>(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '18' }, unset: ['QWEN38_PREFIX_CHUNK'] }] }));
    expect([both.parts[0].changes.map((c) => c.key), both.confirm_fingerprint]).toEqual([['MAX_QUEUE'], null]);
  });

  it('refuses what the controller refuses, each with the contract’s status', async () => {
    const r = rig();
    expect(refused(await get(r, '/memory'))).toEqual([404, 'not_found']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'memory', set: { a: 1 } }] }))).toEqual([404, 'no_scope']);
    expect(refused(await get(r, '/engine/schema'))).toEqual([400, 'target_required']);
    expect(refused(await get(r, '/engine?target=eh-gone'))).toEqual([404, 'no_target']);
    expect(refused(await post(r, '/plan', { changes: [] }))).toEqual([400, 'bad_request']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'app', set: { chat: 'on' } }, { scope: 'app', set: { conductor: 'on' } }] }))).toEqual([400, 'bad_request']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'app', set: { chat: 'on' }, restore: '1' }] }))).toEqual([400, 'bad_request']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'app', set: { memory: 'on' } }] }))).toEqual([422, 'unknown_key']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'app', set: { chat: 'maybe' } }] }))).toEqual([422, 'bad_value']);
    // a field the environment sets names its variable
    const locked = await post(r, '/plan', { changes: [{ scope: 'backends', set: { 'backends.engine.url': 'http://192.0.2.9/v1' } }] });
    expect(refused(locked)).toEqual([422, 'locked']);
    expect((locked.body as { error: { key: string; variable: string } }).error).toMatchObject({ key: 'backends.engine.url', variable: 'TANDEM_BACKENDS' });
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'backends', set: { 'backends.cloud.url': 'ftp://nope' } }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'backends', set: { 'backends.cloud.fallback': ['cloud'] } }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'backends', unset: ['routes'] }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'backends', set: { routes: [{ pattern: 'x-*', backend: 'cloud', enabled: true }] } }] }))).toEqual([422, 'bad_value']); // the environment's rule is gone
    // the conductor: a document that does not check, a locked profile, a document that is none
    const invalid = await post(r, '/plan', { changes: [{ scope: 'conductor', set: { router: { fallback: 'main' } } }] });
    expect(refused(invalid)).toEqual([422, 'invalid_document']);
    expect((invalid.body as { error: { errors: string[] } }).error.errors).toHaveLength(2);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'conductor', set: { 'profiles.safe': { writer: 'on' } } }] }))).toEqual([422, 'locked']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'conductor', set: { memory: {} } }] }))).toEqual([422, 'unknown_key']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'conductor', set: { router: 'main' } }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'conductor', restore: 'router@9' }] }))).toEqual([404, 'no_version']);
    // an engine: a key it does not manage, a wrong value, an engine reached over HTTP only
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { PORT: '9000' } }] }))).toEqual([422, 'locked']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: 'many' } }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: true } }] }))).toEqual([422, 'bad_value']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { NOPE: '1' } }] }))).toEqual([422, 'unknown_key']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'http:bench', set: { QSE_ALLOW_FROM: '' } }] }))).toEqual([409, 'read_only']);
    expect(refused(await get(r, '/engine/versions?target=http%3Abench'))).toEqual([409, 'read_only']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', restore: '9' }] }))).toEqual([404, 'no_version']);
    // an apply: no confirm, another plan's, a bad when, an unknown field
    const none = await post(r, '/apply', { changes: QUEUE });
    expect(refused(none)).toEqual([428, 'confirm_required']);
    valid('plan', (none.body as { error: { plan: Plan } }).error.plan);
    expect(refused(await post(r, '/apply', { changes: QUEUE, confirm: 'f'.repeat(32) }))).toEqual([409, 'stale_plan']);
    expect(refused(await post(r, '/apply', { changes: QUEUE, confirm: 'x', when: 'soon' }))).toEqual([400, 'bad_request']);
    expect(refused(await post(r, '/apply', { changes: QUEUE, confirm: 'x', when: 'now', deadline_s: 60 }))).toEqual([400, 'bad_request']);
    expect(refused(await post(r, '/apply', { changes: QUEUE, confirm: 'x', force: true }))).toEqual([400, 'bad_request']);
    expect(refused(await get(r, '/audit?change_set=nope'))).toEqual([400, 'bad_request']);
    expect(refused(await get(r, '/restarts/rs-nope'))).toEqual([404, 'no_restart']);
    expect(refused(await post(r, '/restarts/rs-nope/cancel', {}))).toEqual([404, 'no_restart']);
    // an engine host that does not answer over SSH
    r.mock.control({ unreachable: 'eh-desk' });
    expect(refused(await get(r, '/engine/schema?target=eh-desk'))).toEqual([502, 'engine_unreachable']);
    r.mock.control({ unreachable: null });
    ok(await get(r, '/engine/schema?target=eh-desk'));
  });

  it('every error type of the contract can be made to answer, with its status', async () => {
    const r = rig();
    for (const [type, status] of Object.entries(ERRORS)) {
      r.mock.control({ fail: `overview:${type}` });
      // the rig holds the answer to error.schema.json and the status to scopes.json
      expect(refused(await get(r)), type).toEqual([status, type]);
    }
    r.mock.control({ fail: null });
    ok(await get(r));
    expect(() => r.mock.control({ fail: 'plan:not_an_error' })).toThrow('no settings error not_an_error');
  });
});

describe('secrets', () => {
  it('a key is set and unset, and no answer, version or audit row holds it', async () => {
    const r = rig();
    r.mock.control({ hooks: true });
    const changes = [
      { scope: 'backends', set: { 'backends.cloud.key': SECRETS[0], 'backends.cloud.max_inflight': 4 } },
      { scope: 'hooks', set: { 'hooks.audit.token': SECRETS[1] } },
    ];
    const plan = ok<Plan>(await post(r, '/plan', { changes }));
    const key = plan.parts[0].changes.find((c) => c.key === 'backends.cloud.key')!;
    expect([key.secret, key.from, key.to, key.was_set, key.is_set]).toEqual([true, null, null, true, true]);
    const res = ok<Result>(await post(r, '/apply', { changes, confirm: plan.confirm }));
    expect(res.parts.map((p) => p.state)).toEqual(['applied', 'applied']);
    const stored = await valueOf(r, 'backends', 'backends.cloud.key');
    expect([stored.value, stored.set, typeof stored.hint]).toEqual([null, true, 'string']);
    expect((await valueOf(r, 'hooks', 'hooks.audit.token')).set).toBe(true);
    // versions keep every key but the secrets; a restore says they stay as they are
    const vers = ok<VersionsView>(await get(r, '/backends/versions'));
    expect(vers.versions[0].keys).toContain('backends.cloud.key');
    expect(Object.keys(vers.versions[0].values!)).not.toContain('backends.cloud.key');
    const back = ok<Plan>(await post(r, '/backends/restore', { version: '1' }));
    expect(back.parts[0].warnings).toContain('secrets are not kept in versions: they stay as they are');
    expect(back.parts[0].changes.map((c) => c.key)).toEqual(['backends.cloud.max_inflight']);
    // unset: the key is gone
    await change(r, [{ scope: 'backends', unset: ['backends.cloud.key'] }]);
    expect((await valueOf(r, 'backends', 'backends.cloud.key')).set).toBe(false);
    ok<{ rows: AuditRow[] }>(await get(r, '/audit'));
    for (const s of SECRETS) for (const a of r.seen) expect(a.includes(s), a.slice(0, 80)).toBe(false);
  });
});

describe('a change set', () => {
  it('applies in the scopes’ order, binds its confirm to the state, and answers the same apply again', async () => {
    const r = rig();
    // given out of order: planned and applied in the order of the scopes
    const changes = [
      { scope: 'engine', target: 'eh-desk', set: { QSE_ALLOW_FROM: '192.0.2.0/24' } },
      { scope: 'conductor', set: { router: { default: 'side', rules: [] } } },
      { scope: 'app', set: { conductor: 'on' } },
    ];
    const plan = ok<Plan>(await post(r, '/plan', { changes }));
    expect(plan.parts.map((p) => [p.scope, p.restart, p.restart_needed])).toEqual([['app', 'none', false], ['conductor', 'hot', false], ['engine', 'hot', false]]);
    expect(plan.change_set).toMatch(/^cs-[0-9a-f]{12}$/);
    expect([plan.restart, plan.restart_needed, plan.needs_confirm]).toEqual(['hot', false, true]);
    // the conductor gives the diff, and says it is off
    const doc = plan.parts[1].changes[0];
    expect([doc.from, doc.to, doc.diff_count]).toEqual([{ version: 1 }, { version: 'new' }, 2]);
    expect(doc.diff!.map((d) => d.path)).toEqual(['default', 'rules']);
    expect(plan.parts[1].warnings).toEqual(['the conductor is off: the documents are stored and read when it is switched on']);
    // a plan writes nothing
    expect((await valueOf(r, 'app', 'conductor')).value).toBe('off');
    const res = ok<Result>(await post(r, '/apply', { changes, confirm: plan.confirm, note: 'evening setup' }));
    expect([res.state, res.replayed, res.applied, res.failed, res.not_run]).toEqual(['applied', false, ['app/controller', 'conductor/controller', 'engine/eh-desk'], null, []]);
    // hot keys only: the engine takes them without a restart
    expect([res.parts[2].state, res.parts[2].restart, res.parts[2].version_before, res.parts[2].version_after]).toEqual(['applied', null, '1', '2']);
    expect(res.parts[1].restore).toEqual([{ scope: 'conductor', target: 'controller', version: 'router@1' }]);
    expect((await valueOf(r, 'engine', 'QSE_ALLOW_FROM', 'eh-desk')).source).toBe('managed');
    // the same confirm with the same body again: the same answer, nothing applied twice
    const again = ok<Result>(await post(r, '/apply', { changes, confirm: plan.confirm, note: 'evening setup' }));
    expect([again.replayed, again.change_set]).toEqual([true, res.change_set]);
    expect(ok<VersionsView>(await get(r, '/engine/versions?target=eh-desk')).versions.map((v) => v.id)).toEqual(['2', '1', '0']);
    // the audit: a row a part and one for the set, all with the change set's id, keys never values
    const rows = ok<{ rows: AuditRow[] }>(await get(r, `/audit?change_set=${res.change_set}`)).rows;
    expect(rows.map((x) => [x.action, x.object_id, x.result]).reverse()).toEqual([
      ['settings.apply', 'app/controller', 'applied'], ['settings.apply', 'conductor/controller', 'applied'], ['settings.apply', 'engine/eh-desk', 'applied'], ['settings.change_set', null, 'applied'],
    ]);
    expect(rows[0].keys).toEqual(['QSE_ALLOW_FROM', 'conductor', 'router']);
    // what holds already is unchanged, and needs no confirm
    const same = ok<Plan>(await post(r, '/plan', { changes: [{ scope: 'app', set: { conductor: 'on' } }] }));
    expect([same.changed, same.confirm, same.parts[0].changes]).toEqual([false, null, []]);
    expect(ok<Result>(await post(r, '/apply', { changes: [{ scope: 'app', set: { conductor: 'on' } }] })).state).toBe('unchanged');
  });

  it('a confirm that no longer matches is stale and comes with the new plan; busy while another applies', async () => {
    const r = rig();
    const plan = ok<Plan>(await post(r, '/plan', { changes: QUEUE }));
    r.mock.control({ move: true });
    const stale = await post(r, '/apply', { changes: QUEUE, confirm: plan.confirm });
    expect(refused(stale)).toEqual([409, 'stale_plan']);
    const next = (stale.body as { error: { plan: Plan } }).error.plan;
    valid('plan', next);
    expect(next.confirm).not.toBe(plan.confirm);
    r.mock.control({ busy: true });
    expect(refused(await post(r, '/apply', { changes: QUEUE, confirm: next.confirm }))).toEqual([409, 'busy']);
    r.mock.control({ busy: false });
    expect(ok<Result>(await post(r, '/apply', { changes: QUEUE, confirm: next.confirm })).parts[0].state).toBe('scheduled');
  });

  it('a key that feeds the disk cache’s fingerprint asks for a second confirm', async () => {
    const r = rig();
    const changes = [{ scope: 'engine', target: 'eh-desk', set: { QWEN38_PREFIX_CHUNK: '4096' } }];
    const plan = ok<Plan>(await post(r, '/plan', { changes }));
    expect([plan.fingerprint_keys, typeof plan.confirm_fingerprint, plan.parts[0].changes[0].fingerprint]).toEqual([['engine/eh-desk:QWEN38_PREFIX_CHUNK'], 'string', true]);
    expect(plan.parts[0].warnings).toEqual(['QWEN38_PREFIX_CHUNK feeds the disk cache’s fingerprint']);
    const first = await post(r, '/apply', { changes, confirm: plan.confirm });
    expect(refused(first)).toEqual([428, 'fingerprint_confirm_required']);
    expect(refused(await post(r, '/apply', { changes, confirm: plan.confirm, confirm_fingerprint: 'f'.repeat(32) }))).toEqual([409, 'stale_plan']);
    expect(ok<Result>(await post(r, '/apply', { changes, confirm: plan.confirm, confirm_fingerprint: plan.confirm_fingerprint })).parts[0].state).toBe('scheduled');
  });

  it('the first failure stops the set: what was applied stays and says how to restore it, the rest is not run', async () => {
    const r = rig();
    r.mock.control({ hooks: true, fail: 'apply-engine:engine_unreachable' });
    const changes = [{ scope: 'app', set: { chat: 'on' } }, ...QUEUE, { scope: 'hooks', set: { 'hooks.audit.mode': 'enforce' } }];
    const res = await change(r, changes);
    expect([res.state, res.applied, res.failed, res.not_run]).toEqual(['partial', ['app/controller'], 'engine/eh-desk', ['hooks/controller']]);
    expect(res.parts.map((p) => p.state)).toEqual(['applied', 'failed', 'not_run']);
    expect(res.parts[1].error!.type).toBe('engine_unreachable');
    expect(res.parts[0].restore).toEqual([{ scope: 'app', target: 'controller', version: '1' }]);
    expect(r.parts.on.chat).toBe(true);
    // planned again: what holds is unchanged, the rest is planned anew
    r.mock.control({ fail: null });
    const plan = ok<Plan>(await post(r, '/plan', { changes }));
    expect(plan.parts.map((p) => p.changed)).toEqual([false, true, true]);
    // a set whose first part fails applies nothing
    r.mock.control({ fail: 'apply-app:store_refused' });
    const none = await change(r, [{ scope: 'app', set: { chat: 'off' } }, { scope: 'hooks', set: { 'hooks.audit.mode': 'enforce' } }]);
    expect([none.state, none.applied, none.parts.map((p) => p.state)]).toEqual(['failed', [], ['failed', 'not_run']]);
  });

  it('a version is restored through a plan: a new version equal to the old one', async () => {
    const r = rig();
    // the conductor's router has a stored version: unset goes back to the built-in one
    const off = await change(r, [{ scope: 'conductor', unset: ['router'] }]);
    expect([off.parts[0].version_before, off.parts[0].version_after]).toEqual(['router@1', 'router@2']);
    const back = await change(r, [{ scope: 'conductor', restore: 'router@1' }]);
    expect([back.parts[0].kind, back.parts[0].version_after]).toEqual(['restore', 'router@3']);
    const router = await valueOf(r, 'conductor', 'router');
    expect([(router.value as { rules: unknown[] }).rules.length, router.version, router.source]).toEqual([2, 'router@3', 'stored']);
    const vers = ok<VersionsView>(await get(r, '/conductor/versions')).versions.filter((v) => v.keys[0] === 'router');
    expect(vers.map((v) => [v.id, v.current])).toEqual([['router@3', true], ['router@2', false], ['router@1', false], ['router@0', false]]);
    // an engine's overlay: a restore restarts it, as the change did
    const plan = ok<Plan>(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', restore: '0' }] }));
    expect([plan.parts[0].kind, plan.parts[0].restart, plan.parts[0].changes.map((c) => [c.key, c.was_set, c.is_set])]).toEqual(['restore', 'engine', [['MAX_QUEUE', true, false]]]);
    // the version that holds restores nothing
    expect(ok<Plan>(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', restore: '1' }] })).changed).toBe(false);
  });
});

describe('an engine restart on the clock', () => {
  const at = async (r: Rig, id: string) => ok<Restart>(await get(r, `/restarts/${id}`));

  it('draining with its countdown, restarting, warming, done: the new version holds and one row says so', async () => {
    const r = rig();
    const res = await change(r, QUEUE);
    expect([res.state, res.parts[0].state, res.parts[0].version_after]).toEqual(['applied', 'scheduled', null]);
    const rs = res.parts[0].restart!;
    expect([rs.state, rs.remaining_s, rs.deadline_s, rs.when, rs.can_cancel, rs.draining, rs.backends, rs.alias, rs.kind]).toEqual(['draining', 600, 600, 'idle', true, true, ['engine'], 'spark-desk', 'settings']);
    expect(rs.job!.id).toMatch(/^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$/);
    // while it drains the gateway answers 503 with Retry-After for the backend on that host
    expect(r.mock.gateway('engine', r.now())).toMatchObject({ status: 503, headers: { 'Retry-After': '30' }, body: { error: { type: 'node_draining' } } });
    expect(r.mock.gateway('cloud', r.now())).toBeNull();
    // nothing is written before the restart acts
    expect((await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk')).value).toBe('16');
    r.wait(4000);
    expect([(await at(r, rs.id)).state, (await at(r, rs.id)).remaining_s]).toEqual(['draining', 596]);
    r.wait(3000);
    const acting = await at(r, rs.id);
    expect([acting.state, acting.can_cancel, acting.remaining_s, acting.job!.state]).toEqual(['restarting', false, null, 'acting']);
    r.wait(4000);
    expect([(await at(r, rs.id)).state, (await at(r, rs.id)).draining]).toEqual(['warming', true]);
    r.wait(3000);
    const done = await at(r, rs.id);
    expect([done.state, done.draining, done.warm, done.message, typeof done.ended_at]).toEqual(['done', false, { ok: true, backends: { engine: { ok: true, why: null } } }, 'the engine runs the new settings', 'string']);
    expect(r.mock.gateway('engine', r.now())).toBeNull();
    const q = await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk');
    expect([q.value, q.source]).toEqual(['32', 'managed']);
    expect(ok<{ restarts: Restart[] }>(await get(r, '/restarts')).restarts).toHaveLength(1);
    const rows = ok<{ rows: AuditRow[] }>(await get(r, `/audit?change_set=${res.change_set}`)).rows;
    expect(rows.map((x) => [x.action, x.result])).toEqual([['settings.restart', 'done'], ['settings.change_set', 'applied'], ['settings.apply', 'scheduled']]);
    // while one job waits on an engine host, a second one is refused there
    const busy = rig();
    await change(busy, QUEUE);
    const second = await change(busy, [{ scope: 'engine', target: 'eh-desk', set: { mode: 'chat' } }]);
    expect([second.state, second.parts[0].error!.type]).toEqual(['failed', 'engine_busy']);
  });

  it('cancel while it waits: the engine keeps its settings and the gateway serves again; later it is too late', async () => {
    const r = rig();
    const rs = (await change(r, QUEUE)).parts[0].restart!;
    r.wait(2000);
    expect(r.mock.gateway('engine', r.now())!.status).toBe(503);
    const cancelled = ok<Restart>(await post(r, `/restarts/${rs.id}/cancel`, {}));
    expect([cancelled.state, cancelled.draining, cancelled.can_cancel, cancelled.message]).toEqual(['cancelled', false, false, 'cancelled by admin: the engine keeps the settings it runs']);
    expect(r.mock.gateway('engine', r.now())).toBeNull();
    r.wait(60_000);
    expect((await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk')).value).toBe('16');
    expect(refused(await post(r, `/restarts/${rs.id}/cancel`, {}))).toEqual([409, 'ended']);
    const rows = ok<{ rows: AuditRow[] }>(await get(r, '/audit')).rows.map((x) => [x.action, x.result]);
    expect(rows.slice(0, 2)).toEqual([['settings.restart', 'cancelled'], ['settings.cancel', 'cancelled']]);
    // once the job acts it cannot be cancelled
    const late = (await change(r, QUEUE)).parts[0].restart!;
    r.wait(7000);
    expect(refused(await post(r, `/restarts/${late.id}/cancel`, {}))).toEqual([409, 'too_late']);
  });

  it('a start that fails puts the last good version back; a deadline that passes changes nothing', async () => {
    const r = rig();
    r.mock.control({ restart: 'fail' });
    const res = await change(r, QUEUE);
    r.wait(11_000);
    const failed = await at(r, res.parts[0].restart!.id);
    expect([failed.state, failed.restored, failed.draining, typeof failed.message]).toEqual(['failed', { version: 1, healthy: true }, false, 'string']);
    expect((await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk')).value).toBe('16');
    expect(ok<VersionsView>(await get(r, '/engine/versions?target=eh-desk')).current).toBe('1');
    // the result it belonged to holds no longer: the same change applied again is a new apply
    const again = await change(r, QUEUE);
    expect([again.replayed, again.parts[0].state]).toEqual([false, 'scheduled']);
    await post(r, `/restarts/${again.parts[0].restart!.id}/cancel`, {});
    // the engine stays busy past the deadline
    r.mock.control({ restart: 'timeout' });
    const slow = (await change(r, QUEUE, { deadline_s: 30 })).parts[0].restart!;
    expect([slow.deadline_s, slow.remaining_s]).toEqual([30, 30]);
    r.wait(29_000);
    expect([(await at(r, slow.id)).state, (await at(r, slow.id)).remaining_s]).toEqual(['draining', 1]);
    r.wait(2000);
    const out = await at(r, slow.id);
    expect([out.state, out.draining, out.restored]).toEqual(['timeout', false, null]);
    expect((await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk')).value).toBe('16');
    expect(r.mock.gateway('engine', r.now())).toBeNull();
  });

  it('a set time waits without draining; now acts at once', async () => {
    const r = rig();
    const when = `at=${new Date(T0 + 60_000).toISOString().replace(/\.\d{3}Z$/, 'Z')}`;
    const rs = (await change(r, QUEUE, { when, deadline_s: 120 })).parts[0].restart!;
    expect([rs.state, rs.when, rs.remaining_s, rs.draining, rs.backends, rs.can_cancel, rs.deadline_at]).toEqual(['scheduled', when, null, false, [], true, null]);
    expect(r.mock.gateway('engine', r.now())).toBeNull();
    r.wait(61_000);
    const drain = await at(r, rs.id);
    expect([drain.state, drain.remaining_s, drain.draining]).toEqual(['draining', 119, true]);
    await post(r, `/restarts/${rs.id}/cancel`, {});
    const now = (await change(r, QUEUE, { when: 'now' })).parts[0].restart!;
    expect([now.state, now.deadline_s, now.can_cancel, now.remaining_s]).toEqual(['restarting', null, false, null]);
  });
});

describe('the engine’s mode', () => {
  const MODE = [{ scope: 'engine', target: 'eh-desk', set: { mode: 'chat' } }];
  const at = async (r: Rig, id: string) => ok<Restart>(await get(r, `/restarts/${id}`));
  it('chat installs TandemApp’s wheel first and restarts once, as a restart of kind mode; then it holds', async () => {
    const r = rig();
    const sch = ok<SchemaView>(await get(r, '/engine/schema?target=eh-desk'));
    expect(sch.keys.slice(0, 2).map((k) => [k.key, k.type, k.restart, k.default])).toEqual([['mode', 'enum', 'engine', 'engine'], ['mode.ext', 'module', 'engine', 'tandemapp.engine_ext']]);
    expect(sch.keys.some((k) => k.key === 'QSE_MODE' || k.key === 'QSE_EXT')).toBe(false);
    const plan = ok<Plan>(await post(r, '/plan', { changes: MODE }));
    expect(plan.parts[0].changes.map((c) => [c.key, c.from, c.to])).toEqual([['mode', 'engine', 'chat'], ['mode.ext', null, 'tandemapp.engine_ext']]);
    expect(plan.parts[0].warnings).toEqual(['the engine host has no tandemapp.engine_ext that loads: TandemApp installs its own wheel first (tandemapp-0.15.0-py3-none-any.whl, 4 MB)']);
    expect(plan.parts[0].restart_estimate).toEqual({ seconds: 180, source: 'default' });
    const res = await change(r, MODE);
    const rs = res.parts[0].restart!;
    expect([res.state, res.parts[0].state, rs.kind, rs.state]).toEqual(['applied', 'scheduled', 'mode', 'draining']);
    r.wait(20_000);
    expect((await at(r, rs.id)).state).toBe('done');
    expect([(await valueOf(r, 'engine', 'mode', 'eh-desk')).value, (await valueOf(r, 'engine', 'mode.ext', 'eh-desk')).value]).toEqual(['chat', 'tandemapp.engine_ext']);
    const again = ok<Plan>(await post(r, '/plan', { changes: MODE }));
    expect([again.changed, again.confirm]).toEqual([false, null]);
    // back to the bare engine, then chat again: the extension is there, no install
    await change(r, [{ scope: 'engine', target: 'eh-desk', set: { mode: 'engine' } }]);
    r.wait(20_000);
    const later = ok<Plan>(await post(r, '/plan', { changes: MODE })).parts[0];
    expect([later.warnings, later.restart_estimate]).toEqual([[], { seconds: 7, source: 'measured' }]);
    // a hot change and a store's change have none
    const hot = ok<Plan>(await post(r, '/plan', { changes: [{ scope: 'app', set: { chat: 'on' } }, { scope: 'engine', target: 'eh-desk', set: { QSE_ALLOW_FROM: '192.0.2.0/24' } }] }));
    expect(hot.parts.map((x) => x.restart_estimate)).toEqual([null, null]);
    // the versions show the mode's keys as the page knows them
    const v = ok<VersionsView>(await get(r, '/engine/versions?target=eh-desk')).versions[0];
    expect([v.keys, v.values]).toEqual([['MAX_QUEUE', 'mode', 'mode.ext'], { MAX_QUEUE: '16', mode: 'engine', 'mode.ext': null }]);
  });

  it('no wheel here or a foreign module: refused on mode.ext with the fix; QSE_MODE itself is locked; with another key one overlay change', async () => {
    const r = rig();
    r.mock.control({ wheel: false });
    const none = await post(r, '/plan', { changes: MODE });
    expect(refused(none)).toEqual([409, 'extension_missing']);
    expect((none.body as { error: { key: string; message: string } }).error.key).toBe('mode.ext');
    expect((none.body as { error: { message: string } }).error.message).toContain('TANDEM_NODE_WHEEL');
    r.mock.control({ wheel: true });
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { mode: 'chat', 'mode.ext': 'acme.ext' } }] }))).toEqual([409, 'extension_missing']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { QSE_MODE: 'chat' } }] }))).toEqual([422, 'locked']);
    expect(refused(await post(r, '/plan', { changes: [{ scope: 'engine', target: 'eh-desk', set: { mode: 'engine', 'mode.ext': 'x.y' } }] }))).toEqual([422, 'bad_value']);
    const both = [{ scope: 'engine', target: 'eh-desk', set: { mode: 'chat', MAX_QUEUE: '24' } }];
    const plan = ok<Plan>(await post(r, '/plan', { changes: both }));
    expect(plan.parts[0].changes.map((c) => c.key).sort()).toEqual(['MAX_QUEUE', 'mode', 'mode.ext']);
    const res = await change(r, both);
    expect(res.parts[0].restart!.kind).toBe('settings');
    r.wait(20_000);
    expect([(await valueOf(r, 'engine', 'mode', 'eh-desk')).value, (await valueOf(r, 'engine', 'MAX_QUEUE', 'eh-desk')).value]).toEqual(['chat', '24']);
  });
});

describe('the mock in its fleet', () => {
  it('the app scope is What runs here’s switches; a first run has no engine target', async () => {
    const f = new MockFleet({ scenario: 'empty', now: T0 / 1000 });
    const call = (method: string, path: string, body: unknown = null) => f.settings.handle(method, path, new URLSearchParams(), body, T0);
    expect((call('GET', '/v1/settings').body as Overview).scopes.find((s) => s.scope === 'engine')!.targets).toEqual([]);
    expect((call('GET', '/v1/settings/engine/schema').body as { error: { type: string } }).error.type).toBe('target_required');
    const plan = call('POST', '/v1/settings/plan', { changes: [{ scope: 'app', set: { chat: 'on' } }] }).body as Plan;
    call('POST', '/v1/settings/apply', { changes: [{ scope: 'app', set: { chat: 'on' } }], confirm: plan.confirm });
    const parts = f.handle('GET', '/v1/fleet/app/parts', new URLSearchParams(), null, T0).body as { chat: { on: boolean; switched: boolean } };
    expect([parts.chat.on, parts.chat.switched]).toEqual([true, true]);
    // unset hands the switch back to the install
    const back = call('POST', '/v1/settings/plan', { changes: [{ scope: 'app', unset: ['chat'] }] }).body as Plan;
    call('POST', '/v1/settings/apply', { changes: [{ scope: 'app', unset: ['chat'] }], confirm: back.confirm });
    expect((call('GET', '/v1/settings/app').body as ValuesView).values.find((v) => v.key === 'chat')).toEqual({ key: 'chat', value: 'off', set: false, source: 'install' });
    // the hooks scope answers an empty schema and why, not an error
    const hooks = call('GET', '/v1/settings/hooks/schema');
    expect([hooks.status, (hooks.body as SchemaView).keys, (hooks.body as SchemaView).why]).toEqual([200, [], 'no hook is registered yet: the hooks scope has no key']);
    expect((call('POST', '/v1/settings/plan', { changes: [{ scope: 'hooks', set: { 'hooks.audit.mode': 'observe' } }] }).body as { error: { type: string } }).error.type).toBe('unknown_key');
  });
});
