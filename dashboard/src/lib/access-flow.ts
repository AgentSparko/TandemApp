// The account and access pages' state, apart from the element so tests run it without a DOM
// (SRV-875): who is signed in, what the page that shows has read, the dialog that is open and the
// call under way. A token or a one-time code lives here only while its dialog shows it: closing
// the dialog drops it, and nothing here writes to the URL, the console or the browser's storage.

import { AuthError, type AuditRow, type AuthApi, type ExposureView, type Exposure, type Finding, type Me, type Protocol, type Role, type Session, type Token, type TokenOnce, type TokenUsage, type User, type UserCode } from '../api/auth';
import { can, errorWords, pageDef, tokenBody, userNameError, type Page, type TokenForm } from './access';

export type Dialog =
  | { kind: 'token-new' }
  | { kind: 'token-once'; token: TokenOnce; rotated: boolean }
  | { kind: 'token-rotate'; token: Token }
  | { kind: 'user-new' }
  | { kind: 'user-code'; data: UserCode; reset: boolean }
  | { kind: 'confirm'; title: string; text: string; go: string; run: () => Promise<unknown>; next?: (v: unknown) => Dialog };

/** why a page shows no data: accounts off, signed out, the right is missing, or no password yet */
export type Stop = 'off' | 'signed-out' | 'not-allowed' | 'password-first' | null;

export class AccessFlow {
  me: Me | null | undefined = undefined;
  page: Page = 'account';
  loading = false;
  /** the page's read failed */
  error: string | null = null;
  /** what the last action left, in words */
  note: string | null = null;
  /** an action outside a dialog failed */
  failed: string | null = null;
  busy: string | null = null;
  dialog: Dialog | null = null;
  dialogError: string | null = null;
  users: User[] | null = null;
  sessions: Session[] | null = null;
  tokens: Token[] | null = null;
  /** SRV-873: My tokens: each live token's use of the last 30 days and its limits' state */
  usage: Record<string, TokenUsage> = {};
  audit: AuditRow[] | null = null;
  exposure: ExposureView | null = null;
  findings: Finding[] = [];
  /** All tokens and Sessions: one account's alone */
  filter: string | null = null;
  auditOnly: 'all' | 'sign-ins' = 'all';

  constructor(
    private api: AuthApi,
    private changed: () => void = () => undefined,
    readonly now: () => number = () => Date.now(),
  ) {}

  get stop(): Stop {
    const me = this.me;
    if (me === undefined) return null;
    if (me === null) return 'off';
    if (!me.authenticated) return 'signed-out';
    if (me.local_mode) return this.page === 'exposure' ? null : 'password-first';
    return can(me, pageDef(this.page).right) ? null : 'not-allowed';
  }

  /** The page opened (or the person signed in again): who am I, then what the page shows. */
  async open(page: Page, filter: string | null = null): Promise<void> {
    const moved = page !== this.page;
    this.page = page;
    this.filter = filter;
    if (moved) {
      this.note = this.failed = this.error = null;
      // nothing of the page before shows under this one's head
      this.users = this.sessions = this.tokens = this.audit = this.exposure = null;
      this.findings = [];
      this.usage = {};
    }
    this.loading = true;
    this.changed();
    try {
      this.me = await this.api.me();
      if (!this.stop) await this.read();
      this.error = null;
    } catch (e) {
      this.error = this.words(e);
    }
    this.loading = false;
    this.changed();
  }

  /** Reads what the page shows. An answer that comes back after the person went on to another
   *  page is dropped: `tokens` and `sessions` mean one's own on one page and everyone's on another. */
  private async read(): Promise<void> {
    const a = this.api;
    const page = this.page;
    const here = (): boolean => page === this.page;
    if (page === 'account' || page === 'sessions') {
      const got = await a.sessions(page === 'sessions' ? '*' : undefined);
      if (here()) this.sessions = got;
    } else if (page === 'my-tokens' || page === 'tokens') {
      const got = await a.tokens(page === 'tokens');
      if (here()) this.tokens = got;
      if (page === 'my-tokens') {
        // a usage read that fails leaves its token without the line, never the page without its list
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const live = got.filter((t) => t.state === 'active');
        const use = await Promise.all(live.map((t) => a.tokenUsage(t.id, tz).catch(() => null)));
        if (here()) this.usage = Object.fromEntries(live.flatMap((t, i) => (use[i] ? [[t.id, use[i]!]] : [])));
      }
    } else if (page === 'users') {
      const got = await a.users();
      if (here()) this.users = got;
    } else if (page === 'audit') {
      const got = await a.audit();
      if (here()) this.audit = got;
    } else {
      const [x, findings, tokens] = [await a.exposure(), await a.findings().catch(() => []), this.me?.local_mode ? [] : await a.tokens(true).catch(() => [])];
      if (here()) [this.exposure, this.findings, this.tokens] = [x, findings, tokens];
    }
  }

  private words(e: unknown): string {
    if (e instanceof AuthError) {
      if (e.status === 401 && this.me) this.me = { ...this.me, authenticated: false };
      return errorWords(e, this.me);
    }
    return (e as Error)?.message ?? String(e);
  }

  /** One call outside a dialog: the row's button holds while it runs, then the page reads again. */
  private async act(key: string, run: () => Promise<unknown>, note: string): Promise<boolean> {
    if (this.busy) return false;
    this.busy = key;
    this.failed = this.note = null;
    this.changed();
    let ok = true;
    try {
      await run();
      this.note = note;
      await this.read();
    } catch (e) {
      ok = false;
      this.failed = this.words(e);
    }
    this.busy = null;
    this.changed();
    return ok;
  }

  // ---- dialogs --------------------------------------------------------------------------------------
  show(d: Dialog): void {
    this.dialog = d;
    this.dialogError = null;
    this.changed();
  }
  only(v: 'all' | 'sign-ins'): void {
    this.auditOnly = v;
    this.changed();
  }
  /** closes the dialog and forgets what it showed (a token, a one-time code) */
  close(): void {
    if (this.busy === 'dialog') return;
    this.dialog = null;
    this.dialogError = null;
    this.changed();
  }
  /** One call from a dialog: its error stays in the dialog; `then` gets the answer. */
  private async inDialog<T>(run: () => Promise<T>, then: (v: T) => void): Promise<void> {
    if (this.busy) return;
    this.busy = 'dialog';
    this.dialogError = null;
    this.changed();
    try {
      const v = await run();
      this.busy = null;
      then(v);
      // a session of one's own that ended shows here: the next read is a 401
      await this.read().catch((e) => void this.words(e));
    } catch (e) {
      this.dialogError = this.words(e);
    }
    this.busy = null;
    this.changed();
  }
  /** the confirm dialog's button */
  go(): Promise<void> {
    const d = this.dialog;
    if (d?.kind !== 'confirm') return Promise.resolve();
    return this.inDialog(d.run, (v) => {
      this.dialog = d.next?.(v) ?? null;
      this.note = `${d.title}: done.`;
    });
  }

  // ---- my account -----------------------------------------------------------------------------------
  /** A new password. The controller ends every session and answers this browser's new one; `keep`
   *  takes its CSRF token. */
  async password(current: string, password: string, again: string, keep: (csrf: string) => void): Promise<boolean> {
    if (password !== again) {
      this.failed = 'The two passwords differ.';
      this.note = null;
      this.changed();
      return false;
    }
    return this.act('password', async () => keep((await this.api.password(current, password)).csrf), 'The password is changed. Every other browser was signed out.');
  }
  endSession(s: Session): void {
    this.show({ kind: 'confirm', title: s.current ? 'Sign out' : 'End this session', text: s.current ? 'This browser is signed out.' : `${s.username} is signed out on that browser at once.`, go: s.current ? 'Sign out' : 'End the session', run: () => this.api.endSession(s.id) });
  }
  signOutEverywhere(user: User | null = null): void {
    this.show({ kind: 'confirm', title: user ? `End every session of ${user.username}` : 'Sign out everywhere', text: user ? `${user.username} is signed out on every browser at once.` : 'Every browser you are signed in on is signed out, this one too.', go: user ? 'End the sessions' : 'Sign out everywhere', run: () => this.api.endSessions(user?.id) });
  }
  /** this browser alone; the element then hands over to the shell's sign-in */
  async signOut(): Promise<void> {
    await this.api.signOut().catch(() => undefined);
    if (this.me) this.me = { ...this.me, authenticated: false };
    this.changed();
  }

  // ---- tokens ---------------------------------------------------------------------------------------
  createToken(f: TokenForm): Promise<void> {
    const body = tokenBody(f, this.me);
    if (typeof body === 'string') {
      this.dialogError = body;
      this.changed();
      return Promise.resolve();
    }
    return this.inDialog(
      () => this.api.createToken(body),
      (token) => (this.dialog = { kind: 'token-once', token, rotated: false }),
    );
  }
  rotateToken(t: Token, grace_s: number): Promise<void> {
    return this.inDialog(
      () => this.api.rotateToken(t.id, grace_s),
      (token) => (this.dialog = { kind: 'token-once', token, rotated: true }),
    );
  }
  revokeToken(t: Token): void {
    this.show({ kind: 'confirm', title: `Revoke ${t.label}`, text: 'The token stops at once and for good. A client that uses it gets a 401.', go: 'Revoke', run: () => this.api.revokeToken(t.id) });
  }
  confirmToken(t: Token): Promise<boolean> {
    return this.act(`confirm:${t.id}`, () => this.api.confirmToken(t.id), `${t.label} is confirmed.`);
  }

  // ---- users ----------------------------------------------------------------------------------------
  addUser(username: string, role: Role, display: string): Promise<void> {
    const bad = userNameError(username);
    if (bad) {
      this.dialogError = bad;
      this.changed();
      return Promise.resolve();
    }
    return this.inDialog(
      () => this.api.addUser({ username: username.trim().toLowerCase(), role, ...(display.trim() ? { display_name: display.trim() } : {}) }),
      (data) => (this.dialog = { kind: 'user-code', data, reset: false }),
    );
  }
  setRole(u: User, role: Role): Promise<boolean> {
    return this.act(`user:${u.id}`, () => this.api.patchUser(u.id, { role }), `${u.username} is now ${role}.`);
  }
  setEnabled(u: User, enabled: boolean): void {
    if (enabled) return void this.act(`user:${u.id}`, () => this.api.patchUser(u.id, { enabled }), `${u.username} is enabled.`);
    this.show({ kind: 'confirm', title: `Disable ${u.username}`, text: 'Their sessions end and their tokens are revoked for good. The account stays, and can be enabled again.', go: 'Disable', run: () => this.api.patchUser(u.id, { enabled }) });
  }
  resetUser(u: User): void {
    this.show({
      kind: 'confirm', title: `Reset the password of ${u.username}`, text: 'The old password stops at once and their sessions end. You get a one-time code to hand over.', go: 'Reset',
      run: () => this.api.resetUser(u.id), next: (data) => ({ kind: 'user-code', data: data as UserCode, reset: true }),
    });
  }
  removeUser(u: User): void {
    this.show({ kind: 'confirm', title: `Remove ${u.username}`, text: 'The account, its sessions and its tokens go for good. Past usage keeps the name.', go: 'Remove', run: () => this.api.removeUser(u.id) });
  }

  // ---- exposure -------------------------------------------------------------------------------------
  expose(p: Protocol, label: string, to: Exposure): void {
    const open = to === 'network';
    this.show({
      kind: 'confirm', title: `${label}: ${open ? 'let other devices in' : 'this machine only'}`,
      text: open ? 'Any device that reaches TandemApp can then use this API with a token that holds it. No device gets in without a token.' : 'Other devices are refused from now on, with a token or without. Clients on other machines stop working.',
      go: open ? 'Switch to Network' : 'Switch to This machine', run: () => this.api.setApi(`${p}.exposure`, to),
    });
  }
}
