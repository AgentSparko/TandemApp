// Tests for VIS-284: the mock controller's plugins against the contract (contracts/plugin-v1). The
// rig holds every answer to its schema as it passes; here every route of api.json is asked once,
// every error type is answered with the status the contract gives it, the plugins scope of the
// settings interface is read and written, and a secret is in no answer.

import { describe, expect, it } from 'vitest';
import { GIT_COMMIT, GIT_URL, PLUGIN_ERRORS, UPLOADS, UPLOAD_MAX, UPLOAD_TTL_MS, needsConfirm } from '../mock/plugins.ts';
import { SETTINGS_ERRORS } from '../mock/settings.ts';
import type { Overview, Plan, Result, SchemaView, ValuesView } from '../src/api/settings.ts';
import { PluginError, pluginsApi } from '../src/api/plugins.ts';
import { API, NOT_MOCKED, T0, rig, schemaOf, wheelBytes, type PluginsRig } from './plugins-rig.ts';

const err = (body: unknown): { type: string; message: string; [k: string]: unknown } => (body as { error: { type: string; message: string } }).error;
const plan = async (r: PluginsRig, target: string, set: Record<string, unknown>, unset: string[] = []) => r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'plugins', target, ...(Object.keys(set).length ? { set } : {}), ...(unset.length ? { unset } : {}) }] });
async function apply(r: PluginsRig, target: string, set: Record<string, unknown>, unset: string[] = []): Promise<Result> {
  const p = (await plan(r, target, set, unset)).body as Plan;
  const out = await r.send('POST', '/v1/settings/apply', { changes: [{ scope: 'plugins', target, ...(Object.keys(set).length ? { set } : {}), ...(unset.length ? { unset } : {}) }], confirm: p.confirm, ...(p.confirm_grant ? { confirm_grant: p.confirm_grant } : {}) });
  return out.body as Result;
}

describe('the mock answers the plugins contract', () => {
  it('the same error table as the contract; every route of it answers its schema', async () => {
    expect(PLUGIN_ERRORS).toEqual(API.errors);
    const r = rig({ plugins: 'demo' });
    const folder = { type: 'folder', path: '/srv/plugins/usage-board' };
    const asked: [string, string, unknown?][] = [
      ['GET', '/v1/fleet/plugins'],
      ['POST', '/v1/fleet/plugins/inspect', { source: folder }],
      ['POST', '/v1/fleet/plugins/upload?name=hello_tool-0.2.0-py3-none-any.whl', wheelBytes()],
      ['POST', '/v1/fleet/plugins', { source: folder }],
      ['GET', '/v1/fleet/plugins/usage-board'],
      ['POST', '/v1/fleet/plugins/usage-board/enable', {}],
      ['POST', '/v1/fleet/plugins/usage-board/verify', {}],
      ['GET', '/v1/fleet/plugins/usage-board/status'],
      ['GET', '/v1/fleet/plugins/usage-board/logs?tail=20'],
      ['POST', '/v1/fleet/plugins/usage-board/stop', {}],
      ['POST', '/v1/fleet/plugins/usage-board/start', {}],
      ['POST', '/v1/fleet/plugins/usage-board/restart', {}],
    ];
    for (const [method, path, body] of asked) expect((await r.send(method as 'GET' | 'POST', path, body)).status, `${method} ${path}`).toBe(method === 'POST' && (path === '/v1/fleet/plugins' || path.startsWith('/v1/fleet/plugins/upload')) ? 201 : 200);
    r.wait(2000);
    for (const [method, path, body] of [['GET', '/v1/fleet/plugins/usage-board/panels'], ['POST', '/v1/fleet/plugins/usage-board/bridge', { method: 'plugin.status', panel: 'board' }], ['POST', '/v1/fleet/plugins/usage-board/disable', {}], ['DELETE', '/v1/fleet/plugins/usage-board']] as [string, string, unknown?][])
      expect((await r.send(method as 'GET' | 'POST' | 'DELETE', path, body)).status, `${method} ${path}`).toBe(200);
    // each route of the contract was one of them
    const shapes = new Set(r.paths.map((p) => {
      const [method, path] = p.split(' ');
      return `${method} ${API.routes.find((x) => x.method === method && x.schema.replace('.schema.json', '') === schemaOf(method, path) && new RegExp(`^${x.path.replace('{id}', '[^/]+')}$`).test(path.split('?')[0]))!.path}`;
    }));
    // but the trust routes, named here so that a route added to the contract fails this test
    expect([...shapes].sort()).toEqual(API.routes.map((x) => `${x.method} ${x.path}`).filter((x) => !NOT_MOCKED.includes(x)).sort());
    expect(NOT_MOCKED.every((x) => API.routes.some((y) => `${y.method} ${y.path}` === x))).toBe(true);
    // what is not a route, and a body with a field it does not take
    expect([(await r.send('GET', '/v1/fleet/plugins/hello-tool/nope')).status, (await r.send('GET', '/v1/fleet/plugins/nope')).status]).toEqual([404, 404]);
    const extra = await r.send('POST', '/v1/fleet/plugins/inspect', { source: folder, upgrade: true });
    expect([extra.status, err(extra.body).field]).toEqual([400, 'upgrade']);
    expect((await r.send('GET', '/v1/fleet/plugins/hello-tool/logs?tail=x')).status).toBe(400);
  });

  it('a route told to refuse answers the contract’s status for it, and the client throws it with its type', async () => {
    const r = rig();
    for (const [route, type, call] of [
      ['list', 'unavailable', () => r.api.list()],
      ['inspect', 'git_timeout', () => r.api.inspect({ type: 'folder', path: '/srv/plugins/usage-board' })],
      ['install', 'unreadable', () => r.api.install({ type: 'folder', path: '/srv/plugins/usage-board' })],
      ['get', 'unavailable', () => r.api.get('hello-tool')],
      ['status', 'unavailable', () => r.api.status('hello-tool')],
      ['logs', 'unavailable', () => r.api.logs('hello-tool')],
      ['restart', 'conflict', () => r.api.restart('hello-tool')],
      ['bridge', 'plugin_error', () => r.api.bridge('hello-tool', 'plugin.info')],
      ['delete', 'conflict', () => r.api.remove('hello-tool')],
    ] as [string, string, () => Promise<unknown>][]) {
      r.reg.control({ fail: `${route}:${type}` });
      const e = await call().then(() => null, (x: unknown) => x);
      expect(e, route).toBeInstanceOf(PluginError);
      expect([(e as PluginError).type, (e as PluginError).status], route).toEqual([type, API.errors[type]]);
      r.reg.control({ fail: null });
    }
    expect(() => r.reg.control({ fail: 'list:no_such_error' })).toThrow('no plugins error no_such_error');
    // a controller that does not answer, and one that answers something else
    const down = pluginsApi(async () => ({ status: 0, body: null }));
    expect(await down.list().then(() => null, (x: PluginError) => [x.type, x.status])).toEqual(['offline', 0]);
    const odd = pluginsApi(async () => ({ status: 502, body: 'Bad Gateway' }));
    expect(await odd.get('x').then(() => null, (x: PluginError) => [x.type, x.message])).toEqual(['error', 'TandemApp answered with an error (502).']);
    const locked = pluginsApi(async () => ({ status: 401, body: null }));
    expect(await locked.remove('x').then(() => null, (x: PluginError) => x.type)).toBe('unauthorized');
  });

  it('the registry as seeded: the demo set, each with its state on the clock', async () => {
    const r = rig({ plugins: 'demo' });
    const l = await r.api.list();
    expect([l.contract_version, l.plugin_api, l.hooks_api, l.tandemapp]).toEqual(['1.0', '1.0', '1.0', '0.16.0']);
    expect(l.plugins.map((p) => `${p.id} ${p.version} ${p.kind} ${p.enabled}`)).toEqual(['audit-hook 0.1.0 plugin true', 'classify-provider 0.1.0 plugin false', 'flaky-export 0.3.1 plugin false', 'hello-tool 0.1.0 plugin true', 'tandemllm-vllm-plugin 0.1.0 engine-stats false']);
    const states = await Promise.all(l.plugins.map((p) => r.api.status(p.id)));
    expect(states.map((s) => s.state)).toEqual(['running', 'disabled', 'failed', 'running', 'disabled']);
    expect([states[2].gave_up?.failures, states[2].last_error]).toEqual([5, 'export.js exited with 1: Error: connect ECONNREFUSED 192.168.1.52:4318']);
    expect(states[0].hooks).toEqual(['audit-hook/finished']);
    const audit = await r.api.get('audit-hook');
    expect([audit.settings, audit.references, audit.capabilities.granted, audit.settings_form!.fields.map((f) => f.key)]).toEqual([{ keep_days: 14, fields: ['id', 'model', 'tokens', 'ms'] }, [{ id: 'audit-to-disk', name: 'Audit to disk', enabled: true }], ['prompts:meta'], ['keep_days', 'fields']]);
    expect((await r.api.logs('flaky-export', 3)).at(-1)).toContain('gave up and disabled flaky-export');
    // nothing seeded: an empty list, and a scope without a target
    const none = rig({ plugins: false });
    expect((await none.api.list()).plugins).toEqual([]);
    expect(((await none.settings.overview()) as Overview).scopes.find((s) => s.scope === 'plugins')!.targets).toEqual([]);
  });

  it('a git source is pinned to a full commit id; a wheel records its archive’s sha256', async () => {
    const r = rig();
    for (const commit of ['main', 'v1.2.0', GIT_COMMIT.slice(0, 12), GIT_COMMIT.toUpperCase()]) {
      const got = await r.send('POST', '/v1/fleet/plugins/inspect', { source: { type: 'git', url: GIT_URL, commit } });
      expect([got.status, err(got.body).type, err(got.body).field], commit).toEqual([422, 'commit_required', 'source.commit']);
    }
    const git = await r.api.inspect({ type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' });
    expect([git.ok, git.source, git.capabilities, git.manifest!.trust]).toEqual([true, { type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' }, { requested: ['prompts:meta', 'engine:control'], confirm: ['engine:control'] }, 'host']);
    const wheel = await r.api.inspect({ type: 'wheel', path: '/srv/dist/hello_tool-0.2.0-py3-none-any.whl' });
    expect([wheel.source.type, (wheel.source as { archive_sha256: string }).archive_sha256.length, wheel.installed]).toEqual(['wheel', 64, { version: '0.1.0', sha256: (await r.api.get('hello-tool')).sha256 }]);
    const bad = await r.send('POST', '/v1/fleet/plugins/inspect', { source: { type: 'zip', path: '/x.zip' } });
    expect([bad.status, err(bad.body).type, err(bad.body).field]).toEqual([422, 'bad_source', 'source.type']);
    expect(err((await r.send('POST', '/v1/fleet/plugins/inspect', { source: { type: 'folder', path: '/srv/dist/hello_tool-0.2.0-py3-none-any.whl' } })).body).type).toBe('not_a_folder');
    expect([needsConfirm('prompts:full'), needsConfirm('engine:control'), needsConfirm('network:10.1.2.3:80'), needsConfirm('network:api.example.com'), needsConfirm('secrets:X'), needsConfirm('settings:read')]).toEqual([true, true, false, true, false, false]);
  });
});

describe('the upload, the list’s run state and the grants that ask twice, in the mock (SRV-857)', () => {
  it('a wheel uploaded is kept for an hour at a path it is checked and installed from; what is no wheel is refused', async () => {
    const r = rig();
    const up = await r.api.upload('hello_tool-0.2.0-py3-none-any.whl', wheelBytes(100));
    expect([up.source.type, up.source.path.startsWith(`${UPLOADS}/`), up.source.path.endsWith('/hello_tool-0.2.0-py3-none-any.whl'), up.name, up.bytes, up.sha256.length, up.expires_at]).toEqual(['wheel', true, true, 'hello_tool-0.2.0-py3-none-any.whl', 100, 64, T0 + UPLOAD_TTL_MS]);
    // the upload holds what the wheel of that name holds: hello-tool 0.2.0, an upgrade of the one installed
    const rep = await r.api.inspect(up.source);
    expect([rep.ok, rep.manifest!.version, rep.installed!.version]).toEqual([true, '0.2.0', '0.1.0']);
    expect((await r.api.install(up.source, { upgrade: true })).action).toBe('upgraded');
    // a name the catalog does not know is a wheel without plugin.json
    const other = await r.api.upload('other-1.0-py3-none-any.whl', wheelBytes());
    expect((await r.api.inspect(other.source)).errors.map((e) => e.code)).toEqual(['manifest_missing']);
    // an hour later it is gone
    r.wait(UPLOAD_TTL_MS);
    const gone = await r.send('POST', '/v1/fleet/plugins/inspect', { source: other.source });
    expect([gone.status, err(gone.body).type, err(gone.body).field]).toEqual([404, 'not_found', 'source.path']);
    // what is refused, with the contract's statuses (the rig holds each to them)
    const ask = (name: string, body: unknown) => r.send('POST', `/v1/fleet/plugins/upload?name=${encodeURIComponent(name)}`, body);
    expect([(await ask('x.zip', wheelBytes())).status, err((await ask('x.zip', wheelBytes())).body).field]).toEqual([400, 'name']);
    expect(err((await ask('x-1.0-py3-none-any.whl', new Uint8Array())).body).type).toBe('bad_request');
    expect(err((await ask('x-1.0-py3-none-any.whl', new TextEncoder().encode('not a zip'))).body).type).toBe('bad_wheel');
    expect(err((await ask('x-1.0-py3-none-any.whl', { source: 'json' })).body).type).toBe('unsupported_media_type');
    expect(err((await ask('x-1.0-py3-none-any.whl', wheelBytes(UPLOAD_MAX + 1))).body).type).toBe('upload_too_large');
    r.reg.control({ trust: false });
    expect(err((await ask('x-1.0-py3-none-any.whl', wheelBytes())).body).type).toBe('unauthorized');
  });

  it('the list carries each plugin’s run state as its status has it; every view names the grants that ask twice; times are milliseconds', async () => {
    const r = rig({ plugins: 'demo' });
    const l = await r.api.list();
    for (const p of l.plugins) {
      const s = await r.api.status(p.id);
      expect(p.run, p.id).toEqual({ state: s.state, held: s.held, restarts: s.restarts, failures_last_hour: s.failures_last_hour, next_start_at: s.next_start_at, started_at: s.started_at, last_error: s.last_error, gave_up: s.gave_up });
    }
    const flaky = l.plugins.find((p) => p.id === 'flaky-export')!;
    expect([flaky.run!.state, flaky.run!.gave_up!.failures, flaky.run!.gave_up!.version, flaky.run!.gave_up!.at]).toEqual(['failed', 5, '0.3.1', T0 - 1_500_000]);
    // the full view and the list say the same of what asks twice
    await r.api.install({ type: 'folder', path: '/srv/plugins/usage-board' });
    const board = await r.api.get('usage-board');
    expect(board.capabilities.confirm).toEqual(board.capabilities.requested.filter(needsConfirm));
    expect(board.capabilities.confirm).toEqual(['prompts:full', 'network:api.example.com:443']);
    expect((await r.api.list()).plugins.find((p) => p.id === 'usage-board')!.capabilities.confirm).toEqual(board.capabilities.confirm);
    // a signature names its key, the plugin and the version; its key is not one this controller trusts
    expect([board.signature!.trusted, board.signature!.plugin, board.signature!.version, board.signature!.fingerprint!.length]).toEqual([false, 'usage-board', board.version, 64]);
  });
});

describe('the plugins scope of the settings interface, in the mock', () => {
  it('one target a plugin, its keys from its manifest; values say what is set and what is a default; a secret is write-only', async () => {
    const r = rig({ plugins: 'demo' });
    expect(SETTINGS_ERRORS.trust_required).toBe(403);
    const o = await r.settings.overview();
    expect(o.order).toEqual(['app', 'backends', 'conductor', 'engine', 'hooks', 'plugins']);
    expect(o.scopes.find((s) => s.scope === 'plugins')!.targets.map((t) => `${t.id}=${t.label}`)).toEqual(['audit-hook=Audit hook', 'classify-provider=Keyword classifier', 'flaky-export=Flaky export', 'hello-tool=Hello tool', 'tandemllm-vllm-plugin=vLLM stats']);
    const s: SchemaView = await r.settings.schema('plugins', 'classify-provider');
    expect(s.keys.map((k) => `${k.key} ${k.type}${k.secret ? ' secret' : ''}`)).toEqual([
      'capabilities.network:192.168.1.30:8720 boolean', 'capabilities.prompts:redacted boolean', 'capabilities.secrets:API_KEY boolean', 'enabled boolean', 'secrets.API_KEY secret secret', 'settings.classes object', 'settings.endpoint string', 'settings.fallback enum',
    ]);
    expect(s.keys.find((k) => k.key === 'settings.fallback')!.values).toEqual(['code', 'chat', 'unclear']);
    const v: ValuesView = await r.settings.values('plugins', 'audit-hook');
    expect(v.values.map((x) => `${x.key}=${JSON.stringify(x.value)} ${x.set}`)).toEqual(['capabilities.prompts:meta=true true', 'enabled=true true', 'settings.fields=["id","model","tokens","ms"] false', 'settings.keep_days=14 true']);
    // a plugin that asks for the host has the trust key; no other has it
    const git = await r.send('POST', '/v1/fleet/plugins', { source: { type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' } });
    expect(git.status).toBe(201);
    expect((await r.settings.schema('plugins', 'host-metrics')).keys.map((k) => k.key)).toEqual(['capabilities.engine:control', 'capabilities.prompts:meta', 'enabled', 'trust']);
    // a secret goes in and never comes out
    const secret = 'sk-mock-fedcba9876543210';
    expect((await apply(r, 'classify-provider', { 'secrets.API_KEY': secret })).state).toBe('applied');
    const after = await r.settings.values('plugins', 'classify-provider');
    const row = after.values.find((x) => x.key === 'secrets.API_KEY')!;
    expect([row.value, row.set, typeof row.hint]).toEqual([null, true, 'string']);
    expect((await r.api.get('classify-provider')).secrets.API_KEY).toEqual({ set: true, last4: '3210', sha256_6: row.hint, set_at: T0 });
    expect(r.seen.join('\n')).not.toContain(secret);
    // an unknown key, a key of another plugin, a plugin that is not there
    expect(err((await plan(r, 'hello-tool', { 'settings.nope': 1 })).body).type).toBe('unknown_key');
    expect(err((await plan(r, 'hello-tool', { 'secrets.API_KEY': 'x' })).body).type).toBe('unknown_key');
    expect(err((await plan(r, 'gone', { enabled: true })).body).type).toBe('no_target');
  });

  it('enabling and a grant take pairing’s rights; a risky grant takes the second confirm; each plugin keeps its own versions', async () => {
    const r = rig({ plugins: 'demo' });
    r.mock.control({ trust: false });
    for (const set of [{ enabled: true }, { 'capabilities.prompts:redacted': true }]) {
      const got = await plan(r, 'classify-provider', set);
      expect([got.status, err(got.body).type, err(got.body).key], JSON.stringify(set)).toEqual([403, 'trust_required', Object.keys(set)[0]]);
    }
    // taking a grant back, turning it off and a setting need no such rights
    expect((await plan(r, 'audit-hook', { 'capabilities.prompts:meta': false, enabled: false, 'settings.keep_days': 7 })).status).toBe(200);
    r.mock.control({ trust: true });
    const p = (await plan(r, 'classify-provider', { enabled: true, 'capabilities.prompts:redacted': true, 'capabilities.network:192.168.1.30:8720': true })).body as Plan;
    expect([p.grant_keys, p.confirm_grant, p.parts[0].restart, p.parts[0].warnings.length]).toEqual([[], null, 'none', 3]);
    // a grant that holds already, set again, is no change
    expect(((await plan(r, 'audit-hook', { 'capabilities.prompts:meta': true, enabled: true })).body as Plan).changed).toBe(false);
    await r.send('POST', '/v1/fleet/plugins', { source: { type: 'folder', path: '/srv/plugins/usage-board' } });
    const risky = (await plan(r, 'usage-board', { 'capabilities.prompts:full': true, 'capabilities.settings:read': true })).body as Plan;
    expect([risky.grant_keys, typeof risky.confirm_grant]).toEqual([['plugins/usage-board:capabilities.prompts:full'], 'string']);
    expect((await apply(r, 'usage-board', { 'capabilities.prompts:full': true, 'capabilities.settings:read': true })).state).toBe('applied');
    const g = (await r.api.get('usage-board')).capabilities.grants;
    expect([g['prompts:full'].confirmed_by, g['settings:read'].confirmed_by, g['prompts:full'].by]).toEqual(['admin', null, 'admin']);
    // versions are a plugin's own: restoring one does not touch another
    await apply(r, 'audit-hook', { 'settings.keep_days': 60 });
    const versions = await r.settings.versions('plugins', 'audit-hook');
    expect(versions.versions.map((x) => x.keys)).toEqual([['settings.keep_days'], []]);
    expect((await r.settings.versions('plugins', 'usage-board')).versions[0].keys).toEqual(['capabilities.prompts:full', 'capabilities.settings:read']);
    const back = (await r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'plugins', target: 'audit-hook', restore: versions.versions[1].id }] })).body as Plan;
    expect(back.parts[0].changes.map((c) => `${c.key} ${c.from}->${c.to}`)).toEqual(['settings.keep_days 60->14']);
    // a restore that brings a risky grant back asks again
    await apply(r, 'usage-board', { 'capabilities.prompts:full': false });
    const uv = await r.settings.versions('plugins', 'usage-board');
    const regrant = (await r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'plugins', target: 'usage-board', restore: uv.versions[1].id }] })).body as Plan;
    expect(regrant.grant_keys).toEqual(['plugins/usage-board:capabilities.prompts:full']);
    // in one change set with another scope's change, the plugins' part applies last
    const both = (await r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'plugins', target: 'classify-provider', set: { enabled: true } }, { scope: 'app', set: { chat: 'on' } }] })).body as Plan;
    expect(both.parts.map((x) => x.scope)).toEqual(['app', 'plugins']);
  });
});
