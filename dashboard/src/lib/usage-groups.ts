// SRV-873: the Usage view's token and user dimensions, apart from the element so tests run them
// without a DOM. With the accounts on, the ledger names each request's API token and user; the view
// filters by a token (`token=` in the hash) and sums the range shown per token or per user
// (`group=`), each sum the ledger's rows.

import type { UsageGroup } from '../api/types';
import { addDays } from './time';

export type GroupBy = 'token' | 'user';

/** the days a group read covers: the range the charts show */
export function groupWindow(range: string, from: string | null, to: string | null, today: string): { from: string; to: string } {
  if (range === 'custom' && from && to) return { from, to };
  const n = range === '30d' ? 30 : range === '365d' ? 365 : 90;
  return { from: addDays(today, -(n - 1)), to: today };
}

export interface GroupRow {
  key: string | null;
  name: string;
  /** a token's owner */
  sub: string | null;
  requests: number;
  refused: number;
  tokens: number;
  cost: number | null;
}

/** the table's rows: a token by its label (its id when it has none), a user by name; the rows
 *  without one last, in words */
export function groupRows(groups: UsageGroup[], by: GroupBy): GroupRow[] {
  const none = by === 'token' ? 'no token (the page, local mode)' : 'no user';
  return groups
    .map((g) => ({ key: g.key, name: g.key == null ? none : (by === 'token' ? (g.label ?? g.key) : g.key), sub: by === 'token' ? (g.owner ?? null) : null, requests: g.requests, refused: g.refused, tokens: g.total_tokens, cost: g.cost_micros }))
    .sort((a, b) => Number(a.key == null) - Number(b.key == null));
}
