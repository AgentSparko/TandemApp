// SEC-26: the page's side of the controller's sign-in. Gherkin "The first screen": with no owner
// the page shows the setup screen and says where the code is, and after the setup it shows the
// view that was asked for (the shell keeps the route); a signed-in page sends X-Tandem-CSRF on
// every write to its own origin and nothing elsewhere; the screens are a chunk of their own.
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { T, errorOf, needOf, type AuthSession } from '../src/auth/logic';
import { withCsrf } from '../src/lib/csrf';

const S = (o: Partial<AuthSession>): AuthSession => ({ authenticated: false, account: null, principal: null, csrf: null, setup_required: false, local_mode: false, ...o });
const SRC = join(__dirname, '..', 'src');
const DIST = join(__dirname, '..', 'dist');

describe('the first screen', () => {
  it('no owner, not signed in: the setup screen, which says where the code is', () => {
    expect(needOf(S({ setup_required: true }))).toBe('setup');
    expect(T.setup).toBe('This TandemApp has no owner yet. Enter the setup code to create one.');
    expect(T.where).toContain('tandem-app users setup-code');
    expect(T.where).toContain('service log');
  });
  it('an owner, not signed in: the sign-in; signed in: the page itself (its route)', () => {
    expect(needOf(S({}))).toBe('signin');
    expect(needOf(S({ authenticated: true, account: { username: 'kim', role: 'owner', must_change: false } }))).toBeNull();
    expect(T.signin).toBe('Sign in to TandemApp');
  });
  it('a one-time code: the password step first', () => {
    expect(needOf(S({ authenticated: true, account: { username: 'kim', role: 'viewer', must_change: true } }))).toBe('password');
  });
  it('local mode and the accounts off: no screen', () => {
    expect(needOf(S({ authenticated: true, local_mode: true, setup_required: true }))).toBeNull();
    expect(needOf(null)).toBeNull();
  });
  it('the shell keeps the route through the screen and starts once signed in', () => {
    const app = readFileSync(join(SRC, 'app.ts'), 'utf8');
    expect(app).toContain("import('./auth/view')");
    expect(app).toContain("r.headers.get('X-Tandem-Auth') === 'accounts'");
    expect(app).toMatch(/if \(this\.accounts\) await this\.checkAuth\(\);\s*this\.booted = true;\s*if \(!this\.setupMode && !this\.auth\) this\.start\(\);/);
    expect(app).not.toMatch(/navigate\([^)]*\)[^\n]*checkAuth/);
    // the screens are not imported by the entry
    expect(readFileSync(join(SRC, 'main.ts'), 'utf8')).not.toContain('auth/view');
  });
});

describe('a form’s answer in words', () => {
  it('names a wrong code with its tries left, a spent code, a wrong password, a wait', () => {
    expect(errorOf(201, null)).toBeNull();
    expect(errorOf(401, { error: { code: 'wrong_setup_code', tries_left: 3 } })).toBe('The setup code is not right. 3 tries left.');
    expect(errorOf(401, { error: { code: 'wrong_setup_code', tries_left: 1 } })).toBe('The setup code is not right. 1 try left.');
    expect(errorOf(410, { error: { code: 'setup_code_spent' } })).toBe(T.spent);
    expect(errorOf(401, { error: { code: 'wrong_credentials' } })).toBe(T.wrong);
    expect(errorOf(403, { error: { code: 'wrong_password' } })).toBe(T.current);
    expect(errorOf(429, null)).toBe(T.wait);
    expect(errorOf(0, null)).toBe(T.offline);
    expect(errorOf(400, { error: { message: 'the password is 12 to 1024 characters' } })).toBe('The password is 12 to 1024 characters.');
  });
});

describe('X-Tandem-CSRF', () => {
  const O = 'http://127.0.0.1:8080';
  const h = (init: RequestInit | undefined) => new Headers(init?.headers).get('X-Tandem-CSRF');
  it('is on every write to this origin', () => {
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'post']) {
      expect(h(withCsrf('/v1/settings/plan', { method }, 'tok', O)), method).toBe('tok');
    }
    expect(h(withCsrf(`${O}/v1/fleet/layouts/fleet`, { method: 'PUT', headers: { 'Content-Type': 'application/json' } }, 'tok', O))).toBe('tok');
    expect(new Headers(withCsrf('/x', { method: 'PUT', headers: { 'Content-Type': 'application/json' } }, 'tok', O)!.headers).get('Content-Type')).toBe('application/json');
    expect(h(withCsrf(new Request(`${O}/v1/x`, { method: 'POST' }), undefined, 'tok', O))).toBe('tok');
  });
  it('never on a read, another origin, or without a session', () => {
    expect(withCsrf('/v1/settings', undefined, 'tok', O)).toBeUndefined();
    expect(h(withCsrf('/v1/settings', { method: 'GET' }, 'tok', O))).toBeNull();
    expect(h(withCsrf('https://elsewhere.example/v1/x', { method: 'POST' }, 'tok', O))).toBeNull();
    expect(withCsrf('/v1/x', { method: 'POST' }, null, O)).toEqual({ method: 'POST' });
    // a header the caller set stays
    expect(h(withCsrf('/v1/x', { method: 'POST', headers: { 'X-Tandem-CSRF': 'own' } }, 'tok', O))).toBe('own');
  });
  it('is installed by the entry before any view', () => {
    const main = readFileSync(join(SRC, 'main.ts'), 'utf8');
    expect(main.indexOf("import './lib/csrf'")).toBeGreaterThanOrEqual(0);
    expect(main.indexOf("import './lib/csrf'")).toBeLessThan(main.indexOf("import './app'"));
  });
});

describe('dist/', () => {
  const present = existsSync(join(DIST, 'index.html'));
  it.skipIf(!present)('the sign-in screens are a chunk of their own, not part of the entry', () => {
    const files = readdirSync(join(DIST, 'assets'));
    const src = (f: string) => readFileSync(join(DIST, 'assets', f), 'utf8');
    const js = files.filter((f) => f.endsWith('.js'));
    const screens = js.filter((f) => src(f).includes('Create and sign in'));
    expect(screens).toHaveLength(1);
    const entry = js.filter((f) => f.startsWith('app-'));
    expect(entry).toHaveLength(1);
    expect(entry[0]).not.toBe(screens[0]);
    expect(src(entry[0])).toContain('X-Tandem-CSRF');
    expect(files.filter((f) => f.endsWith('.css') && src(f).includes('.au-card'))).toHaveLength(1);
  });
});
