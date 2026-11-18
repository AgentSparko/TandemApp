// What the account and access pages know without a DOM (SRV-875): which page a route is, the
// right each page needs, what a role may put on a token, a form's text as the body the controller
// takes, a refusal in words, and how a session, a token and an audit row read.

import { ACCESS_PAGES } from './menu';
import { LIMIT_KEYS, type AuditRow, type AuthError, type Finding, type LimitKey, type Limits, type LimitState, type Me, type NewToken, type Protocol, type Role, type Session, type Token, type TokenUsage, type User } from '../api/auth';
import { compact, money } from './format';

export type Page = 'account' | 'my-tokens' | 'users' | 'tokens' | 'exposure' | 'sessions' | 'audit' | 'views';

export interface PageDef {
  id: Page;
  href: string;
  label: string;
  title: string;
  sub: string;
  /** the right a signed-in person needs to open it */
  right: string;
}

const HEADS: Record<Page, [string, string]> = {
  account: ['Account', 'Your password and the browsers you are signed in on.'],
  'my-tokens': ['My tokens', 'API tokens for your own scripts and clients. Each one acts with your rights, never more.'],
  users: ['Users', 'Who may sign in to this TandemApp, and as what.'],
  tokens: ['All tokens', 'Every API token, the keys imported from an older setup, and the identities TandemApp gives its own services.'],
  exposure: ['API exposure', 'Whether other devices may use each API. No setting lets a device in without a token.'],
  sessions: ['Sessions', 'Every browser that is signed in.'],
  audit: ['Sign-ins and changes', 'Who signed in from where, and every change to users, sessions, tokens and these settings.'],
  views: ['Views', 'Which pages each role sees, and the role a new account gets. Owners see every page.'],
};
/** the menu's pages (lib/menu.ts) with each one's head */
export const PAGES: PageDef[] = ACCESS_PAGES.map(([id, href, label, right]) => ({ id: id as Page, href, label, right, title: HEADS[id as Page][0], sub: HEADS[id as Page][1] }));

/** The page of a route: `account` and `access` are the two views, `path` what follows. */
export function pageOf(view: string, path: string[]): Page {
  const p = path[0] ?? '';
  if (view === 'account') return p === 'tokens' ? 'my-tokens' : 'account';
  return (['tokens', 'exposure', 'sessions', 'audit', 'views'] as const).find((x) => x === p) ?? 'users';
}
export const pageDef = (p: Page): PageDef => PAGES.find((x) => x.id === p)!;

export const can = (me: Me | null | undefined, right: string): boolean => !!me?.authenticated && me.permissions.includes(right);
export const roleOf = (me: Me | null | undefined): Role | null => me?.account?.role ?? (me?.local_mode ? 'owner' : null);
export const nameOf = (me: Me | null | undefined): string => me?.account?.username ?? me?.principal?.name ?? '';

// ---- tokens ---------------------------------------------------------------------------------------

export interface ScopeDef {
  id: string;
  label: string;
  text: string;
  /** the rights the person must hold to put it on a token */
  needs: string[];
}
/** proxy/tokens.py USER_SCOPES with what each opens (IMPLIES) and the right to make one */
export const SCOPE_DEFS: ScopeDef[] = [
  { id: 'inference:openai', label: 'OpenAI-compatible API', text: 'chat, completions, responses, embeddings, the model list', needs: ['tokens:inference'] },
  { id: 'inference:anthropic', label: 'Messages API', text: 'messages, token counts, the model list', needs: ['tokens:inference'] },
  { id: 'usage:read', label: 'Read usage', text: 'the ledger’s views', needs: ['tokens:management', 'read'] },
  { id: 'metrics:read', label: 'Read metrics', text: '/metrics and the full health answer', needs: ['tokens:management', 'metrics:read'] },
  { id: 'settings:read', label: 'Read settings', text: 'settings and configuration, without sign-in and API settings', needs: ['tokens:management', 'read'] },
  { id: 'settings:write', label: 'Change settings', text: 'the same, with writes', needs: ['tokens:management', 'settings:write'] },
  { id: 'fleet:read', label: 'Read the fleet', text: 'hosts, deployments, health', needs: ['tokens:management', 'fleet:read'] },
  { id: 'fleet:write', label: 'Run the fleet', text: 'deployments, engine start and stop, the Setup Guide', needs: ['tokens:management', 'fleet:write'] },
  { id: 'trust', label: 'Trust', text: 'pairing, release, plugin install; 30 days at most', needs: ['tokens:management', 'trust', 'users'] },
];
export const scopeLabel = (id: string): string => SCOPE_DEFS.find((s) => s.id === id)?.label ?? id;
/** the scopes this person may put on a token; none: no create button */
export const scopesFor = (me: Me | null | undefined): ScopeDef[] => SCOPE_DEFS.filter((s) => s.needs.every((n) => can(me, n)));
/** decision 4: a viewer makes tokens only once an admin allows it */
export const mayCreate = (me: Me | null | undefined): boolean => scopesFor(me).length > 0;

export const TRUST_MAX_DAYS = 30;
export const DAY_CHOICES = [30, 90, 365];

export interface TokenForm {
  label: string;
  scopes: string[];
  /** globs, split on commas, spaces and lines */
  models: string;
  /** a number of days, or `never` */
  days: string;
  limits: Partial<Record<(typeof LIMIT_KEYS)[number], string>>;
}

/** The body of a create, or the sentence that says what is wrong with the form. */
export function tokenBody(f: TokenForm, me: Me | null | undefined): NewToken | string {
  const label = f.label.trim();
  if (!label || label.length > 64) return 'Give the token a name of 1 to 64 characters.';
  const allowed = scopesFor(me).map((s) => s.id);
  const scopes = f.scopes.filter((s) => allowed.includes(s));
  if (!scopes.length) return 'Pick at least one thing the token may do.';
  const models = [...new Set(f.models.split(/[\s,]+/).filter(Boolean))];
  if (models.length > 64) return 'A token names at most 64 models.';
  let days: number | 'never';
  if (f.days === 'never') {
    if (!can(me, 'users')) return 'Only an admin makes a token that never expires.';
    if (scopes.includes('trust')) return `A token with trust lives at most ${TRUST_MAX_DAYS} days.`;
    days = 'never';
  } else {
    days = Number(f.days);
    if (!Number.isInteger(days) || days < 1) return 'The days are a whole number of 1 or more.';
    if (scopes.includes('trust') && days > TRUST_MAX_DAYS) return `A token with trust lives at most ${TRUST_MAX_DAYS} days.`;
  }
  const limits: Limits = {};
  for (const k of LIMIT_KEYS) {
    const raw = (f.limits[k] ?? '').trim();
    if (!raw) continue;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0) return 'A limit is a number of 0 or more.';
    limits[k] = n;
  }
  return { label, scopes, days, ...(models.length ? { models } : {}), ...(Object.keys(limits).length ? { limits } : {}) };
}

export const LIMIT_WORDS: Record<(typeof LIMIT_KEYS)[number], string> = {
  requests_per_minute: 'Requests a minute',
  tokens_per_minute: 'Tokens a minute',
  concurrent: 'Requests at once',
  cost_per_day: 'Cost a day',
  cost_per_month: 'Cost a month',
};

// ---- SRV-873: a token's use and its limits' state (My tokens) ---------------------------------------

/** a refusal's code in a few words */
export const REFUSED_WORDS: Record<string, string> = {
  rate_limited: 'requests a minute',
  tokens_limited: 'tokens a minute',
  concurrency_limited: 'requests at once',
  spend_limit_reached: 'spend limit',
  permission_denied: 'not allowed',
};

/** "Cost a day $0.58 of $1.00": one limit that holds, what is used of it; "(role)" when the
 *  owner's role sets it lower than the token does */
export function limitText(k: LimitKey, s: LimitState): string {
  const cost = k.startsWith('cost');
  return `${LIMIT_WORDS[k]} ${cost ? money(s.used) : compact(s.used)} of ${cost ? money(s.limit * 1e6) : compact(s.limit)}${s.from === 'role' ? ' (role)' : ''}`;
}

/** "30 days: 12 requests, 3.4k tokens, $0.58, 2 refused" */
export function useText(u: TokenUsage): string {
  const t = u.totals;
  return `30 days: ${t.requests} ${t.requests === 1 ? 'request' : 'requests'}, ${compact(t.total_tokens)} tokens${t.cost_micros != null ? `, ${money(t.cost_micros)}` : ''}${t.refused ? `, ${t.refused} refused` : ''}`;
}

/** the models a spend limit cannot hold (no price), in a sentence; null without a spend limit or
 *  without such a model */
export function unpricedText(u: TokenUsage): string | null {
  const spend = Object.keys(u.limits.limits).some((k) => k.startsWith('cost'));
  if (!spend || !u.unpriced.length) return null;
  return `The spend limit does not apply to ${u.unpriced.join(', ')}: ${u.unpriced.length === 1 ? 'it has' : 'they have'} no price.`;
}

/** the two lines a client needs beside a new token: where each kind of client points */
export const snippets = (origin: string): { label: string; value: string }[] => [
  { label: 'Base URL for an OpenAI-compatible client', value: `${origin}/v1` },
  { label: 'Base URL for a Messages API client', value: origin },
];

export const isImported = (t: Token): boolean => t.source !== 'created' && t.kind === 'user';
/** the Review list: imported keys nobody has confirmed or revoked yet */
export const reviewOf = (ts: Token[]): Token[] => ts.filter((t) => t.needs_review && t.state === 'active');
export const servicesOf = (ts: Token[]): Token[] => ts.filter((t) => t.kind === 'service' && !t.needs_review);
/** people's tokens, the live ones first; `owner`: one account's alone */
export function peopleOf(ts: Token[], owner: string | null = null): Token[] {
  return ts.filter((t) => t.kind === 'user' && !(t.needs_review && t.state === 'active') && (!owner || t.owner_id === owner)).sort((a, b) => Number(a.state !== 'active') - Number(b.state !== 'active'));
}
export const ownersOf = (ts: Token[]): { id: string; name: string }[] => {
  const m = new Map<string, string>();
  for (const t of ts) if (t.kind === 'user' && t.owner_id) m.set(t.owner_id, t.owner ?? t.owner_id);
  return [...m].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name));
};
/** how many live tokens could use a protocol */
export const tokensFor = (ts: Token[], p: Protocol): number => ts.filter((t) => t.kind === 'user' && t.state === 'active' && t.scopes.includes(`inference:${p}`)).length;

// ---- users ----------------------------------------------------------------------------------------

/** the one enabled owner: it cannot be disabled, demoted or removed */
export const lastOwner = (u: User, all: User[]): boolean => u.role === 'owner' && u.enabled && all.filter((x) => x.role === 'owner' && x.enabled).length === 1;
/** what the person may do to an account: only an owner touches an owner */
export const mayChange = (me: Me | null | undefined, u: User): boolean => can(me, 'users') && (u.role !== 'owner' || can(me, 'owner'));
export const rolesFor = (me: Me | null | undefined): Role[] => (can(me, 'owner') ? ['viewer', 'operator', 'admin', 'owner'] : ['viewer', 'operator', 'admin']);
export const ROLE_WORDS: Record<Role, string> = {
  viewer: 'reads the page and the usage',
  operator: 'also changes settings, runs deployments and chats',
  admin: 'also manages users, tokens, sessions and plugins',
  owner: 'also manages owners, sign-in and API exposure',
};
const NAME = /^[a-z0-9][a-z0-9._@-]{0,63}$/;
export const userNameError = (name: string): string | null => (NAME.test(name.trim().toLowerCase()) ? null : 'A username is 1 to 64 characters: letters, digits and . _ @ -, starting with a letter or a digit.');

// ---- exposure -------------------------------------------------------------------------------------

export const PROTOCOLS: { id: Protocol; label: string; routes: string; fact: string }[] = [
  { id: 'openai', label: 'OpenAI-compatible API', routes: '/v1/chat/completions, /v1/completions, /v1/responses, /v1/embeddings', fact: 'OpenAI' },
  { id: 'anthropic', label: 'Messages API', routes: '/v1/messages, /v1/messages/count_tokens', fact: 'Anthropic' },
];
export const EXPOSURE_WORDS = {
  'this-machine': { label: 'This machine', text: 'Only callers on this machine, with a token.' },
  network: { label: 'Network', text: 'Any device that reaches TandemApp, with a token that holds this API.' },
} as const;
export const NOT_EXPOSED = 'Other devices cannot use this API. Switch it on under API exposure.';
const EXPOSURE_CODES = ['exposure_no_tokens', 'exposure_unreachable'];
/** the health findings that belong to one protocol's card, and the ones about the API as a whole */
export const findingsFor = (fs: Finding[], fact: string | null): Finding[] =>
  fs.filter((f) => (fact ? EXPOSURE_CODES.includes(f.code) && f.facts?.protocol === fact : ['exposure_plain_http', 'tokens_need_review', 'legacy_admin_token', 'local_mode_proxy_seen'].includes(f.code)));
export const isLoopback = (host: string | null): boolean => !!host && /^(127\.|localhost$|\[?::1\]?$)/.test(host);

// ---- words ----------------------------------------------------------------------------------------

const cap = (s: string): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) + (/[.!?]$/.test(s) ? '' : '.') : s);

/** A refusal in words. A missing right is named, with the role the person has. */
export function errorWords(e: AuthError, me: Me | null | undefined = null): string {
  const right = typeof e.facts.permission === 'string' ? e.facts.permission : null;
  if (right) return `Not allowed: this needs the right “${right}”.${roleOf(me) ? ` Your role is ${roleOf(me)}.` : ''}`;
  switch (e.code) {
    case 'password_first':
      return 'Set the owner’s password first. Until then TandemApp lets in this machine only.';
    case 'last_owner':
      return 'This is the last owner. Make another owner first.';
    case 'wrong_password':
      return 'The current password is not right.';
    case 'own_account':
      return 'Change your own password under Account, with the current one.';
    case 'service_identity':
      return 'TandemApp manages this identity itself.';
    case 'owner_required':
      return 'Not allowed: only an owner changes this.';
    case 'admin_required':
      return 'Not allowed: only an admin changes this.';
    case 'too_many':
      return 'Too many tries. Wait a minute and try again.';
    case 'sign_in_required':
      return 'You are signed out. Sign in again to go on.';
    case 'offline':
      return 'TandemApp did not answer. Check the connection and try again.';
  }
  return e.message ? cap(e.message) : `TandemApp answered ${e.status}.`;
}

/** `Firefox on Linux` from a User-Agent; the header itself when it names neither */
export function browserOf(agent: string | null): string {
  const a = agent ?? '';
  const b = /Edg\//.test(a) ? 'Edge' : /Firefox\//.test(a) ? 'Firefox' : /Chrome\//.test(a) ? 'Chrome' : /Safari\//.test(a) ? 'Safari' : null;
  const os = /iPhone|iPad/.test(a) ? 'iOS' : /Android/.test(a) ? 'Android' : /Windows/.test(a) ? 'Windows' : /Mac OS X/.test(a) ? 'macOS' : /Linux/.test(a) ? 'Linux' : null;
  return b && os ? `${b} on ${os}` : (b ?? os ?? (a.slice(0, 40) || 'unknown browser'));
}

/** `2 minutes ago`, `3 days ago`, `in 12 days`; `never` for nothing */
export function ago(t: string | number | null | undefined, now: number): string {
  if (t == null) return 'never';
  const d = (now - (typeof t === 'number' ? t : Date.parse(t))) / 1000;
  const s = Math.abs(d);
  const n = s < 90 ? 0 : s < 5400 ? Math.round(s / 60) : s < 86_400 ? Math.round(s / 3600) : Math.round(s / 86_400);
  if (!n) return d < 0 ? 'in a moment' : 'just now';
  const unit = s < 5400 ? 'minute' : s < 86_400 ? 'hour' : 'day';
  const w = `${n} ${unit}${n === 1 ? '' : 's'}`;
  return d < 0 ? `in ${w}` : `${w} ago`;
}

export const stateWords = (t: Token, now: number): string => (t.state === 'revoked' ? 'revoked' : t.state === 'expired' ? 'expired' : t.expires_at ? `expires ${ago(t.expires_at, now)}` : 'never expires');
/** a token that ends within 7 days (the health page's token_expiring) */
export const endsSoon = (t: Token, now: number): boolean => t.state === 'active' && !!t.expires_at && Date.parse(t.expires_at) - now < 7 * 86_400_000;

const AUDIT: Record<string, string> = {
  'session sign_in': 'signed in',
  'session sign_in_failed': 'failed to sign in',
  'session sign_out': 'signed out',
  'session revoke': 'session ended',
  'account create': 'account made',
  'account delete': 'account removed',
  'account role': 'role changed',
  'account enable': 'account enabled',
  'account disable': 'account disabled',
  'account password': 'password changed',
  'account reset': 'password reset',
  'account setup': 'first owner made',
  'account update': 'account changed',
  'api_token create': 'token made',
  'api_token rotate': 'token rotated',
  'api_token revoke': 'token revoked',
  'api_token confirm': 'imported key confirmed',
  'api_token update': 'token changed',
  'setup_code create': 'setup code made',
  'setup_code use': 'setup code used',
  'setup_code setup_failed': 'wrong setup code',
  'setting set': 'setting changed',
  'setting delete': 'setting back to its default',
};
export const isSignIn = (r: AuditRow): boolean => r.object === 'session' && r.action.startsWith('sign_');
/** an audit row as a line: what happened, and the few facts worth a glance */
export function auditWords(r: AuditRow): { what: string; about: string; bad: boolean } {
  const d = r.detail;
  const s = (k: string) => (d[k] == null ? '' : String(d[k]));
  const about = [
    s('username') || s('label') || (r.object === 'setting' ? (r.object_id ?? '') : ''),
    d.from !== undefined || d.to !== undefined ? `${s('from')} → ${s('to')}` : '',
    s('role') && !('from' in d) ? s('role') : '',
    s('address') ? `from ${s('address')}` : '',
    s('why') || s('text'),
  ].filter(Boolean);
  return { what: AUDIT[`${r.object} ${r.action}`] ?? `${r.object} ${r.action}`, about: about.join(' · '), bad: r.action.includes('failed') };
}

export const mine = (s: Session[]): Session[] => [...s].sort((a, b) => Number(b.current) - Number(a.current));
