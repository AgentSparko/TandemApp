// Tests for SRV-831 (the page part): the other ways to connect an engine host (lib/
// engine-connect.ts, views/fleet/engine/connect.ts), against the mock controller.
//   Gherkin (plan 13 item 9):
//   Given "Other ways", When the user gives a user name and password, Then the key is installed,
//     the session closed and the password kept nowhere      "password once"
//   When the user chooses "paste a key", Then the page shows the authorized_keys line and waits
//     for the first login                                   "paste a key"
import { describe, expect, it } from 'vitest';
import { KEY_PASTE_S, MOCK_ENGINES as M } from '../mock/enginehosts.ts';
import { CHECK_EVERY_MS, ConnectFlow, compareOf, fingerprintCommand, parseSshTarget, pasteCommand, shownFingerprint, stopped } from '../src/lib/engine-connect.ts';
import { connectView, type ConnectHandlers } from '../src/views/fleet/engine/connect.ts';
import { rig, text, type Rig } from './engine-rig.ts';

const ON: ConnectHandlers = {
  way: () => undefined, field: () => undefined, pasted: () => undefined, password: () => undefined, read: () => undefined, connect: () => undefined,
  makeLine: () => undefined, confirmKey: () => undefined, check: () => undefined, refuse: () => undefined, reset: () => undefined, back: () => undefined,
};

function flow(r: Rig, host: string = M.attic, user: string = M.user): ConnectFlow {
  const f = new ConnectFlow(r.api, () => undefined, host);
  f.setField('user', user);
  return f;
}

describe('the form', () => {
  const form = (o: Partial<{ host: string; port: string; user: string; dir: string; way: 'password' | 'key' }>) => parseSshTarget({ host: '', port: '', user: '', dir: '', way: 'password', ...o });
  it('reads an address, a port, a user and a folder; user@host and host:port in the address field too', () => {
    expect(form({ host: '192.0.2.82', user: 'spark' })).toEqual({ host: '192.0.2.82', port: 22, user: 'spark', dir: null });
    expect(form({ host: 'spark@192.0.2.82' })).toEqual({ host: '192.0.2.82', port: 22, user: 'spark', dir: null });
    expect(form({ host: 'ssh gpu@gpu-box.tailnet.example:2222' })).toEqual({ host: 'gpu-box.tailnet.example', port: 2222, user: 'gpu', dir: null });
    expect(form({ host: '192.0.2.82', port: '2201', user: 'spark', dir: '~/engines/tandem/' })).toEqual({ host: '192.0.2.82', port: 2201, user: 'spark', dir: '~/engines/tandem' });
    expect(form({ host: '192.0.2.82', user: 'spark', dir: '/opt/tandem', way: 'key' })).toMatchObject({ dir: '/opt/tandem' });
  });
  it('says in words what is missing or wrong', () => {
    expect(form({ user: 'spark' })).toMatch(/Type the engine host’s address/);
    expect(form({ host: '192.0.2.82' })).toMatch(/Type the user name/);
    expect(form({ host: '192.0.2.82', user: 'a b' })).toMatch(/A user name is letters/);
    expect(form({ host: '192.0.2.82', user: 'spark', port: '0' })).toMatch(/The SSH port is a whole number/);
    expect(form({ host: '192.0.2.82', user: 'spark', dir: 'relative' })).toMatch(/The engine’s folder is a path/);
    expect(form({ host: '192.0.2.82', user: 'spark', dir: '/a/../b' })).toMatch(/The engine’s folder is a path/);
    expect(form({ host: '192.0.2.82', user: 'spark', dir: '~/TandemLLM', way: 'key' })).toMatch(/a full path that starts with \//);
  });
  it('the key line as one command for the terminal, and where a host prints its own fingerprint', () => {
    const cmd = pasteCommand({ line: 'command="/bin/bash /home/spark/TandemLLM/bin/tandem remote --key-id k",no-pty ssh-ed25519 AAAA k' });
    expect(cmd).toBe(`mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo 'command="/bin/bash /home/spark/TandemLLM/bin/tandem remote --key-id k",no-pty ssh-ed25519 AAAA k' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`);
    expect(pasteCommand({ line: "it's" })).toBeNull();
    expect([fingerprintCommand('ssh-ed25519'), fingerprintCommand('ecdsa-sha2-nistp256'), fingerprintCommand('ssh-rsa'), fingerprintCommand(null)]).toEqual([
      'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub', 'ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub', 'ssh-keygen -lf /etc/ssh/ssh_host_rsa_key.pub', 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub',
    ]);
  });
});

describe('password once', () => {
  it('the fingerprint is read and shown first; "It matches: connect" logs in once and pairs', async () => {
    const r = rig();
    const f = flow(r);
    let t = text(connectView(f.state, '', ON));
    expect(t).toContain('data-act="read"');
    expect(t).not.toContain('data-in="password"');
    await f.read();
    expect([f.state.plan?.host, f.state.plan?.host_key.known, f.state.error]).toEqual([M.attic, 'new', null]);
    t = text(connectView(f.state, '', ON));
    expect(t).toContain(`Check the fingerprint of ${M.attic}`);
    expect(t).toContain(`data-fp=${f.state.plan!.host_key.fingerprint} `);
    expect(t).toContain('<code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>');
    expect(t).toContain('data-in="password"');
    expect(t).toContain('It matches: connect');
    expect(t).toContain('It does not match');
    // no password typed: nothing to send
    expect(t).toMatch(/data-act="connect" \?disabled=true/);
    expect(await f.connect('')).toBeNull();
    const paired = await f.connect(M.password);
    expect([paired?.state, paired?.via, paired?.engine?.via, paired?.engine?.ssh.user]).toEqual(['paired', 'password', 'ssh', M.user]);
  });
  it('a wrong password says so and keeps the fingerprint; the password is in no state and no answer', async () => {
    const r = rig();
    const f = flow(r);
    await f.read();
    expect(await f.connect('wrong-pass')).toBeNull();
    expect([f.state.error?.code, !!f.state.plan, f.state.busy]).toEqual(['auth_failed', true, null]);
    const t = text(connectView(f.state, '', ON));
    expect(t).toContain('The engine host did not accept that user name and password.');
    expect(t).toContain(`Check the user name and password for ${M.attic} and try again.`);
    expect(JSON.stringify(f.state)).not.toContain('wrong-pass');
    expect((await f.connect(M.password))?.state).toBe('paired');
    expect(JSON.stringify(f.state)).not.toContain(M.password);
    expect(r.seen.join('\n')).not.toContain(M.password);
  });
  it('"It does not match" sends nothing; a pasted fingerprint that differs keeps Connect off', async () => {
    const r = rig();
    const f = flow(r);
    await f.read();
    const fp = f.state.plan!.host_key.fingerprint;
    f.setPasted(fp.replace(/.$/, '/'));
    expect(compareOf(f.state)).toBe('different');
    expect(text(connectView(f.state, M.password, ON))).toMatch(/data-act="connect" \?disabled=true/);
    expect(await f.connect(M.password)).toBeNull();
    const calls = r.seen.length;
    f.refuse();
    expect([f.state.refused, f.state.plan, f.state.confirmed]).toEqual([true, null, null]);
    expect(r.seen.length).toBe(calls);
    const t = text(connectView(f.state, '', ON));
    expect(t).toContain('Stopped. Nothing was sent to that host.');
    expect(t).toContain('data-act="read"');
  });
  it('a host key that changed is refused with both fingerprints and no way to connect anyway', async () => {
    const r = rig();
    const f = flow(r, M.changed);
    await f.read();
    expect([f.state.error?.code, f.state.plan, stopped(f.state)]).toEqual(['host_key_changed', null, true]);
    const t = text(connectView(f.state, M.password, ON));
    expect(t).toContain(`<dt>Pinned before</dt><dd class=num eh-wrap>${f.state.error!.pinned}</dd>`);
    expect(t).toContain(`<dt>Offered now</dt><dd class=num eh-wrap>${f.state.error!.offered}</dd>`);
    expect(t).toContain('otherwise do not connect.');
    for (const act of ['connect', 'read', 'confirm-key', 'line']) expect(t, act).not.toContain(`data-act="${act}"`);
    expect(t).toContain('Start over');
  });
  it('an engine in another folder: the fix names it, the folder is set, and Connect needs no second look at the fingerprint', async () => {
    const r = rig();
    const f = flow(r, M.tailnet, 'gpu');
    await f.read();
    const fp = f.state.plan!.host_key.fingerprint;
    expect(await f.connect('gpu-pass')).toBeNull();
    expect([f.state.error?.code, f.state.error?.dir]).toEqual(['not_installed', '~/TandemLLM']);
    let t = text(connectView(f.state, '', ON));
    expect(t).toContain(`No TandemLLM engine is installed at ~/TandemLLM on ${M.tailnet}.`);
    expect(t).toMatch(/<details class="eh-folder" \?open=true>/);
    f.setField('dir', '/opt/tandem');
    expect([f.state.plan, f.state.error, f.state.confirmed?.fingerprint]).toEqual([null, null, fp]);
    t = text(connectView(f.state, '', ON));
    expect(t).toContain('data-confirmed');
    expect(t).toContain('data-in="password"');
    expect((await f.connect('gpu-pass'))?.state).toBe('paired');
    // another host is another fingerprint to look at
    f.setField('host', M.attic);
    expect(f.state.confirmed).toBeNull();
  });
  it('what else can stop it, each in the controller’s words', async () => {
    for (const [host, code] of [[M.silent, 'ssh_unreachable'], ['198.51.100.7', 'ssh_unreachable']] as const) {
      const r = rig();
      const f = flow(r, host);
      await f.read();
      expect(f.state.error?.code, host).toBe(code);
    }
    const r = rig();
    const old = flow(r, M.ancient);
    await old.read();
    await old.connect(M.password);
    expect(old.state.error?.code).toBe('engine_too_old');
    for (const code of ['key_present', 'key_refused', 'bad_engine_dir', 'ssh_failed', 'ctl_failed', 'too_many', 'not_found']) {
      const x = rig();
      const f = flow(x);
      await f.read();
      x.fleet.engines.fail('password-confirm', code);
      await f.connect(M.password);
      expect(f.state.error?.code, code).toBe(code);
      const t = text(connectView(f.state, '', ON)).replace(/<\/?code>/g, '`');
      expect(t, code).toContain(f.state.error!.message);
      expect(t, code).toContain(f.state.error!.fix);
      // a plan this TandemApp forgot is read again; the others keep it
      expect(!!f.state.plan, code).toBe(code !== 'not_found');
    }
    const bad = flow(rig(), '');
    await bad.read();
    expect(bad.state.formError).toMatch(/Type the engine host’s address/);
  });
});

describe('paste a key', () => {
  it('the page shows the line and where it goes, then waits for the first login', async () => {
    const r = rig();
    const f = flow(r);
    f.setWay('key');
    expect(text(connectView(f.state, '', ON))).toContain('data-act="line"');
    await f.makeLine();
    const k = f.state.key!;
    expect([k.file, k.user, k.host, k.engine_dir_guessed]).toEqual(['~/.ssh/authorized_keys', M.user, M.attic, true]);
    let t = text(connectView(f.state, '', ON));
    expect(t).toContain(`aria-label="The line to add">${k.line}</pre>`);
    expect(t).toContain('<code>~/.ssh/authorized_keys</code>');
    expect(t).toContain('aria-label=Copy the line ');
    expect(t).toContain('aria-label=Copy one command that adds the line ');
    expect(t).toContain('data-guessed');
    expect(t).toContain('/home/spark/TandemLLM');
    expect(t).toContain(`data-fp=${k.host_key!.fingerprint} `);
    expect(t).toContain('It matches: start checking');
    expect(t).toContain('It starts once you said the fingerprint matches.');
    // confirmed: the first check says not yet, with the controller's reason
    expect(await f.confirmKey()).toBeNull();
    expect([f.state.polling, f.state.waiting?.code, f.state.checks, f.state.confirmed?.fingerprint]).toEqual([true, 'key_not_installed', 1, k.host_key!.fingerprint]);
    t = text(connectView(f.state, '', ON));
    expect(t).toContain('The engine host does not take TandemApp\'s key yet.');
    expect(t).toContain(`Add the line shown to ~/.ssh/authorized_keys of ${M.user} on ${M.attic}, then check again.`);
    expect(t).toContain('Asked 1 time.');
    expect(t).toContain(`It asks again every ${CHECK_EVERY_MS / 1000} seconds`);
    expect(t).not.toContain('It matches: start checking');
    // the fields are locked while it waits
    expect(t).toMatch(/data-in="host"[^>]*\?readonly=true/);
    r.wait(CHECK_EVERY_MS);
    expect(await f.check()).toBeNull();
    expect(f.state.checks).toBe(2);
    r.wait(KEY_PASTE_S * 1000);
    const paired = await f.check();
    expect([paired?.state, paired?.via, f.state.polling, f.state.waiting]).toEqual(['paired', 'key', false, null]);
  });
  it('an SSH server that answers late: its finding first, then its fingerprint to confirm', async () => {
    const r = rig();
    const f = flow(r, M.sleepy);
    f.setWay('key');
    await f.makeLine();
    expect([f.state.key?.host_key, f.state.key?.finding?.code, shownFingerprint(f.state)]).toEqual([null, 'ssh_unreachable', null]);
    let t = text(connectView(f.state, '', ON));
    expect(t).toContain(`TandemApp cannot reach SSH on ${M.sleepy} port 22.`);
    expect(t).toContain('Try SSH again');
    expect(t).not.toContain('It matches: start checking');
    expect(await f.check()).toBeNull();
    expect([f.state.error, f.state.polling]).toEqual([null, false]);
    expect(shownFingerprint(f.state)).toMatch(/^SHA256:/);
    t = text(connectView(f.state, '', ON));
    expect(t).toContain('It matches: start checking');
    r.wait(KEY_PASTE_S * 1000);
    expect((await f.confirmKey())?.state).toBe('paired');
  });
  it('a line for the wrong folder stops the waiting and says where the engine was looked for', async () => {
    const r = rig();
    const f = flow(r, M.tailnet, 'gpu');
    f.setWay('key');
    await f.makeLine();
    r.wait(KEY_PASTE_S * 1000);
    expect(await f.confirmKey()).toBeNull();
    expect([f.state.error?.code, f.state.error?.dir, f.state.polling]).toEqual(['not_installed', '/home/gpu/TandemLLM', false]);
    const t = text(connectView(f.state, '', ON));
    expect(t).toContain('The checking stopped.');
    expect(t).toContain('Start over');
    // the folder set: a new line, and it pairs
    f.setField('dir', '/opt/tandem');
    expect(f.state.key).toBeNull();
    await f.makeLine();
    expect([f.state.key?.engine_dir, f.state.key?.engine_dir_guessed]).toEqual(['/opt/tandem', false]);
    expect(text(connectView(f.state, '', ON))).not.toContain('data-guessed');
    r.wait(KEY_PASTE_S * 1000);
    expect((await f.confirmKey())?.state).toBe('paired');
  });
  it('a host key that changed while it waits stops with both fingerprints', async () => {
    const r = rig();
    const f = flow(r);
    f.setWay('key');
    await f.makeLine();
    await f.confirmKey();
    r.fleet.engines.fail('key-check', 'host_key_changed');
    expect(await f.check()).toBeNull();
    expect([f.state.error?.code, f.state.polling, f.state.confirmed, stopped(f.state)]).toEqual(['host_key_changed', false, null, true]);
    const t = text(connectView(f.state, '', ON));
    expect(t).toContain('<dt>Pinned before</dt>');
    expect(t).not.toContain('data-act="check"');
    expect(t).not.toContain('data-act="confirm-key"');
  });
});

describe('the page', () => {
  it('two ways, one form; switching the way keeps the target and drops what was read', async () => {
    const r = rig();
    const f = flow(r);
    let t = text(connectView(f.state, '', ON));
    expect(t).toContain('role="radiogroup" aria-label="How to connect"');
    expect(t).toMatch(/aria-checked=true data-way-btn=password/);
    expect(t).toContain('kept nowhere');
    for (const id of ['host', 'ssh-port', 'user', 'dir']) expect(t).toContain(`data-in="${id}"`);
    await f.read();
    f.setWay('key');
    expect([f.state.plan, f.state.host, f.state.user, f.state.way]).toEqual([null, M.attic, M.user, 'key']);
    t = text(connectView(f.state, '', ON));
    expect(t).toMatch(/aria-checked=true data-way-btn=key/);
    expect(t).toContain('no password is typed here');
    expect(t).toContain('Back to Add engine');
  });
});
