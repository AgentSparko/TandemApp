// Tests for SRV-827 (the page part): Add engine lists what the LAN advertises and takes an address
// (lib/engine-find.ts, views/fleet/engine/find.ts), against the mock controller.
//   Gherkin (plan 13 item 7):
//   Given an advertised engine on the LAN, When the user opens "Add engine", Then it is listed
//     with its name and fingerprint                 "opening the page searches and lists"
//   Given no engine is found, Then the user can type an address, a tailnet one included
//                                                   "no engine found", "an address typed by hand"
import { describe, expect, it } from 'vitest';
import { MOCK_ENGINES as M } from '../mock/enginehosts.ts';
import { enginesApi, type DiscoverView, type ProbeView } from '../src/api/engines.ts';
import { ENGINE_PORT, FindFlow, listWords, parseTarget, probeNext, rowKey, startNext } from '../src/lib/engine-find.ts';
import { findView, guide, type FindHandlers } from '../src/views/fleet/engine/find.ts';
import { finding } from '../mock/enginecodes.ts';
import { rig, text } from './engine-rig.ts';

const ON: FindHandlers = { search: () => undefined, pick: () => undefined, address: () => undefined, port: () => undefined, check: () => undefined, other: () => undefined };

describe('the address field', () => {
  it('reads a name, an address, host:port, a bracketed IPv6 address and a pasted URL', () => {
    expect(parseTarget('192.0.2.80', '')).toEqual({ address: '192.0.2.80', port: ENGINE_PORT });
    expect(parseTarget('  gpu-box.tailnet.example ', '8010')).toEqual({ address: 'gpu-box.tailnet.example', port: 8010 });
    expect(parseTarget('192.0.2.80:8010', '')).toEqual({ address: '192.0.2.80', port: 8010 });
    expect(parseTarget('http://192.0.2.80:8000/v1', '')).toEqual({ address: '192.0.2.80', port: 8000 });
    expect(parseTarget('[fd7a:115c::1]:8001', '')).toEqual({ address: 'fd7a:115c::1', port: 8001 });
    expect(parseTarget('fd7a:115c::1', '')).toEqual({ address: 'fd7a:115c::1', port: ENGINE_PORT });
  });
  it('says in words what is missing', () => {
    expect(parseTarget('', '')).toMatch(/Type the engine’s address/);
    expect(parseTarget('two words', '')).toMatch(/a name or an IP address/);
    expect(parseTarget('-oProxyCommand=x', '')).toMatch(/a name or an IP address/);
    for (const p of ['0', '70000', '8000.5', 'abc']) expect(parseTarget('192.0.2.80', p), p).toMatch(/A port is a whole number/);
  });
});

describe('what an answer means for the page', () => {
  const probe = (o: Partial<ProbeView>): ProbeView => ({ contract_version: '1.0', address: 'a', port: 8000, reachable: true, engine: true, pairing: null, fingerprint: null, node: null, finding: null, ...o });
  it('a window open pairs; an engine in the fleet opens its setup; the rest says what to do', () => {
    expect(probeNext(probe({ pairing: { open: true, state: 'open', expires_in: 300, tries_left: 5, fingerprint: null } }))).toBe('pair');
    expect(probeNext(probe({ node: 'eh-1', finding: finding('code_spent') }))).toBe('in-fleet');
    expect(probeNext(probe({ finding: finding('no_window') }))).toBe('new-code');
    expect(probeNext(probe({ finding: finding('code_spent') }))).toBe('new-code');
    expect(probeNext(probe({ finding: finding('pairing_unsupported') }))).toBe('other-ways');
    for (const c of ['unreachable', 'not_an_engine', 'not_allowed', 'rate_limited']) expect(probeNext(probe({ finding: finding(c) })), c).toBe('retry');
    expect(probeNext(probe({ engine: false }))).toBe('retry');
    expect([startNext(finding('no_window')), startNext(finding('pairing_unsupported')), startNext(finding('host_key_mismatch'))]).toEqual(['new-code', 'other-ways', 'retry']);
  });
  it('the list’s status line', () => {
    const v = (o: Partial<DiscoverView>): DiscoverView => ({ contract_version: '1.0', engines: [], browsed_at: 1, seconds: 2, lan: 'ok', finding: null, ...o });
    expect(listWords({ view: null, searching: true })).toBe('Searching your network…');
    expect(listWords({ view: null, searching: false })).toBe('Not searched yet.');
    expect(listWords({ view: v({}), searching: false })).toBe('No engine answered.');
    expect(listWords({ view: v({ lan: 'denied' }), searching: false })).toBe('The search could not run.');
    const one = { name: 'a', instance: 'a', version: null, address: '192.0.2.1', addresses: [], port: 8000, fingerprint: null, verified: false as const, node: null, alias: null };
    expect(listWords({ view: v({ engines: [one] }), searching: false })).toBe('1 engine answered.');
    expect(listWords({ view: v({ engines: [one, one] }), searching: false })).toBe('2 engines answered.');
  });
});

describe('Add engine against the controller', () => {
  it('opening the page searches and lists each engine with its name, version, address and fingerprint', async () => {
    const r = rig();
    let changes = 0;
    const f = new FindFlow(r.api, () => changes++);
    await f.open();
    expect(changes).toBeGreaterThan(1);
    expect([f.state.searching, f.state.view?.lan, f.state.view?.engines.length]).toEqual([false, 'ok', 4]);
    const t = text(findView(f.state, ON));
    for (const e of f.state.view!.engines) {
      expect(t).toContain(`data-engine=${rowKey(e)} `);
      expect(t).toContain(`<span class="eh-row-name">${e.name}</span>`);
      expect(t).toContain(`${e.address}:${e.port}`);
      if (e.version) expect(t).toContain(`<span class="wz-pill">${e.version}</span>`);
      if (e.fingerprint) expect(t).toContain(e.fingerprint);
    }
    expect(t).toContain('advertises no fingerprint');
    expect(t).toContain('4 engines answered.');
    // each row is a button with a name a screen reader can say
    expect(t).toContain(`aria-label=Pair with spark-desk at ${M.desk}:8000 `);
    // the address field and the other ways are on the page from the start
    expect(t).toContain('data-in="address"');
    expect(t).toContain('Other ways to connect');
  });
  it('a row starts the pairing with its address, port and advertised fingerprint', async () => {
    const r = rig();
    const f = new FindFlow(r.api);
    await f.open();
    const desk = f.state.view!.engines[0];
    const got = await f.pick(desk);
    expect(got?.name).toBe('spark-desk');
    expect([got?.start.address, got?.start.port, got?.start.fingerprint, got?.start.advertised, got?.start.window.open]).toEqual([M.desk, 8000, desk.fingerprint, desk.fingerprint, true]);
    expect([f.state.busyRow, f.state.rowError]).toEqual([null, {}]);
  });
  it('a refusal stays under its row with the sentence, the fix and what to press', async () => {
    const r = rig();
    const f = new FindFlow(r.api);
    await f.open();
    const [, rack, attic, lab] = f.state.view!.engines;
    expect(await f.pick(rack)).toBeNull();
    expect(f.state.rowError[rowKey(rack)].code).toBe('no_window');
    let t = text(findView(f.state, ON));
    expect(t).toContain('No pairing code is showing on the engine host.');
    expect(t).toContain('Run <code>tandem pair</code> on the engine host and type the code it prints.');
    expect(t).toContain('I ran it: check again');
    // `tandem pair` ran: the same row pairs now, and its error is gone
    expect((await f.pick(rack))?.start.window.open).toBe(true);
    expect(f.state.rowError[rowKey(rack)]).toBeUndefined();
    expect(await f.pick(attic)).toBeNull();
    expect(await f.pick(lab)).toBeNull();
    t = text(findView(f.state, ON));
    expect(f.state.rowError[rowKey(attic)].code).toBe('pairing_unsupported');
    expect(t).toContain('data-act="other-ways"');
    expect(t).toContain('Connect another way');
    const m = f.state.rowError[rowKey(lab)];
    expect(m.code).toBe('host_key_mismatch');
    expect(t).toContain(`<dt>Shown here</dt><dd class=num eh-wrap>${m.expected}</dd>`);
    expect(t).toContain(`<dt>The engine host has</dt><dd class=num eh-wrap>${m.offered}</dd>`);
  });
  it('no engine found: the page says so and the address field takes a tailnet name', async () => {
    const r = rig();
    r.fleet.engines.lanMode = 'deny';
    const f = new FindFlow(r.api);
    await f.open();
    expect([f.state.view?.lan, f.state.view?.engines.length]).toEqual(['denied', 0]);
    const t = text(findView(f.state, ON));
    expect(t).toContain('data-lan=denied ');
    expect(t).toContain('This TandemApp may not use the local network, so it found no engine.');
    expect(t).toContain('Allow it under System Settings, Privacy & Security, Local Network, or type the engine\'s address.');
    expect(t).toContain('data-in="address"');
    f.setAddress(M.tailnet);
    const got = await f.check();
    expect([got?.name, got?.start.address, got?.start.port, got?.start.advertised]).toEqual([M.tailnet, M.tailnet, 8000, null]);
    expect(got?.start.fingerprint).toMatch(/^SHA256:/);
  });
  it('an address typed by hand: a finding shows with its fix; a form that cannot be sent says why', async () => {
    const r = rig();
    const f = new FindFlow(r.api);
    expect(await f.check()).toBeNull();
    expect(f.state.formError).toMatch(/Type the engine’s address/);
    f.setAddress(M.silent);
    expect(f.state.formError).toBeNull();
    expect(await f.check()).toBeNull();
    expect([f.state.probe?.reachable, f.state.probe?.finding?.code]).toEqual([false, 'unreachable']);
    let t = text(findView(f.state, ON));
    expect(t).toContain(`TandemApp cannot reach the engine at ${M.silent}:8000.`);
    expect(t).toContain('Try again');
    f.setAddress(`${M.other}:8000`);
    await f.check();
    expect(f.state.probe?.finding?.code).toBe('not_an_engine');
    f.setAddress(M.attic);
    await f.check();
    t = text(findView(f.state, ON));
    expect(f.state.probe?.finding?.code).toBe('pairing_unsupported');
    expect(t).toContain('Connect another way');
    // no code showing: said, and after `tandem pair` the same button pairs
    f.setAddress(M.rack);
    expect(await f.check()).toBeNull();
    expect(f.state.probe?.finding?.code).toBe('no_window');
    expect((await f.check())?.start.window.open).toBe(true);
    f.setAddress('not an address');
    expect(await f.check()).toBeNull();
    expect(f.state.formError).toMatch(/a name or an IP address/);
  });
  it('an engine already in the fleet says so in the list and at its address, and leads to its setup', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.desk });
    const id = (await r.api.pairCode(s.pairing, M.code)).engine!.id;
    const f = new FindFlow(r.api);
    await f.open();
    expect(f.state.view!.engines[0].node).toBe(id);
    expect(await f.pick(f.state.view!.engines[0])).toBeNull();
    f.setAddress(M.desk);
    expect(await f.check()).toBeNull();
    expect(probeNext(f.state.probe!)).toBe('in-fleet');
    const t = text(findView(f.state, ON));
    expect(t).toContain('data-in-fleet=true>');
    expect(t).toContain('in your fleet');
    expect(t.match(new RegExp(`href=#/add-engine/${id}`, 'g'))).toHaveLength(2);
    expect(t).toContain('is in your fleet already.');
  });
  it('a search that is refused shows its finding; the guard’s 401 is a sign-in, not an error with a button', async () => {
    const r = rig();
    r.fleet.engines.fail('discover', 'lan_failed');
    const f = new FindFlow(r.api);
    await f.open();
    expect(f.state.searchError?.code).toBe('lan_failed');
    expect(text(findView(f.state, ON))).toContain('The search on the local network failed: PermissionError');
    const locked = new FindFlow(enginesApi(async () => ({ status: 401, body: { error: { type: 'unauthorized', message: 'a session or an admin token is required' } } })));
    await locked.open();
    expect(locked.state.searchError?.code).toBe('sign_in');
    const t = text(findView(locked.state, ON));
    expect(t).toContain('TandemApp’s admin password is needed for this.');
    expect(t).not.toContain('Try again');
  });
});

describe('the page', () => {
  it('first says what to look for on the engine host: the three things its install printed', () => {
    const t = text(guide());
    for (const id of ['code', 'fingerprint', 'address']) expect(t).toContain(`data-slip=${id}>`);
    for (const label of ['Pairing code', 'Fingerprint', 'Address']) expect(t).toContain(`<dt>${label}</dt>`);
    expect(t).toContain('<code>tandem pair</code>');
    // nothing here looks like a real code or address to type
    expect(t).not.toMatch(/\d{6}/);
  });
  it('while it searches the list is busy; with nothing found it points at the address field', () => {
    const base = new FindFlow(rig().api).state;
    expect(text(findView({ ...base, searching: true }, ON))).toContain('Searching your network…');
    const none = text(findView({ ...base, view: { contract_version: '1.0', engines: [], browsed_at: 1, seconds: 2, lan: 'ok', finding: null } }, ON));
    expect(none).toContain('No engine answered.');
    expect(none).toContain('data-empty');
    expect(none).toContain('Type its address: the terminal printed it.');
  });
});
