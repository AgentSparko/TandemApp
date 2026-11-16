// The controller's accounts as the mock answers them (SRV-875): /v1/auth/* as proxy/authapi.py and
// proxy/tokenapi.py do, with the gate in front of them (proxy/accounts.py, proxy/authz.py) and the
// `auth` and `api` settings the rights read (proxy/settingscopes.py). tests/access-mock.test.ts
// holds every answer's fields, the scopes, the limits and the rights to the Python sources, since
// no contract folder describes these routes yet.
//
// A caller is who the request comes from: an account's session, local mode (a bare install bound
// to this machine with no password: it acts as the owner `local`), an API token, or nobody. The
// mock keeps a password as text (it is a mock); no answer ever holds one, a token after the answer
// that made it, or a one-time code after the answer that made it.
//
// The scenario `seed()` makes: an owner, an admin, an operator, a viewer and a disabled account,
// each signed in somewhere; their tokens (one ending soon, one revoked); two keys imported from an
// older setup and the old admin token under Review; the service identities.

import type { AuditRow, Me, Role, Session, Token, User } from '../src/api/auth.ts';

export interface Reply {
  status: number;
  body: unknown;
  /** the session the answer's cookie carries (a sign-in, a new password) */
  session?: string;
}
export type Caller = { kind: 'account'; id: string; session: string } | { kind: 'local' } | { kind: 'token' } | null;

export const ROLES: Role[] = ['viewer', 'operator', 'admin', 'owner'];
const VIEWER = ['read', 'metrics:read', 'fleet:read', 'inference', 'self'];
const OPERATOR = [...VIEWER, 'settings:write', 'fleet:write', 'chat', 'tokens:inference', 'tokens:management'];
const ADMIN = [...OPERATOR, 'trust', 'users'];
const BY_ROLE: Record<Role, string[]> = { viewer: VIEWER, operator: OPERATOR, admin: ADMIN, owner: [...ADMIN, 'owner'] };

export const INFERENCE = ['inference:openai', 'inference:anthropic'];
export const MANAGEMENT = ['usage:read', 'metrics:read', 'settings:read', 'settings:write', 'fleet:read', 'fleet:write'];
export const USER_SCOPES = [...INFERENCE, ...MANAGEMENT, 'trust'];
export const SERVICE_SCOPES = ['collect:write', 'chat-engine', 'plugin-backend'];
export const LIMIT_KEYS = ['requests_per_minute', 'tokens_per_minute', 'concurrent', 'cost_per_day', 'cost_per_month'];
/** what a scope opens, before the owner's role cuts it (proxy/tokens.py IMPLIES) */
export const IMPLIES: Record<string, string[]> = {
  'inference:openai': ['inference'], 'inference:anthropic': ['inference'],
  'usage:read': ['read'], 'metrics:read': ['metrics:read', 'read'],
  'settings:read': ['read'], 'settings:write': ['read', 'settings:write'],
  'fleet:read': ['read', 'fleet:read'], 'fleet:write': ['read', 'fleet:read', 'fleet:write'],
  trust: ['trust', 'read', 'fleet:read'],
};
export const TRUST_MAX_DAYS = 30;
export const CODE_VALID_S = 86_400;
const DAY = 86_400_000;
const USERNAME = /^[a-z0-9][a-z0-9._@-]{0,63}$/;
const CONTRACT = '1.0';
/** the audit rows about people (proxy/authapi.py AUDIT_OBJECTS) */
export const AUDIT_OBJECTS = ['session', 'account', 'api_token', 'setup_code'];

interface Account extends User {
  password: string | null;
}
interface Sess {
  id: string;
  account_id: string;
  created_at: number;
  last_seen_at: number;
  address: string;
  agent: string;
}
interface Tok {
  id: string;
  kind: 'user' | 'service';
  owner_id: string | null;
  label: string;
  last4: string;
  scopes: string[];
  models: string[];
  limits: Record<string, number>;
  memory_user: string | null;
  source: string;
  needs_review: boolean;
  created_at: number;
  expires_at: number | null;
  revoked_at: number | null;
  last_used_at: number | null;
}

class Refuse extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public facts: Record<string, unknown> = {},
  ) {
    super(message);
  }
}
const no = (status: number, code: string, message: string, facts: Record<string, unknown> = {}): never => {
  throw new Refuse(status, code, message, facts);
};
const iso = (ms: number | null): string | null => (ms == null ? null : new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'));
const hex = (n: number, seed: number): string => Array.from({ length: n }, (_, i) => ((seed * 2654435761 + i * 40503 + (seed >> i % 7)) >>> 0).toString(16).slice(-1)).join('');
const FIREFOX = 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0';
const SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Mobile/15E148 Safari/604.1';
const CHROME = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';

export class MockAuth {
  accounts: Account[] = [];
  sessions: Sess[] = [];
  tokens: Tok[] = [];
  audit: (AuditRow & { n: number })[] = [];
  /** the `auth` and `api` settings that were set (the rest: their defaults) */
  settings: Record<string, unknown> = {};
  /** an install whose migration imported legacy keys starts with the OpenAI switch on network */
  hadLegacyKeys = false;
  /** what the controller listens on, and whether it runs in a pod */
  bind = '127.0.0.1';
  kubernetes = false;
  /** who the settings scopes see as the caller of the request under way */
  caller: Caller = null;
  private seq = 0;

  constructor(readonly t0: number = Date.now()) {}

  // ---- the rights -----------------------------------------------------------------------------------
  setting<T>(key: string, fallback: T): T {
    return key in this.settings ? (this.settings[key] as T) : fallback;
  }
  rights(role: Role | null): string[] {
    if (!role) return [];
    const out = [...BY_ROLE[role]];
    if (role === 'viewer') {
      if (this.setting<string>('auth.chat_min_role', 'operator') === 'viewer') out.push('chat');
      if (this.setting<string>('api.tokens.viewers_may_create', 'off') === 'on') out.push('tokens:inference');
    }
    return out;
  }
  account(c: Caller): Account | null {
    return c?.kind === 'account' ? (this.accounts.find((a) => a.id === c.id && a.enabled) ?? null) : null;
  }
  roleOf(c: Caller): Role | null {
    return c?.kind === 'local' ? 'owner' : (this.account(c)?.role ?? null);
  }
  may(c: Caller, right: string): boolean {
    return this.rights(this.roleOf(c)).includes(right);
  }
  /** no owner has a password: the first-run setup is open, and a bare install is in local mode */
  setupRequired(): boolean {
    return !this.accounts.some((a) => a.role === 'owner' && a.enabled && a.password !== null);
  }
  /** the caller of a session id (a cookie's value in the mock server), or nobody */
  bySession(sid: string | null | undefined): Caller {
    const s = sid ? this.sessions.find((x) => x.id === sid) : null;
    return s && this.accounts.some((a) => a.id === s.account_id && a.enabled) ? { kind: 'account', id: s.account_id, session: s.id } : null;
  }

  // ---- seeding --------------------------------------------------------------------------------------
  private id(n: number): string {
    return hex(n, ++this.seq + 977);
  }
  private log(now: number, who: string, action: string, object: string, object_id: string | null, detail: Record<string, unknown> = {}, fields: string[] = []): void {
    this.audit.push({ n: this.audit.length, at: iso(now), who, action, object, object_id, fields, detail });
  }
  add(username: string, role: Role, o: Partial<Account> = {}): Account {
    const a: Account = { id: this.id(12), username, display_name: null, role, enabled: true, provider: 'local', has_password: true, must_change: false, created_at: this.t0 - 40 * DAY, last_login_at: null, password: `the password of ${username}`, ...o };
    a.has_password = a.password !== null;
    this.accounts.push(a);
    return a;
  }
  open(a: Account, now: number, agent = FIREFOX, address = '192.0.2.10'): Sess {
    const s: Sess = { id: this.id(16), account_id: a.id, created_at: now, last_seen_at: now, address, agent };
    this.sessions.push(s);
    a.last_login_at = now;
    return s;
  }
  mint(owner: Account | null, label: string, scopes: string[], o: Partial<Tok> = {}): Tok {
    const t: Tok = { id: this.id(8), kind: 'user', owner_id: owner?.id ?? null, label, last4: this.id(4), scopes, models: [], limits: {}, memory_user: owner?.username ?? label, source: 'created', needs_review: false, created_at: this.t0 - 20 * DAY, expires_at: this.t0 + 70 * DAY, revoked_at: null, last_used_at: null, ...o };
    this.tokens.push(t);
    return t;
  }
  /** The scenario of the head of this file. Returns the accounts by name. */
  seed(): Record<'kim' | 'sam' | 'ola' | 'vic' | 'old', Account> {
    const t = this.t0;
    const kim = this.add('kim', 'owner', { display_name: 'Kim Reyes' });
    const sam = this.add('sam', 'admin');
    const ola = this.add('ola', 'operator');
    const vic = this.add('vic', 'viewer');
    const old = this.add('old', 'viewer', { enabled: false });
    this.open(kim, t - 3 * 3600_000);
    this.open(kim, t - 9 * DAY, SAFARI, '192.0.2.44');
    this.open(sam, t - 2 * DAY, CHROME, '192.0.2.21');
    this.open(ola, t - 5 * 3600_000, CHROME, '192.0.2.30');
    this.open(vic, t - 40 * 60_000, FIREFOX, '192.0.2.31');
    for (const a of [kim, sam, ola, vic]) this.log(a.last_login_at!, a.username, 'sign_in', 'session', null, { address: '192.0.2.10' });
    this.log(t - 30 * 60_000, 'vic', 'sign_in_failed', 'session', null, { address: '198.51.100.7' });
    this.mint(kim, 'laptop', [...INFERENCE], { last_used_at: t - 120_000 });
    this.mint(kim, 'backup script', ['settings:read', 'usage:read'], { expires_at: t + 3 * DAY, limits: { requests_per_minute: 60 } });
    this.mint(kim, 'old phone', ['inference:openai'], { revoked_at: t - 6 * DAY });
    this.mint(ola, 'ci', ['inference:openai'], { models: ['small-*'], last_used_at: t - 3600_000 });
    this.mint(null, 'phone', ['inference:openai'], { source: 'users', needs_review: true, expires_at: null, memory_user: 'phone' });
    this.mint(null, 'laptop', ['inference:openai'], { source: 'env', needs_review: true, expires_at: null, memory_user: 'laptop' });
    this.mint(kim, 'admin token', ['settings:read', 'settings:write', 'fleet:read', 'fleet:write', 'metrics:read'], { source: 'admin_token', needs_review: true, expires_at: t + 30 * DAY });
    this.mint(null, 'svc:chatd', ['inference:openai', 'chat-engine'], { kind: 'service', source: 'service', expires_at: null, memory_user: 'chatd' });
    this.mint(null, 'svc:cli', [...INFERENCE, ...MANAGEMENT], { kind: 'service', source: 'service', expires_at: null, memory_user: null });
    this.hadLegacyKeys = true;
    return { kim, sam, ola, vic, old };
  }

  /** A bare install nobody set a password on: no account can sign in, so there is no session and
   *  no token, and a request from this machine acts as the owner `local`. */
  toLocal(): void {
    for (const a of this.accounts) (a.password = null), (a.has_password = false);
    this.sessions = [];
    this.tokens = [];
    this.hadLegacyKeys = false;
  }

  // ---- views ----------------------------------------------------------------------------------------
  private user(a: Account): User {
    const { password: _pw, ...view } = a;
    return view;
  }
  private state(t: Tok, now: number): Token['state'] {
    return t.revoked_at !== null ? 'revoked' : t.expires_at !== null && now >= t.expires_at ? 'expired' : 'active';
  }
  private token(t: Tok, now: number): Token {
    const owner = this.accounts.find((a) => a.id === t.owner_id) ?? null;
    return {
      id: t.id, kind: t.kind, label: t.label, owner: owner?.username ?? null, owner_id: t.owner_id,
      hint: t.source === 'created' ? `tdm-${t.id}-...${t.last4}` : t.last4 ? `...${t.last4}` : null,
      scopes: t.scopes, models: t.models, limits: t.limits, memory_user: t.memory_user, source: t.source, needs_review: t.needs_review, state: this.state(t, now),
      created_at: iso(t.created_at), expires_at: iso(t.expires_at), revoked_at: iso(t.revoked_at), last_used_at: iso(t.last_used_at),
    };
  }
  private session(s: Sess, c: Caller): Session {
    const name = this.accounts.find((a) => a.id === s.account_id)!.username;
    const idle = Number(this.setting('auth.session.idle_days', 30));
    const abs = Number(this.setting('auth.session.absolute_days', 180));
    return { id: s.id, account_id: s.account_id, username: name, created_at: iso(s.created_at), last_seen_at: iso(s.last_seen_at), idle_until: iso(s.last_seen_at + idle * DAY), expires_at: iso(s.created_at + abs * DAY), address: s.address, agent: s.agent, current: c?.kind === 'account' && c.session === s.id };
  }
  me(c: Caller): Me & Record<string, unknown> {
    const a = this.account(c);
    const local = c?.kind === 'local';
    return {
      contract_version: CONTRACT, authenticated: !!a || local,
      account: a ? { id: a.id, username: a.username, display_name: a.display_name, role: a.role, must_change: a.must_change } : null,
      principal: a ? { kind: 'account', name: a.username } : local ? { kind: 'local', name: 'local' } : null,
      permissions: [...this.rights(this.roleOf(c))].sort(), csrf: a ? `csrf-${(c as { session: string }).session}` : null,
      provider: { kind: 'local', capabilities: { password: true, redirect: false, header: false, groups: false, manage_users: true, change_password: true } },
      setup_required: this.setupRequired(), local_mode: local, expires_at: null,
    };
  }

  // ---- the routes -----------------------------------------------------------------------------------
  /** One /v1/auth/* request of `c`. */
  handle(method: string, path: string, q: URLSearchParams, body: unknown, now: number, c: Caller): Reply {
    const parts = path.replace(/\/$/, '').split('/').slice(3).map(decodeURIComponent);
    const head = parts[0] ?? '';
    try {
      if (head === 'session' && parts.length === 1) return this.signIn(method, body, now, c);
      const need = head === 'users' || head === 'audit' ? 'users' : head === 'sessions' || head === 'password' || head === 'tokens' ? 'self' : null;
      const methods: Record<string, string[]> = { users: ['GET', 'POST', 'PATCH', 'DELETE'], audit: ['GET'], sessions: ['GET', 'DELETE'], password: ['PUT'], tokens: ['GET', 'POST', 'PATCH', 'DELETE'] };
      if (!need || !methods[head].includes(method) || (head === 'audit' && parts.length > 1) || (head === 'password' && parts.length > 1)) no(403, 'unmapped_route', 'this route has no permission entry');
      if (c === null) return { status: 401, body: { error: { type: 'unauthorized', code: 'sign_in_required', message: 'sign in, or send an API token' } } };
      if (this.account(c)?.must_change && !(head === 'password' && method === 'PUT')) no(403, 'password_change_required', 'set a new password first (PUT /v1/auth/password)');
      // a token never manages accounts, sessions or tokens; an account or local mode needs the right
      if (c.kind === 'token' || !this.may(c, need!)) no(403, 'permission_denied', `this needs the permission ${need}`, { permission: need });
      if (head === 'sessions') return this.sessionsRoute(method, parts.slice(1), q, now, c);
      if (head === 'password') return this.password(body, now, c);
      if (head === 'users') return this.users(method, parts.slice(1), body, now, c);
      if (head === 'tokens') return this.tokensRoute(method, parts.slice(1), q, body, now, c);
      return this.auditRoute(q, c);
    } catch (e) {
      if (!(e instanceof Refuse)) throw e;
      const gate = e.code === 'permission_denied' && e.facts.permission && e.message.startsWith('this needs');
      return { status: e.status, body: { error: { type: gate ? 'forbidden' : e.code, code: e.code, message: e.message, ...e.facts } } };
    }
  }

  private signIn(method: string, body: unknown, now: number, c: Caller): Reply {
    if (method === 'GET') return { status: 200, body: this.me(c) };
    if (method === 'DELETE') {
      if (c?.kind === 'account') {
        this.sessions = this.sessions.filter((s) => s.id !== c.session);
        this.log(now, this.account(c)?.username ?? '', 'sign_out', 'session', c.session);
      }
      return { status: 204, body: null };
    }
    if (method !== 'POST') no(405, 'method_not_allowed', 'the session takes GET, POST and DELETE');
    const b = body as { username?: unknown; password?: unknown } | null;
    if (!b || typeof b !== 'object' || Object.keys(b).some((k) => k !== 'username' && k !== 'password')) no(400, 'bad_request', 'the body is {username, password}');
    const name = typeof b!.username === 'string' ? b!.username.trim().toLowerCase() : '';
    const a = this.accounts.find((x) => x.username === name && x.enabled && x.password !== null && x.password === b!.password);
    if (!a) {
      this.log(now, name || '-', 'sign_in_failed', 'session', null, { address: '192.0.2.10' });
      return { status: 401, body: { error: { type: 'unauthorized', code: 'wrong_credentials', message: 'the username or the password is not right' } } };
    }
    const s = this.open(a, now);
    this.log(now, a.username, 'sign_in', 'session', s.id, { address: s.address });
    return { status: 200, body: { contract_version: CONTRACT, account: { id: a.id, username: a.username, display_name: a.display_name, role: a.role, must_change: a.must_change }, csrf: `csrf-${s.id}`, expires_at: iso(now + 30 * DAY) }, session: s.id };
  }

  private end(ids: string[], now: number, actor: string, why: string): void {
    for (const id of ids) this.log(now, actor, 'revoke', 'session', id, { why });
    this.sessions = this.sessions.filter((s) => !ids.includes(s.id));
  }

  private sessionsRoute(method: string, rest: string[], q: URLSearchParams, now: number, c: Caller): Reply {
    const me = this.account(c);
    if (!me) no(403, 'permission_denied', 'sessions belong to an account', { permission: 'self' });
    const users = this.may(c, 'users');
    const whose = q.get('user') || me!.id;
    if (method === 'GET' && !rest.length) {
      if (whose !== me!.id && !users) no(403, 'permission_denied', 'another account’s sessions need the permission users', { permission: 'users' });
      const rows = this.sessions.filter((s) => whose === '*' || s.account_id === whose).sort((a, b) => b.created_at - a.created_at);
      return { status: 200, body: { contract_version: CONTRACT, sessions: rows.map((s) => this.session(s, c)) } };
    }
    if (method !== 'DELETE' || rest.length > 1) no(404, 'not_found', 'no such sessions route');
    if (rest.length) {
      const s = this.sessions.find((x) => x.id === rest[0]);
      if (!s) no(404, 'not_found', 'no such session');
      if (s!.account_id !== me!.id && !users) no(403, 'permission_denied', 'another account’s session needs the permission users', { permission: 'users' });
      this.end([s!.id], now, me!.username, s!.account_id === me!.id ? 'ended' : `ended by ${me!.username}`);
      return { status: 204, body: null };
    }
    if (whose !== me!.id && !users) no(403, 'permission_denied', 'another account’s sessions need the permission users', { permission: 'users' });
    if (!this.accounts.some((a) => a.id === whose)) no(404, 'not_found', 'no such account');
    this.end(this.sessions.filter((s) => s.account_id === whose).map((s) => s.id), now, me!.username, whose === me!.id ? 'signed out everywhere' : `ended by ${me!.username}`);
    return { status: 204, body: null };
  }

  private checkPassword(p: unknown): string {
    const min = Number(this.setting('auth.password.min_length', 12));
    if (typeof p !== 'string' || p.length < min || p.length > 1024) no(400, 'bad_password', `the password is at least ${min} characters`);
    return p as string;
  }

  private password(body: unknown, now: number, c: Caller): Reply {
    const me = this.account(c);
    if (!me) no(403, 'permission_denied', 'a password belongs to an account', { permission: 'self' });
    const b = body as { current?: unknown; password?: unknown } | null;
    if (!b || typeof b !== 'object' || !('password' in b) || Object.keys(b).some((k) => k !== 'current' && k !== 'password')) no(400, 'bad_request', 'the body is {current, password}');
    if (b!.current !== me!.password) no(403, 'wrong_password', 'the current password is not right');
    me!.password = this.checkPassword(b!.password);
    me!.must_change = false;
    this.log(now, me!.username, 'password', 'account', me!.id, { text: 'password changed' });
    this.end(this.sessions.filter((s) => s.account_id === me!.id).map((s) => s.id), now, me!.username, 'password changed');
    const s = this.open(me!, now);
    return { status: 200, body: { contract_version: CONTRACT, csrf: `csrf-${s.id}`, expires_at: iso(now + 30 * DAY) }, session: s.id };
  }

  private lastOwner(a: Account): boolean {
    return a.role === 'owner' && a.enabled && this.accounts.filter((x) => x.role === 'owner' && x.enabled).length === 1;
  }
  private code(a: Account, now: number, actor: string, action: string): Reply['body'] {
    const code = `${this.id(5)}-${this.id(5)}-${this.id(5)}-${this.id(5)}`;
    a.password = code;
    a.has_password = true;
    a.must_change = true;
    this.log(now, actor, action, 'account', a.id, { text: 'one-time code made' });
    this.end(this.sessions.filter((s) => s.account_id === a.id).map((s) => s.id), now, actor, 'password reset');
    return { contract_version: CONTRACT, user: this.user(a), code, code_valid_s: CODE_VALID_S };
  }
  private revokeOwner(a: Account, now: number, actor: string, why: string): void {
    for (const t of this.tokens) if (t.owner_id === a.id) this.revoke(t, now, actor, why);
  }

  /** a token stops for good and leaves the Review list; one that was revoked before stays as it is */
  private revoke(t: Tok, now: number, actor: string, why: string): void {
    if (t.revoked_at !== null) return;
    t.revoked_at = now;
    t.needs_review = false;
    this.log(now, actor, 'revoke', 'api_token', t.id, { label: t.label, why });
  }

  private users(method: string, rest: string[], body: unknown, now: number, c: Caller): Reply {
    const owner = this.may(c, 'owner');
    const actor = this.account(c)?.username ?? 'local';
    if (!rest.length) {
      if (method === 'GET') return { status: 200, body: { contract_version: CONTRACT, users: this.accounts.map((a) => this.user(a)) } };
      if (method !== 'POST') no(405, 'method_not_allowed', 'the users take GET and POST');
      const b = body as Record<string, unknown> | null;
      if (!b || typeof b !== 'object' || Object.keys(b).some((k) => !['username', 'role', 'password', 'display_name'].includes(k))) no(400, 'bad_request', 'the body is {username, role, password?, display_name?}');
      if (c?.kind === 'local') no(409, 'password_first', 'set the owner’s password first: local mode trusts this machine only');
      if (b!.role === 'owner' && !owner) no(403, 'permission_denied', 'only an owner makes an owner', { permission: 'owner' });
      const name = typeof b!.username === 'string' ? b!.username.trim().toLowerCase() : '';
      if (!USERNAME.test(name)) no(400, 'bad_request', 'a username is 1 to 64 characters: letters, digits and . _ @ -, starting with a letter or a digit');
      if (!ROLES.includes(b!.role as Role)) no(400, 'bad_request', `the role is one of ${ROLES.join(', ')}`);
      if (b!.display_name != null && typeof b!.display_name !== 'string') no(400, 'bad_request', 'display_name is a string');
      if ('password' in b!) this.checkPassword(b!.password);
      if (this.accounts.some((a) => a.username === name)) no(409, 'conflict', `the username '${name}' is taken`);
      const a = this.add(name, b!.role as Role, { display_name: (b!.display_name as string | undefined)?.trim() || null, created_at: now, password: 'password' in b! ? (b!.password as string) : null });
      this.log(now, actor, 'create', 'account', a.id, { username: name, role: a.role });
      if ('password' in b!) return { status: 201, body: { contract_version: CONTRACT, user: this.user(a) } };
      return { status: 201, body: this.code(a, now, actor, 'create') };
    }
    if (rest.length > 2) no(404, 'not_found', 'no such users route');
    const a = this.accounts.find((x) => x.id === rest[0]);
    if (!a) no(404, 'not_found', 'no such account');
    const t = a!;
    if (rest.length === 2) {
      if (rest[1] !== 'reset' || method !== 'POST') no(404, 'not_found', 'no such users route');
      if (c?.kind === 'local') no(409, 'password_first', 'set the owner’s password first: local mode trusts this machine only');
      if (c?.kind === 'account' && c.id === t.id) no(409, 'own_account', 'change your own password with the current one');
      if (t.provider !== 'local') no(409, 'external_account', 'this account signs in elsewhere: it has no password here');
      if (t.role === 'owner' && !owner) no(403, 'permission_denied', 'only an owner resets an owner', { permission: 'owner' });
      return { status: 200, body: this.code(t, now, actor, 'reset') };
    }
    if (method === 'PATCH') {
      const b = body as { role?: Role; enabled?: boolean; display_name?: string | null } | null;
      if (!b || typeof b !== 'object' || Array.isArray(b)) no(400, 'bad_request', 'the body is {role?, enabled?, display_name?}');
      const role = b!.role ?? t.role;
      const enabled = b!.enabled ?? t.enabled;
      if (this.lastOwner(t) && (role !== 'owner' || enabled === false)) no(409, 'last_owner', 'this is the last enabled owner: make another owner first');
      if ((t.role === 'owner' || role === 'owner') && !owner) no(403, 'permission_denied', 'only an owner changes an owner', { permission: 'owner' });
      if (!Object.keys(b!).length || Object.keys(b!).some((k) => !['role', 'enabled', 'display_name'].includes(k))) no(400, 'bad_request', 'the body is {role?, enabled?, display_name?}');
      if (!ROLES.includes(role) || typeof enabled !== 'boolean') no(400, 'bad_request', `the role is one of ${ROLES.join(', ')}; enabled a boolean`);
      const ends = (!enabled && t.enabled) || ROLES.indexOf(role) < ROLES.indexOf(t.role);
      if (role !== t.role) this.log(now, actor, 'role', 'account', t.id, { username: t.username, from: t.role, to: role });
      if (enabled !== t.enabled) this.log(now, actor, enabled ? 'enable' : 'disable', 'account', t.id, { username: t.username, from: t.enabled, to: enabled });
      t.role = role;
      t.enabled = enabled;
      if ('display_name' in b!) t.display_name = b!.display_name?.trim() || null;
      if (!enabled) this.revokeOwner(t, now, actor, 'owner disabled');
      if (ends) this.end(this.sessions.filter((s) => s.account_id === t.id).map((s) => s.id), now, actor, enabled ? 'role' : 'disabled');
      return { status: 200, body: { contract_version: CONTRACT, user: this.user(t), sessions_ended: ends } };
    }
    if (method === 'DELETE') {
      if (this.lastOwner(t)) no(409, 'last_owner', 'this is the last enabled owner: make another owner first');
      if (t.role === 'owner' && !owner) no(403, 'permission_denied', 'only an owner removes an owner', { permission: 'owner' });
      this.end(this.sessions.filter((s) => s.account_id === t.id).map((s) => s.id), now, actor, 'removed');
      this.revokeOwner(t, now, actor, 'owner removed');
      this.accounts = this.accounts.filter((x) => x !== t);
      this.log(now, actor, 'delete', 'account', t.id, { username: t.username });
      return { status: 204, body: null };
    }
    return no(405, 'method_not_allowed', 'a user takes PATCH and DELETE');
  }

  // ---- tokens (proxy/tokenapi.py, proxy/tokens.py) ---------------------------------------------------
  private checkScopes(scopes: unknown, role: Role): string[] {
    if (!Array.isArray(scopes) || !scopes.length || !scopes.every((s) => typeof s === 'string')) no(400, 'bad_request', `scopes is a list of: ${USER_SCOPES.join(', ')}`);
    const out = [...new Set(scopes as string[])];
    const unknown = out.find((s) => !USER_SCOPES.includes(s) && !SERVICE_SCOPES.includes(s));
    if (unknown) no(400, 'bad_request', `no scope '${unknown}'; the scopes are ${USER_SCOPES.join(', ')}`);
    const service = out.find((s) => SERVICE_SCOPES.includes(s));
    if (service) no(403, 'permission_denied', `${service} belongs to service identities only`, { scope: service });
    const perms = this.rights(role);
    if (out.some((s) => INFERENCE.includes(s)) && !perms.includes('tokens:inference'))
      no(403, 'permission_denied', role === 'viewer' ? 'an admin must allow viewers to make inference tokens (api.tokens.viewers_may_create)' : 'this role makes no inference tokens', { permission: 'tokens:inference' });
    if (out.some((s) => MANAGEMENT.includes(s)) && !perms.includes('tokens:management')) no(403, 'permission_denied', 'this role makes no management tokens', { permission: 'tokens:management' });
    if (out.includes('trust') && ROLES.indexOf(role) < ROLES.indexOf('admin')) no(403, 'permission_denied', 'only an admin’s token holds trust', { permission: 'trust' });
    for (const s of out) if (IMPLIES[s].some((p) => !perms.includes(p))) no(403, 'permission_denied', `the scope ${s} needs more than this role has`, { scope: s });
    return out;
  }
  private days(days: unknown, role: Role, trust: boolean): number | null {
    const admin = ROLES.indexOf(role) >= ROLES.indexOf('admin');
    const d = days === undefined || days === null || days === '' ? Number(this.setting('api.tokens.default_days', 90)) : days;
    if (d === 'never' || d === 0) {
      if (trust) no(400, 'bad_request', `a token with trust lives at most ${TRUST_MAX_DAYS} days`);
      if (!admin) no(403, 'permission_denied', 'a token that never expires needs an admin', { permission: 'users' });
      return null;
    }
    if (typeof d !== 'number' || !Number.isInteger(d) || d < 1) no(400, 'bad_request', 'days is a whole number of 1 or more, or "never"');
    const n = d as number;
    const cap = Number(this.setting('api.tokens.max_days', 0));
    if (cap && n > cap) no(400, 'bad_request', `a token lives at most ${cap} days here (api.tokens.max_days)`);
    if (trust && n > TRUST_MAX_DAYS) no(400, 'bad_request', `a token with trust lives at most ${TRUST_MAX_DAYS} days`);
    return n;
  }

  private tokensRoute(method: string, rest: string[], q: URLSearchParams, body: unknown, now: number, c: Caller): Reply {
    if (c?.kind === 'local') no(409, 'password_first', 'set the owner’s password first: local mode trusts this machine only, and a token is for another device');
    const me = this.account(c);
    if (!me) no(403, 'permission_denied', 'tokens belong to a signed-in account', { permission: 'self' });
    const admin = this.may(c, 'users');
    if (!rest.length) {
      if (method === 'GET') {
        const all = ['1', 'true'].includes(q.get('all') ?? '');
        if (all && !admin) no(403, 'permission_denied', 'every token needs the permission users', { permission: 'users' });
        return { status: 200, body: { tokens: this.tokens.filter((t) => all || t.owner_id === me!.id).map((t) => this.token(t, now)) } };
      }
      if (method !== 'POST') no(405, 'method_not_allowed', 'the tokens take GET and POST');
      const b = body as Record<string, unknown> | null;
      if (!b || typeof b !== 'object' || Object.keys(b).some((k) => !['label', 'scopes', 'models', 'days', 'limits'].includes(k))) no(400, 'bad_request', 'the body is {label, scopes, models?, days?, limits?}');
      const label = typeof b!.label === 'string' ? b!.label.trim() : '';
      if (!label || label.length > 64) no(400, 'bad_request', 'a label is 1 to 64 characters');
      const scopes = this.checkScopes(b!.scopes, me!.role);
      const models = b!.models ?? [];
      if (!Array.isArray(models) || models.length > 64 || !models.every((m) => typeof m === 'string' && m.length > 0 && m.length <= 200 && m.trim() === m)) no(400, 'bad_request', 'models is a list of at most 64 model ids or globs ("small-*")');
      const limits = (b!.limits ?? {}) as Record<string, unknown>;
      if (typeof limits !== 'object' || Array.isArray(limits) || Object.entries(limits).some(([k, v]) => !LIMIT_KEYS.includes(k) || (v !== null && (typeof v !== 'number' || v < 0)))) no(400, 'bad_request', `limits takes ${LIMIT_KEYS.join(', ')}, each a number of 0 or more`);
      const days = this.days(b!.days, me!.role, scopes.includes('trust'));
      if (this.tokens.some((t) => t.owner_id === me!.id && t.label === label && t.revoked_at === null)) no(409, 'conflict', `you have a token '${label}' already`);
      const secret = `${this.id(20)}${this.id(19)}${this.id(4)}`;
      const t = this.mint(me!, label, scopes, { models: [...new Set(models as string[])], limits: Object.fromEntries(Object.entries(limits).filter(([, v]) => v !== null)) as Record<string, number>, created_at: now, expires_at: days ? now + days * DAY : null, last4: secret.slice(-4) });
      this.log(now, me!.username, 'create', 'api_token', t.id, { label, owner: me!.username, scopes, days });
      return { status: 201, body: { ...this.token(t, now), token: `tdm-${t.id}-${secret}` } };
    }
    const t = this.tokens.find((x) => x.id === rest[0]);
    if (!t || rest.length > 2) no(404, 'not_found', 'no such token');
    const tok = t!;
    const mine = tok.owner_id === me!.id;
    const tokOwner = this.accounts.find((a) => a.id === tok.owner_id) ?? null;
    const others = admin && (this.may(c, 'owner') || !tokOwner || tokOwner.role !== 'owner' || tok.source === 'admin_token');
    if (!mine && !others) no(404, 'not_found', 'no such token');
    if (rest.length === 2) {
      if (rest[1] !== 'rotate' || method !== 'POST') no(404, 'not_found', 'no such token route');
      if (!mine) no(403, 'permission_denied', 'only its owner rotates a token');
      const secret = `${this.id(20)}${this.id(19)}${this.id(4)}`;
      tok.last4 = secret.slice(-4);
      this.log(now, me!.username, 'rotate', 'api_token', tok.id, { grace_s: Number((body as { grace_s?: number } | null)?.grace_s ?? 0) });
      return { status: 200, body: { ...this.token(tok, now), token: `tdm-${tok.id}-${secret}` } };
    }
    if (method === 'GET') return { status: 200, body: this.token(tok, now) };
    if (tok.kind === 'service' && !(['collect_token', 'metrics_token'].includes(tok.source) && admin)) no(409, 'service_identity', 'a service identity is managed by TandemApp itself');
    if (method === 'DELETE') {
      this.revoke(tok, now, me!.username, mine ? 'revoked' : `revoked by ${me!.username}`);
      return { status: 200, body: this.token(tok, now) };
    }
    if (method === 'PATCH') {
      const b = body as Record<string, unknown> | null;
      if (!b || typeof b !== 'object' || !Object.keys(b).length) no(400, 'bad_request', 'the body is {label?, models?} or {needs_review: false}');
      if ('needs_review' in b!) {
        if (Object.keys(b!).length !== 1 || b!.needs_review !== false) no(400, 'bad_request', 'confirm with {needs_review: false} alone');
        if (!admin) no(403, 'permission_denied', 'an admin confirms an imported key', { permission: 'users' });
        tok.needs_review = false;
        this.log(now, me!.username, 'confirm', 'api_token', tok.id);
        return { status: 200, body: this.token(tok, now) };
      }
      if (!mine) no(403, 'permission_denied', 'only its owner changes a token');
      if (typeof b!.label === 'string') tok.label = b!.label.trim();
      if (Array.isArray(b!.models)) tok.models = b!.models as string[];
      this.log(now, me!.username, 'update', 'api_token', tok.id, {}, Object.keys(b!).sort());
      return { status: 200, body: this.token(tok, now) };
    }
    return no(405, 'method_not_allowed', 'a token takes GET, PATCH and DELETE');
  }

  private auditRoute(q: URLSearchParams, c: Caller): Reply {
    if (c?.kind === 'token' || !this.may(c, 'users')) no(403, 'permission_denied', 'the sign-in audit needs a signed-in account with the permission users', { permission: 'users' });
    const limit = /^\d+$/.test(q.get('limit') ?? '100') ? Number(q.get('limit') ?? 100) : 0;
    if (limit < 1 || limit > 500) no(400, 'bad_request', 'limit is a whole number 1..500');
    const rows = this.audit.filter((r) => AUDIT_OBJECTS.includes(r.object) || (r.object === 'setting' && /^(auth|api)\./.test(r.object_id ?? '')));
    return { status: 200, body: { contract_version: CONTRACT, rows: rows.sort((a, b) => (a.at === b.at ? b.n - a.n : a.at! < b.at! ? 1 : -1)).slice(0, limit).map(({ n: _n, ...r }) => r) } };
  }

  /** What the health page says about the sign-in (proxy/accounts.py findings, proxy/exposure.py):
   *  the controller's findings as {code, facts}. `local`: the install is in local mode. */
  findings(now: number, local = false): { code: string; facts: Record<string, string | number> }[] {
    const out: { code: string; facts: Record<string, string | number> }[] = [];
    const need = this.setupRequired();
    if (need && !local) out.push({ code: 'auth_setup_pending', facts: {} });
    if (local) out.push({ code: 'auth_owner_no_password', facts: {} });
    const live = this.tokens.filter((t) => this.state(t, now) === 'active');
    const review = live.filter((t) => t.needs_review).length;
    if (review) out.push({ code: 'tokens_need_review', facts: { n: review } });
    if (live.some((t) => t.source === 'admin_token')) out.push({ code: 'legacy_admin_token', facts: {} });
    for (const [p, name] of [['openai', 'OpenAI'], ['anthropic', 'Anthropic']] as const) {
      if (this.exposure(p) !== 'network') continue;
      if (!live.some((t) => t.kind !== 'service' && t.scopes.includes(`inference:${p}`))) out.push({ code: 'exposure_no_tokens', facts: { protocol: name } });
      if (/^(127\.|localhost$|\[?::1\]?$)/.test(this.bind)) out.push({ code: 'exposure_unreachable', facts: { protocol: name, reason: 'listening on this machine only' } });
    }
    const soon = live.filter((t) => t.kind !== 'service' && t.expires_at !== null && t.expires_at - now < 7 * DAY).length;
    if (soon) out.push({ code: 'token_expiring', facts: { n: soon } });
    return out;
  }
  /** a protocol's switch as it stands: what was set, else the install's first value */
  exposure(p: 'openai' | 'anthropic'): string {
    return this.setting<string>(`api.${p}.exposure`, p === 'openai' && this.hadLegacyKeys ? 'network' : 'this-machine');
  }

  /** a settings key of the `auth` or `api` scope was written (mock/settings.ts) */
  wrote(now: number, key: string, from: unknown, to: unknown, set: boolean): void {
    this.log(now, this.account(this.caller)?.username ?? 'local', set ? 'set' : 'delete', 'setting', key, { from, to });
  }
}
