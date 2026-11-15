// Tests for SRV-829 (the page part): click the engine, type the code (lib/engine-pair.ts,
// views/fleet/engine/pair.ts), against the mock controller.
//   Gherkin (plan 13 item 8):
//   Given a found engine and its code, When the user types the code, Then TandemApp's key is
//     installed, the host key pinned and the HTTP token stored, And the engine's status is shown
//                                                   "the right code pairs"
//   When the code is wrong, Then the page says so and how to get a new one
//                                                   "a wrong code", "five wrong codes"
import { describe, expect, it } from 'vitest';
import { MOCK_ENGINES as M } from '../mock/enginehosts.ts';
import type { PairStart } from '../src/api/engines.ts';
import { PairFlow, RAN_OUT, blocked, codeDigits, compareOf, ranOut, secondsLeft, waitLeft } from '../src/lib/engine-pair.ts';
import { pairView, type PairHandlers } from '../src/views/fleet/engine/pair.ts';
import { rig, text, type Rig } from './engine-rig.ts';

const ON: PairHandlers = { code: () => undefined, pasted: () => undefined, submit: () => undefined, renew: () => undefined, cancel: () => undefined, other: () => undefined };

/** a pairing started from the list, on the rig's clock */
async function begin(r: Rig, address: string = M.desk): Promise<{ f: PairFlow; start: PairStart }> {
  const adv = (await r.api.discover()).engines.find((e) => e.address === address);
  const start = await r.api.pairStart({ address, ...(adv?.fingerprint ? { fingerprint: adv.fingerprint } : {}) });
  return { f: new PairFlow(r.api, start, adv?.name ?? address, () => undefined, r.now), start };
}

describe('the code field', () => {
  it('keeps six digits of whatever is typed or pasted', () => {
    expect([codeDigits('482913'), codeDigits('482 913'), codeDigits('482-913\n'), codeDigits('code: 4829137'), codeDigits('abc')]).toEqual(['482913', '482913', '482913', '482913', '']);
  });
});

describe('pairing by code against the controller', () => {
  it('the right code pairs: the engine is in the fleet and its status comes back', async () => {
    const r = rig();
    const { f, start } = await begin(r);
    expect([f.state.triesLeft, secondsLeft(f.state, r.now()), blocked(f.state, r.now())]).toEqual([5, 300, 'code']);
    f.setCode('482 913');
    expect([f.state.code, blocked(f.state, r.now())]).toEqual(['482913', null]);
    const paired = await f.submit();
    expect([paired?.state, paired?.via, paired?.engine?.alias, paired?.engine?.host_key]).toEqual(['paired', 'code', 'spark-desk', start.fingerprint]);
    // the key is installed, the host key pinned, the token stored; the status is the engine's own
    expect([paired?.engine?.ssh.key_id, paired?.engine?.http.token.set, paired?.engine?.status?.engine.state]).toEqual([start.key_id, true, 'healthy']);
    expect(paired?.engine?.wizard?.next).toBe('model');
    expect([f.state.code, f.state.error, f.state.busy]).toEqual(['', null, null]);
  });
  it('a wrong code: the page says so, how many tries are left, and empties the field', async () => {
    const r = rig();
    const { f } = await begin(r);
    f.setCode('000000');
    expect(await f.submit()).toBeNull();
    expect([f.state.error?.code, f.state.triesLeft, f.state.code, f.state.ended]).toEqual(['wrong_code', 4, '', null]);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain('The code does not match: 4 tries left.');
    expect(t).toContain('after five wrong tries run <code>tandem pair</code> there again.');
    expect(t).toContain('<dt>Tries left</dt>');
    expect(t).toContain('<span data-tries>4 tries left</span>');
    expect(t).not.toContain('data-act="renew"');
    f.setCode(M.code);
    expect((await f.submit())?.state).toBe('paired');
  });
  it('five wrong codes spend it: the page says how to get a new one, and one button takes it', async () => {
    const r = rig();
    const { f, start } = await begin(r);
    for (let i = 0; i < 5; i++) {
      f.setCode('000000');
      await f.submit();
    }
    expect([f.state.error?.code, f.state.ended, blocked(f.state, r.now())]).toEqual(['code_spent', 'spent', 'ended']);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain('data-ended=spent ');
    expect(t).toContain('This code cannot be used any more');
    expect(t).toContain('Run <code>tandem pair</code> on the engine host again for a new code.');
    expect(t).toContain('data-act="renew"');
    // the field is off until then
    expect(t).toMatch(/id="eh-code"[^>]*\?disabled=true/);
    f.setCode(M.code);
    expect(await f.submit()).toBeNull();
    expect(await f.renew()).toBe(true);
    expect(f.state.start.pairing).not.toBe(start.pairing);
    expect([f.state.ended, f.state.error, f.state.triesLeft, f.state.code]).toEqual([null, null, 5, '']);
    f.setCode(M.code);
    expect((await f.submit())?.state).toBe('paired');
  });
  it('a code whose five minutes are over: the page says so before the engine host does', async () => {
    const r = rig();
    const { f } = await begin(r);
    r.wait(120_000);
    expect(secondsLeft(f.state, r.now())).toBe(180);
    expect(text(pairView(f.state, r.now(), ON))).toContain('<span class="num eh-left" data-left>3:00</span>');
    r.wait(180_000);
    expect([ranOut(f.state, r.now()), blocked({ ...f.state, code: M.code }, r.now())]).toEqual([true, 'ended']);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain(RAN_OUT.message);
    expect(t).toContain('data-act="renew"');
    // the engine host agrees; a new code after `tandem pair`
    r.wait(2_000);
    expect(await f.renew()).toBe(false);
    expect(f.state.error?.code).toBe('code_spent');
    expect(await f.renew()).toBe(true);
    expect(secondsLeft(f.state, r.now())).toBe(300);
  });
  it('a host key that is not the one shown stops the pairing: both fingerprints, nothing to press but back', async () => {
    const r = rig();
    const { f } = await begin(r);
    r.fleet.engines.fail('pair-code', 'host_key_mismatch');
    f.setCode(M.code);
    expect(await f.submit()).toBeNull();
    expect([f.state.error?.code, f.state.ended, blocked(f.state, r.now())]).toEqual(['host_key_mismatch', 'stop', 'ended']);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain('<dt>Shown here</dt>');
    expect(t).toContain('<dt>The engine host has</dt>');
    expect(t).toContain('do not pair.');
    expect(t).toContain('Back to Add engine');
    expect(t).not.toContain('data-act="renew"');
    expect(t).toMatch(/data-act="pair" \?disabled=true/);
  });
  it('too many tries wait: the button is off for the seconds the answer names', async () => {
    const r = rig();
    const { f } = await begin(r);
    r.fleet.engines.fail('pair-code', 'rate_limited');
    f.setCode(M.code);
    await f.submit();
    expect([f.state.error?.code, waitLeft(f.state, r.now()), f.state.ended]).toEqual(['rate_limited', 30, null]);
    f.setCode(M.code);
    expect(blocked(f.state, r.now())).toBe('wait');
    expect(text(pairView(f.state, r.now(), ON))).toContain('You can try again in 30 s.');
    r.fleet.engines.fail('pair-code', null);
    r.wait(30_000);
    expect(blocked(f.state, r.now())).toBeNull();
    expect((await f.submit())?.state).toBe('paired');
  });
  it('the other refusals keep the pairing: the sentence and the fix, and the code may be typed again', async () => {
    for (const code of ['key_present', 'key_refused', 'engine_failed', 'unreachable']) {
      const r = rig();
      const { f } = await begin(r);
      r.fleet.engines.fail('pair-code', code);
      f.setCode(M.code);
      await f.submit();
      expect([f.state.error?.code, f.state.ended], code).toEqual([code, null]);
      const t = text(pairView(f.state, r.now(), ON)).replace(/<\/?code>/g, '`');
      expect(t, code).toContain(f.state.error!.message);
      expect(t, code).toContain(f.state.error!.fix);
    }
    // a pairing this TandemApp forgot is started again like a spent code
    const r = rig();
    const { f } = await begin(r);
    await r.api.pairAbandon(f.state.start.pairing);
    f.setCode(M.code);
    await f.submit();
    expect([f.state.error?.code, f.state.ended]).toEqual(['not_found', 'spent']);
    expect(await f.renew()).toBe(true);
  });
  it('leaving forgets the pairing', async () => {
    const r = rig();
    const { f, start } = await begin(r);
    await f.cancel();
    expect(await r.api.pairAbandon(start.pairing)).toMatchObject({ abandoned: false });
  });
});

describe('the page', () => {
  it('shows the address and the fingerprint it has, and takes the code: the same three rows as the terminal', async () => {
    const r = rig();
    const { f, start } = await begin(r);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain('Pair with spark-desk');
    expect(t).toContain(`<span class="num eh-wrap">${M.desk}:8000</span>`);
    expect(t).toContain(`data-fp=${start.fingerprint} `);
    expect(t).toContain('<label for="eh-code">Pairing code</label>');
    expect(t).toContain('autocomplete="one-time-code"');
    expect(t).toContain('inputmode="numeric"');
    expect(t).toContain('<span class="num eh-left" data-left>5:00</span>');
    expect(t).toContain('<span data-tries>5 tries left</span>');
    for (const id of ['address', 'fingerprint', 'code']) expect(t).toContain(`data-slip=${id}>`);
    expect(t).toMatch(/class="eh-slip-row is-now\s*" data-slip=code>/);
    expect(t).toContain('No code? Other ways to connect');
    // the page never shows a code
    expect(t).not.toContain(M.code);
  });
  it('a pasted fingerprint that differs turns Pair off and says why', async () => {
    const r = rig();
    const { f, start } = await begin(r);
    f.setCode(M.code);
    f.setPasted(start.fingerprint!);
    expect([compareOf(f.state), blocked(f.state, r.now())]).toEqual(['same', null]);
    f.setPasted(start.fingerprint!.replace(/.$/, '/'));
    expect([compareOf(f.state), blocked(f.state, r.now())]).toEqual(['different', 'different']);
    const t = text(pairView(f.state, r.now(), ON));
    expect(t).toContain('data-different');
    expect(t).toMatch(/data-act="pair" \?disabled=true/);
    expect(await f.submit()).toBeNull();
    expect(r.seen.filter((x) => x.includes('"state":"paired"'))).toHaveLength(0);
  });
  it('an engine with no fingerprint to show says the code still proves it', async () => {
    const r = rig();
    const { f } = await begin(r);
    const t = text(pairView({ ...f.state, start: { ...f.state.start, fingerprint: null } }, r.now(), ON));
    expect(t).toContain('shows no fingerprint here');
  });
});
