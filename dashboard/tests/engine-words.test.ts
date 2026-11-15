// Tests for VIS-278's ground: what the Add engine pages say about a refusal (lib/engine-words.ts)
// and the parts that say it (views/fleet/engine/bits.ts): the controller's sentence and its fix on
// every finding, the facts on a line of their own (the tries left, both fingerprints, the seconds
// to wait), commands set as code, a pasted fingerprint compared, and the slip's rows.
import { describe, expect, it } from 'vitest';
import { ENGINE_CODES, finding } from '../mock/enginecodes.ts';
import { EngineError, SignInNeeded } from '../src/api/engines.ts';
import { SIGN_IN, clockLeft, compareFingerprint, factsOf, findingOf, hostPort, isFingerprint, isHostKeyStop, keyType, segments, toneOf, waitOf } from '../src/lib/engine-words.ts';
import { command, findingCard, fingerprint, ghost, sentence, slip } from '../src/views/fleet/engine/bits.ts';
import { text } from './engine-rig.ts';

const FP_A = `SHA256:${'A'.repeat(43)}`;
const FP_B = `SHA256:${'B'.repeat(43)}`;

describe('a finding in words', () => {
  it('a refusal is its own finding; the guard’s 401 asks for a sign-in; anything else still says what to do', () => {
    const f = finding('wrong_code', { tries_left: 2 });
    expect(findingOf(new EngineError(f, 403, '/v1/fleet/engines/pair/code'))).toBe(f);
    const s = findingOf(new SignInNeeded('a session or an admin token is required', '/x'));
    expect([s.code, s.detail]).toEqual([SIGN_IN, 'a session or an admin token is required']);
    expect(s.fix).toMatch(/Sign in/);
    const odd = findingOf(new TypeError('x is not a function'));
    expect(odd.code).toBe('error');
    expect(odd.message).toContain('x is not a function');
    expect(odd.fix.length).toBeGreaterThan(10);
  });
  it('the facts that get a line: tries left, both fingerprints, the wait', () => {
    expect(factsOf(finding('wrong_code', { tries_left: 3 }))).toEqual([{ label: 'Tries left', value: '3', mono: false }]);
    expect(factsOf(finding('host_key_mismatch', { expected: FP_A, offered: FP_B }))).toEqual([
      { label: 'Shown here', value: FP_A, mono: true },
      { label: 'The engine host has', value: FP_B, mono: true },
    ]);
    expect(factsOf(finding('host_key_changed', { pinned: FP_A, offered: FP_B })).map((x) => [x.label, x.value])).toEqual([['Pinned before', FP_A], ['Offered now', FP_B]]);
    expect(factsOf(finding('host_key_unconfirmed', { offered: FP_B })).map((x) => x.label)).toEqual(['Its fingerprint']);
    expect(factsOf(finding('rate_limited', { retry_after: 30 }))).toEqual([{ label: 'Wait', value: '30 seconds', mono: false }]);
    // the other side's words show once: in the sentence when it holds them, else on a line
    expect(factsOf(finding('key_refused', {}, '~/.ssh is a link'))).toEqual([]);
    expect(factsOf(finding('engine_too_old', { host: 'h' }, "unknown verb 'token'"))).toEqual([{ label: 'It said', value: "unknown verb 'token'", mono: true }]);
    expect(factsOf(finding('no_window'))).toEqual([]);
  });
  it('a host key that is not the one expected stops; a limit waits; a notice is not an error', () => {
    expect([isHostKeyStop(finding('host_key_mismatch')), isHostKeyStop(finding('host_key_changed')), isHostKeyStop(finding('wrong_code')), isHostKeyStop(null)]).toEqual([true, true, false, false]);
    expect([waitOf(finding('rate_limited', { retry_after: 20 })), waitOf(finding('rate_limited')), waitOf(finding('too_many')), waitOf(finding('wrong_code')), waitOf(null)]).toEqual([20, 60, 60, 0, 0]);
    expect(toneOf(finding('key_not_installed'))).toBe('info');
    for (const c of ['no_window', 'code_spent', 'lan_denied', 'pairing_unsupported', 'rate_limited', 'too_many', 'busy']) expect(toneOf(finding(c)), c).toBe('warn');
    for (const c of ['wrong_code', 'host_key_changed', 'unreachable', 'auth_failed', 'setup_locked']) expect(toneOf(finding(c)), c).toBe('err');
  });
  it('commands in a sentence are set apart', () => {
    expect(segments('Run `tandem pair` on the engine host and type the code it prints.')).toEqual([
      { text: 'Run ', code: false },
      { text: 'tandem pair', code: true },
      { text: ' on the engine host and type the code it prints.', code: false },
    ]);
    expect(segments('No command here.')).toEqual([{ text: 'No command here.', code: false }]);
    expect(text(sentence('Run `tandem doctor` there.'))).toBe('Run <code>tandem doctor</code> there.');
  });
  it('a pasted fingerprint: the same, different, or not one yet; a whole pasted line is read', () => {
    expect(isFingerprint(FP_A)).toBe(true);
    expect([isFingerprint('SHA256:short'), isFingerprint(null), isFingerprint(`MD5:${'a'.repeat(43)}`)]).toEqual([false, false, false]);
    expect(compareFingerprint(FP_A, '')).toBe('empty');
    expect(compareFingerprint(FP_A, '   ')).toBe('empty');
    expect(compareFingerprint(FP_A, FP_A)).toBe('same');
    expect(compareFingerprint(FP_A, `  Fingerprint: ${FP_A} (ED25519)\n`)).toBe('same');
    expect(compareFingerprint(FP_A, FP_B)).toBe('different');
    expect(compareFingerprint(FP_A, 'SHA256:AAAA')).toBe('unreadable');
    expect(compareFingerprint(FP_A, `${FP_A}A`)).toBe('unreadable');
    expect(compareFingerprint(null, FP_A)).toBe('unreadable');
  });
  it('small words: a countdown, an address with its port, a key type', () => {
    expect([clockLeft(272), clockLeft(60), clockLeft(9), clockLeft(0), clockLeft(-4)]).toEqual(['4:32', '1:00', '0:09', '0:00', '0:00']);
    expect([hostPort('192.0.2.80', 8000), hostPort('gpu-box.tailnet.example', 8000), hostPort('fd7a::1', 8000)]).toEqual(['192.0.2.80:8000', 'gpu-box.tailnet.example:8000', '[fd7a::1]:8000']);
    expect([keyType('ssh-ed25519'), keyType('ssh-rsa'), keyType('ecdsa-sha2-nistp256'), keyType(null)]).toEqual(['ED25519', 'RSA', 'ECDSA 256', '']);
  });
});

describe('the parts', () => {
  it('every code’s card shows the controller’s sentence and its fix, word for word', () => {
    for (const code of Object.keys(ENGINE_CODES)) {
      const f = finding(code, { address: '192.0.2.80', port: 8000, host: '192.0.2.80', user: 'spark', tries_left: 2, retry_after: 30, expected: FP_A, offered: FP_B, pinned: FP_A, model: 'm', node: 'Desk', dir: '~/TandemLLM' }, 'its own words');
      const t = text(findingCard(f)).replace(/<\/?code>/g, '`');
      expect(t, code).toContain(`data-finding=${code}>`);
      expect(t, code).toContain(f.message);
      expect(t, code).toContain(f.fix);
    }
  });
  it('an error is announced, a notice is not; the facts and the buttons are in the card', () => {
    const wrong = text(findingCard(finding('wrong_code', { tries_left: 3 })));
    expect(wrong).toContain('role=alert ');
    expect(wrong).toContain('<dt>Tries left</dt>');
    const mism = text(findingCard(finding('host_key_mismatch', { expected: FP_A, offered: FP_B })));
    expect(mism).toContain(`<dt>Shown here</dt><dd class=num eh-wrap>${FP_A}</dd>`);
    expect(mism).toContain(`<dt>The engine host has</dt><dd class=num eh-wrap>${FP_B}</dd>`);
    expect(text(findingCard(finding('key_not_installed', { user: 'spark', host: 'h' })))).toContain('role=status ');
    expect(text(findingCard(finding('no_window')))).toContain('role=status ');
    expect(text(findingCard(finding('unreachable'), { quiet: true }))).toContain('role=status ');
    const waits = text(findingCard(finding('rate_limited', { retry_after: 30 }), { wait: 12 }));
    expect(waits).toContain('You can try again in 12 s.');
    expect(text(findingCard(finding('no_window'), { actions: sentence('`x`') }))).toContain('<div class="eh-actions"><code>x</code></div>');
  });
  it('a fingerprint is shown whole with a copy button; pasted, it says the same or different', () => {
    const plain = text(fingerprint(FP_A, { type: 'ssh-ed25519' }));
    expect(plain).toContain(FP_A);
    expect(plain).toContain('ED25519');
    expect(plain).toContain('aria-label=Copy the fingerprint ');
    expect(plain).toContain('type="button"');
    expect(plain).not.toContain('data-in="fp-paste"');
    const on = () => undefined;
    expect(text(fingerprint(FP_A, { pasted: '', onPaste: on }))).toContain('data-compare=empty>');
    const same = text(fingerprint(FP_A, { pasted: FP_A, onPaste: on }));
    expect(same).toContain('data-compare=same>');
    expect(same).toContain('The same.');
    const diff = text(fingerprint(FP_A, { pasted: FP_B, onPaste: on }));
    expect(diff).toContain('data-compare=different>');
    expect(diff).toContain('Different. Stop here');
    expect(text(fingerprint(null))).toContain('shows no fingerprint');
  });
  it('the slip: three labelled rows, the one the page waits for marked, a placeholder read as words', () => {
    const t = text(
      slip(
        [
          { id: 'address', label: 'Address', value: '192.0.2.80:8000', have: true },
          { id: 'fingerprint', label: 'Fingerprint', value: FP_A, hint: 'compare it' },
          { id: 'code', label: 'Pairing code', value: ghost('••• •••', 'six digits'), now: true },
        ],
        { caption: 'the engine host’s terminal' },
      ),
    );
    expect(t).toContain('role="group" aria-label=the engine host’s terminal>');
    expect(t.match(/data-slip=/g)).toHaveLength(3);
    expect(t).toMatch(/class="eh-slip-row\s+is-have" data-slip=address>/);
    expect(t).toMatch(/class="eh-slip-row is-now\s*" data-slip=code>/);
    expect(t).toContain('<dd class="eh-slip-hint">compare it</dd>');
    expect(t).toContain('aria-hidden="true">••• •••</span><span class="sr-only">six digits</span>');
    expect(text(command('tandem pair'))).toContain('<code>tandem pair</code>');
    expect(text(command('tandem pair'))).toContain('aria-label=Copy tandem pair ');
  });
});
