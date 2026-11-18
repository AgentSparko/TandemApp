// What the account and access pages' tests share (tests for SRV-875): the mock controller with its
// accounts on a clock the test moves, reached through the page's own client (api/auth.ts), so no
// HTTP is needed. A browser's cookie is the rig's `caller`: a sign-in or a new password sets it,
// an ended session leaves it dead. Every answer of /v1/auth/* is checked for exactly the fields
// the controller's views have (KEYS, which access-mock.test.ts holds to the Python sources); the
// settings answers against contracts/settings-v1 and the health answer against
// contracts/fleet-v1. Every path, every body sent and every answer is kept, so a test can say
// what was asked and that no secret is where it must not be.
import { MockFleet } from '../mock/fleet.ts';
import type { Caller, MockAuth } from '../mock/auth.ts';
import { authApi, type AuthApi, type Send, type Sent } from '../src/api/auth.ts';
import { AccessFlow } from '../src/lib/access-flow.ts';
import type { Page } from '../src/lib/access.ts';
import { accessView, dialogView, type On } from '../src/views/access/view.ts';
import { text, valid as validFleet } from './engine-rig.ts';
import { ERRORS, schemaOf, valid as validSettings } from './settings-rig.ts';

export const T0 = Date.parse('2026-11-16T09:00:00Z');
export const ORIGIN = 'https://tandem.example';

/** the fields of each view of /v1/auth/*, as the controller's answers hold them */
export const KEYS = {
  me: ['contract_version', 'authenticated', 'account', 'principal', 'permissions', 'csrf', 'provider', 'setup_required', 'local_mode', 'views', 'expires_at'],
  account: ['id', 'username', 'display_name', 'role', 'must_change'],
  user: ['id', 'username', 'display_name', 'role', 'enabled', 'provider', 'has_password', 'must_change', 'created_at', 'last_login_at'],
  session: ['id', 'account_id', 'username', 'created_at', 'last_seen_at', 'idle_until', 'expires_at', 'address', 'agent', 'current'],
  token: ['id', 'kind', 'label', 'owner', 'owner_id', 'hint', 'scopes', 'backends', 'models', 'limits', 'memory_user', 'source', 'needs_review', 'state', 'created_at', 'expires_at', 'revoked_at', 'last_used_at'],
  audit: ['at', 'who', 'action', 'object', 'object_id', 'fields', 'detail'],
  // SRV-873: GET /v1/auth/tokens/{id}/usage (proxy/tokenusage.py)
  usage: ['from', 'to', 'tz', 'ledger', 'totals', 'days', 'models', 'unpriced', 'refusals', 'last_used_at', 'token', 'limits'],
  sums: ['requests', 'refused', 'prompt_tokens', 'cached_tokens', 'completion_tokens', 'total_tokens', 'cost_micros'],
};

function same(what: string, o: unknown, keys: string[], more: string[] = []): void {
  const got = Object.keys((o ?? {}) as object).sort();
  const want = [...keys, ...more].sort();
  if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`${what}: the fields are ${got.join(', ')}; the controller's are ${want.join(', ')}`);
}

/** Holds a 2xx answer of /v1/auth/* to the controller's shape. */
function shape(method: string, path: string, body: unknown): void {
  const p = path.replace(/^\/v1\/auth/, '').split('?')[0];
  const b = body as Record<string, unknown>;
  if (b === null) return; // a 204
  if (p === '/session' && method === 'GET') {
    same('session', b, KEYS.me);
    if (b.account) same('session.account', b.account, KEYS.account);
  } else if (p === '/session') same('sign-in', b, ['contract_version', 'account', 'csrf', 'expires_at']);
  else if (p === '/password') same('password', b, ['contract_version', 'csrf', 'expires_at']);
  else if (p === '/sessions') for (const s of b.sessions as unknown[]) same('session row', s, KEYS.session);
  else if (p === '/audit') for (const r of b.rows as unknown[]) same('audit row', r, KEYS.audit);
  else if (p === '/users' && method === 'GET') for (const u of b.users as unknown[]) same('user', u, KEYS.user);
  else if (p.startsWith('/users')) {
    same('user answer', b, ['contract_version', 'user'], 'code' in b ? ['code', 'code_valid_s'] : 'sessions_ended' in b ? ['sessions_ended'] : []);
    same('user', b.user, KEYS.user);
  } else if (p === '/tokens' && method === 'GET') {
    same('tokens', b, ['tokens']);
    for (const t of b.tokens as unknown[]) same('token', t, KEYS.token);
  } else if (/^\/tokens\/[^/]+\/usage$/.test(p)) {
    same('token usage', b, KEYS.usage);
    same('usage totals', b.totals, KEYS.sums);
    for (const d of b.days as unknown[]) same('usage day', d, KEYS.sums, ['date']);
    for (const m of b.models as unknown[]) same('usage model', m, KEYS.sums, ['model', 'priced']);
    same('token', b.token, KEYS.token);
  } else if (p.startsWith('/tokens')) same('token', b, KEYS.token, 'token' in b ? ['token'] : []);
  else throw new Error(`the rig knows no shape for ${method} ${path}`);
}

export interface AccessRig {
  fleet: MockFleet;
  mock: MockAuth;
  api: AuthApi;
  flow: AccessFlow;
  send: Send;
  /** who the browser is: an account's session, local mode, or nobody */
  caller: Caller;
  /** every path asked so far, with its method */
  paths: string[];
  /** every body sent so far */
  bodies: unknown[];
  /** every answer so far, as text */
  seen: string[];
  /** nothing answers: the calls end with no status */
  offline: boolean;
  /** the accounts by name */
  who: Record<string, string>;
  now(): number;
  wait(ms: number): void;
  /** this browser as another account (a session of its own), as local mode, or signed out */
  as(name: string | 'local' | null): void;
}

/** A controller with the mock's accounts and the page's flow on it, signed in as `name`
 *  (`local`: local mode, nobody has a password; `off`: a controller without accounts). */
export function rig(name: 'kim' | 'sam' | 'ola' | 'vic' | 'local' | 'off' | null = 'kim'): AccessRig {
  const fleet = new MockFleet({ scenario: 'fleet4', now: T0 / 1000, accounts: name !== 'off' });
  const mock = fleet.auth!;
  let now = T0;
  const r = { caller: null as Caller, offline: false };
  const who: Record<string, string> = {};
  if (mock) for (const a of mock.accounts) who[a.username] = a.id;
  const as = (n: string | 'local' | null): void => {
    if (n === 'local') {
      mock.toLocal();
      r.caller = { kind: 'local' };
    } else if (n === null) r.caller = null;
    else r.caller = { kind: 'account', id: who[n], session: mock.open(mock.accounts.find((a) => a.id === who[n])!, now, 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0', '192.0.2.99').id };
  };
  if (mock) as(name);
  const paths: string[] = [];
  const bodies: unknown[] = [];
  const seen: string[] = [];
  const send: Send = async (method, path, body): Promise<Sent> => {
    const [p, qs = ''] = path.split('?');
    paths.push(`${method} ${path}`);
    if (body !== undefined) bodies.push(body);
    if (r.offline) return { status: 0, body: null };
    const q = new URLSearchParams(qs);
    let a: { status: number; body: unknown; session?: string };
    if (!mock) a = { status: 404, body: { error: { type: 'not_found', message: `no route ${method} ${p}` } } };
    else {
      // a cookie whose session ended is nobody's
      if (r.caller?.kind === 'account' && !mock.bySession(r.caller.session)) r.caller = null;
      mock.caller = r.caller;
      const signIn = { status: 401, body: { error: { type: 'unauthorized', code: 'sign_in_required', message: 'sign in, or send an API token' } } };
      if (p.startsWith('/v1/auth/')) {
        a = mock.handle(method, p, q, body ?? null, now, r.caller);
        if (a.status >= 200 && a.status < 300) shape(method, path, a.body);
        if (a.session) r.caller = mock.bySession(a.session);
      } else if (!r.caller) a = signIn;
      else if (p === '/v1/settings' || p.startsWith('/v1/settings/')) {
        a = fleet.settings.handle(method, p, q, body ?? null, now, mock.account(r.caller)?.username ?? 'local');
        if (a.status === 200) validSettings(schemaOf(method, path, a.body), a.body);
        else {
          validSettings('error', a.body);
          const type = (a.body as { error: { type: string } }).error.type;
          if (ERRORS[type] !== a.status) throw new Error(`${type} answered ${a.status}, the contract says ${ERRORS[type]}`);
        }
      } else if (p.startsWith('/v1/fleet/health')) {
        a = fleet.handle(method, p, q, body ?? null, now);
        if (a.status === 200) validFleet('health-target', a.body);
      } else throw new Error(`the pages asked ${method} ${path}: not a route of theirs`);
    }
    seen.push(JSON.stringify(a.body));
    // a copy, as JSON over the wire is: the page can never reach into the mock's state
    return { status: a.status, body: a.body == null ? null : JSON.parse(JSON.stringify(a.body)) };
  };
  const api = authApi(send);
  return {
    fleet, mock, api, flow: new AccessFlow(api, () => undefined, () => now), send, paths, bodies, seen, who, as, now: () => now, wait: (ms) => void (now += ms),
    get caller() {
      return r.caller;
    },
    get offline() {
      return r.offline;
    },
    set offline(v: boolean) {
      r.offline = v;
    },
  };
}

const noop = (): void => undefined;
export const ON: On = { submit: noop, signOut: noop, filter: noop, retry: noop };

/** the page as text */
export const page = (r: AccessRig): string => text(accessView(r.flow, ON));
/** the dialog as text; empty when none is open */
export const dialog = (r: AccessRig): string => text(dialogView(r.flow, ON, ORIGIN));
/** opens a page as the element does */
export const open = (r: AccessRig, p: Page, filter: string | null = null): Promise<void> => r.flow.open(p, filter);
/** the buttons a text draws: their data-act values */
export const acts = (t: string): string[] => [...t.matchAll(/data-act="?([\w-]+)/g)].map((m) => m[1]);
/** one row of a list, by its id */
export function rowText(t: string, id: string): string {
  const at = t.indexOf(`data-row=${id}>`);
  if (at < 0) throw new Error(`no row ${id}`);
  const end = t.indexOf('data-row=', at + 9);
  const panel = t.indexOf('data-panel=', at);
  const stop = [end, panel].filter((x) => x > 0);
  return t.slice(at, stop.length ? Math.min(...stop) : undefined);
}
/** one panel of the page, by its name */
export function panelText(t: string, name: string): string {
  const at = t.indexOf(`data-panel=${name}>`);
  if (at < 0) throw new Error(`no panel ${name}`);
  const end = t.indexOf('data-panel=', at + 11);
  return t.slice(at, end < 0 ? undefined : end);
}
