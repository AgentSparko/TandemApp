// Add engine, the other ways (SRV-831): for an engine host with no code to type. Password once:
// the host's fingerprint is read and shown, the user says it matches, then one login with the
// password installs TandemApp's key and ends; the password is in that one call and nowhere else.
// Paste a key: the page shows the one line to add on the engine host, the fingerprint is
// confirmed, and the page asks every few seconds until the host takes the key. A host key that
// changed or is not the one confirmed stops either way, with both fingerprints. Kept free of the
// DOM: the page (views/fleet/engine/connect.ts) draws `state` and calls the methods.

import type { ConnectKey, ConnectPlan, EngineFinding, EnginesApi, PairedView, SshTargetBody } from '../api/engines';
import { compareFingerprint, findingOf, isHostKeyStop, type Compare } from './engine-words';

export type Way = 'password' | 'key';
export const SSH_PORT = 22;
/** how often the page asks whether the pasted key is taken */
export const CHECK_EVERY_MS = 3000;

export interface ConnectState {
  way: Way;
  host: string;
  port: string;
  user: string;
  /** the engine's install folder there; empty for the usual one */
  dir: string;
  formError: string | null;
  busy: 'read' | 'connect' | 'line' | 'check' | null;
  error: EngineFinding | null;
  plan: ConnectPlan | null;
  key: ConnectKey | null;
  /** the fingerprint the user said matches, and the target it was read from */
  confirmed: { target: string; fingerprint: string } | null;
  /** a fingerprint the host offered late: SSH did not answer when the key line was made */
  offered: string | null;
  /** the user said the fingerprint does not match: nothing was sent */
  refused: boolean;
  /** the check's "not yet", and how many times the page asked */
  waiting: EngineFinding | null;
  checks: number;
  polling: boolean;
  pasted: string;
}

export interface SshTarget {
  host: string;
  port: number;
  user: string;
  dir: string | null;
}

const USER = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$/;
const DIR = /^(?:~|~\/[A-Za-z0-9._/+@-]*|\/[A-Za-z0-9._/+@-]*)$/;

/** What the form holds, as an SSH target; a string says what is missing. `user@host` and
 *  `user@host:port` in the address field are read as both. A pasted key's folder is absolute. */
export function parseSshTarget(s: Pick<ConnectState, 'host' | 'port' | 'user' | 'dir' | 'way'>): SshTarget | string {
  let host = s.host.trim().replace(/^ssh\s+/, '');
  let user = s.user.trim();
  let port = s.port.trim();
  const at = host.lastIndexOf('@');
  if (at > 0) {
    user = user || host.slice(0, at);
    host = host.slice(at + 1);
  }
  const hp = /^([^:\s]+):(\d+)$/.exec(host);
  if (hp) {
    host = hp[1];
    port = port || hp[2];
  }
  host = host.replace(/^\[|\]$/g, '');
  if (!host) return 'Type the engine host’s address: a name or an IP address.';
  if (/\s/.test(host) || host.startsWith('-') || !/^[A-Za-z0-9.:_-]+$/.test(host)) return 'An address is a name or an IP address, such as 192.0.2.80.';
  const n = port === '' ? SSH_PORT : Number(port);
  if (!Number.isInteger(n) || n < 1 || n > 65535) return 'The SSH port is a whole number from 1 to 65535; 22 unless the host uses another.';
  if (!user) return 'Type the user name you log in with on the engine host.';
  if (!USER.test(user)) return 'A user name is letters, digits and . _ - (32 at most).';
  const dir = s.dir.trim().replace(/\/+$/, '') || null;
  if (dir !== null && (!DIR.test(dir) || `${dir}/`.includes('/../'))) return 'The engine’s folder is a path of letters, digits and . _ / + @ -, such as /home/spark/TandemLLM.';
  if (dir !== null && s.way === 'key' && !dir.startsWith('/')) return 'For a pasted key the engine’s folder is a full path that starts with /, such as /home/spark/TandemLLM.';
  return { host, port: n, user, dir };
}

const body = (t: SshTarget): SshTargetBody => ({ host: t.host, user: t.user, ...(t.port === SSH_PORT ? {} : { port: t.port }), ...(t.dir ? { engine_dir: t.dir } : {}) });
const targetKey = (t: Pick<SshTarget, 'host' | 'port'>): string => `${t.host}:${t.port}`;

/** the fingerprint the page shows to confirm now, whichever answer brought it */
export function shownFingerprint(s: Pick<ConnectState, 'way' | 'plan' | 'key' | 'offered'>): string | null {
  return s.way === 'password' ? (s.plan?.host_key.fingerprint ?? null) : (s.key?.host_key?.fingerprint ?? s.offered);
}

export const compareOf = (s: Pick<ConnectState, 'way' | 'plan' | 'key' | 'offered' | 'pasted'>): Compare => compareFingerprint(shownFingerprint(s), s.pasted);

/** a host key that is not the one expected: no button connects anyway */
export const stopped = (s: Pick<ConnectState, 'error'>): boolean => isHostKeyStop(s.error);

/** The key line as one command for the engine host's terminal: the folder and the file made with
 *  the rights SSH wants, the line appended. Null for a line that single quotes cannot hold. */
export function pasteCommand(k: Pick<ConnectKey, 'line'>): string | null {
  if (k.line.includes("'") || k.line.includes('\n')) return null;
  return `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '${k.line}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`;
}

/** the command that prints a host key's fingerprint on the engine host itself */
export function fingerprintCommand(type: string | null | undefined): string {
  const kind = /ed25519/.test(type ?? '') ? 'ed25519' : /ecdsa/.test(type ?? '') ? 'ecdsa' : /rsa/.test(type ?? '') ? 'rsa' : 'ed25519';
  return `ssh-keygen -lf /etc/ssh/ssh_host_${kind}_key.pub`;
}

const START: ConnectState = {
  way: 'password', host: '', port: '', user: '', dir: '', formError: null, busy: null, error: null, plan: null, key: null,
  confirmed: null, offered: null, refused: false, waiting: null, checks: 0, polling: false, pasted: '',
};

export class ConnectFlow {
  state: ConnectState;

  constructor(
    private api: EnginesApi,
    private changed: () => void = () => undefined,
    host = '',
  ) {
    this.state = { ...START, host };
  }

  private set(p: Partial<ConnectState>): void {
    this.state = { ...this.state, ...p };
    this.changed();
  }

  /** what an answer about another target, or another way, leaves behind */
  private cleared(): Partial<ConnectState> {
    return { plan: null, key: null, error: null, formError: null, offered: null, refused: false, waiting: null, checks: 0, polling: false, busy: null };
  }

  setWay(way: Way): void {
    if (way !== this.state.way) this.set({ ...this.cleared(), way, pasted: '' });
  }

  /** A field of the target changed: what was read for the old one no longer holds. The folder is
   *  not part of who the host is, so a confirmed fingerprint outlives its change. */
  setField(k: 'host' | 'port' | 'user' | 'dir', v: string): void {
    if (this.state[k] === v) return;
    this.set({ ...this.cleared(), [k]: v, ...(k === 'dir' ? {} : { confirmed: null, pasted: '' }) });
  }
  setPasted(text: string): void {
    this.set({ pasted: text });
  }

  private target(): SshTarget | null {
    const t = parseSshTarget(this.state);
    if (typeof t === 'string') {
      this.set({ formError: t });
      return null;
    }
    return t;
  }

  /** start over with the same target: what was read is dropped */
  reset(): void {
    this.set({ ...this.cleared(), confirmed: null, pasted: '' });
  }

  /** the user says the fingerprint is not the one the engine host has: nothing goes out */
  refuse(): void {
    this.set({ ...this.cleared(), confirmed: null, refused: true });
  }

  // ---- password once ---------------------------------------------------------------------------
  /** The host's fingerprint is read; nothing has logged in yet. */
  async read(): Promise<void> {
    const t = this.target();
    if (!t || this.state.busy) return;
    this.set({ ...this.cleared(), busy: 'read' });
    try {
      this.set({ plan: await this.api.passwordPlan(body(t)), busy: null });
    } catch (e) {
      this.set({ busy: null, error: findingOf(e) });
    }
  }

  /** "It matches: connect": one login with the password. A folder changed since the fingerprint
   *  was confirmed makes a new plan first; the controller refuses a host key that is not the one
   *  confirmed, so nothing is trusted that the user did not see. */
  async connect(password: string): Promise<PairedView | null> {
    const t = this.target();
    if (!t || this.state.busy || !password || compareOf(this.state) === 'different') return null;
    let plan = this.state.plan;
    const fingerprint = plan?.host_key.fingerprint ?? (this.state.confirmed?.target === targetKey(t) ? this.state.confirmed.fingerprint : null);
    if (!fingerprint) return null;
    this.set({ busy: 'connect', error: null, refused: false, confirmed: { target: targetKey(t), fingerprint } });
    try {
      if (!plan) {
        plan = await this.api.passwordPlan(body(t));
        this.set({ plan });
      }
      const paired = await this.api.passwordConfirm(plan.plan, password, fingerprint);
      this.set({ busy: null });
      return paired;
    } catch (e) {
      const f = findingOf(e);
      // a plan this TandemApp forgot (ten minutes) is read again at the next try
      this.set({ busy: null, error: f, ...(f.code === 'not_found' || isHostKeyStop(f) ? { plan: null } : {}), ...(isHostKeyStop(f) ? { confirmed: null } : {}) });
      return null;
    }
  }

  // ---- paste a key -----------------------------------------------------------------------------
  /** A new key pair's line, and where it goes. */
  async makeLine(): Promise<void> {
    const t = this.target();
    if (!t || this.state.busy) return;
    this.set({ ...this.cleared(), busy: 'line' });
    try {
      this.set({ key: await this.api.keyNew(body(t)), busy: null });
    } catch (e) {
      this.set({ busy: null, error: findingOf(e) });
    }
  }

  /** "It matches": from now on the page asks whether the host takes the key. */
  async confirmKey(): Promise<PairedView | null> {
    const k = this.state.key;
    const fingerprint = shownFingerprint(this.state);
    if (!k || !fingerprint || compareOf(this.state) === 'different') return null;
    this.set({ confirmed: { target: targetKey(k), fingerprint }, polling: true, refused: false, error: null });
    return this.check();
  }

  /** One check: paired, not yet (its finding), or what stops the waiting. Before a fingerprint
   *  was confirmed it only asks what the host offers. */
  async check(): Promise<PairedView | null> {
    const k = this.state.key;
    if (!k || this.state.busy) return null;
    const confirmed = this.state.confirmed?.fingerprint;
    this.set({ busy: 'check' });
    try {
      const r = await this.api.keyCheck(k.plan, confirmed);
      if (r.state === 'paired') {
        this.set({ busy: null, polling: false, waiting: null });
        return r;
      }
      this.set({ busy: null, waiting: r.finding, checks: this.state.checks + 1, error: null });
      return null;
    } catch (e) {
      const f = findingOf(e);
      if (f.code === 'host_key_unconfirmed' && f.offered && !confirmed) {
        // SSH answers now: this is the fingerprint to confirm
        this.set({ busy: null, offered: f.offered, error: null });
        return null;
      }
      this.set({ busy: null, polling: false, waiting: null, error: f, ...(isHostKeyStop(f) ? { confirmed: null } : {}) });
      return null;
    }
  }
}
