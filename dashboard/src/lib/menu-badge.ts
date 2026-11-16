// The Health entry's badge in the menu (SRV-835): the controller's last state, read with
// `GET /v1/fleet/health?view=state`, which checks nothing (the kept answers only), once a minute
// while the page shows. Errors and warnings are counted; an ok state, an unknown one (nothing
// checked yet) and a read that fails show no badge. Kept out of the health page's chunk (lib/health*
// and views/health/ load with the page): the entry carries only this and the types.

import { request } from '../api/client';
import type { HealthStateView } from '../api/health';
import { poll } from './poll';

export interface HealthBadge {
  n: number;
  tone: 'err' | 'warn';
  label: string;
}

export const BADGE_EVERY_MS = 60_000;

/** The badge of a state view, or null when there is nothing to say. */
export function badgeOf(v: HealthStateView | null | undefined): HealthBadge | null {
  if (!v || (v.state !== 'error' && v.state !== 'warning')) return null;
  const n = v.counts.error + v.counts.warning;
  if (n < 1) return null;
  return { n, tone: v.state === 'error' ? 'err' : 'warn', label: `Health: ${n} ${n === 1 ? 'problem' : 'problems'}` };
}

const readState = (): Promise<HealthStateView> => request<HealthStateView>('/v1/fleet/health?view=state');

/** Reads the state now and once a minute; `set` gets each badge. Returns the stop. */
export function watchHealthBadge(set: (b: HealthBadge | null) => void, read: () => Promise<HealthStateView> = readState, every = BADGE_EVERY_MS): () => void {
  const p = poll(async () => {
    try {
      set(badgeOf(await read()));
    } catch {
      set(null);
    }
  }, every);
  p.start();
  return () => p.stop();
}
