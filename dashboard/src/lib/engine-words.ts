// What the Add engine pages say about a refusal or a finding (VIS-278): the controller's own
// sentence and its fix, the facts worth a line of their own (both fingerprints of a host key that
// does not match or changed, the tries left, the seconds to wait), how long a button waits, and
// whether a pasted fingerprint is the one shown. Kept pure so it can be tested; the templates are
// views/fleet/engine/bits.ts.

import { EngineError, LOCAL_FIX, SignInNeeded, type EngineFinding } from '../api/engines';

/** the page's own code for the guard's 401 (not in the catalog) */
export const SIGN_IN = 'sign_in';

/** Any thrown value as a finding: an engine refusal's own, the guard's 401, or what the page can
 *  say about a failure with no words of its own. */
export function findingOf(e: unknown): EngineFinding {
  if (e instanceof EngineError) return e.finding;
  if (e instanceof SignInNeeded) return { code: SIGN_IN, message: 'TandemApp’s admin password is needed for this.', fix: 'Sign in at the top of this page. With no admin password set yet, open this page on the machine TandemApp runs on.', detail: e.message };
  return { code: 'error', message: `The page could not finish: ${(e as Error)?.message ?? String(e)}`, fix: LOCAL_FIX.error };
}

export interface Fact {
  label: string;
  value: string;
  /** a fingerprint or an address: set in the fixed-width face, wrapped anywhere */
  mono: boolean;
}

/** The facts of a finding that get a line of their own under its sentence. */
export function factsOf(f: EngineFinding): Fact[] {
  const out: Fact[] = [];
  if (f.code === 'host_key_mismatch') {
    if (f.expected) out.push({ label: 'Shown here', value: f.expected, mono: true });
    if (f.offered) out.push({ label: 'The engine host has', value: f.offered, mono: true });
  } else if (f.code === 'host_key_changed') {
    if (f.pinned) out.push({ label: 'Pinned before', value: f.pinned, mono: true });
    if (f.offered) out.push({ label: 'Offered now', value: f.offered, mono: true });
  } else if (f.code === 'host_key_unconfirmed') {
    if (f.offered) out.push({ label: 'Its fingerprint', value: f.offered, mono: true });
  } else if (f.code === 'wrong_code') {
    if (f.tries_left != null) out.push({ label: 'Tries left', value: String(f.tries_left), mono: false });
  } else if (f.code === 'rate_limited') {
    if (f.retry_after != null) out.push({ label: 'Wait', value: `${f.retry_after} seconds`, mono: false });
  }
  // the other side's own words, when the sentence does not hold them already
  if (f.detail && !f.message.includes(f.detail) && f.code !== SIGN_IN) out.push({ label: 'It said', value: f.detail, mono: true });
  return out;
}

/** A host key that is not the one expected: nothing on the page may connect anyway. */
export const isHostKeyStop = (f: EngineFinding | null): boolean => f?.code === 'host_key_mismatch' || f?.code === 'host_key_changed';

/** how long the action that was refused waits before it may run again, seconds; 0 for no wait */
export function waitOf(f: EngineFinding | null): number {
  if (!f) return 0;
  if (f.code === 'rate_limited') return Math.max(1, f.retry_after ?? 60);
  if (f.code === 'too_many') return 60;
  return 0;
}

export type Tone = 'err' | 'warn' | 'info';

/** A finding that waits for the user (a code to show, a line to paste, a sign-in) is a notice;
 *  the rest are errors. */
export function toneOf(f: EngineFinding): Tone {
  if (f.code === 'key_not_installed') return 'info';
  if (f.code === 'engine_loading') return 'info';
  if (['no_window', 'code_spent', 'lan_denied', 'pairing_unsupported', 'rate_limited', 'too_many', 'busy', SIGN_IN].includes(f.code)) return 'warn';
  return 'err';
}

export interface Seg {
  text: string;
  /** a command or a path to type as it stands */
  code: boolean;
}

/** A sentence with its `commands` apart, so the page can set them as code. */
export function segments(text: string): Seg[] {
  const out: Seg[] = [];
  text.split('`').forEach((part, i) => {
    if (part) out.push({ text: part, code: i % 2 === 1 });
  });
  return out;
}

const FP = /^SHA256:[A-Za-z0-9+/]{43}$/;
export const isFingerprint = (s: string | null | undefined): s is string => !!s && FP.test(s);

export type Compare = 'empty' | 'same' | 'different' | 'unreadable';

/** A fingerprint pasted from the engine host's terminal against the one the page shows. A whole
 *  printed line may be pasted: the fingerprint is found in it. */
export function compareFingerprint(shown: string | null, pasted: string): Compare {
  const text = pasted.trim();
  if (!text) return 'empty';
  const found = /SHA256:[A-Za-z0-9+/]{43}(?![A-Za-z0-9+/])/.exec(text)?.[0];
  if (!found || !shown) return 'unreadable';
  return found === shown ? 'same' : 'different';
}

/** "4:32" for a countdown; "0:00" at and after its end */
export function clockLeft(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** "192.0.2.80:8000", an IPv6 address in brackets */
export const hostPort = (address: string, port: number): string => `${address.includes(':') ? `[${address}]` : address}:${port}`;

/** A host key type as people read it: ssh-ed25519 -> ED25519 */
export const keyType = (t: string | null | undefined): string => (t ?? '').replace(/^ssh-/, '').replace(/^ecdsa-sha2-nistp/, 'ECDSA ').toUpperCase();
