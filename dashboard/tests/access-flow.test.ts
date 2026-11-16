// Tests for SRV-875: the account and access pages against the mock controller, without a browser.
// A page here is what the element holds: the flow (src/lib/access-flow.ts) and the templates
// drawn from it (src/views/access/view.ts).
//
//   Gherkin (SRV-875):
//   The token is shown once      "Given a signed-in user on My tokens, when they create a token,
//                                 then the token and two client snippets are shown with a copy
//                                 button; when they close the dialog and open the token again,
//                                 then only its last 4 characters are shown"
//   A viewer sees only their own "Given a signed-in viewer and api.tokens.viewers_may_create at its
//                                 default (off), then Users, All tokens and API exposure are not in
//                                 the menu, and My tokens shows no create button and says an admin
//                                 must allow viewers to create tokens, and a direct link to them
//                                 shows 'not allowed', not a broken page"
//   Signed out in the middle     "Given an open page whose session was revoked, when the next
//                                 request gets 401, then the sign-in screen appears and returns to
//                                 the same view after sign-in"
//   A viewer when an admin allows it
//                                "Given an admin switched api.tokens.viewers_may_create on, when a
//                                 signed-in viewer opens My tokens, then they can create an
//                                 inference token and see only their own"
//   Acceptance criteria:
//   every screen is usable at 360 px width
//   no token or password is ever in the URL, the console or local storage
//   every screen has a mock and a page test
//   the Review list lets an admin confirm or revoke each imported key
//   the viewer rule follows decision 4: no create button for a viewer by default, one with the
//   switch on; operators and admins always have it
// and the screens around them: the account's password and sessions, users (add with a one-time
// code, role, disable, reset, remove, the last owner), all tokens, exposure with its findings and
// the password-first rule, every session, the audit, a missing right in words, a controller
// without accounts, and one that does not answer.

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { PAGES, pageOf, type Page, type TokenForm } from '../src/lib/access.ts';
import { signedOutBy } from '../src/lib/csrf.ts';
import { menuNow, setRights, subPages } from '../src/lib/menu.ts';
import { parseHash } from '../src/lib/router.ts';
import type { SchemaView, ValuesView } from '../src/api/settings.ts';
import { fieldsOf, groupsOf, type Field } from '../src/lib/settings-form.ts';
import { T } from '../src/views/access/view.ts';
import { groupPanel, type FormHandlers, type FormView } from '../src/views/settings/form.ts';
import { text } from './engine-rig.ts';
import { ORIGIN, acts, dialog, open, page, panelText, rig, rowText, type AccessRig } from './access-rig.ts';

const SRC = join(__dirname, '..', 'src');
const form = (o: Partial<TokenForm> = {}): TokenForm => ({ label: 'desk', scopes: ['inference:openai'], models: '', days: '90', limits: {}, ...o });
const tokenOf = (r: AccessRig): string => (r.flow.dialog?.kind === 'token-once' ? r.flow.dialog.token.token : '');
/** the menu a person with these rights sees, as section ids */
function menuOf(rights: string[] | null): string[] {
  setRights(rights);
  const ids = menuNow().flatMap((g) => g.items.map((i) => i.id));
  setRights(null);
  return ids;
}
const rightsOf = async (r: AccessRig): Promise<string[]> => (await r.api.me())!.permissions;

describe('Gherkin: the auth screens (SRV-875)', () => {
  it('The token is shown once: with two client snippets and a copy button; after the dialog only its last 4 characters', async () => {
    // Given a signed-in user on My tokens
    const r = rig('ola');
    await open(r, 'my-tokens');
    expect(acts(page(r))).toContain('token-new');
    // When they create a token
    r.flow.show({ kind: 'token-new' });
    expect(dialog(r)).toContain('Create a token');
    await r.flow.createToken(form({ models: 'small-*, big-1', limits: { requests_per_minute: '30' } }));
    // Then the token and two client snippets are shown with a copy button
    const token = tokenOf(r);
    expect(token).toMatch(/^tdm-[0-9a-f]{8}-/);
    const d = dialog(r);
    expect(d).toContain(`data-secret=token>${token}<`);
    expect(d).toContain(T.once);
    expect(T.once).toBe('This token is shown once. Copy it now.');
    expect(d).toContain('You will not see it again');
    expect(d).toContain(`Base URL for an OpenAI-compatible client</dt><dd><div class="ac-secret"><code class="num" data-secret=url>${ORIGIN}/v1<`);
    expect(d).toContain(`Base URL for a Messages API client</dt><dd><div class="ac-secret"><code class="num" data-secret=url>${ORIGIN}<`);
    expect(d.match(/aria-label=Copy/g)).toHaveLength(3);
    expect(d).toContain('aria-label=Copy the token');
    // the body the controller got: the form's text as its fields
    expect(r.bodies.at(-1)).toEqual({ label: 'desk', scopes: ['inference:openai'], days: 90, models: ['small-*', 'big-1'], limits: { requests_per_minute: 30 } });
    // When they close the dialog and open the token again
    r.flow.close();
    expect(dialog(r)).toBe('');
    await open(r, 'my-tokens');
    // Then only its last 4 characters are shown
    const id = token.split('-')[1];
    const row = rowText(page(r), id);
    expect(row).toContain(`tdm-${id}-...${token.slice(-4)}`);
    expect(page(r)).not.toContain(token);
    expect(JSON.stringify(r.flow.tokens) + JSON.stringify(r.flow.dialog)).not.toContain(token);
    // and no later answer of the controller holds it
    expect(r.seen.filter((s) => s.includes(token))).toHaveLength(1);
  });

  it('A viewer sees only their own: no Users, All tokens or API exposure in the menu, no create button, and "not allowed" on a direct link', async () => {
    // Given a signed-in viewer and api.tokens.viewers_may_create at its default (off)
    const r = rig('vic');
    expect(r.mock.setting('api.tokens.viewers_may_create', 'off')).toBe('off');
    const rights = await rightsOf(r);
    // Then Users, All tokens and API exposure are not in the menu
    expect(menuOf(rights)).toContain('account');
    expect(menuOf(rights)).not.toContain('access');
    setRights(rights);
    expect(subPages(parseHash('#/account')).map((s) => s.label)).toEqual(['Password and sessions', 'My tokens']);
    expect(subPages(parseHash('#/access'))).toEqual([]);
    setRights(null);
    // And My tokens shows no create button and says an admin must allow viewers to create tokens
    await open(r, 'my-tokens');
    const t = page(r);
    expect(acts(t)).not.toContain('token-new');
    expect(t).toContain('data-no-create>An admin must allow viewers to create tokens.');
    // And a direct link to them shows "not allowed", not a broken page
    for (const hash of ['#/access', '#/access/tokens', '#/access/exposure', '#/access/sessions', '#/access/audit']) {
      const route = parseHash(hash);
      const before = r.paths.length;
      await open(r, pageOf(route.view, route.path));
      const p = page(r);
      expect(p, hash).toContain('data-stop="not-allowed"');
      expect(p, hash).toContain('Not allowed');
      expect(p, hash).toContain('needs the right “users”. You are signed in as vic, and your role is viewer.');
      // the page asked who is signed in and nothing it may not read
      expect(r.paths.slice(before), hash).toEqual(['GET /v1/auth/session']);
      // its section's other pages are not offered either
      expect(p, hash).not.toContain('class="ac-tabs"');
    }
  });

  it('Signed out in the middle: the next request gets 401, the sign-in takes the page, and the same view opens after sign-in', async () => {
    // Given an open page whose session was revoked
    const r = rig('ola');
    await open(r, 'my-tokens');
    expect(page(r)).toContain('data-panel=my-tokens');
    r.mock.sessions = [];
    // When the next request gets 401
    r.flow.show({ kind: 'token-new' });
    await r.flow.createToken(form());
    expect(r.flow.me!.authenticated).toBe(false);
    expect(dialog(r)).toContain('You are signed out. Sign in again to go on.');
    // Then the sign-in screen appears: the fetch wrapper tells the shell about a 401 of a signed-in page
    expect(signedOutBy(401, '/v1/auth/tokens', 'csrf', ORIGIN)).toBe(true);
    expect(signedOutBy(401, `${ORIGIN}/v1/settings/plan`, 'csrf', ORIGIN)).toBe(true);
    // (never for the sign-in's own form, another origin, another status, or a page that is not signed in)
    expect([signedOutBy(401, '/v1/auth/session', 'csrf', ORIGIN), signedOutBy(401, 'https://elsewhere.example/v1/x', 'csrf', ORIGIN), signedOutBy(403, '/v1/auth/tokens', 'csrf', ORIGIN), signedOutBy(401, '/v1/auth/tokens', null, ORIGIN)]).toEqual([false, false, false, false]);
    const csrf = readFileSync(join(SRC, 'lib', 'csrf.ts'), 'utf8');
    expect(csrf).toMatch(/if \(signedOutBy\(res\.status, res\.url, token, location\.origin\)\) window\.dispatchEvent\(new CustomEvent\('qse-unauthorized'\)\)/);
    const app = readFileSync(join(SRC, 'app.ts'), 'utf8');
    expect(app).toContain("const onUnauth = () => (this.accounts ? void this.checkAuth() : (this.refused = true));");
    expect(app).toContain("window.addEventListener('qse-unauthorized', onUnauth)");
    // the shell draws the sign-in in place of the page and never changes the route for it
    expect(app).toMatch(/if \(this\.accounts && \(this\.auth \|\| \(this\.authLocal && location\.hash\.startsWith\('#\/account'\)\)\)\) \{\s*return html`<qse-auth /);
    expect(app).not.toMatch(/checkAuth\(\)[^\n]*navigate\(|navigate\([^)]*\)[^\n]*checkAuth/);
    // the page itself says it too, should the shell not have taken over yet
    r.flow.close();
    await open(r, 'my-tokens');
    expect(page(r)).toContain('data-stop="signed-out"');
    // And returns to the same view after sign-in: the element is drawn again on the same route and reads again
    expect(app).toContain('<qse-access .view=${this.route.view} .path=${this.route.path} .params=${this.route.params}></qse-access>');
    r.as('ola');
    await open(r, 'my-tokens');
    expect(page(r)).toContain('data-panel=my-tokens');
    expect(page(r)).toContain('data-access=my-tokens');
  });

  it('A viewer when an admin allows it: they create an inference token and see only their own', async () => {
    // Given an admin switched api.tokens.viewers_may_create on
    const r = rig('sam');
    await r.api.setApi('tokens.viewers_may_create', 'on');
    // When a signed-in viewer opens My tokens
    r.as('vic');
    await open(r, 'my-tokens');
    // Then they can create an inference token
    expect(acts(page(r))).toContain('token-new');
    expect(page(r)).not.toContain('data-no-create');
    r.flow.show({ kind: 'token-new' });
    const d = dialog(r);
    expect([...d.matchAll(/name="scopes" value=([\w:]+)/g)].map((m) => m[1])).toEqual(['inference:openai', 'inference:anthropic']);
    expect(d).not.toContain('<option value="never">');
    await r.flow.createToken(form({ label: 'tablet', scopes: ['inference:openai', 'settings:write'] }));
    expect(tokenOf(r)).toMatch(/^tdm-/);
    // a scope the form does not offer never leaves the page
    expect((r.bodies.at(-1) as { scopes: string[] }).scopes).toEqual(['inference:openai']);
    r.flow.close();
    // and see only their own
    expect(r.flow.tokens!.map((t) => [t.label, t.owner])).toEqual([['tablet', 'vic']]);
    expect(page(r)).not.toContain('backup script');
    await open(r, 'tokens');
    expect(page(r)).toContain('data-stop="not-allowed"');
  });
});

describe('the viewer rule (decision 4)', () => {
  it('no create button for a viewer by default, one with the switch on; operators, admins and owners always', async () => {
    const has = async (name: 'vic' | 'ola' | 'sam' | 'kim', viewers: string): Promise<boolean> => {
      const r = rig(name);
      r.mock.settings['api.tokens.viewers_may_create'] = viewers;
      await open(r, 'my-tokens');
      return acts(page(r)).includes('token-new');
    };
    expect([await has('vic', 'off'), await has('vic', 'on')]).toEqual([false, true]);
    for (const name of ['ola', 'sam', 'kim'] as const) expect([await has(name, 'off'), await has(name, 'on')], name).toEqual([true, true]);
    // an admin's form offers every scope and a token that never ends; an operator's neither trust nor never
    const forms = async (name: 'ola' | 'sam') => {
      const r = rig(name);
      await open(r, 'my-tokens');
      r.flow.show({ kind: 'token-new' });
      const d = dialog(r);
      return [[...d.matchAll(/name="scopes" value=([\w:]+)/g)].length, d.includes('<option value="never">')];
    };
    expect([await forms('ola'), await forms('sam')]).toEqual([[8, false], [9, true]]);
  });
});

describe('every screen has a mock and a page', () => {
  it('each page opens for an owner on the mock, with its head, its strip and its panels', async () => {
    const panels: Record<Page, string[]> = { account: ['me', 'password', 'sessions'], 'my-tokens': ['my-tokens'], users: ['users'], tokens: ['review', 'tokens', 'services'], exposure: ['exposure-openai', 'exposure-anthropic', 'exposure-now'], sessions: ['all-sessions'], audit: ['audit'] };
    expect(PAGES.map((p) => p.id).sort()).toEqual(Object.keys(panels).sort());
    for (const def of PAGES) {
      const r = rig('kim');
      const route = parseHash(def.href);
      expect(pageOf(route.view, route.path), def.href).toBe(def.id);
      await open(r, def.id);
      const t = page(r);
      expect(t, def.id).toContain(`data-access=${def.id}>`);
      expect(t, def.id).toContain(`<h1>${def.title}</h1>`);
      expect([...t.matchAll(/data-panel=([\w-]+)>/g)].map((m) => m[1]), def.id).toEqual(panels[def.id]);
      expect(t, def.id).not.toMatch(/data-unread|data-failed|data-stop|undefined|\[object/);
      // the strip names the pages of its section, this one marked
      expect(t, def.id).toContain(`aria-current=page data-tab=${def.id}>`);
      expect(r.flow.error, def.id).toBeNull();
    }
  });

  it('every page says so when the controller runs without accounts, or does not answer', async () => {
    for (const def of PAGES) {
      const off = rig('off');
      await open(off, def.id);
      expect(page(off), def.id).toContain('data-stop="off"');
      expect(page(off), def.id).toContain('This TandemApp runs without accounts');
      const r = rig('kim');
      r.offline = true;
      await open(r, def.id);
      expect(page(r), def.id).toContain('data-unread');
      expect(page(r), def.id).toContain('TandemApp did not answer. Check the connection and try again.');
      // it comes back with a retry
      r.offline = false;
      await open(r, def.id);
      expect(page(r), def.id).not.toContain('data-unread');
    }
  });

  it('the menu follows the rights: nothing without accounts, Account for everyone, Users & access for an admin', async () => {
    const base = menuOf(null);
    expect(base).not.toContain('account');
    expect(base).not.toContain('access');
    for (const [name, want] of [['vic', ['account']], ['ola', ['account']], ['sam', ['account', 'access']], ['kim', ['account', 'access']]] as const) {
      const got = menuOf(await rightsOf(rig(name)));
      expect(got.filter((x) => !base.includes(x)), name).toEqual(want);
      expect(got.slice(0, base.length), name).toEqual(base);
    }
    setRights(await rightsOf(rig('sam')));
    expect(subPages(parseHash('#/access/tokens')).map((s) => [s.label, s.on])).toEqual([['Users', false], ['All tokens', true], ['API exposure', false], ['Sessions', false], ['Sign-ins', false]]);
    expect(subPages(parseHash('#/account/tokens')).find((s) => s.on)!.label).toBe('My tokens');
    setRights(null);
  });
});

describe('Account', () => {
  it('shows who is signed in and where, this browser marked; a new password signs the others out', async () => {
    const r = rig('kim');
    await open(r, 'account');
    const t = page(r);
    expect(panelText(t, 'me')).toContain('Signed in as kim');
    expect(panelText(t, 'me')).toContain('data-role=owner');
    expect(acts(panelText(t, 'me'))).toEqual(['sign-out']);
    const s = panelText(t, 'sessions');
    expect(s.match(/data-row=/g)).toHaveLength(3);
    expect(s.indexOf('this browser')).toBeLessThan(s.indexOf('Safari on iOS'));
    expect(s).toContain('Firefox on Linux');
    expect(s).toContain('192.0.2.44');
    expect(acts(s)).toEqual(['everywhere', 'end', 'end', 'end']);
    expect(panelText(t, 'password')).toMatch(/name=current type="password" autocomplete=current-password/);
    // two passwords that differ never leave the page
    const sent = r.paths.length;
    let csrf = '';
    expect(await r.flow.password('the password of kim', 'a new password here', 'another one entirely', (c) => (csrf = c))).toBe(false);
    expect(page(r)).toContain('data-failed>The two passwords differ.');
    expect(r.paths.length).toBe(sent);
    expect(await r.flow.password('not it', 'a new password here', 'a new password here', (c) => (csrf = c))).toBe(false);
    expect(page(r)).toContain('data-failed>The current password is not right.');
    expect(await r.flow.password('the password of kim', 'a new password here', 'a new password here', (c) => (csrf = c))).toBe(true);
    // the controller's new CSRF token is kept, and only this browser is left signed in
    expect(csrf).toMatch(/^csrf-/);
    expect(page(r)).toContain('data-note>The password is changed. Every other browser was signed out.');
    expect(panelText(page(r), 'sessions').match(/data-row=/g)).toHaveLength(1);
    // no password is kept anywhere in the page's state
    expect(JSON.stringify({ ...r.flow, api: null })).not.toContain('a new password here');
  });

  it('ends one session after a question, and signs out everywhere', async () => {
    const r = rig('kim');
    await open(r, 'account');
    const other = r.flow.sessions!.find((s) => !s.current)!;
    r.flow.endSession(other);
    expect(dialog(r)).toContain('End this session');
    expect(acts(dialog(r))).toEqual(['dlg-x', 'dlg-cancel', 'dlg-go']);
    await r.flow.go();
    expect(r.flow.dialog).toBeNull();
    expect(r.flow.sessions!.map((s) => s.id)).not.toContain(other.id);
    r.flow.signOutEverywhere();
    expect(dialog(r)).toContain(T.everywhere);
    expect(T.everywhere).toBe('Sign out everywhere');
    await r.flow.go();
    expect(r.paths).toContain('DELETE /v1/auth/sessions');
    expect(r.mock.sessions.filter((s) => s.account_id === r.who.kim)).toHaveLength(0);
    // this browser's session went too: the page is signed out, and the shell's sign-in takes over
    expect(r.flow.me!.authenticated).toBe(false);
    const area = readFileSync(join(SRC, 'views', 'access', 'area.ts'), 'utf8');
    expect(area).toMatch(/if \(f\.me && !f\.me\.authenticated && !this\.asked\) \{\s*this\.asked = true;\s*again\(\);/);
  });

  it('signs this browser out', async () => {
    const r = rig('ola');
    await open(r, 'account');
    await r.flow.signOut();
    expect(r.paths.at(-1)).toBe('DELETE /v1/auth/session');
    expect((await r.api.me())!.authenticated).toBe(false);
  });
});

describe('Users', () => {
  it('lists every account with its role and state, and surfaces the last owner', async () => {
    const r = rig('kim');
    await open(r, 'users');
    const t = page(r);
    expect(t.match(/data-row=/g)).toHaveLength(5);
    const kim = rowText(t, r.who.kim);
    expect(kim).toContain('data-last-owner>The last owner: it cannot be disabled, demoted or removed. Make another owner first.');
    expect(kim).toContain('Kim Reyes');
    expect(kim).toContain('>you<');
    // its role, Disable and Remove are held; its own password is not reset here
    expect(kim.match(/disabled=true/g)).toHaveLength(4);
    expect(rowText(t, r.who.old)).toContain('>disabled<');
    expect(acts(rowText(t, r.who.old))).toEqual(['enable', 'reset', 'remove']);
    expect(rowText(t, r.who.ola)).not.toContain('disabled=true');
    expect(rowText(t, r.who.ola)).toContain(`href="#/access/sessions?user=${r.who.ola}"`);
  });

  it('adds a user with a one-time code shown once', async () => {
    const r = rig('sam');
    await open(r, 'users');
    r.flow.show({ kind: 'user-new' });
    // an admin makes no owner
    expect([...dialog(r).matchAll(/<option value=(\w+)>/g)].map((m) => m[1])).toEqual(['viewer', 'operator', 'admin']);
    await r.flow.addUser('Bad Name!', 'viewer', '');
    expect(dialog(r)).toContain('data-dialog-error>A username is 1 to 64 characters');
    await r.flow.addUser('sam', 'viewer', '');
    expect(dialog(r)).toContain("data-dialog-error>The username 'sam' is taken.");
    await r.flow.addUser(' Eve ', 'operator', 'Eve A');
    expect(r.bodies.at(-1)).toEqual({ username: 'eve', role: 'operator', display_name: 'Eve A' });
    const d = r.flow.dialog!;
    expect(d.kind).toBe('user-code');
    const code = d.kind === 'user-code' ? d.data.code : '';
    expect(dialog(r)).toContain(`data-secret=code>${code}<`);
    expect(dialog(r)).toContain('This code is shown once. Copy it now.');
    expect(dialog(r)).toContain('eve signs in with it in place of a password, within 24 hours');
    r.flow.close();
    expect(rowText(page(r), r.flow.users!.find((u) => u.username === 'eve')!.id)).toContain('one-time code');
    expect(page(r) + JSON.stringify(r.flow.users)).not.toContain(code);
  });

  it('changes a role, disables and enables, resets and removes, each named in words', async () => {
    const r = rig('kim');
    await open(r, 'users');
    const user = (n: string) => r.flow.users!.find((u) => u.username === n)!;
    expect(await r.flow.setRole(user('vic'), 'operator')).toBe(true);
    expect(page(r)).toContain('data-note>vic is now operator.');
    r.flow.setEnabled(user('vic'), false);
    expect(dialog(r)).toContain('Their sessions end and their tokens are revoked for good.');
    await r.flow.go();
    expect(user('vic').enabled).toBe(false);
    r.flow.setEnabled(user('vic'), true);
    await Promise.resolve();
    await new Promise((x) => setTimeout(x, 0));
    expect(user('vic').enabled).toBe(true);
    r.flow.resetUser(user('ola'));
    expect(dialog(r)).toContain('The old password stops at once and their sessions end.');
    await r.flow.go();
    expect(r.flow.dialog?.kind).toBe('user-code');
    expect(dialog(r)).toContain('One-time code for ola');
    r.flow.close();
    r.flow.removeUser(user('old'));
    await r.flow.go();
    expect(r.flow.users!.map((u) => u.username)).toEqual(['kim', 'sam', 'ola', 'vic']);
  });

  it('an admin cannot change an owner, and a refusal names the right that is missing', async () => {
    const r = rig('sam');
    await open(r, 'users');
    const kim = rowText(page(r), r.who.kim);
    expect(kim).toContain('data-last-owner');
    // with a second owner the row says who may change it
    r.mock.add('zed', 'owner');
    await open(r, 'users');
    const zed = rowText(page(r), r.flow.users!.find((u) => u.username === 'zed')!.id);
    expect(zed).toContain('data-owner-only>Only an owner changes an owner.');
    expect(zed.match(/disabled=true/g)).toHaveLength(4);
    // a call the page would not offer is refused in words that name the right
    expect(await r.flow.setRole(r.flow.users!.find((u) => u.username === 'ola')!, 'owner')).toBe(false);
    expect(page(r)).toContain('data-failed>Not allowed: this needs the right “owner”. Your role is admin.');
  });
});

describe('All tokens', () => {
  it('The Review list lets an admin confirm or revoke each imported key', async () => {
    const r = rig('sam');
    await open(r, 'tokens');
    let review = panelText(page(r), 'review');
    expect(review).toContain('3 imported keys wait for a decision.');
    expect(review.match(/data-imported>This key was imported from an older setup\. Confirm it or revoke it\./g)).toHaveLength(3);
    expect(T.imported).toBe('This key was imported from an older setup. Confirm it or revoke it.');
    expect(acts(review)).toEqual(['confirm', 'revoke', 'confirm', 'revoke', 'confirm', 'revoke']);
    const [phone, laptop, admin] = r.flow.tokens!.filter((t) => t.needs_review);
    expect(await r.flow.confirmToken(phone)).toBe(true);
    expect(page(r)).toContain('data-note>phone is confirmed.');
    r.flow.revokeToken(laptop);
    expect(dialog(r)).toContain('The token stops at once and for good.');
    await r.flow.go();
    review = panelText(page(r), 'review');
    expect(review).toContain('1 imported key waits for a decision.');
    expect(acts(review)).toEqual(['confirm', 'revoke']);
    // the confirmed key is a token like the others now, the revoked one is listed as revoked
    const people = panelText(page(r), 'tokens');
    expect(rowText(people, phone.id)).toContain('>imported<');
    expect(rowText(people, laptop.id)).toContain('>revoked<');
    await r.flow.confirmToken(admin);
    expect(page(r)).not.toContain('data-panel=review');
  });

  it('lists people’s tokens with a filter by owner, and the service identities read-only', async () => {
    const r = rig('kim');
    await open(r, 'tokens');
    const people = panelText(page(r), 'tokens');
    expect([...people.matchAll(/<option value=(\w*)/g)].length).toBe(3);
    expect(people).toContain('>kim</option>');
    // one's own tokens rotate; another's are only revoked
    const mine = r.flow.tokens!.find((t) => t.label === 'backup script')!;
    const ci = r.flow.tokens!.find((t) => t.label === 'ci')!;
    expect(acts(rowText(people, mine.id))).toEqual(['rotate', 'revoke']);
    expect(acts(rowText(people, ci.id))).toEqual(['revoke']);
    expect(rowText(people, mine.id)).toContain('>ends soon<');
    expect(rowText(people, mine.id)).toContain('limits requests a minute 60');
    expect(rowText(people, ci.id)).toContain('small-*');
    const services = panelText(page(r), 'services');
    expect(services).toContain('svc:chatd');
    expect(acts(services)).toEqual([]);
    await open(r, 'tokens', r.who.ola);
    expect([...panelText(page(r), 'tokens').matchAll(/data-row=(\w+)/g)].map((m) => m[1])).toEqual([ci.id]);
  });

  it('rotates a token of one’s own: the new one is shown once, the same way', async () => {
    const r = rig('kim');
    await open(r, 'my-tokens');
    const t = r.flow.tokens!.find((x) => x.label === 'laptop')!;
    r.flow.show({ kind: 'token-rotate', token: t });
    expect(dialog(r)).toContain('Rotate laptop');
    await r.flow.rotateToken(t, 3600);
    expect(r.bodies.at(-1)).toEqual({ grace_s: 3600 });
    expect(dialog(r)).toContain('laptop: the new token');
    expect(dialog(r)).toContain(T.once);
    const token = tokenOf(r);
    r.flow.close();
    expect(page(r)).not.toContain(token);
    expect(rowText(page(r), t.id)).toContain(`...${token.slice(-4)}`);
  });
});

describe('API exposure', () => {
  it('one switch per protocol, what holds now, and the findings inline', async () => {
    const r = rig('kim');
    await open(r, 'exposure');
    const t = page(r);
    const openai = panelText(t, 'exposure-openai');
    expect(openai).toContain('OpenAI-compatible API');
    expect(openai).toMatch(/aria-pressed=false data-value=this-machine \?disabled=false[^>]*>This machine</);
    expect(openai).toMatch(/aria-pressed=true data-value=network \?disabled=true[^>]*>Network</);
    expect(openai).toContain('data-now=network>Any device that reaches TandemApp, with a token that holds this API.');
    // the live tokens that hold it: two people's and the two imported keys
    expect(openai).toContain('data-tokens=4>4 tokens hold this API.');
    // the health page's finding about this protocol stands in its card
    expect(openai).toContain('data-finding=exposure_unreachable>');
    expect(openai).toContain('but they cannot reach it: listening on this machine only.');
    const messages = panelText(t, 'exposure-anthropic');
    expect(messages).toContain('Messages API');
    expect(messages).toContain('data-now=this-machine>Only callers on this machine, with a token.');
    expect(messages).toContain('Other devices cannot use this API.');
    expect(messages).toContain('data-tokens=1>1 token holds this API.');
    expect(messages).not.toContain('data-finding=');
    const now = panelText(t, 'exposure-now');
    expect(now).toContain('TandemApp listens on <b class="num">127.0.0.1</b>. Other devices cannot reach it, whatever the switches say.');
    expect([...now.matchAll(/data-finding=(\w+)/g)].map((m) => m[1])).toEqual(['tokens_need_review', 'legacy_admin_token']);
  });

  it('a switch asks first, then plans and applies through the settings interface', async () => {
    const r = rig('kim');
    await open(r, 'exposure');
    r.flow.expose('anthropic', 'Messages API', 'network');
    expect(dialog(r)).toContain('Messages API: let other devices in');
    expect(dialog(r)).toContain('No device gets in without a token.');
    const sent = r.paths.length;
    await r.flow.go();
    expect(r.paths.slice(sent, sent + 2)).toEqual(['POST /v1/settings/plan', 'POST /v1/settings/apply']);
    expect(r.flow.exposure!.values.anthropic).toBe('network');
    expect(panelText(page(r), 'exposure-anthropic')).toMatch(/aria-pressed=true data-value=network/);
    r.flow.expose('openai', 'OpenAI-compatible API', 'this-machine');
    expect(dialog(r)).toContain('Clients on other machines stop working.');
    await r.flow.go();
    expect(r.flow.exposure!.values.openai).toBe('this-machine');
  });

  it('an admin reads it and changes nothing; local mode asks for the password first', async () => {
    const r = rig('sam');
    await open(r, 'exposure');
    expect(page(r)).toContain('data-owner-only>Only an owner changes these switches.');
    expect(page(r)).not.toMatch(/data-value=[\w-]+ \?disabled=false/);
    r.flow.expose('anthropic', 'Messages API', 'network');
    await r.flow.go();
    expect(dialog(r)).toContain('data-dialog-error>Not allowed: only an owner changes this.');
    const l = rig('local');
    await open(l, 'exposure');
    const t = page(l);
    expect(t).toContain('data-password-first');
    expect(t).toContain('Before another device is let in, the owner gets a password.');
    expect(t).toContain('href="#/account">Set a password</a>');
    expect(t).not.toMatch(/data-value=[\w-]+ \?disabled=false/);
    // the controller holds the rule too
    l.flow.expose('openai', 'OpenAI-compatible API', 'network');
    await l.flow.go();
    expect(dialog(l)).toContain('data-dialog-error>Set the owner’s password first.');
    // the other pages of local mode say the same, and ask the controller for nothing they cannot have
    for (const p of ['users', 'tokens', 'sessions', 'audit', 'my-tokens'] as const) {
      await open(l, p);
      expect(page(l), p).toContain('data-stop="password-first"');
    }
  });
});

describe('Sessions and the audit', () => {
  it('an admin sees every session and ends one, or all of an account', async () => {
    const r = rig('sam');
    await open(r, 'sessions');
    const t = page(r);
    expect(t.match(/data-row=/g)!.length).toBe(r.mock.sessions.length);
    expect(t).toContain('<b>kim</b>');
    expect(t).toContain('this browser');
    const olas = r.flow.sessions!.find((s) => s.username === 'ola')!;
    r.flow.endSession(olas);
    expect(dialog(r)).toContain('ola is signed out on that browser at once.');
    await r.flow.go();
    expect(r.mock.sessions.some((s) => s.id === olas.id)).toBe(false);
    await open(r, 'sessions', r.who.kim);
    expect(page(r)).toContain('data-act="end-all"');
    expect(page(r)).toContain('End every session of kim');
    expect(page(r).match(/data-row=/g)).toHaveLength(2);
  });

  it('the audit reads as lines, the failed sign-ins marked, with a filter for the sign-ins', async () => {
    const r = rig('kim');
    await open(r, 'audit');
    const t = page(r);
    expect(t).toContain('vic failed to sign in');
    expect(t).toContain('from 198.51.100.7');
    expect(t).toMatch(/<b class=err-ink>vic failed to sign in<\/b>/);
    expect(t).toContain('2026-11-16 08:30 UTC');
    r.flow.only('sign-ins');
    expect([...page(r).matchAll(/data-audit=(\w+)/g)].every((m) => m[1].startsWith('sign_'))).toBe(true);
    expect(page(r)).toContain('aria-pressed=true data-only=sign-ins');
  });
});

describe('a page shows its own lists only', () => {
  it('My tokens never shows under All tokens’ head, and an answer that comes late for another page is dropped', async () => {
    const r = rig('kim');
    await open(r, 'my-tokens');
    expect(r.flow.tokens!.every((t) => t.owner === 'kim')).toBe(true);
    // the next page starts empty: its head never stands over the list of the page before
    const going = open(r, 'tokens');
    expect(r.flow.tokens).toBeNull();
    expect(page(r)).not.toContain('data-row=');
    await going;
    expect(panelText(page(r), 'review')).toContain('3 imported keys');
    // two pages asked one after the other: the slower answer of the first is not kept
    const first = open(r, 'my-tokens');
    const second = open(r, 'sessions');
    await Promise.all([first, second]);
    expect(r.flow.page).toBe('sessions');
    expect(r.flow.tokens).toBeNull();
    expect(r.flow.sessions!.length).toBe(r.mock.sessions.length);
  });
});

describe('Settings: the Sign-in and the API page', () => {
  /** a scope's page as the settings area draws it: the schema's keys as fields, in their groups */
  async function scopePage(r: AccessRig, scope: 'auth' | 'api'): Promise<{ text: string; fields: Field[] }> {
    const schema = (await r.send('GET', `/v1/settings/${scope}/schema`)).body as SchemaView;
    const values = (await r.send('GET', `/v1/settings/${scope}`)).body as ValuesView;
    expect((await r.send('GET', `/v1/settings/${scope}/versions`)).status).toBe(200);
    const fields = fieldsOf(schema, values, undefined);
    const view: FormView = { raw: new Map(), invalid: new Map(), secrets: new Set(), open: new Set(), backends: [] };
    const noop = (): void => undefined;
    const on: FormHandlers = { edit: noop, blur: noop, set: noop, unset: noop, undo: noop, secret: noop, toggle: noop } as unknown as FormHandlers;
    return { text: groupsOf(scope, fields).map((g) => text(groupPanel(g, view, on))).join('\n'), fields };
  }

  it('Sign-in draws every key of the auth scope by name, with the session times and their limits', async () => {
    const r = rig('kim');
    const { text: t, fields } = await scopePage(r, 'auth');
    expect(fields.map((f) => f.key).sort()).toEqual(['chat_min_role', 'local_login', 'local_mode', 'password.min_length', 'provider', 'session.absolute_days', 'session.idle_days']);
    for (const label of ['Idle days before a session ends', 'Longest session, in days', 'Shortest password', 'Local mode', 'Who may chat', 'The password form', 'Who checks a sign-in']) expect(t, label).toContain(label);
    expect(fields.find((f) => f.key === 'session.idle_days')!.value).toBe(30);
    expect(fields.find((f) => f.key === 'session.absolute_days')!.value).toBe(180);
    // the form that nothing reads yet says so
    expect(t).toContain('It takes effect once another way to sign in exists');
    // a change of the session time goes through the settings interface and is kept
    const changes = [{ scope: 'auth', set: { 'session.idle_days': 14 } }];
    const plan = (await r.send('POST', '/v1/settings/plan', { changes })).body as { confirm: string };
    expect((await r.send('POST', '/v1/settings/apply', { changes, confirm: plan.confirm })).status).toBe(200);
    expect((await scopePage(r, 'auth')).fields.find((f) => f.key === 'session.idle_days')!.value).toBe(14);
    expect((await r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'auth', set: { 'session.idle_days': 401 } }] })).status).toBe(422);
  });

  it('API draws the exposure switches, the listen address and the tokens’ rules, the viewers’ switch among them', async () => {
    const r = rig('kim');
    const { text: t, fields } = await scopePage(r, 'api');
    expect(fields.map((f) => f.key).sort()).toEqual(['anthropic.exposure', 'listen', 'openai.exposure', 'tokens.default_days', 'tokens.max_days', 'tokens.max_days_external', 'tokens.viewers_may_create']);
    expect([...t.matchAll(/data-group=([\w:]+)/g)].map((m) => m[1])).toEqual(['api:exposure', 'api:tokens']);
    for (const label of ['Who may use the APIs', 'API tokens', 'OpenAI-compatible API', 'Messages API', 'Listen address', 'Viewers create inference tokens', 'A new token’s days']) expect(t, label).toContain(label);
    expect(fields.find((f) => f.key === 'tokens.viewers_may_create')!.value).toBe('off');
    expect(fields.find((f) => f.key === 'listen')!.spec.restart).toBe('app');
    // a pod's listen address is not changed from the page
    r.mock.kubernetes = true;
    expect((await scopePage(r, 'api')).fields.find((f) => f.key === 'listen')!.spec.writable).toBe(false);
  });
});

describe('no token or password is ever in the URL, the console or local storage', () => {
  it('the pages’ sources write to none of them, and no path ever carried one', async () => {
    const files = ['api/auth.ts', 'lib/access.ts', 'lib/access-flow.ts', 'views/access/area.ts', 'views/access/view.ts'];
    for (const f of files) {
      const src = readFileSync(join(SRC, f), 'utf8');
      expect(src, f).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie|console\.|history\.(push|replace)State|location\.(hash|href|search) =/);
    }
    // the route's params are the owner or the user filter only (ids), set through the router
    expect(readFileSync(join(SRC, 'views', 'access', 'area.ts'), 'utf8').match(/setParams\(/g)).toHaveLength(1);
    const r = rig('kim');
    await open(r, 'my-tokens');
    r.flow.show({ kind: 'token-new' });
    await r.flow.createToken(form());
    const token = tokenOf(r);
    r.flow.close();
    await open(r, 'users');
    r.flow.show({ kind: 'user-new' });
    await r.flow.addUser('eve', 'viewer', '');
    const code = r.flow.dialog?.kind === 'user-code' ? r.flow.dialog.data.code : '';
    r.flow.close();
    await open(r, 'account');
    await r.flow.password('the password of kim', 'a new password here', 'a new password here', () => undefined);
    expect(token && code).toBeTruthy();
    for (const secret of [token, code, 'a new password here', 'the password of kim']) expect(r.paths.join('\n')).not.toContain(secret);
    // a password travels in a PUT's body only, a token and a code in no request at all
    expect(r.bodies.filter((b) => JSON.stringify(b).includes('a new password here'))).toEqual([{ current: 'the password of kim', password: 'a new password here' }]);
    expect(JSON.stringify(r.bodies)).not.toContain(token);
    expect(JSON.stringify(r.bodies)).not.toContain(code);
  });
});

describe('every screen is usable at 360 px width', () => {
  const css = readFileSync(join(SRC, 'styles', 'access.css'), 'utf8');
  it('lists are rows that stack in a narrow column, never a table that scrolls sideways', () => {
    const view = readFileSync(join(SRC, 'views', 'access', 'view.ts'), 'utf8');
    expect(view).not.toMatch(/<table|table-scroll/);
    expect(css).toMatch(/container: ac \/ inline-size;/);
    expect(css).toMatch(/@container ac \(max-width: 620px\) \{\s*\.ac-row \{\s*flex-direction: column;/);
    // nothing is wider than the column: long ids and tokens wrap, the strip of pages scrolls by itself
    expect(css).toMatch(/\.ac-secret code \{[^}]*overflow-wrap: anywhere;/);
    expect(css).toMatch(/\.ac-tabs \{[^}]*overflow-x: auto;/);
    expect(css).not.toMatch(/(min-|[^-])width: (3[6-9]\d|[4-9]\d\d|\d{4,})px/);
    for (const m of css.matchAll(/minmax\((\d+)px/g)) expect(Number(m[1])).toBeLessThanOrEqual(160);
  });
  it('fields are 16 px (no zoom on a phone), targets at least 36 px, the focus ring shows, and nothing moves', () => {
    expect(css).toMatch(/\.ac-field input,\s*\.ac-field select \{[^}]*min-height: 38px;[^}]*font-size: 16px;/);
    expect(css).toMatch(/\.ac-acts \.btn,\s*\.ac-acts select \{\s*min-height: 36px;/);
    expect(css).toMatch(/:focus-visible[^{]*\{[^}]*box-shadow: var\(--focus\);/);
    // no animation or transition of its own: reduced motion has nothing to switch off here
    expect(css).not.toMatch(/animation|transition|@keyframes/);
    // light and dark: only the page's tokens, never a colour of its own
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
  });
  it('the dialog is the page’s own, opened and closed with the keyboard', () => {
    const area = readFileSync(join(SRC, 'views', 'access', 'area.ts'), 'utf8');
    expect(area).toContain('<dialog class="act-dialog" aria-labelledby="ac-dlg-t" data-dialog="access"');
    expect(area).toContain('d.showModal();');
    expect(area).toMatch(/@cancel=\$\{\(e: Event\) => f\.busy === 'dialog' && e\.preventDefault\(\)\}/);
    // every form is sent with Enter: its button is a submit
    const view = readFileSync(join(SRC, 'views', 'access', 'view.ts'), 'utf8');
    expect(view.match(/foot\('[^']+', \{ submit: true \}\)/g)).toHaveLength(3);
  });
});

describe('the build', () => {
  const dist = join(__dirname, '..', 'dist');
  let files: string[] = [];
  try {
    files = readdirSync(join(dist, 'assets'));
  } catch {
    /* not built here */
  }
  it.skipIf(!files.length)('the pages are a chunk of their own, not part of the entry', () => {
    const src = (f: string) => readFileSync(join(dist, 'assets', f), 'utf8');
    const js = files.filter((f) => f.endsWith('.js'));
    const pages = js.filter((f) => src(f).includes(T.once));
    expect(pages).toHaveLength(1);
    expect(pages[0].startsWith('app-')).toBe(false);
    expect(files.filter((f) => f.endsWith('.css') && src(f).includes('.ac-secret'))).toHaveLength(1);
  });
  it('the shell loads them on demand', () => {
    const app = readFileSync(join(SRC, 'app.ts'), 'utf8');
    expect(app).toContain("import('./views/access/area')");
    expect(readFileSync(join(SRC, 'main.ts'), 'utf8')).not.toContain('access');
  });
});
