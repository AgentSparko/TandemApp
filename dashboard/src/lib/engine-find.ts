// Add engine, the first step (SRV-827): the engines the LAN advertises and an address typed by
// hand. The list is read once as it was and searched once anew when the page opens; a row starts
// the pairing with its address, port and advertised fingerprint; the address field asks what
// answers there and shows the finding, or goes straight on when a pairing window is open. Kept
// free of the DOM: the page (views/fleet/engine/find.ts) draws `state` and calls the methods.

import type { DiscoverView, DiscoveredEngine, EngineFinding, EnginesApi, PairStart, ProbeView } from '../api/engines';
import { findingOf, hostPort } from './engine-words';

export const ENGINE_PORT = 8000;

export interface FindState {
  /** the last search; null before the first answer */
  view: DiscoverView | null;
  searching: boolean;
  /** the search itself was refused (the LAN's own finding is in `view`) */
  searchError: EngineFinding | null;
  /** the row whose pairing starts: its key */
  busyRow: string | null;
  rowError: Record<string, EngineFinding>;
  address: string;
  port: string;
  probing: boolean;
  /** why the form cannot be sent yet */
  formError: string | null;
  probe: ProbeView | null;
  probeError: EngineFinding | null;
}

export const rowKey = (e: Pick<DiscoveredEngine, 'address' | 'port'>): string => hostPort(e.address, e.port);

export interface Target {
  address: string;
  port: number;
}

/** What the address field holds, as an address and a port: `host`, `host:port`, `[v6]:port` or a
 *  pasted URL (its host and port). A string says why it is not one. */
export function parseTarget(address: string, port: string): Target | string {
  let a = address.trim().replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/[/?#].*$/, '');
  let p = port.trim();
  if (!a) return 'Type the engine’s address: a name or an IP address, a tailnet one included.';
  const v6 = /^\[([0-9a-fA-F:]+)\](?::(\d+))?$/.exec(a);
  const hp = /^([^:\s]+):(\d+)$/.exec(a);
  if (v6) {
    a = v6[1];
    p = v6[2] ?? p;
  } else if (hp) {
    a = hp[1];
    p = hp[2];
  }
  if (/\s/.test(a) || a.startsWith('-') || a.length > 253 || !/^[A-Za-z0-9.:_-]+$/.test(a)) return 'An address is a name or an IP address, such as 192.0.2.80 or gpu-box.example.net.';
  const n = p === '' ? ENGINE_PORT : Number(p);
  if (!Number.isInteger(n) || n < 1 || n > 65535) return 'A port is a whole number from 1 to 65535. The engine’s is 8000 unless its install chose another.';
  return { address: a, port: n };
}

/** What a probe's answer means for the page. */
export type ProbeNext = 'pair' | 'in-fleet' | 'new-code' | 'other-ways' | 'retry';
export function probeNext(p: ProbeView): ProbeNext {
  if (p.node) return 'in-fleet';
  if (!p.finding) return p.engine && p.pairing?.open ? 'pair' : 'retry';
  if (p.finding.code === 'pairing_unsupported') return 'other-ways';
  if (p.finding.code === 'no_window' || p.finding.code === 'code_spent') return 'new-code';
  return 'retry';
}

/** A refusal at the start of a pairing, the same way: what the page offers beside its fix. */
export function startNext(f: EngineFinding): ProbeNext {
  if (f.code === 'pairing_unsupported') return 'other-ways';
  if (f.code === 'no_window' || f.code === 'code_spent') return 'new-code';
  return 'retry';
}

/** The list's line of status: what the search did. */
export function listWords(s: Pick<FindState, 'view' | 'searching'>): string {
  if (s.searching) return 'Searching your network…';
  const v = s.view;
  if (!v || v.lan === 'not_run') return 'Not searched yet.';
  if (v.lan === 'denied' || v.lan === 'failed') return 'The search could not run.';
  const n = v.engines.length;
  return n === 0 ? 'No engine answered.' : `${n} engine${n === 1 ? '' : 's'} answered.`;
}

export class FindFlow {
  state: FindState = { view: null, searching: false, searchError: null, busyRow: null, rowError: {}, address: '', port: '', probing: false, formError: null, probe: null, probeError: null };

  constructor(
    private api: EnginesApi,
    private changed: () => void = () => undefined,
  ) {}

  private set(p: Partial<FindState>): void {
    this.state = { ...this.state, ...p };
    this.changed();
  }

  /** The page opens: the last search as it was, then a new one. */
  async open(): Promise<void> {
    try {
      this.set({ view: await this.api.found() });
    } catch {
      /* the new search says what is wrong */
    }
    await this.search();
  }

  async search(): Promise<void> {
    if (this.state.searching) return;
    this.set({ searching: true, searchError: null });
    try {
      this.set({ view: await this.api.discover(), searching: false });
    } catch (e) {
      this.set({ searching: false, searchError: findingOf(e) });
    }
  }

  /** A row was chosen: its pairing starts. The answer goes to the pairing step; a refusal stays
   *  under the row. */
  async pick(e: DiscoveredEngine): Promise<{ start: PairStart; name: string } | null> {
    const key = rowKey(e);
    if (this.state.busyRow || e.node) return null;
    const { [key]: _gone, ...rest } = this.state.rowError;
    this.set({ busyRow: key, rowError: rest });
    try {
      const start = await this.api.pairStart({ address: e.address, port: e.port, ...(e.fingerprint ? { fingerprint: e.fingerprint } : {}), name: e.name });
      this.set({ busyRow: null });
      return { start, name: e.name };
    } catch (err) {
      this.set({ busyRow: null, rowError: { ...this.state.rowError, [key]: findingOf(err) } });
      return null;
    }
  }

  setAddress(address: string): void {
    this.set({ address, formError: null });
  }
  setPort(port: string): void {
    this.set({ port, formError: null });
  }

  /** The address field: what answers there. With a pairing window open the pairing starts at
   *  once; else the finding shows, with what to do. */
  async check(): Promise<{ start: PairStart; name: string } | null> {
    if (this.state.probing) return null;
    const t = parseTarget(this.state.address, this.state.port);
    if (typeof t === 'string') {
      this.set({ formError: t });
      return null;
    }
    this.set({ probing: true, formError: null, probe: null, probeError: null });
    try {
      const probe = await this.api.probe(t.address, t.port);
      if (probeNext(probe) !== 'pair') {
        this.set({ probing: false, probe });
        return null;
      }
      const start = await this.api.pairStart({ address: probe.address, port: probe.port });
      this.set({ probing: false, probe });
      return { start, name: probe.address };
    } catch (e) {
      this.set({ probing: false, probeError: findingOf(e) });
      return null;
    }
  }
}
