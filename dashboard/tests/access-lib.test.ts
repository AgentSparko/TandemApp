// Tests for SRV-875: what the account and access pages know without a controller
// (src/lib/access.ts), their place in the routes, the menu and the settings area, the links from
// the health page's findings, and a refusal's words.
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { ENGINE_CODES } from '../mock/enginecodes.ts';
import { AuthError, type Me, type Token, type User } from '../src/api/auth.ts';
import { PAGES, ago, auditWords, browserOf, endsSoon, errorWords, findingsFor, isLoopback, lastOwner, mayChange, mayCreate, pageOf, rolesFor, scopesFor, snippets, stateWords, tokenBody, tokensFor, userNameError, type TokenForm } from '../src/lib/access.ts';
import { FINDING_PAGE } from '../src/lib/health.ts';
import { ACCESS_PAGES, ACCOUNT_MENU, ACCOUNT_TABS, MENU, SETTINGS_TABS, crumb, menuItem, menuNow, sectionOf, setRights, settingsHref, settingsTabs, subPages } from '../src/lib/menu.ts';
import { VIEWS, parseHash } from '../src/lib/router.ts';
import { groupsOf, labelOf, type Field } from '../src/lib/settings-form.ts';
import { SCOPE_NAME, errorWords as settingsWords } from '../src/lib/settings-words.ts';

const SRC = join(__dirname, '..', 'src');
const NOW = Date.parse('2026-11-16T09:00:00Z');
const me = (role: Me['account'] extends infer A ? (A extends { role: infer R } ? R : never) : never, more: string[] = []): Me => ({
  authenticated: true, account: { id: 'a1', username: 'kim', display_name: null, role, must_change: false }, principal: { kind: 'account', name: 'kim' }, setup_required: false, local_mode: false, expires_at: null,
  permissions: [...{ viewer: ['read', 'metrics:read', 'fleet:read', 'inference', 'self'], operator: ['read', 'metrics:read', 'fleet:read', 'inference', 'self', 'settings:write', 'fleet:write', 'chat', 'tokens:inference', 'tokens:management'], admin: ['read', 'metrics:read', 'fleet:read', 'inference', 'self', 'settings:write', 'fleet:write', 'chat', 'tokens:inference', 'tokens:management', 'trust', 'users'], owner: ['read', 'metrics:read', 'fleet:read', 'inference', 'self', 'settings:write', 'fleet:write', 'chat', 'tokens:inference', 'tokens:management', 'trust', 'users', 'owner'] }[role], ...more],
});
const form = (o: Partial<TokenForm> = {}): TokenForm => ({ label: 'desk', scopes: ['inference:openai'], models: '', days: '90', limits: {}, ...o });
const token = (o: Partial<Token> = {}): Token => ({ id: 'aaaaaaaa', kind: 'user', label: 'desk', owner: 'kim', owner_id: 'a1', hint: 'tdm-aaaaaaaa-...wxyz', scopes: ['inference:openai'], models: [], limits: {}, memory_user: 'kim', source: 'created', needs_review: false, state: 'active', created_at: null, expires_at: null, revoked_at: null, last_used_at: null, ...o });
const user = (o: Partial<User> = {}): User => ({ id: 'a1', username: 'kim', display_name: null, role: 'owner', enabled: true, provider: 'local', has_password: true, must_change: false, created_at: NOW, last_login_at: null, ...o });

describe('the routes and the menu', () => {
  it('two views with their pages below: #/account, #/account/tokens, #/access/<page>', () => {
    expect(VIEWS).toContain('account');
    expect(VIEWS).toContain('access');
    const of = (h: string) => {
      const r = parseHash(h);
      return [r.view, pageOf(r.view, r.path)];
    };
    expect([of('#/account'), of('#/account/tokens'), of('#/access'), of('#/access/tokens'), of('#/access/exposure'), of('#/access/sessions'), of('#/access/audit'), of('#/access/nope')]).toEqual([
      ['account', 'account'], ['account', 'my-tokens'], ['access', 'users'], ['access', 'tokens'], ['access', 'exposure'], ['access', 'sessions'], ['access', 'audit'], ['access', 'users'],
    ]);
    // a filter rides in the route's params: an account's id, nothing else
    expect(parseHash('#/access/tokens?owner=abc').params.get('owner')).toBe('abc');
    // each page of the menu has its head, and its route leads back to it
    expect(PAGES.map((p) => [p.id, p.href, p.right])).toEqual(ACCESS_PAGES.map(([id, href, , right]) => [id, href, right]));
    for (const p of PAGES) expect(p.title.length && p.sub.length, p.id).toBeGreaterThan(0);
  });

  it('the entries stand under Settings, with their own crumb, and only with accounts', () => {
    expect(MENU.flatMap((g) => g.items.map((i) => i.id))).not.toContain('account');
    expect(menuNow()).toBe(MENU);
    setRights(['self']);
    const settings = menuNow().find((g) => g.label === 'Settings')!.items.map((i) => i.label);
    expect(settings.slice(-1)).toEqual(['Account']);
    setRights(['self', 'users']);
    expect(menuNow().find((g) => g.label === 'Settings')!.items.map((i) => i.label).slice(-2)).toEqual(['Account', 'Users & access']);
    // the menu is new each time: the constant is never changed
    expect(MENU.find((g) => g.label === 'Settings')!.items.map((i) => i.id)).toEqual(['start', 'add-engine', 'add-spark', 'app']);
    setRights(null);
    expect([sectionOf('account'), sectionOf('access')]).toEqual(['account', 'access']);
    expect([crumb('account'), crumb('access')]).toEqual([{ group: 'Settings', label: 'Account' }, { group: 'Settings', label: 'Users & access' }]);
    expect(menuItem('access').label).toBe('Users & access');
    expect(ACCOUNT_MENU.map((x) => [x.id, x.right])).toEqual([['account', 'self'], ['access', 'users']]);
    // both have an icon of their own
    const bits = readFileSync(join(SRC, 'ui', 'bits.ts'), 'utf8');
    expect(bits).toMatch(/\n  account: g\(svg`/);
    expect(bits).toMatch(/\n  access: g\(svg`/);
  });

  it('the settings area gets a Sign-in and an API page with accounts, and stays as it was without', () => {
    expect(settingsTabs(false)).toBe(SETTINGS_TABS);
    expect(settingsTabs(true).map((t) => [t.id, t.scope])).toEqual([['app', 'app'], ['signin', 'auth'], ['api', 'api'], ...SETTINGS_TABS.slice(1).map((t) => [t.id, t.scope])]);
    expect(ACCOUNT_TABS.map((t) => t.label)).toEqual(['Sign-in', 'API']);
    expect([settingsHref('signin'), settingsHref('api')]).toEqual(['#/app/signin', '#/app/api']);
    expect(subPages(parseHash('#/app/api')).map((s) => s.label)).not.toContain('API');
    setRights(['self']);
    expect(subPages(parseHash('#/app/api')).filter((s) => s.on).map((s) => s.label)).toEqual(['API']);
    setRights(null);
    expect([SCOPE_NAME.auth, SCOPE_NAME.api]).toEqual(['Sign-in', 'API']);
    const area = readFileSync(join(SRC, 'views', 'settings', 'area.ts'), 'utf8');
    expect(area).toContain("return this.scopePage('auth', HERE,");
    expect(area).toContain("return this.scopePage('api', HERE,");
  });

  it('the settings form names each key of the two scopes and groups the API page', () => {
    expect([labelOf('auth', 'session.idle_days').label, labelOf('auth', 'local_login').label, labelOf('api', 'tokens.viewers_may_create').label, labelOf('api', 'openai.exposure').label, labelOf('api', 'anthropic.exposure').label]).toEqual([
      'Idle days before a session ends', 'The password form', 'Viewers create inference tokens', 'OpenAI-compatible API', 'Messages API',
    ]);
    const f = (key: string) => ({ key }) as Field;
    const groups = groupsOf('api', ['openai.exposure', 'anthropic.exposure', 'listen', 'tokens.default_days', 'tokens.viewers_may_create'].map(f));
    expect(groups.map((g) => [g.title, g.fields.map((x) => x.key)])).toEqual([
      ['Who may use the APIs', ['openai.exposure', 'anthropic.exposure', 'listen']],
      ['API tokens', ['tokens.default_days', 'tokens.viewers_may_create']],
    ]);
    // a refusal of the two scopes in words
    const said = (type: string, key = 'api.listen') => settingsWords({ type, message: 'x', facts: { key } }).title;
    expect([said('owner_required'), said('admin_required', 'api.tokens.viewers_may_create'), said('password_first')]).toEqual([
      'Not allowed: only an owner changes api.listen.', 'Not allowed: only an admin changes api.tokens.viewers_may_create.', 'api.listen lets other devices in, and no password is set yet.',
    ]);
  });

  it('a health finding about the sign-in links to the page that settles it', () => {
    expect(Object.keys(FINDING_PAGE).sort()).toEqual(['auth_owner_no_password', 'exposure_no_tokens', 'exposure_plain_http', 'exposure_unreachable', 'legacy_admin_token', 'local_mode_proxy_seen', 'token_expiring', 'tokens_need_review']);
    const hrefs = PAGES.map((p) => p.href);
    for (const [code, link] of Object.entries(FINDING_PAGE)) {
      expect(ENGINE_CODES[code], code).toBeTruthy();
      expect(hrefs, code).toContain(link.href);
      // the button says the page the fix sentence names
      const fix = ENGINE_CODES[code][2];
      const page = link.label.replace(/^Open /, '');
      if (link.label.startsWith('Open ') && code !== 'exposure_plain_http' && code !== 'token_expiring') expect(fix, code).toContain(page);
    }
    expect([FINDING_PAGE.tokens_need_review.label, FINDING_PAGE.exposure_unreachable.label, FINDING_PAGE.exposure_no_tokens.label]).toEqual(['Open All tokens', 'Open API exposure', 'Open My tokens']);
    const view = readFileSync(join(SRC, 'views', 'health', 'view.ts'), 'utf8');
    expect(view).toContain('if (pg) return html`<a class="btn hl-act" href=${pg.href} data-act="open-page">${pg.label}</a>`;');
  });
});

describe('a token’s form', () => {
  it('becomes the body the controller takes', () => {
    expect(tokenBody(form(), me('operator'))).toEqual({ label: 'desk', scopes: ['inference:openai'], days: 90 });
    expect(tokenBody(form({ label: '  desk  ', models: 'small-*,\n big-1  small-*', days: '30', limits: { requests_per_minute: '60', cost_per_day: ' 2.5 ', concurrent: '' } }), me('operator'))).toEqual({
      label: 'desk', scopes: ['inference:openai'], days: 30, models: ['small-*', 'big-1'], limits: { requests_per_minute: 60, cost_per_day: 2.5 },
    });
    expect(tokenBody(form({ days: 'never', scopes: ['usage:read'] }), me('admin'))).toMatchObject({ days: 'never' });
  });
  it('or says what is wrong before anything is sent', () => {
    const said = (o: Partial<TokenForm>, who = me('operator')) => tokenBody(form(o), who);
    expect(said({ label: ' ' })).toBe('Give the token a name of 1 to 64 characters.');
    expect(said({ label: 'x'.repeat(65) })).toBe('Give the token a name of 1 to 64 characters.');
    expect(said({ scopes: [] })).toBe('Pick at least one thing the token may do.');
    // a scope this role may not give is dropped, so none is left
    expect(said({ scopes: ['trust'] })).toBe('Pick at least one thing the token may do.');
    expect(said({ days: 'never' })).toBe('Only an admin makes a token that never expires.');
    expect(said({ days: '0' })).toBe('The days are a whole number of 1 or more.');
    expect(said({ days: '1.5' })).toBe('The days are a whole number of 1 or more.');
    expect(said({ limits: { concurrent: '-1' } })).toBe('A limit is a number of 0 or more.');
    expect(said({ limits: { concurrent: 'many' } })).toBe('A limit is a number of 0 or more.');
    expect(said({ models: Array.from({ length: 65 }, (_, i) => `m${i}`).join(',') })).toBe('A token names at most 64 models.');
    expect(said({ scopes: ['trust'], days: '31' }, me('admin'))).toBe('A token with trust lives at most 30 days.');
    expect(said({ scopes: ['trust'], days: 'never' }, me('admin'))).toBe('A token with trust lives at most 30 days.');
  });
  it('offers each role what it may give', () => {
    expect(scopesFor(me('viewer'))).toEqual([]);
    expect(mayCreate(me('viewer'))).toBe(false);
    expect(scopesFor(me('viewer', ['tokens:inference'])).map((s) => s.id)).toEqual(['inference:openai', 'inference:anthropic']);
    expect(scopesFor(me('operator')).map((s) => s.id)).not.toContain('trust');
    expect(scopesFor(me('admin')).map((s) => s.id)).toContain('trust');
    expect(scopesFor(null)).toEqual([]);
    expect(snippets('https://tandem.example')).toEqual([{ label: 'Base URL for an OpenAI-compatible client', value: 'https://tandem.example/v1' }, { label: 'Base URL for a Messages API client', value: 'https://tandem.example' }]);
  });
  it('reads as when it ends, and counts for a protocol while it is live', () => {
    expect(stateWords(token(), NOW)).toBe('never expires');
    expect(stateWords(token({ expires_at: '2026-11-19T09:00:00Z' }), NOW)).toBe('expires in 3 days');
    expect(stateWords(token({ state: 'expired', expires_at: '2026-11-01T09:00:00Z' }), NOW)).toBe('expired');
    expect(stateWords(token({ state: 'revoked' }), NOW)).toBe('revoked');
    expect([endsSoon(token({ expires_at: '2026-11-19T09:00:00Z' }), NOW), endsSoon(token({ expires_at: '2026-12-19T09:00:00Z' }), NOW), endsSoon(token(), NOW)]).toEqual([true, false, false]);
    const ts = [token(), token({ scopes: ['inference:anthropic', 'inference:openai'] }), token({ state: 'revoked' }), token({ kind: 'service' })];
    expect([tokensFor(ts, 'openai'), tokensFor(ts, 'anthropic')]).toEqual([2, 1]);
  });
});

describe('users', () => {
  it('the last owner, and who may change whom', () => {
    const all = [user(), user({ id: 'a2', username: 'sam', role: 'admin' }), user({ id: 'a3', username: 'zed', role: 'owner', enabled: false })];
    expect(all.map((u) => lastOwner(u, all))).toEqual([true, false, false]);
    expect(lastOwner(all[0], [...all, user({ id: 'a4', role: 'owner' })])).toBe(false);
    expect([mayChange(me('admin'), all[0]), mayChange(me('admin'), all[1]), mayChange(me('owner'), all[0]), mayChange(me('operator'), all[1])]).toEqual([false, true, true, false]);
    expect([rolesFor(me('admin')), rolesFor(me('owner'))]).toEqual([['viewer', 'operator', 'admin'], ['viewer', 'operator', 'admin', 'owner']]);
    expect([userNameError('eve'), userNameError(' Eve.A@x '), userNameError('')?.slice(0, 10), userNameError('a b')?.slice(0, 10)]).toEqual([null, null, 'A username', 'A username']);
  });
});

describe('words', () => {
  it('a refusal names the right that is missing, with the role', () => {
    const e = (code: string, status: number, facts = {}, message = '') => errorWords(new AuthError(code, message, status, facts), me('viewer'));
    expect(e('permission_denied', 403, { permission: 'users' })).toBe('Not allowed: this needs the right “users”. Your role is viewer.');
    expect(errorWords(new AuthError('permission_denied', '', 403, { permission: 'owner' }), null)).toBe('Not allowed: this needs the right “owner”.');
    // a 403 without a right is not called a missing right
    expect(e('wrong_password', 403)).toBe('The current password is not right.');
    expect(e('password_first', 409)).toBe('Set the owner’s password first. Until then TandemApp lets in this machine only.');
    expect(e('last_owner', 409)).toBe('This is the last owner. Make another owner first.');
    expect(e('service_identity', 409)).toBe('TandemApp manages this identity itself.');
    expect(e('own_account', 409)).toBe('Change your own password under Account, with the current one.');
    expect(e('too_many', 429)).toBe('Too many tries. Wait a minute and try again.');
    expect(e('offline', 0)).toBe('TandemApp did not answer. Check the connection and try again.');
    expect(e('sign_in_required', 401)).toBe('You are signed out. Sign in again to go on.');
    expect(e('conflict', 409, {}, "you have a token 'desk' already")).toBe("You have a token 'desk' already.");
    expect(e('error', 500)).toBe('TandemApp answered 500.');
  });
  it('a browser, a time and an audit row', () => {
    expect(browserOf('Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0')).toBe('Firefox on Linux');
    expect(browserOf('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')).toBe('Chrome on macOS');
    expect(browserOf('Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Mobile/15E148 Safari/604.1')).toBe('Safari on iOS');
    expect([browserOf('curl/8.7'), browserOf(''), browserOf(null)]).toEqual(['curl/8.7', 'unknown browser', 'unknown browser']);
    expect([ago(null, NOW), ago(NOW - 20_000, NOW), ago(NOW - 5 * 60_000, NOW), ago('2026-11-16T06:00:00Z', NOW), ago(NOW - 3 * 86_400_000, NOW), ago(NOW - 86_400_000, NOW), ago(NOW + 12 * 86_400_000, NOW)]).toEqual(['never', 'just now', '5 minutes ago', '3 hours ago', '3 days ago', '1 day ago', 'in 12 days']);
    const row = (object: string, action: string, detail = {}, object_id: string | null = null) => auditWords({ at: null, who: 'kim', action, object, object_id, fields: [], detail });
    expect(row('session', 'sign_in_failed', { address: '198.51.100.7' })).toEqual({ what: 'failed to sign in', about: 'from 198.51.100.7', bad: true });
    expect(row('account', 'role', { username: 'vic', from: 'viewer', to: 'operator' })).toEqual({ what: 'role changed', about: 'vic · viewer → operator', bad: false });
    expect(row('setting', 'set', { from: 'off', to: 'on' }, 'api.tokens.viewers_may_create')).toEqual({ what: 'setting changed', about: 'api.tokens.viewers_may_create · off → on', bad: false });
    expect(row('api_token', 'create', { label: 'desk', owner: 'kim' }).what).toBe('token made');
    expect(row('thing', 'done').what).toBe('thing done');
  });
  it('the findings of a protocol’s card, and a listener other devices cannot reach', () => {
    const f = (code: string, protocol?: string) => ({ code, severity: 'warning', sentence: 's', fix: 'f', facts: protocol ? { protocol } : {} });
    const all = [f('exposure_unreachable', 'OpenAI'), f('exposure_no_tokens', 'Anthropic'), f('exposure_plain_http'), f('tokens_need_review'), f('disk_low')];
    expect([findingsFor(all, 'OpenAI').map((x) => x.code), findingsFor(all, 'Anthropic').map((x) => x.code), findingsFor(all, null).map((x) => x.code)]).toEqual([['exposure_unreachable'], ['exposure_no_tokens'], ['exposure_plain_http', 'tokens_need_review']]);
    expect(['127.0.0.1', 'localhost', '::1', '[::1]', '0.0.0.0', '192.0.2.5', null].map(isLoopback)).toEqual([true, true, true, true, false, false, false]);
  });
});
