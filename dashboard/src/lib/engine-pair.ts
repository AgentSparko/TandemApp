// Add engine, pairing by code (SRV-829): the engine host showed a 6-digit code, its fingerprint
// and its address; the page shows the fingerprint and the address it has and takes the code. A
// wrong code says how many tries are left; a code that ran out, was used or had five wrong tries
// asks for `tandem pair` again and starts a new pairing; a host key that is not the one shown
// stops the pairing with both fingerprints and nothing to press. Kept free of the DOM: the page
// (views/fleet/engine/pair.ts) draws `state` and calls the methods.

import type { EngineFinding, EnginesApi, PairStart, PairedView } from '../api/engines';
import { compareFingerprint, findingOf, isHostKeyStop, waitOf, type Compare } from './engine-words';

export interface PairState {
  start: PairStart;
  /** what the engine calls itself, or its address */
  name: string;
  code: string;
  /** the terminal's fingerprint, pasted to compare */
  pasted: string;
  busy: 'code' | 'renew' | null;
  error: EngineFinding | null;
  /** the tries the engine host still takes for this code; null when it did not say */
  triesLeft: number | null;
  /** `spent`: this code is over, a new one is needed; `stop`: the host key is not the one shown */
  ended: 'spent' | 'stop' | null;
  /** when the window closes and when a refused try may run again, on the page's clock (ms) */
  closesAt: number | null;
  waitUntil: number | null;
}

/** the digits of what was typed or pasted, six at most ("482 913" and "482-913" are a code too) */
export const codeDigits = (text: string): string => text.replace(/\D/g, '').slice(0, 6);

export const compareOf = (s: Pick<PairState, 'start' | 'pasted'>): Compare => compareFingerprint(s.start.fingerprint, s.pasted);

/** seconds the code is still good for; null when the engine gave no end */
export function secondsLeft(s: Pick<PairState, 'closesAt'>, now: number): number | null {
  return s.closesAt === null ? null : Math.max(0, Math.ceil((s.closesAt - now) / 1000));
}

/** seconds until a refused try may run again; 0 when it may */
export function waitLeft(s: Pick<PairState, 'waitUntil'>, now: number): number {
  return s.waitUntil === null ? 0 : Math.max(0, Math.ceil((s.waitUntil - now) / 1000));
}

/** the code's time is up on the page's clock: the engine host shows no code any more */
export const ranOut = (s: Pick<PairState, 'closesAt' | 'ended'>, now: number): boolean => s.ended === null && secondsLeft(s, now) === 0;

/** Why Pair cannot be pressed now; null when it can. */
export function blocked(s: PairState, now: number): string | null {
  if (s.busy) return 'busy';
  if (s.ended || ranOut(s, now)) return 'ended';
  if (compareOf(s) === 'different') return 'different';
  if (waitLeft(s, now) > 0) return 'wait';
  if (s.code.length !== 6) return 'code';
  return null;
}

/** what the page says when the code's time ran out before the engine host said so */
export const RAN_OUT: EngineFinding = {
  code: 'code_spent',
  message: 'This code has run out: it is good for 5 minutes.',
  fix: 'Run `tandem pair` on the engine host again for a new code.',
};

const fresh = (start: PairStart, name: string, now: number): PairState => ({
  start, name, code: '', pasted: '', busy: null, error: null, triesLeft: start.window.tries_left, ended: null,
  closesAt: start.window.expires_in == null ? null : now + start.window.expires_in * 1000, waitUntil: null,
});

export class PairFlow {
  state: PairState;
  /** the advertised fingerprint the pairing began with: a new pairing checks it again */
  private advertised: string | null;

  constructor(
    private api: EnginesApi,
    start: PairStart,
    name: string,
    private changed: () => void = () => undefined,
    private now: () => number = () => Date.now(),
  ) {
    this.state = fresh(start, name, this.now());
    this.advertised = start.advertised;
  }

  private set(p: Partial<PairState>): void {
    this.state = { ...this.state, ...p };
    this.changed();
  }

  setCode(text: string): void {
    this.set({ code: codeDigits(text), error: this.state.error?.code === 'bad_code' ? null : this.state.error });
  }
  setPasted(text: string): void {
    this.set({ pasted: text });
  }

  /** The code goes to the engine host. Paired: the answer; else what it refused, and what that
   *  means for the next try. */
  async submit(): Promise<PairedView | null> {
    if (blocked(this.state, this.now())) return null;
    const { start, code } = this.state;
    this.set({ busy: 'code', error: null });
    try {
      const paired = await this.api.pairCode(start.pairing, code);
      this.set({ busy: null, code: '' });
      return paired;
    } catch (e) {
      const f = findingOf(e);
      const wait = waitOf(f);
      this.set({
        busy: null,
        code: '',
        error: f,
        triesLeft: f.code === 'wrong_code' && f.tries_left != null ? f.tries_left : this.state.triesLeft,
        // not_found: this TandemApp forgot the started pairing (ten minutes, or a restart)
        ended: isHostKeyStop(f) ? 'stop' : ['code_spent', 'no_window', 'not_found'].includes(f.code) || (f.code === 'wrong_code' && f.tries_left === 0) ? 'spent' : null,
        waitUntil: wait ? this.now() + wait * 1000 : null,
      });
      return null;
    }
  }

  /** `tandem pair` ran again on the engine host: a new pairing at the same address. */
  async renew(): Promise<boolean> {
    if (this.state.busy) return false;
    const { start, name } = this.state;
    this.set({ busy: 'renew', error: null });
    void this.api.pairAbandon(start.pairing).catch(() => undefined);
    try {
      const next = await this.api.pairStart({ address: start.address, port: start.port, ...(this.advertised ? { fingerprint: this.advertised } : {}), name });
      this.state = { ...fresh(next, name, this.now()), pasted: this.state.pasted };
      this.changed();
      return true;
    } catch (e) {
      const f = findingOf(e);
      this.set({ busy: null, error: f, ended: isHostKeyStop(f) ? 'stop' : 'spent' });
      return false;
    }
  }

  /** The user leaves the pairing: this TandemApp forgets its key pair for it. */
  async cancel(): Promise<void> {
    await this.api.pairAbandon(this.state.start.pairing).catch(() => undefined);
  }
}
