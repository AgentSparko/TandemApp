// SEC-26: with the controller's accounts on, every write a signed-in page sends to its own origin
// carries the session's X-Tandem-CSRF header (GET /v1/auth/session hands it out). One wrapper
// around fetch covers every caller; with no token (the accounts off, local mode, signed out) it
// adds nothing.

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

if (typeof window !== 'undefined' && typeof window.fetch === 'function') {
  const send = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => send(input, withCsrf(input, init, token, location.origin));
}
