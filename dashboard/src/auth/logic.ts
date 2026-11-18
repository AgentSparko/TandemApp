// The sign-in's logic (SEC-26), apart from the element so tests run it without a DOM: what the
// page shows first, a form's answer in words, the session read and the plain fetch the forms send.

export type AuthNeed = 'signin' | 'setup' | 'password' | 'account';

export interface AuthSession {
  authenticated: boolean;
  account: { username: string; role: string; must_change: boolean } | null;
  principal: { kind: string; name: string } | null;
  /** SRV-875: the rights of who is signed in: the menu shows what they open */
  permissions?: string[];
  csrf: string | null;
  setup_required: boolean;
  local_mode: boolean;
  /** SRV-890: the sections this account's role is shown; null or absent: every one */
  views?: string[] | null;
}

export const T = {
  signin: 'Sign in to TandemApp',
  setup: 'This TandemApp has no owner yet. Enter the setup code to create one.',
  where: 'The setup code is in the installer’s last lines and in the service log. Run tandem-app users setup-code for a new one.',
  password: 'Set a password',
  once: 'You signed in with a one-time code. Choose your own password to go on.',
  local: 'No password is set. That is fine while only this machine can reach TandemApp. Setting one ends that: from then on every browser signs in.',
  wrong: 'The username or the password is not right.',
  wrongCode: 'The setup code is not right.',
  current: 'The current password or code is not right.',
  spent: 'This setup code is used up. Run tandem-app users setup-code on the machine for a new one.',
  differ: 'The two passwords differ.',
  wait: 'Too many tries. Wait a minute and try again.',
  offline: 'TandemApp did not answer. Check the connection and try again.',
};

/** what the page shows first: null for the page itself (accounts off, local mode, signed in) */
export function needOf(s: AuthSession | null): AuthNeed | null {
  if (!s || s.local_mode) return null;
  if (!s.authenticated) return s.setup_required ? 'setup' : 'signin';
  return s.account?.must_change ? 'password' : null;
}

export async function readSession(): Promise<AuthSession | null> {
  try {
    const r = await fetch('/v1/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    return r.ok ? ((await r.json()) as AuthSession) : null;
  } catch {
    return null;
  }
}

/** a form's answer in words: null when it went through */
export function errorOf(status: number, body: { error?: { code?: string; message?: string; tries_left?: number } } | null): string | null {
  if (status >= 200 && status < 300) return null;
  const e = body?.error;
  if (status === 429) return T.wait;
  if (e?.code === 'wrong_setup_code') return `${T.wrongCode} ${e.tries_left ?? 0} ${e.tries_left === 1 ? 'try' : 'tries'} left.`;
  if (e?.code === 'setup_code_spent') return T.spent;
  if (e?.code === 'wrong_credentials') return T.wrong;
  if (e?.code === 'wrong_password') return T.current;
  if (status === 0) return T.offline;
  return e?.message ? e.message.charAt(0).toUpperCase() + e.message.slice(1) + '.' : `TandemApp answered ${status}.`;
}

export async function send(method: string, path: string, body?: unknown): Promise<[number, { csrf?: string; error?: { code?: string; message?: string; tries_left?: number } } | null]> {
  try {
    const r = await fetch(path, { method, credentials: 'same-origin', cache: 'no-store', headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
    const text = await r.text();
    return [r.status, text ? JSON.parse(text) : null];
  } catch {
    return [0, null];
  }
}
