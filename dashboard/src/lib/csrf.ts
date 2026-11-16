// SEC-26: with the controller's accounts on, every write a signed-in page sends to its own origin
// carries the session's X-Tandem-CSRF header (GET /v1/auth/session hands it out). One wrapper
// around fetch covers every caller; with no token (the accounts off, local mode, signed out) it
// adds nothing. SRV-875: the same wrapper sees a 401 on a signed-in page's own origin, on any
// call of any view: the session ended, so the shell is told (`qse-unauthorized`), asks who is
// signed in and shows the sign-in with the route kept.

let token: string | null = null;

export function setCsrf(t: string | null | undefined): void {
  token = t || null;
}

export function csrf(): string | null {
  return token;
}

/** `init` with the header added when `input` is a write to this page's own origin. */
export function withCsrf(input: RequestInfo | URL, init: RequestInit | undefined, t: string | null, origin: string): RequestInit | undefined {
  if (!t) return init;
  const req = typeof input === 'object' && 'method' in input ? (input as Request) : null;
  const method = (init?.method ?? req?.method ?? 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return init;
  let url: URL;
  try {
    url = new URL(req ? req.url : String(input), origin + '/');
  } catch {
    return init;
  }
  if (url.origin !== origin) return init;
  const headers = new Headers(init?.headers ?? req?.headers);
  if (!headers.has('X-Tandem-CSRF')) headers.set('X-Tandem-CSRF', t);
  return { ...init, headers };
}

/** A signed-in page got a 401 from its own origin (the sign-in's own routes answer a wrong
 *  password with one: those are the form's). */
export function signedOutBy(status: number, url: string, t: string | null, origin: string): boolean {
  if (status !== 401 || !t) return false;
  try {
    const u = new URL(url, origin + '/');
    return u.origin === origin && !/^\/v1\/auth\/(session|setup)$/.test(u.pathname);
  } catch {
    return false;
  }
}

if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
  const send = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const res = await send(input, withCsrf(input, init, token, location.origin));
    if (signedOutBy(res.status, res.url, token, location.origin)) window.dispatchEvent(new CustomEvent('qse-unauthorized'));
    return res;
  };
}
