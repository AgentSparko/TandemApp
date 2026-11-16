// Tests for VIS-278's ground: the engine host routes in the mock (mock/enginehosts.ts) as the
// controller answers them (proxy/engineonboard.py), and the page's client for them
// (api/engines.ts). Every 200 answer and every refusal holds to its schema in contracts/fleet-v1
// (the rig checks each as it passes); the mock's table of codes is the contract's; every code of
// the table comes back from a route with its status, its sentence and its fix, the facts filled;
// each device behaves as its line in the mock's header says; no answer holds the code, a password
// or a key; a paired engine host is a member of the fleet.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';
import { describe, expect, it } from 'vitest';
import { ENGINE_CODES, ENGINE_CODES_VERSION, finding } from '../mock/enginecodes.ts';
import { FAIL_ROUTES, KEY_PASTE_S, MOCK_ENGINES, type FailRoute } from '../mock/enginehosts.ts';
import { EngineError, SignInNeeded, enginesApi, type EngineFinding, type PairedView } from '../src/api/engines.ts';
import { CONTRACTS, rig, valid, type Rig } from './engine-rig.ts';

const E = '/v1/fleet/engines';
const M = MOCK_ENGINES;

/** a refusal's finding, from a call that must fail */
async function refused(p: Promise<unknown>): Promise<EngineFinding & { status: number }> {
  try {
    await p;
  } catch (e) {
    if (e instanceof EngineError) return { ...e.finding, status: e.status };
    throw e;
  }
  throw new Error('the call was not refused');
}

async function pairDesk(r: Rig): Promise<PairedView> {
  const s = await r.api.pairStart({ address: M.desk });
  return r.api.pairCode(s.pairing, M.code);
}

describe('the codes', () => {
  const contract = JSON.parse(readFileSync(join(CONTRACTS, 'fleet-v1', 'engine-codes.json'), 'utf8')) as { version: string; codes: Record<string, { status: number; message: string; fix: string }> };
  it('the mock’s table is the contract’s, code by code', () => {
    // 76 with trusted_proxy_invalid (OPS-298); 75 with the tokens near or at a spend limit (SRV-873); 73 with harness_unsupported (SRV-879); 72 since the API's exposure and tokens about to end (SRV-871); 68 since the API tokens' findings (SRV-867); 66 since the sign-in's findings (SEC-26, SRV-865); 59 since the health page's codes (SRV-835): an engine host's state, the controller's own (chatd's sandboxes included), the plugin host, a fix refused, a job replaced
    expect(Object.keys(ENGINE_CODES)).toHaveLength(76);
    expect(contract.version.split('.')[0]).toBe(ENGINE_CODES_VERSION.split('.')[0]);
    for (const [code, [status, message, fix]] of Object.entries(ENGINE_CODES)) expect(contract.codes[code], code).toEqual({ status, message, fix });
  });
  it('a sentence is filled from the facts; a fact the call does not know has a word, never a brace', () => {
    expect(finding('wrong_code', { tries_left: 3 }).message).toBe('The code does not match: 3 tries left.');
    expect(finding('unreachable', { address: '192.0.2.90', port: 8000 }).message).toBe('TandemApp cannot reach the engine at 192.0.2.90:8000.');
    expect(finding('rate_limited', { retry_after: 30 }).fix).toBe('Wait 30 seconds, then type the code again.');
    expect(finding('key_refused', {}, '~/.ssh is a link').detail).toBe('~/.ssh is a link');
    for (const code of Object.keys(ENGINE_CODES)) {
      const f = finding(code);
      expect(f.message + f.fix, code).not.toMatch(/[{}]/);
      valid('engine-finding', f);
    }
  });
  it('every code comes back from a route with its status, and the page gets the sentence and the fix', async () => {
    // a route per kind of call; `engine` is a 200 whose view carries the finding
    const calls: Record<FailRoute, (r: Rig, id: string) => Promise<unknown>> = {
      discover: (r) => r.api.discover(),
      probe: (r) => r.api.probe(M.desk),
      'pair-start': (r) => r.api.pairStart({ address: M.tailnet }),
      'pair-code': async (r) => r.api.pairCode((await r.api.pairStart({ address: M.tailnet })).pairing, M.code),
      'password-plan': (r) => r.api.passwordPlan({ host: M.attic, user: M.user }),
      'password-confirm': async (r) => {
        const p = await r.api.passwordPlan({ host: M.attic, user: M.user });
        return r.api.passwordConfirm(p.plan, M.password, p.host_key.fingerprint);
      },
      'key-new': (r) => r.api.keyNew({ host: M.attic, user: M.user }),
      'key-check': async (r) => r.api.keyCheck((await r.api.keyNew({ host: M.attic, user: M.user })).plan),
      engine: (r, id) => r.api.engine(id),
      mode: (r, id) => r.api.setMode(id),
      models: (r, id) => r.api.models(id),
      pull: (r, id) => r.api.pull(id, { model: 'qwen3.8-27b' }),
      progress: (r, id) => r.api.progress(id),
    };
    expect(Object.keys(calls).sort()).toEqual([...FAIL_ROUTES].sort());
    const routes = FAIL_ROUTES.filter((x) => x !== 'engine');
    let i = 0;
    for (const [code, [status, message]] of Object.entries(ENGINE_CODES)) {
      const r = rig();
      const id = (await pairDesk(r)).engine!.id;
      // each code on another route in turn, so every route refuses with many codes
      const route = routes[i++ % routes.length];
      r.fleet.engines.fail(route, code);
      const f = await refused(calls[route](r, id));
      expect([f.code, f.status], `${code} on ${route}`).toEqual([code, status]);
      expect(f.message.length).toBeGreaterThan(10);
      expect(f.fix.length).toBeGreaterThan(5);
      expect(f.message + f.fix).not.toMatch(/[{}]/);
      if (message.includes('{detail}')) expect(f.detail, code).toBeTruthy();
      // the facts the page shows on a line of their own
      if (code === 'wrong_code') expect(f.tries_left).toBe(3);
      if (code === 'rate_limited') expect(f.retry_after).toBe(30);
      if (code === 'host_key_mismatch') expect([f.expected, f.offered].every((x) => /^SHA256:/.test(x ?? ''))).toBe(true);
      if (code === 'host_key_changed') expect([f.pinned, f.offered].every((x) => /^SHA256:/.test(x ?? ''))).toBe(true);
    }
    // cleared, a route answers again
    const c = rig();
    c.fleet.engines.fail('discover', 'lan_failed');
    expect((await refused(c.api.discover())).code).toBe('lan_failed');
    c.fleet.engines.fail('discover', null);
    expect((await c.api.discover()).lan).toBe('ok');
    // a status that cannot be read is the view's finding, in a 200
    const r = rig();
    const id = (await pairDesk(r)).engine!.id;
    r.fleet.engines.fail('engine', 'ssh_unreachable');
    const v = await r.api.engine(id);
    expect([v.status, v.finding?.code, v.wizard?.next]).toEqual([null, 'ssh_unreachable', null]);
    expect(() => r.fleet.engines.fail('models', 'no_such_code')).toThrow();
  });
});

describe('finding the engine', () => {
  it('before a search the list says so; a search lists what the LAN advertises, unverified', async () => {
    const r = rig();
    expect((await r.api.found()).lan).toBe('not_run');
    const d = await r.api.discover();
    expect(d.lan).toBe('ok');
    expect(d.engines.map((e) => [e.name, e.address, e.port, e.version])).toEqual([
      ['spark-desk', M.desk, 8000, '0.16.0'],
      ['spark-rack', M.rack, 8000, '0.16.0'],
      ['spark-attic', M.attic, 8000, '0.15.4'],
      ['spark-lab', M.lab, 8000, '0.16.0'],
    ]);
    expect(d.engines.every((e) => e.verified === false && e.node === null)).toBe(true);
    expect(d.engines.map((e) => !!e.fingerprint)).toEqual([true, true, false, true]);
    expect(await r.api.found()).toEqual(d);
  });
  it('with the local network refused the answer says so, in a 200 with its finding', async () => {
    const r = rig();
    r.fleet.engines.lanMode = 'deny';
    const d = await r.api.discover();
    expect([d.lan, d.engines.length, d.finding?.code]).toEqual(['denied', 0, 'lan_denied']);
    expect(d.finding!.fix).toContain('type the engine\'s address');
  });
  it('an address typed by hand: what answers there, a tailnet name included, in a 200 either way', async () => {
    const r = rig();
    const ok = await r.api.probe(M.tailnet);
    expect([ok.reachable, ok.engine, ok.pairing?.open, ok.pairing?.tries_left, ok.finding]).toEqual([true, true, true, 5, null]);
    expect(ok.fingerprint).toMatch(/^SHA256:[A-Za-z0-9+/]{43}$/);
    const codes = async (address: string) => {
      const p = await r.api.probe(address);
      return [p.reachable, p.engine, p.finding?.code];
    };
    expect(await codes(M.silent)).toEqual([false, false, 'unreachable']);
    expect(await codes('198.51.100.9')).toEqual([false, false, 'unreachable']);
    expect(await codes(M.other)).toEqual([true, false, 'not_an_engine']);
    expect(await codes(M.refusing)).toEqual([true, false, 'not_allowed']);
    expect(await codes(M.limited)).toEqual([true, true, 'rate_limited']);
    expect(await codes(M.attic)).toEqual([true, true, 'pairing_unsupported']);
    // no code showing: said once, then a window is open (as after `tandem pair`)
    expect(await codes(M.rack)).toEqual([true, true, 'no_window']);
    expect((await r.api.probe(M.rack)).pairing?.open).toBe(true);
    expect((await refused(r.api.probe('not an address'))).code).toBe('bad_request');
    expect((await refused(r.send('POST', `${E}/probe`, { address: M.desk, port: 70000 }).then((x) => enginesApi(async () => x).found()))).code).toBe('bad_request');
  });
});

describe('pairing by code', () => {
  it('the right code pairs: the engine with its status and what is next, and a member of the fleet', async () => {
    const r = rig();
    const adv = (await r.api.discover()).engines[0];
    const s = await r.api.pairStart({ address: adv.address, port: adv.port, fingerprint: adv.fingerprint!, name: adv.name });
    expect([s.window.open, s.window.tries_left, s.window.expires_in, s.fingerprint, s.advertised]).toEqual([true, 5, 300, adv.fingerprint, adv.fingerprint]);
    const p = await r.api.pairCode(s.pairing, M.code);
    expect([p.state, p.via, p.finding]).toEqual(['paired', 'code', null]);
    const e = p.engine!;
    expect([e.alias, e.address, e.via, e.host_key, e.ssh.user, e.http.port, e.http.token.set]).toEqual(['spark-desk', M.desk, 'code', adv.fingerprint, M.user, 8000, true]);
    expect(e.wizard).toEqual({ mode_choice: 'engine', mode: 'engine', needs_model: true, next: 'model' });
    const ctl = new Ajv2020({ strict: false }).compile(JSON.parse(readFileSync(join(CONTRACTS, 'engine-ctl-v1', 'status.schema.json'), 'utf8')));
    expect(ctl(e.status), JSON.stringify(ctl.errors)).toBe(true);
    // the fleet has its first member, reached over SSH; the search now names it
    const nodes = (r.fleet.handle('GET', '/v1/fleet/nodes', new URLSearchParams(), null, r.now()).body as { nodes: { id: string; transport: string; pairing: { host_key: string } }[] }).nodes;
    valid('nodes', r.fleet.handle('GET', '/v1/fleet/nodes', new URLSearchParams(), null, r.now()).body);
    expect(nodes.map((n) => [n.id, n.transport, n.pairing.host_key])).toEqual([[e.id, 'ssh', adv.fingerprint]]);
    valid('node', p.node);
    expect((await r.api.discover()).engines[0]).toMatchObject({ node: e.id, alias: 'spark-desk' });
    expect((await r.api.probe(M.desk)).node).toBe(e.id);
    expect((await r.api.list()).engines.map((x) => x.id)).toEqual([e.id]);
    // the pairing is spent with its use
    expect((await refused(r.api.pairCode(s.pairing, M.code))).code).toBe('not_found');
  });
  it('a wrong code says the tries left; the fifth spends the code; then a new window opens', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.desk });
    for (const left of [4, 3, 2, 1]) {
      const f = await refused(r.api.pairCode(s.pairing, '000000'));
      expect([f.code, f.status, f.tries_left]).toEqual(['wrong_code', 403, left]);
    }
    expect((await refused(r.api.pairCode(s.pairing, '000000'))).code).toBe('code_spent');
    expect((await refused(r.api.pairCode(s.pairing, M.code))).code).toBe('code_spent');
    const again = await r.api.pairStart({ address: M.desk });
    expect(again.window.tries_left).toBe(5);
    expect((await r.api.pairCode(again.pairing, M.code)).state).toBe('paired');
  });
  it('a code is 6 digits; a window ends after 5 minutes; ten tries a minute is the limit', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.desk });
    for (const bad of ['12345', '1234567', 'abcdef', '']) expect((await refused(r.api.pairCode(s.pairing, bad))).code, bad).toBe('bad_code');
    r.wait(301_000);
    expect((await refused(r.api.pairCode(s.pairing, M.code))).code).toBe('code_spent');
    const t = rig();
    let last = '';
    for (let i = 0; i < 12 && last !== 'too_many'; i++) {
      const p = await t.api.pairStart({ address: i % 2 ? M.desk : M.tailnet }).catch(() => null);
      last = p ? (await refused(t.api.pairCode(p.pairing, '000000'))).code : last;
    }
    expect(last).toBe('too_many');
  });
  it('what stops a pairing before the code: no code showing, an old engine, another fingerprint', async () => {
    const r = rig();
    expect((await refused(r.api.pairStart({ address: M.rack }))).code).toBe('no_window');
    expect((await r.api.pairStart({ address: M.rack })).window.open).toBe(true);
    expect((await refused(r.api.pairStart({ address: M.attic }))).code).toBe('pairing_unsupported');
    expect((await refused(r.api.pairStart({ address: M.silent }))).code).toBe('unreachable');
    const lab = (await r.api.discover()).engines.find((e) => e.address === M.lab)!;
    const f = await refused(r.api.pairStart({ address: lab.address, fingerprint: lab.fingerprint! }));
    expect([f.code, f.status, f.expected]).toEqual(['host_key_mismatch', 409, lab.fingerprint]);
    expect(f.offered).not.toBe(f.expected);
    expect(f.message).toContain(f.offered!);
  });
  it('an abandoned pairing is forgotten', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.desk });
    expect(await r.api.pairAbandon(s.pairing)).toMatchObject({ abandoned: true });
    expect(await r.api.pairAbandon(s.pairing)).toMatchObject({ abandoned: false });
    expect((await refused(r.api.pairCode(s.pairing, M.code))).code).toBe('not_found');
  });
});

describe('the other ways', () => {
  it('password once: the fingerprint first, then one login; a wrong password keeps the plan', async () => {
    const r = rig();
    const p = await r.api.passwordPlan({ host: M.attic, user: M.user });
    expect([p.host, p.port, p.user, p.engine_dir, p.host_key.known, p.host_key.stored]).toEqual([M.attic, 22, M.user, null, 'new', null]);
    expect((await refused(r.send('POST', `${E}/connect/password/confirm`, { plan: p.plan, password: M.password }).then((x) => enginesApi(async () => x).found()))).code).toBe('host_key_unconfirmed');
    expect((await refused(r.api.passwordConfirm(p.plan, M.password, p.host_key.fingerprint.replace(/.$/, 'x')))).code).toBe('host_key_mismatch');
    expect((await refused(r.api.passwordConfirm(p.plan, 'wrong', p.host_key.fingerprint))).code).toBe('auth_failed');
    const done = await r.api.passwordConfirm(p.plan, M.password, p.host_key.fingerprint);
    expect([done.state, done.via, done.engine!.via, done.engine!.alias]).toEqual(['paired', 'password', 'ssh', M.attic]);
    // the same host again: the fleet knows its key
    expect((await r.api.passwordPlan({ host: M.attic, user: M.user })).host_key.known).toBe('same');
  });
  it('a host key the fleet pinned differently is refused with both; no SSH, an old engine, another folder', async () => {
    const r = rig();
    const f = await refused(r.api.passwordPlan({ host: M.changed, user: M.user }));
    expect([f.code, f.status]).toEqual(['host_key_changed', 409]);
    expect(f.pinned).not.toBe(f.offered);
    expect((await refused(r.api.passwordPlan({ host: M.silent, user: M.user }))).code).toBe('ssh_unreachable');
    const old = await r.api.passwordPlan({ host: M.ancient, user: M.user });
    expect((await refused(r.api.passwordConfirm(old.plan, M.password, old.host_key.fingerprint))).code).toBe('engine_too_old');
    const tail = await r.api.passwordPlan({ host: M.tailnet, user: 'gpu' });
    const miss = await refused(r.api.passwordConfirm(tail.plan, 'gpu-pass', tail.host_key.fingerprint));
    expect([miss.code, miss.dir, miss.host]).toEqual(['not_installed', '~/TandemLLM', M.tailnet]);
    const there = await r.api.passwordPlan({ host: M.tailnet, user: 'gpu', engine_dir: '/opt/tandem' });
    expect((await r.api.passwordConfirm(there.plan, 'gpu-pass', there.host_key.fingerprint)).state).toBe('paired');
    for (const bad of [{ host: M.attic, user: 'a b' }, { host: M.attic, user: M.user, engine_dir: 'relative/path' }, { host: M.attic, user: M.user, port: 0 }])
      expect((await refused(r.api.passwordPlan(bad))).code, JSON.stringify(bad)).toBe('bad_request');
  });
  it('paste a key: the line, where it goes, then waiting until the host takes it', async () => {
    const r = rig();
    const k = await r.api.keyNew({ host: M.attic, user: M.user });
    expect([k.file, k.engine_dir, k.engine_dir_guessed, k.host_key?.known, k.finding]).toEqual(['~/.ssh/authorized_keys', '/home/spark/TandemLLM', true, 'new', null]);
    expect(k.line).toMatch(/^command="\/bin\/bash \/home\/spark\/TandemLLM\/bin\/tandem remote --key-id tandemapp-[0-9a-f]{10}",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 \S+ tandemapp-[0-9a-f]{10}$/);
    expect((await refused(r.api.keyCheck(k.plan))).code).toBe('host_key_unconfirmed');
    const w = await r.api.keyCheck(k.plan, k.host_key!.fingerprint);
    expect([w.state, w.via, w.engine, w.finding?.code, w.finding?.user, w.finding?.host]).toEqual(['waiting', 'key', null, 'key_not_installed', M.user, M.attic]);
    r.wait(KEY_PASTE_S * 1000);
    const done = await r.api.keyCheck(k.plan, k.host_key!.fingerprint);
    expect([done.state, done.via, done.engine!.via]).toEqual(['paired', 'key', 'ssh']);
    expect((await refused(r.api.keyCheck(k.plan, k.host_key!.fingerprint))).code).toBe('not_found');
  });
  it('paste a key: an SSH server that answers late gives its fingerprint at the check; a wrong folder is said', async () => {
    const r = rig();
    const k = await r.api.keyNew({ host: M.sleepy, user: M.user });
    expect([k.host_key, k.finding?.code]).toEqual([null, 'ssh_unreachable']);
    const f = await refused(r.api.keyCheck(k.plan));
    expect(f.code).toBe('host_key_unconfirmed');
    expect(f.offered).toMatch(/^SHA256:/);
    r.wait(KEY_PASTE_S * 1000);
    expect((await r.api.keyCheck(k.plan, f.offered)).state).toBe('paired');
    const t = await r.api.keyNew({ host: M.tailnet, user: 'gpu' });
    r.wait(KEY_PASTE_S * 1000);
    const miss = await refused(r.api.keyCheck(t.plan, t.host_key!.fingerprint));
    expect([miss.code, miss.dir]).toEqual(['not_installed', '/home/gpu/TandemLLM']);
    expect((await refused(r.api.keyNew({ host: M.attic, user: M.user, engine_dir: '~/TandemLLM' }))).code).toBe('bad_request');
  });
});

describe('after pairing', () => {
  it('the bare engine is kept, a model is planned, installed and followed to its end', async () => {
    const r = rig();
    const id = (await pairDesk(r)).engine!.id;
    const m = await r.api.setMode(id);
    expect([m.mode, m.changed, m.previous.inferred, m.job?.kind]).toEqual(['engine', true, true, 'mode']);
    expect((await r.api.setMode(id)).changed).toBe(false);
    const models = (await r.api.models(id)).models;
    expect(models.map((x) => [x.id, x.recommended])).toEqual([['qwen3.8-27b', true], ['qwen3.8-flash-next', false], ['nemotron-3.5-bolt-74b-a9b', false]]);
    const dry = await r.api.pull(id, { model: 'qwen3.8-27b', dry_run: true });
    expect([dry.plan.ok, dry.plan.download_gb, dry.plan.on_disk, dry.profile, dry.job, dry.progress]).toEqual([true, 18.4, false, 'exl3-4.5', null, null]);
    expect((await r.api.progress(id)).progress).toBeNull();
    const pull = await r.api.pull(id, { model: 'qwen3.8-27b' });
    expect([pull.progress?.state, pull.progress?.finished, pull.progress?.steps]).toEqual(['queued', false, 7]);
    expect((await refused(r.api.pull(id, { model: 'qwen3.8-flash-next' }))).code).toBe('busy');
    expect((await refused(r.api.setMode(id))).code).toBe('busy');
    let sawDownload = false;
    const done: number[] = [];
    for (let i = 0; i < 60; i++) {
      r.wait(1000);
      const p = (await r.api.progress(id, pull.job!.id)).progress!;
      done.push(p.steps_done);
      if (p.downloading) {
        sawDownload = true;
        expect([p.step, p.label]).toEqual(['fetch', 'Download the weights']);
        expect(p.detail).toMatch(/^\d+\.\d of 18\.4 GB$/);
      }
      if (p.finished) {
        expect([p.state, p.steps_done, p.steps]).toEqual(['done', 7, 7]);
        break;
      }
    }
    expect(sawDownload).toBe(true);
    expect(done).toEqual([...done].sort((a, b) => a - b));
    const e = await r.api.engine(id);
    expect([e.status?.engine.model, e.wizard?.needs_model, e.wizard?.next]).toEqual(['Qwen3.8-27B', false, 'chat']);
    expect((await r.api.progress(id)).job).toBeNull();
  });
  it('weights on the disk need no download; a plan with an error is refused in its words; an unknown model', async () => {
    const r = rig();
    const id = (await pairDesk(r)).engine!.id;
    const disk = await r.api.pull(id, { model: 'qwen3.8-flash-next', dry_run: true });
    expect([disk.plan.on_disk, disk.plan.download_gb]).toEqual([true, 0]);
    const f = await refused(r.api.pull(id, { model: 'nemotron-3.5-bolt-74b-a9b', dry_run: true }));
    expect([f.code, f.model]).toEqual(['setup_refused', 'nemotron-3.5-bolt-74b-a9b']);
    expect(f.message).toContain('not enough free disk');
    const u = await refused(r.api.pull(id, { model: 'no-such-model' }));
    expect([u.code, u.status, u.model]).toEqual(['unknown_model', 404, 'no-such-model']);
    expect((await refused(r.api.engine('eh-0000000000'))).code).toBe('not_found');
  });
  it('an install that stops in its download ends failed, with the job’s own words', async () => {
    const r = rig();
    const id = (await pairDesk(r)).engine!.id;
    r.fleet.engines.failJob = true;
    const pull = await r.api.pull(id, { model: 'qwen3.8-27b' });
    r.wait(60_000);
    const p = (await r.api.progress(id, pull.job!.id)).progress!;
    expect([p.state, p.finished]).toEqual(['failed', true]);
    expect(p.message).toContain('The download stopped');
    expect((await r.api.engine(id)).wizard?.next).toBe('model');
    // the next install runs
    expect((await r.api.pull(id, { model: 'qwen3.8-flash-next' })).job).not.toBeNull();
  });
  it('a member that is not an engine host is said to be none', async () => {
    const r = rig('fleet1');
    const f = await refused(r.api.engine('desk'));
    expect([f.code, f.status, f.node]).toEqual(['not_ssh', 409, 'Desk']);
  });
});

describe('secrets, and the page’s client', () => {
  it('no answer holds the code, a password or a private key', async () => {
    const r = rig();
    await r.api.discover();
    await pairDesk(r);
    const p = await r.api.passwordPlan({ host: M.attic, user: M.user });
    await r.api.passwordConfirm(p.plan, 'wrong', p.host_key.fingerprint).catch(() => undefined);
    await r.api.passwordConfirm(p.plan, M.password, p.host_key.fingerprint);
    await r.api.keyNew({ host: M.tailnet, user: 'gpu', engine_dir: '/opt/tandem' });
    const all = r.seen.join('\n');
    expect(r.seen.length).toBeGreaterThan(5);
    for (const secret of [M.code, M.password, 'PRIVATE KEY', '"token":"']) expect(all.includes(secret), secret).toBe(false);
  });
  it('a refusal is an EngineError with its finding; the guard’s 401 asks for a sign-in; no answer is said so', async () => {
    const guard = enginesApi(async () => ({ status: 401, body: { error: { type: 'unauthorized', message: 'a session or an admin token is required' } } }));
    await expect(guard.pairStart({ address: M.desk })).rejects.toBeInstanceOf(SignInNeeded);
    await expect(guard.pairStart({ address: M.desk })).rejects.toThrow('a session or an admin token is required');
    // an engine's refused token is a 401 too, and a finding, not a sign-in
    const r = rig();
    const id = (await pairDesk(r)).engine!.id;
    r.fleet.engines.fail('models', 'token_refused');
    const f = await refused(r.api.models(id));
    expect([f.code, f.status]).toEqual(['token_refused', 401]);
    const off = await refused(enginesApi(async () => ({ status: 0, body: null })).found());
    expect([off.code, off.status]).toEqual(['offline', 0]);
    const odd = await refused(enginesApi(async () => ({ status: 500, body: { error: { type: 'internal_error', message: 'boom' } } })).found());
    expect([odd.code, odd.message]).toEqual(['error', 'TandemApp answered: boom']);
    for (const x of [off, odd]) expect(x.fix.length).toBeGreaterThan(10);
  });
});
