// Tests for SRV-875: the mock controller's accounts (mock/auth.ts) answer as the controller does.
// No contract folder describes /v1/auth/* yet, so the mock is held to the Python sources
// themselves: every view's fields, the scopes and what each opens, the limits, the roles' rights,
// the audit's objects, the one-time code's life and the keys of the `auth` and `api` settings are
// read from proxy/*.py and compared. Then the routes: who may call which, each refusal with its
// status and code, and what never comes back in an answer.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { AUDIT_OBJECTS, CODE_VALID_S, IMPLIES, INFERENCE, LIMIT_KEYS, MANAGEMENT, MockAuth, ROLES, SERVICE_SCOPES, TRUST_MAX_DAYS, USER_SCOPES, type Caller } from '../mock/auth.ts';
import { ACCOUNT_KEYS } from '../mock/settings.ts';
import { AuthError, LIMIT_KEYS as API_LIMITS, ROLES as API_ROLES, type Me } from '../src/api/auth.ts';
import { SCOPE_DEFS, scopesFor } from '../src/lib/access.ts';
import { KEYS, T0, rig, type AccessRig } from './access-rig.ts';

const PY = join(__dirname, '..', '..', 'proxy');
const py = (f: string): string => readFileSync(join(PY, f), 'utf8');
/** the text of a Python source between two markers */
function between(src: string, from: string, to: string): string {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a + from.length);
  if (a < 0 || b < 0) throw new Error(`the source has no ${a < 0 ? from : to}`);
  return src.slice(a, b);
}
const keysOf = (text: string): string[] => [...new Set([...text.matchAll(/"(\w+)":/g)].map((m) => m[1]))];
const strings = (text: string): string[] => [...text.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
/** a tuple or a set constant of a module, as its strings */
const constant = (src: string, name: string): string[] => strings(new RegExp(`^${name} = [^\\n(]*[({]([^)}]*)[)}]`, 'm').exec(src)![1]);

async function refused(p: Promise<unknown>): Promise<[number, string, unknown?]> {
  try {
    await p;
  } catch (e) {
    if (e instanceof AuthError) return e.facts.permission ? [e.status, e.code, e.facts.permission] : [e.status, e.code];
    throw e;
  }
  throw new Error('the call was not refused');
}
const raw = async (r: AccessRig, method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown): Promise<[number, string | null]> => {
  const a = await r.send(method, path, body);
  return [a.status, (a.body as { error?: { code?: string; type?: string } } | null)?.error?.code ?? (a.body as { error?: { type?: string } } | null)?.error?.type ?? null];
};

describe('the mock’s tables are the controller’s', () => {
  const accounts = py('accounts.py');
  const tokens = py('tokens.py');
  const authapi = py('authapi.py');
  const authz = py('authz.py');

  it('every view has the fields of the Python view', () => {
    expect(keysOf(between(accounts, 'def view(row: dict) -> dict:', 'def check_password'))).toEqual(KEYS.user);
    expect(keysOf(between(tokens, 'def view(self, row: dict) -> dict:', 'def _days'))).toEqual(KEYS.token);
    expect(keysOf(between(accounts, 'def sessions(self, account_id', 'def find_session')).filter((k) => k !== '_id_hash')).toEqual(KEYS.session.filter((k) => k !== 'current'));
    expect(authapi).toContain('"current": r["_id_hash"] == p.session');
    expect(keysOf(between(authapi, 'def _account_view', 'def _session_answer'))).toEqual(KEYS.account);
    // the session answer, with the fields of its principal and its provider
    expect(keysOf(between(authapi, 'def _session_answer', 'def handle')).filter((k) => !['kind', 'name', 'capabilities'].includes(k))).toEqual(KEYS.me);
    expect(keysOf(between(authapi, 'out = [{"at"', 'for t, w, act'))).toEqual(KEYS.audit);
    expect(between(authapi, 'def _with_code', 'def _reset')).toContain('"user": st.view(st.get(account_id)), "code": code,');
  });

  it('the scopes, what each opens, the limits and the days of trust', () => {
    expect(constant(tokens, 'INFERENCE')).toEqual(INFERENCE);
    expect(constant(tokens, 'MANAGEMENT')).toEqual(MANAGEMENT);
    expect(constant(tokens, 'SERVICE_SCOPES')).toEqual(SERVICE_SCOPES);
    expect(tokens).toContain('USER_SCOPES = INFERENCE + MANAGEMENT + (TRUST,)');
    expect(USER_SCOPES).toEqual([...INFERENCE, ...MANAGEMENT, 'trust']);
    expect(constant(tokens, 'LIMIT_KEYS')).toEqual(LIMIT_KEYS);
    expect(API_LIMITS).toEqual(LIMIT_KEYS);
    expect(Number(/^TRUST_MAX_DAYS = (\d+)/m.exec(tokens)![1])).toBe(TRUST_MAX_DAYS);
    const implies = between(tokens, 'IMPLIES = {', 'CREATE_RIGHT');
    for (const s of USER_SCOPES) {
      const m = new RegExp(`"${s}": \\{([^}]*)\\}`).exec(implies);
      expect(strings(m![1]).sort(), s).toEqual([...IMPLIES[s]].sort());
    }
    // the page offers exactly the scopes a person's token may hold
    expect(SCOPE_DEFS.map((s) => s.id)).toEqual(USER_SCOPES);
  });

  it('the roles and their rights', () => {
    expect(constant(authz, 'ROLES')).toEqual(ROLES);
    expect(API_ROLES).toEqual(ROLES);
    const m = new MockAuth(T0);
    const set = (name: string) => strings(new RegExp(`^${name} = [^\\n{]*\\{([^}]*)\\}`, 'm').exec(authz)![1]);
    expect([...m.rights('viewer')].sort()).toEqual(set('VIEWER').sort());
    expect([...m.rights('operator')].sort()).toEqual([...set('VIEWER'), ...set('OPERATOR')].sort());
    expect([...m.rights('admin')].sort()).toEqual([...set('VIEWER'), ...set('OPERATOR'), ...set('ADMIN')].sort());
    expect([...m.rights('owner')].sort()).toEqual([...set('VIEWER'), ...set('OPERATOR'), ...set('ADMIN'), 'owner'].sort());
    // the two rights a setting gives a viewer
    m.settings['api.tokens.viewers_may_create'] = 'on';
    m.settings['auth.chat_min_role'] = 'viewer';
    expect(m.rights('viewer').filter((x) => !set('VIEWER').includes(x)).sort()).toEqual(['chat', 'tokens:inference']);
    expect(authz).toContain('extra.add("tokens:inference")');
  });

  it('the audit’s objects, the code’s life and the routes’ rights', () => {
    expect(constant(authapi, 'AUDIT_OBJECTS')).toEqual(AUDIT_OBJECTS);
    expect(constant(authapi, 'AUDIT_SETTINGS')).toEqual(['auth.', 'api.']);
    expect(Number(/^CODE_TTL_MS = ([\d_]+)/m.exec(py('authlocal.py'))![1].replace(/_/g, '')) / 1000).toBe(CODE_VALID_S);
    for (const [path, right] of [['sessions', 'self'], ['tokens', 'self'], ['users', 'users']]) expect(authz).toMatch(new RegExp(`"prefix", "/v1/auth/${path}", "${right}"\\)`));
    expect(authz).toContain('(("PUT",), "exact", "/v1/auth/password", "self")');
    expect(authz).toContain('(GET, "exact", "/v1/auth/audit", "users")');
  });

  it('the keys of the auth and the api settings, with their types and defaults', () => {
    const src = py('settingscopes.py');
    let n = 0;
    for (const scope of ['auth', 'api'] as const) {
      for (const [k, d] of Object.entries(ACCOUNT_KEYS[scope])) {
        expect(src, `${scope}.${k}`).toContain(`"${k}": _spec("${d.type}", ${JSON.stringify(d.def)},`);
        n++;
      }
    }
    // no key of the controller is missing here
    expect(src.match(/": _spec\(/g)).toHaveLength(n);
    expect(src).toContain('values=("on", "off"), perm="users")');
    for (const k of ['provider', 'openai.exposure', 'anthropic.exposure', 'listen']) expect([ACCOUNT_KEYS.auth[k] ?? ACCOUNT_KEYS.api[k]].map((d) => d.password), k).toEqual([true]);
  });
});

describe('who may call what', () => {
  it('nobody signed in: 401 everywhere but the session’s own route', async () => {
    const r = rig(null);
    expect((await r.api.me())!.authenticated).toBe(false);
    for (const [m, p] of [['GET', '/v1/auth/users'], ['GET', '/v1/auth/sessions'], ['GET', '/v1/auth/tokens'], ['GET', '/v1/auth/audit'], ['POST', '/v1/auth/tokens'], ['PUT', '/v1/auth/password'], ['GET', '/v1/settings/api']] as const)
      expect(await raw(r, m, p, m === 'GET' ? undefined : {}), `${m} ${p}`).toEqual([401, 'sign_in_required']);
  });

  it('a viewer and an operator reach their own and nothing of the others’', async () => {
    for (const name of ['vic', 'ola'] as const) {
      const r = rig(name);
      expect((await r.api.sessions()).every((s) => s.username === name)).toBe(true);
      expect((await r.api.tokens()).every((t) => t.owner === name)).toBe(true);
      expect(await refused(r.api.users())).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.audit())).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.tokens(true))).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.sessions('*'))).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.sessions(r.who.kim))).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.endSessions(r.who.kim))).toEqual([403, 'permission_denied', 'users']);
      expect(await refused(r.api.resetUser(r.who.kim))).toEqual([403, 'permission_denied', 'users']);
      // someone else's token is as if it were not there
      const kims = r.mock.tokens.find((t) => t.label === 'backup script')!;
      expect(await refused(r.api.revokeToken(kims.id))).toEqual([404, 'not_found']);
      expect(kims.revoked_at).toBeNull();
    }
  });

  it('an admin manages users, sessions and tokens, but never an owner’s', async () => {
    const r = rig('sam');
    expect((await r.api.users()).map((u) => u.username)).toEqual(['kim', 'sam', 'ola', 'vic', 'old']);
    expect((await r.api.sessions('*')).length).toBeGreaterThan(4);
    expect((await r.api.audit()).length).toBeGreaterThan(4);
    expect(await refused(r.api.patchUser(r.who.kim, { enabled: false }))).toEqual([409, 'last_owner']);
    expect(await refused(r.api.removeUser(r.who.kim))).toEqual([409, 'last_owner']);
    expect(await refused(r.api.patchUser(r.who.ola, { role: 'owner' }))).toEqual([403, 'permission_denied', 'owner']);
    expect(await refused(r.api.resetUser(r.who.kim))).toEqual([403, 'permission_denied', 'owner']);
    expect(await refused(r.api.resetUser(r.who.sam))).toEqual([409, 'own_account']);
    expect(await refused(r.api.addUser({ username: 'eve', role: 'owner' }))).toEqual([403, 'permission_denied', 'owner']);
    // an owner's own token is not an admin's to revoke; the imported admin token is
    expect(await refused(r.api.revokeToken(r.mock.tokens.find((t) => t.label === 'backup script')!.id))).toEqual([404, 'not_found']);
    expect((await r.api.revokeToken(r.mock.tokens.find((t) => t.source === 'admin_token')!.id)).state).toBe('revoked');
    // someone else's token: revoked, never rotated
    const ci = r.mock.tokens.find((t) => t.label === 'ci')!;
    expect(await refused(r.api.rotateToken(ci.id, 0))).toEqual([403, 'permission_denied']);
    expect((await r.api.revokeToken(ci.id)).state).toBe('revoked');
    // a service identity is listed, never changed
    expect(await refused(r.api.revokeToken(r.mock.tokens.find((t) => t.label === 'svc:chatd')!.id))).toEqual([409, 'service_identity']);
  });

  it('the last owner stays until there is another; then an owner may go', async () => {
    const r = rig('kim');
    expect(await refused(r.api.patchUser(r.who.kim, { role: 'admin' }))).toEqual([409, 'last_owner']);
    expect((await r.api.patchUser(r.who.sam, { role: 'owner' })).role).toBe('owner');
    expect((await r.api.patchUser(r.who.kim, { role: 'admin' })).role).toBe('admin');
    // a lower role ended the sessions: this browser is nobody now
    expect(await refused(r.api.users())).toEqual([401, 'sign_in_required']);
  });

  it('a token never manages accounts, sessions or tokens', () => {
    const m = new MockAuth(T0);
    m.seed();
    const t: Caller = { kind: 'token' };
    for (const [method, path] of [['GET', '/v1/auth/users'], ['GET', '/v1/auth/tokens'], ['POST', '/v1/auth/tokens'], ['GET', '/v1/auth/sessions'], ['GET', '/v1/auth/audit'], ['PUT', '/v1/auth/password']])
      expect(m.handle(method, path, new URLSearchParams(), {}, T0, t).status, `${method} ${path}`).toBe(403);
  });

  it('local mode: no user, no token, no session until the owner has a password', async () => {
    const r = rig('local');
    const me = (await r.api.me()) as Me;
    expect([me.authenticated, me.local_mode, me.setup_required, me.account, me.permissions.includes('owner')]).toEqual([true, true, true, null, true]);
    expect(await refused(r.api.addUser({ username: 'eve', role: 'viewer' }))).toEqual([409, 'password_first']);
    expect(await refused(r.api.tokens())).toEqual([409, 'password_first']);
    expect(await refused(r.api.createToken({ label: 'x', scopes: ['inference:openai'] }))).toEqual([409, 'password_first']);
    expect(await refused(r.api.sessions())).toEqual([403, 'permission_denied', 'self']);
    expect(await refused(r.api.resetUser(r.who.vic))).toEqual([409, 'password_first']);
    expect(await refused(r.api.setApi('openai.exposure', 'network'))).toEqual([409, 'password_first']);
  });

  it('a controller without accounts answers none of it', async () => {
    const r = rig('off');
    expect(await r.api.me()).toBeNull();
    expect(await raw(r, 'GET', '/v1/auth/users')).toEqual([404, 'not_found']);
  });
});

describe('users', () => {
  it('an account made here has a one-time code, once: it signs in and must set a password', async () => {
    const r = rig('kim');
    const made = await r.api.addUser({ username: 'Eve', role: 'operator', display_name: 'Eve A' });
    expect([made.user.username, made.user.role, made.user.must_change, made.user.has_password, made.code_valid_s]).toEqual(['eve', 'operator', true, true, CODE_VALID_S]);
    expect(made.code.length).toBeGreaterThan(15);
    // no later answer holds the code
    const before = r.seen.length;
    await r.api.users();
    await r.api.audit();
    expect(r.seen.slice(before).join('')).not.toContain(made.code);
    // the code is the password until a new one is set
    r.as(null);
    expect((await r.send('POST', '/v1/auth/session', { username: 'eve', password: made.code })).status).toBe(200);
    expect((await r.api.me())!.account!.must_change).toBe(true);
    expect(await refused(r.api.tokens())).toEqual([403, 'password_change_required']);
    await r.api.password(made.code, 'a password of her own');
    expect((await r.api.me())!.account!.must_change).toBe(false);
    expect((await r.send('POST', '/v1/auth/session', { username: 'eve', password: made.code })).status).toBe(401);
  });

  it('the refusals of an add: a taken name, a bad name, a bad role', async () => {
    const r = rig('kim');
    expect(await refused(r.api.addUser({ username: 'sam', role: 'viewer' }))).toEqual([409, 'conflict']);
    expect(await refused(r.api.addUser({ username: 'Bad Name!', role: 'viewer' }))).toEqual([400, 'bad_request']);
    expect(await refused(r.api.addUser({ username: 'eve', role: 'root' as 'viewer' }))).toEqual([400, 'bad_request']);
  });

  it('a reset stops the old password and ends the sessions; a disable also revokes the tokens', async () => {
    const r = rig('kim');
    const had = r.mock.sessions.filter((s) => s.account_id === r.who.ola).length;
    expect(had).toBeGreaterThan(0);
    const got = await r.api.resetUser(r.who.ola);
    expect(got.user.must_change).toBe(true);
    expect(r.mock.sessions.filter((s) => s.account_id === r.who.ola)).toHaveLength(0);
    expect((await r.send('POST', '/v1/auth/session', { username: 'ola', password: 'the password of ola' })).status).toBe(401);
    const ci = r.mock.tokens.find((t) => t.label === 'ci')!;
    expect(ci.revoked_at).toBeNull();
    expect((await r.api.patchUser(r.who.ola, { enabled: false })).enabled).toBe(false);
    expect(ci.revoked_at).not.toBeNull();
    expect((await r.api.patchUser(r.who.ola, { enabled: true })).enabled).toBe(true);
    expect(ci.revoked_at).not.toBeNull();
    await r.api.removeUser(r.who.ola);
    expect((await r.api.users()).map((u) => u.username)).not.toContain('ola');
  });
});

describe('tokens', () => {
  it('a token is in the answer that made it and never again; a rotate keeps its id and name', async () => {
    const r = rig('kim');
    const made = await r.api.createToken({ label: 'desk', scopes: ['inference:openai'], models: ['small-*'], days: 30, limits: { requests_per_minute: 10 } });
    expect(made.token).toMatch(/^tdm-[0-9a-f]{8}-[A-Za-z0-9_-]{43}$/);
    expect([made.hint, made.state, made.models, made.limits, made.owner]).toEqual([`tdm-${made.id}-...${made.token.slice(-4)}`, 'active', ['small-*'], { requests_per_minute: 10 }, 'kim']);
    const before = r.seen.length;
    expect((await r.api.tokens()).find((t) => t.id === made.id)).toEqual((({ token: _t, ...rest }) => rest)(made));
    await r.api.tokens(true);
    await r.api.audit();
    expect(r.seen.slice(before).join('')).not.toContain(made.token);
    const again = await r.api.rotateToken(made.id, 3600);
    expect([again.id, again.label, again.token !== made.token, again.hint!.endsWith(again.token.slice(-4))]).toEqual([made.id, 'desk', true, true]);
  });

  it('each role makes exactly the tokens the page offers it, viewers once an admin allows it', async () => {
    for (const name of ['vic', 'ola', 'sam', 'kim'] as const) {
      for (const viewers of ['off', 'on']) {
        const r = rig(name);
        r.mock.settings['api.tokens.viewers_may_create'] = viewers;
        const offered = scopesFor(await r.api.me()).map((s) => s.id);
        for (const scope of USER_SCOPES) {
          const a = await r.send('POST', '/v1/auth/tokens', { label: `t-${scope}`, scopes: [scope], days: 7 });
          expect(a.status === 201, `${name} viewers=${viewers} ${scope}`).toBe(offered.includes(scope));
          if (a.status !== 201) expect(a.status).toBe(403);
        }
        if (name === 'vic') expect(offered).toEqual(viewers === 'on' ? INFERENCE : []);
      }
    }
  });

  it('the refusals of a create', async () => {
    const r = rig('ola');
    const make = (b: Record<string, unknown>) => refused(r.api.createToken({ label: 'x', scopes: ['inference:openai'], ...b } as never));
    expect(await make({ label: '' })).toEqual([400, 'bad_request']);
    expect(await make({ scopes: [] })).toEqual([400, 'bad_request']);
    expect(await make({ scopes: ['nope'] })).toEqual([400, 'bad_request']);
    expect(await make({ scopes: ['chat-engine'] })).toEqual([403, 'permission_denied']);
    expect(await make({ scopes: ['trust'] })).toEqual([403, 'permission_denied', 'trust']);
    expect(await make({ days: 'never' })).toEqual([403, 'permission_denied', 'users']);
    expect(await make({ days: 0.5 })).toEqual([400, 'bad_request']);
    expect(await make({ limits: { speed: 1 } })).toEqual([400, 'bad_request']);
    expect(await make({ label: 'ci' })).toEqual([409, 'conflict']);
    r.mock.settings['api.tokens.max_days'] = 30;
    expect(await make({ days: 31 })).toEqual([400, 'bad_request']);
    const k = rig('kim');
    expect(await refused(k.api.createToken({ label: 't', scopes: ['trust'], days: 31 }))).toEqual([400, 'bad_request']);
    expect(await refused(k.api.createToken({ label: 't', scopes: ['trust'], days: 'never' }))).toEqual([400, 'bad_request']);
    expect((await k.api.createToken({ label: 'forever', scopes: ['usage:read'], days: 'never' })).expires_at).toBeNull();
  });

  it('an imported key is confirmed or revoked by an admin, and then leaves the Review list', async () => {
    const r = rig('sam');
    const review = () => r.mock.tokens.filter((t) => t.needs_review && t.revoked_at === null).map((t) => t.label);
    expect(review()).toEqual(['phone', 'laptop', 'admin token']);
    const [phone, laptop] = r.mock.tokens.filter((t) => t.needs_review);
    expect((await r.api.confirmToken(phone.id)).needs_review).toBe(false);
    expect((await r.api.revokeToken(laptop.id)).state).toBe('revoked');
    expect(review()).toEqual(['admin token']);
    expect((await raw(r, 'PATCH', `/v1/auth/tokens/${phone.id}`, { needs_review: true }))[0]).toBe(400);
    const v = rig('ola');
    expect(await refused(v.api.confirmToken(v.mock.tokens.find((t) => t.label === 'ci')!.id))).toEqual([403, 'permission_denied', 'users']);
  });
});

describe('sessions and the password', () => {
  it('ending one session, every session of one’s own, and another account’s', async () => {
    const r = rig('kim');
    const mine = await r.api.sessions();
    expect(mine.filter((s) => s.current)).toHaveLength(1);
    expect(mine.length).toBe(3);
    await r.api.endSession(mine.find((s) => !s.current)!.id);
    expect(await r.api.sessions()).toHaveLength(2);
    await r.api.endSessions(r.who.sam);
    expect(r.mock.sessions.filter((s) => s.account_id === r.who.sam)).toHaveLength(0);
    await r.api.endSessions();
    expect(r.mock.sessions.filter((s) => s.account_id === r.who.kim)).toHaveLength(0);
    expect(await refused(r.api.sessions())).toEqual([401, 'sign_in_required']);
  });

  it('a new password ends every session and answers this browser’s new one', async () => {
    const r = rig('ola');
    expect(await refused(r.api.password('not it', 'a new password here'))).toEqual([403, 'wrong_password']);
    expect(await refused(r.api.password('the password of ola', 'short'))).toEqual([400, 'bad_password']);
    const before = r.mock.sessions.filter((s) => s.account_id === r.who.ola).map((s) => s.id);
    const got = await r.api.password('the password of ola', 'a new password here');
    expect(got.csrf).toMatch(/^csrf-/);
    const after = r.mock.sessions.filter((s) => s.account_id === r.who.ola).map((s) => s.id);
    expect(after).toHaveLength(1);
    expect(before).not.toContain(after[0]);
    expect((await r.api.sessions())[0].current).toBe(true);
  });
});

describe('the audit and what no answer holds', () => {
  it('the rows about people, newest first, never a password, a code or a token', async () => {
    const r = rig('kim');
    const code = (await r.api.addUser({ username: 'eve', role: 'viewer' })).code;
    const token = (await r.api.createToken({ label: 'desk', scopes: ['inference:openai'] })).token;
    r.wait(1000);
    await r.api.setApi('tokens.viewers_may_create', 'on');
    const rows = await r.api.audit();
    expect(rows[0]).toMatchObject({ who: 'kim', action: 'set', object: 'setting', object_id: 'api.tokens.viewers_may_create', detail: { from: 'off', to: 'on' } });
    expect(new Set(rows.map((x) => x.object))).toEqual(new Set(['session', 'account', 'api_token', 'setting']));
    expect(rows.map((x) => x.at)).toEqual([...rows.map((x) => x.at)].sort().reverse());
    expect(rows.find((x) => x.action === 'sign_in_failed')).toMatchObject({ who: 'vic', detail: { address: '198.51.100.7' } });
    expect((await raw(r, 'GET', '/v1/auth/audit?limit=0'))[0]).toBe(400);
    expect((await raw(r, 'GET', '/v1/auth/audit?limit=501'))[0]).toBe(400);
    expect((await r.send('GET', '/v1/auth/audit?limit=2')).body).toMatchObject({ rows: [{}, {}] });
    const all = r.seen.join('');
    // each secret came back exactly once: in the answer that made it
    for (const s of [code, token]) expect(all.split(s)).toHaveLength(2);
    expect(all).not.toContain('the password of');
  });
});

describe('the auth and the api settings', () => {
  const order = async (r: AccessRig) => ((await r.send('GET', '/v1/settings')).body as { order: string[] }).order;
  it('exist with accounts, after app, and not without', async () => {
    expect(await order(rig('kim'))).toEqual(['app', 'auth', 'api', 'backends', 'conductor', 'engine', 'hooks', 'plugins']);
    const off = rig('off');
    expect((off.fleet.settings.handle('GET', '/v1/settings', new URLSearchParams(), null, T0).body as { order: string[] }).order).toEqual(['app', 'backends', 'conductor', 'engine', 'hooks', 'plugins']);
    expect(off.fleet.settings.handle('GET', '/v1/settings/api', new URLSearchParams(), null, T0).status).toBe(404);
  });

  it('an owner writes them, an admin only the viewers’ switch, and each write is an audit row', async () => {
    const r = rig('kim');
    expect((await r.api.exposure()).values).toEqual({ openai: 'network', anthropic: 'this-machine' });
    await r.api.setApi('anthropic.exposure', 'network');
    expect((await r.api.exposure()).values.anthropic).toBe('network');
    expect((await r.api.audit())[0]).toMatchObject({ object_id: 'api.anthropic.exposure', detail: { from: 'this-machine', to: 'network' } });
    expect(await refused(r.api.setApi('anthropic.exposure', 'everyone'))).toEqual([422, 'bad_value']);
    const s = rig('sam');
    expect(await refused(s.api.setApi('openai.exposure', 'this-machine'))).toEqual([403, 'owner_required']);
    await s.api.setApi('tokens.viewers_may_create', 'on');
    expect(s.mock.rights('viewer')).toContain('tokens:inference');
    const o = rig('ola');
    expect(await refused(o.api.setApi('tokens.viewers_may_create', 'on'))).toEqual([403, 'admin_required']);
    // a setting written to what it is already plans no change and writes nothing
    const n = s.mock.audit.length;
    await s.api.setApi('tokens.viewers_may_create', 'on');
    expect(s.mock.audit.length).toBe(n);
  });

  it('the first value of the OpenAI switch follows the keys an install came with', async () => {
    const r = rig('kim');
    r.mock.hadLegacyKeys = false;
    expect((await r.api.exposure()).values.openai).toBe('this-machine');
    expect((await r.api.exposure()).listen).toBe('127.0.0.1');
  });
});

describe('the controller’s findings about the sign-in', () => {
  it('are what its accounts and switches say now', async () => {
    const r = rig('kim');
    const codes = async () => (await r.api.findings()).map((f) => f.code).filter((c) => /^(auth_|tokens_|legacy_|exposure_|token_)/.test(c));
    // imported keys wait, the admin token still works, OpenAI is open on a loopback listener, a token ends soon
    expect(await codes()).toEqual(['tokens_need_review', 'legacy_admin_token', 'exposure_unreachable', 'token_expiring']);
    const f = (await r.api.findings()).find((x) => x.code === 'exposure_unreachable')!;
    expect([f.facts!.protocol, f.sentence]).toEqual(['OpenAI', 'The OpenAI API is switched on for other devices, but they cannot reach it: listening on this machine only.']);
    r.mock.bind = '0.0.0.0';
    await r.api.setApi('anthropic.exposure', 'network');
    for (const t of r.mock.tokens) if (t.scopes.includes('inference:anthropic')) t.revoked_at = T0;
    r.wait(60_000);
    expect(await codes()).toContain('exposure_no_tokens');
    expect(await codes()).not.toContain('exposure_unreachable');
  });
});
