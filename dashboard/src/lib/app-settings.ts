// App settings (6.3b, concept §8.13 and A14): the words of each permission the controller reports
// (with the browser's own notification state merged in), the admin password's state and form, the
// fleet's configuration file and an import's plan. views/fleet/app-settings.ts draws them; the
// controller's answers are /v1/fleet/app* (proxy/appsettings.py, proxy/adminpw.py).

import type { AppPermission, FleetNode, ImportConfirms, ImportItem, ImportPlan, ImportResult } from '../api/fleet';
import { mixedVersions, releaseOf } from './fleet-update';

export type Tone = 'ok' | 'warn' | 'err' | 'muted';
/** the browser's notification permission, or `unsupported` (no Notification API, or a page not
 *  served over https, where browsers offer none) */
export type BrowserNotify = 'granted' | 'denied' | 'default' | 'unsupported';

export interface PermRow {
  id: AppPermission['id'];
  title: string;
  badge: string;
  tone: Tone;
  applies: boolean;
  why: string;
  detail: string;
  fallback: string | null;
  settings: string | null;
  action: AppPermission['action'];
  files: NonNullable<AppPermission['files']>;
}

const BADGE: Record<AppPermission['state'], [string, Tone]> = {
  granted: ['Allowed', 'ok'],
  lan_denied: ['Denied', 'err'],
  not_asked: ['Not asked yet', 'warn'],
  not_applicable: ['Does not apply', 'muted'],
  unknown: ['Unknown', 'muted'],
  file: ['Files, 0600', 'ok'],
  too_open: ['Too open', 'err'],
  keychain: ['Keychain', 'ok'],
  in_browser: ['In the browser', 'muted'],
  per_action: ['Asked per action', 'muted'],
};

/** what this browser says of notifications */
export function browserNotify(w: { Notification?: { permission: NotificationPermission }; isSecureContext?: boolean } = globalThis as never): BrowserNotify {
  if (!w.Notification || w.isSecureContext === false) return 'unsupported';
  return w.Notification.permission;
}

/** The rows in the controller's order; the notifications row says this browser's state. */
export function permRows(perms: AppPermission[], notify: BrowserNotify): PermRow[] {
  return perms.map((p) => {
    let [badge, tone] = BADGE[p.state];
    let detail = p.detail;
    let action = p.action;
    if (p.id === 'notifications') {
      [badge, tone] = ({ granted: ['Allowed', 'ok'], denied: ['Blocked', 'err'], default: ['Not asked yet', 'warn'], unsupported: ['Not offered', 'muted'] } as const)[notify];
      if (notify === 'denied') detail = `${detail} This browser blocks them for this site: allow them in its site settings.`;
      if (notify === 'unsupported') detail = `${detail} This browser offers none here (a page not served over https gets none).`;
      if (notify !== 'default') action = null;
    }
    return { id: p.id, title: p.title, badge, tone, applies: p.applies, why: p.why, detail, fallback: p.fallback, settings: p.settings ?? null, action, files: p.files ?? [] };
  });
}

// ---- the configuration file ---------------------------------------------------------------------

export const MAX_FILE = 4 * 1024 * 1024;

/** `tandemapp-fleet-config-2026-10-28.json` */
export function exportName(at: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `tandemapp-fleet-config-${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())}.json`;
}

/** The chosen file read as a configuration, or why not (before anything is sent). */
export function readConfig(text: string, size: number): { doc: Record<string, unknown> } | { error: string } {
  if (size > MAX_FILE) return { error: 'The file is over 4 MiB: no export of this app is that big.' };
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { error: 'This is not a JSON file.' };
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return { error: 'This is not a TandemApp fleet configuration.' };
  const kind = (doc as { kind?: unknown }).kind;
  if (kind === 'tandemapp-export') return { error: 'This is a controller move’s export (it holds keys): use Take the fleet instead.' };
  if (kind !== 'tandemapp-fleet-config') return { error: 'This is not a TandemApp fleet configuration.' };
  return { doc: doc as Record<string, unknown> };
}

const OP_TITLE: Record<ImportItem['op'], string> = { conflict: 'Cannot import', add: 'Added', change: 'Changed', remove: 'Removed', keep: 'Already so' };
export const OPS: ImportItem['op'][] = ['conflict', 'add', 'change', 'remove', 'keep'];
const KIND_WORD: Record<ImportItem['kind'], string> = { node: 'Spark', deployment: 'Deployment', rule: 'Alert rule', layout: 'Layout', setting: 'Setting', hook: 'Hook', provider: 'Decision provider', plugin: 'Plugin', engine: 'Engine host', conductor: 'Conductor document' };
export const kindWord = (k: ImportItem['kind']) => KIND_WORD[k];

/** The plan's items by what happens to them, conflicts first; empty groups left out. */
export function importGroups(p: Pick<ImportPlan, 'items'>): { op: ImportItem['op']; title: string; items: ImportItem[] }[] {
  return OPS.map((op) => ({ op, title: OP_TITLE[op], items: p.items.filter((i) => i.op === op) })).filter((g) => g.items.length);
}

/** `2 added · 4 changed · 3 skipped`; nothing: `nothing to change` */
export function planSummary(c: ImportPlan['counts']): string {
  const parts = [c.add && `${c.add} added`, c.change && `${c.change} changed`, c.remove && `${c.remove} removed`, c.conflict && `${c.conflict} skipped`].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'nothing to change';
}

/** the apply's button: `Import 6 changes` */
export function applyLabel(c: ImportPlan['counts']): string {
  const n = c.add + c.change + c.remove;
  return `Import ${n} change${n === 1 ? '' : 's'}`;
}

/** What an import asks a second time, as the settings interface does (SEC-16): keys that feed an
 *  engine's disk-cache fingerprint, and grants that send prompts or a question's text outside the
 *  LAN. Each is shown with its keys and agreed to on its own before the apply is sent. */
export type ImportAskKind = 'fingerprint' | 'grant';
export interface ImportAsk {
  kind: ImportAskKind;
  keys: string[];
  title: string;
  text: string;
  agree: string;
}

export function importAsks(p: Pick<ImportPlan, 'fingerprint_keys' | 'grant_keys' | 'confirm_fingerprint' | 'confirm_grant'>): ImportAsk[] {
  const out: ImportAsk[] = [];
  const fp = p.fingerprint_keys ?? [];
  const gr = p.grant_keys ?? [];
  if (p.confirm_fingerprint && fp.length)
    out.push({ kind: 'fingerprint', keys: fp, title: 'Saved caches start cold', text: `${fp.length === 1 ? 'This key feeds' : 'These keys feed'} an engine’s disk-cache fingerprint. After its restart every saved prefix and every parked chat there misses once: the first turn of each chat reads its whole context again.`, agree: 'Import them, and let the caches start cold' });
  if (p.confirm_grant && gr.length)
    out.push({ kind: 'grant', keys: gr, title: 'Prompts and answers leave the LAN', text: `The file grants ${gr.length === 1 ? 'a destination' : 'destinations'} outside the LAN what ${gr.length === 1 ? 'it' : 'they'} would be sent: a hook’s full payload (every prompt and answer as it is) or a provider’s questions’ text. TandemApp keeps who granted it and when.`, agree: 'Import them, and send it there' });
  return out;
}

/** The apply's second confirms once every ask was agreed to; null while one is not. */
export function importConfirms(p: Pick<ImportPlan, 'fingerprint_keys' | 'grant_keys' | 'confirm_fingerprint' | 'confirm_grant'>, agreed: ReadonlySet<ImportAskKind>): ImportConfirms | null {
  const out: ImportConfirms = {};
  for (const a of importAsks(p)) {
    if (!agreed.has(a.kind)) return null;
    if (a.kind === 'fingerprint') out.confirm_fingerprint = p.confirm_fingerprint!;
    else out.confirm_grant = p.confirm_grant!;
  }
  return out;
}

export function resultWords(r: ImportResult): string {
  const s = planSummary({ add: r.applied.add, change: r.applied.change, remove: r.applied.remove, keep: 0, conflict: r.skipped });
  return `Imported: ${s}.`;
}

// ---- updates ------------------------------------------------------------------------------------

/** The Updates block's line: the controller's release and the Sparks on another one (a local build
 *  label is its release's, as the version-mismatch rule reads it). */
export function updateLine(release: string | null, nodes: Pick<FleetNode, 'alias' | 'version' | 'status'>[]): { text: string; behind: number; mixed: boolean } {
  const target = release ? releaseOf(release) : '';
  const behind = target ? nodes.filter((n) => n.status !== 'off' && n.version && releaseOf(n.version) !== target).length : 0;
  const mixed = mixedVersions(nodes) != null;
  const rel = release ? `This controller carries TandemLLM ${release}.` : 'This controller’s release is not known.';
  if (!nodes.length) return { text: `${rel} No Spark in the fleet yet.`, behind, mixed };
  if (!behind) return { text: `${rel} Every Spark that answers runs it.`, behind, mixed };
  return { text: `${rel} ${behind} Spark${behind === 1 ? '' : 's'} run${behind === 1 ? 's' : ''} another release${mixed ? ': the fleet runs more than one' : ''}.`, behind, mixed };
}
