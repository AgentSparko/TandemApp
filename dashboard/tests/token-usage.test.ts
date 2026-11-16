// Tests for SRV-873: limits and usage per API token and user, on the page, against the mock.
//
//   Gherkin (SRV-873):
//   A rate limit             the page's side: My tokens marks the reached limit and lists the 429
//   A spend limit            the page's side: the dollars used of the limit, reached, and the 403
//                            (the 429 and the 403 themselves: tests/test_token_limits.py)
//   A model without a price  "the token's page says the spend limit does not apply to that model"
//   Usage adds up            "the usage view grouped by token, then by user: each sum equals the
//                             ledger's rows" (the mock's rows; tests/test_token_limits.py holds
//                             the controller's)
//   and the page around them: My tokens shows each token's use and its limits' state, a reached
//   limit and the last refusals; a usage read that fails leaves the list; the limits form says the
//   role's limit holds; the Usage view's token filter and its By token / By user table; the usage
//   answer has the controller's fields; the role limits are settings an admin writes.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { EventEmitter } from 'node:events';
import { describe, expect, it } from 'vitest';
import { createMockMiddleware } from '../mock/server.ts';
import type { Usage } from '../src/api/types.ts';
import { limitText, unpricedText, useText, type TokenForm } from '../src/lib/access.ts';
import { groupRows, groupWindow } from '../src/lib/usage-groups.ts';
import { KEYS, dialog, open, page, rig, rowText, type AccessRig } from './access-rig.ts';

const form = (o: Partial<TokenForm> = {}): TokenForm => ({ label: 'desk', scopes: ['inference:openai'], models: '', days: '90', limits: {}, ...o });
async function made(r: AccessRig, limits: Record<string, string>, label = 'desk'): Promise<string> {
  await open(r, 'my-tokens');
  r.flow.show({ kind: 'token-new' });
  await r.flow.createToken(form({ label, limits }));
  const d = r.flow.dialog;
  r.flow.close();
  return d?.kind === 'token-once' ? d.token.id : '';
}

describe('Gherkin: limits and usage (SRV-873)', () => {
  it('A rate limit: My tokens marks the reached requests-a-minute limit and lists its 429', async () => {
    const r = rig('ola');
    const id = await made(r, { requests_per_minute: '10' });
    r.mock.usage[id] = {
      limits: { limits: { requests_per_minute: { limit: 10, used: 10, left: 0, reached: true, from: 'token' } }, over: true, refused_at: '2026-11-16T08:00:05Z' },
      refusals: [{ at: '2026-11-16T08:00:05Z', code: 'rate_limited', status: 429, model: 'small-1' }],
    };
    await open(r, 'my-tokens');
    const row = rowText(page(r), id);
    expect(row).toContain('<span class="tag ac-warn">Requests a minute 10 of 10</span>');
    expect(row).toContain('Last refused: 2026-11-16 08:00 UTC requests a minute (small-1)');
  });

  it('A spend limit: My tokens shows the dollars used of the day’s limit, reached, and its 403', async () => {
    const r = rig('ola');
    const id = await made(r, { cost_per_day: '1' });
    r.mock.usage[id] = {
      limits: { limits: { cost_per_day: { limit: 1, used: 1_160_000, left: 0, reached: true, from: 'token' } }, over: true, refused_at: null },
      refusals: [{ at: '2026-11-16T09:30:00Z', code: 'spend_limit_reached', status: 403, model: 'big-1' }],
    };
    await open(r, 'my-tokens');
    const row = rowText(page(r), id);
    expect(row).toContain('<span class="tag ac-warn">Cost a day $1.16 of $1.00</span>');
    expect(row).toContain('Last refused: 2026-11-16 09:30 UTC spend limit (big-1)');
  });

  it('A model without a price: the token’s page says the spend limit does not apply to that model', async () => {
    const r = rig('ola');
    const id = await made(r, { cost_per_day: '1' });
    const free = await made(r, { requests_per_minute: '30' }, 'free');
    await open(r, 'my-tokens');
    expect(r.paths).toContain(`GET /v1/auth/tokens/${id}/usage?days=30&tz=${encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone)}`);
    expect(rowText(page(r), id)).toContain('data-unpriced>The spend limit does not apply to local-1: it has no price.');
    // without a spend limit there is nothing to say
    expect(rowText(page(r), free)).not.toContain('data-unpriced');
    expect(unpricedText({ ...r.flow.usage[id], unpriced: ['a', 'b'] })).toBe('The spend limit does not apply to a, b: they have no price.');
  });

  it('Usage adds up: grouped by token, then by user, each sum is the sum of the ledger’s rows', async () => {
    const mw = createMockMiddleware({ live: false });
    const signIn = await call(mw, 'GET', '/dashboard/?auth=ola');
    const cookie = ([] as string[]).concat(signIn.headers['set-cookie'] as string[]).map((c) => c.split(';')[0]).join('; ');
    const q = '/v1/dashboard/usage?bucket=day&tz=UTC&from=2026-01-01&to=2026-12-31';
    const all = (await call(mw, 'GET', q, cookie)).body as Usage;
    const tokens = all.dimensions.tokens!;
    expect(tokens.length).toBeGreaterThan(1);
    for (const by of ['token', 'user'] as const) {
      const u = (await call(mw, 'GET', `${q}&group=${by}`, cookie)).body as Usage;
      expect(u.group).toBe(by);
      const sum = (k: 'requests' | 'total_tokens') => u.groups!.reduce((a, g) => a + g[k], 0);
      expect([sum('requests'), sum('total_tokens')]).toEqual([all.totals.requests, all.totals.total_tokens]);
      if (by === 'token') {
        // each token's group is that token's rows
        for (const g of u.groups!.filter((x) => x.key)) {
          const one = (await call(mw, 'GET', `${q}&token=${g.key}`, cookie)).body as Usage;
          expect([one.filters.token, one.totals.requests, one.totals.total_tokens]).toEqual([g.key, g.requests, g.total_tokens]);
          expect(g.label).toBe(tokens.find((t) => t.id === g.key)!.label);
        }
      }
    }
    // without accounts: no token dimension
    const off = (await call(mw, 'GET', q)).body as Usage;
    expect(off.dimensions.tokens).toBeUndefined();
  });
});

describe('My tokens: each token’s use and its limits’ state', () => {
  it('30 days of use, each limit with what is used of it, a reached one marked, the last refusals', async () => {
    const r = rig('ola');
    const id = await made(r, { requests_per_minute: '30', cost_per_day: '1' });
    r.mock.usage[id] = {
      limits: { limits: { requests_per_minute: { limit: 30, used: 30, left: 0, reached: true, from: 'token' }, cost_per_day: { limit: 1, used: 580_000, left: 420_000, reached: false, from: 'role' } }, over: true, refused_at: null },
      refusals: [{ at: '2026-11-16T08:00:00Z', code: 'rate_limited', status: 429, model: 'small-1' }, { at: '2026-11-16T07:00:00Z', code: 'spend_limit_reached', status: 403, model: 'big-1' }],
    };
    await open(r, 'my-tokens');
    const row = rowText(page(r), id);
    expect(row).toContain(`data-usage=${id}`);
    expect(row).toMatch(/data-use>30 days: \d+ requests, [\d.K]+ tokens, \$[\d.]+, 1 refused</);
    expect(row).toContain('<span class="tag ac-warn">Requests a minute 30 of 30</span>');
    expect(row).toContain('<span class="tag ">Cost a day $0.58 of $1.00 (role)</span>');
    expect(row).toContain('Last refused: 2026-11-16 08:00 UTC requests a minute (small-1); 2026-11-16 07:00 UTC spend limit (big-1)');
    expect(limitText('tokens_per_minute', { limit: 20000, used: 1500, left: 18500, reached: false, from: 'token' })).toBe('Tokens a minute 1.5K of 20K');
    expect(useText({ ...r.flow.usage[id], totals: { ...r.flow.usage[id].totals, requests: 1, refused: 0, cost_micros: null } })).not.toMatch(/\$|refused|requests/);
  });

  it('a usage read that fails leaves the token without the line, never the page without its list', async () => {
    const r = rig('ola');
    const id = await made(r, {});
    r.api.tokenUsage = () => Promise.reject(new Error('no'));
    await open(r, 'my-tokens');
    expect(r.flow.error).toBeNull();
    expect(rowText(page(r), id)).not.toContain('data-usage');
    // a revoked token is not asked for
    const r2 = rig('ola');
    const gone = await made(r2, {});
    await r2.api.revokeToken(gone);
    const before = r2.paths.length;
    await open(r2, 'my-tokens');
    expect(r2.paths.slice(before).some((p) => p.includes(`${gone}/usage`))).toBe(false);
  });

  it('the limits form: empty takes the role’s limit; cost is dollars for priced models; nothing is “later”', async () => {
    const r = rig('ola');
    await open(r, 'my-tokens');
    r.flow.show({ kind: 'token-new' });
    const d = dialog(r);
    expect(d).toContain('Empty: your role&#39;s limit, when an admin set one; never more than it. Cost in dollars, for models with a price.'.replace('&#39;', "'"));
    expect(d).not.toContain('later version');
  });
});

describe('the Usage view’s token and user dimensions', () => {
  it('rows by token name the label and owner, rows without a key come last in words; the window is the charts’ range', () => {
    const g = (key: string | null, n: number, more = {}) => ({ key, requests: n, refused: 0, prompt_tokens: n, cached_tokens: 0, completion_tokens: n, reasoning_tokens: 0, total_tokens: 2 * n, cost_micros: null, ...more });
    const rows = groupRows([g(null, 9), g('a1', 5, { label: 'laptop', owner: 'ola' }), g('b2', 3, { label: null, owner: null })], 'token');
    expect(rows.map((x) => [x.key, x.name, x.sub])).toEqual([['a1', 'laptop', 'ola'], ['b2', 'b2', null], [null, 'no token (the page, local mode)', null]]);
    expect(groupRows([g('ola', 2), g(null, 1)], 'user').map((x) => x.name)).toEqual(['ola', 'no user']);
    expect(groupWindow('30d', null, null, '2026-11-16')).toEqual({ from: '2026-10-18', to: '2026-11-16' });
    expect(groupWindow('custom', '2026-01-01', '2026-01-31', '2026-11-16')).toEqual({ from: '2026-01-01', to: '2026-01-31' });
    expect(groupWindow('90d', null, null, '2026-11-16').from).toBe('2026-08-19');
  });

  it('the view reads token= and group=, shows the Token filter and the By token / By user table', () => {
    const src = readFileSync(join(__dirname, '..', 'src', 'views', 'usage.ts'), 'utf8');
    for (const s of ['data-filter="token"', 'data-group=${by}', 'usage-groups', "params.get('token')", 'group: by']) expect(src, s).toContain(s);
    const client = readFileSync(join(__dirname, '..', 'src', 'api', 'client.ts'), 'utf8');
    expect(client).toContain("if (p.token) q.set('token', p.token);");
    expect(client).toContain("if (p.group) q.set('group', p.group);");
  });
});

describe('the mock’s answers are the controller’s', () => {
  const py = (f: string) => readFileSync(join(__dirname, '..', '..', 'proxy', f), 'utf8');
  it('the token usage answer has the fields of proxy/tokenusage.py', async () => {
    const src = py('tokenusage.py');
    for (const k of KEYS.usage) expect(src, k).toMatch(new RegExp(`"${k}"`));
    for (const k of KEYS.sums) expect(src, k).toContain(`"${k}"`);
    const r = rig('ola');
    const id = await made(r, { cost_per_day: '1' });
    await open(r, 'my-tokens'); // the rig holds every answer to KEYS.usage
    expect(Object.keys(r.flow.usage)).toContain(id);
  });

  it('the role limits: 20 keys an admin writes, a number for the dollars', async () => {
    const plan = (r: AccessRig, set: Record<string, unknown>) => r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'api', set }] });
    const ola = rig('ola');
    const no = await plan(ola, { 'limits.default.operator.cost_per_day': 2.5 });
    expect([no.status, (no.body as { error: { type: string } }).error.type]).toEqual([403, 'admin_required']);
    const sam = rig('sam');
    expect((await plan(sam, { 'limits.default.operator.cost_per_day': 2.5 })).status).toBe(200);
    expect((await plan(sam, { 'limits.default.operator.requests_per_minute': 1.5 })).status).toBe(422);
    expect((await plan(sam, { 'limits.default.viewer.cost_per_month': -1 })).status).toBe(422);
  });
});

function call(mw: ReturnType<typeof createMockMiddleware>, method: string, url: string, cookie = ''): Promise<{ status: number; headers: Record<string, string | string[]>; body: unknown }> {
  return new Promise((resolve) => {
    const req = Object.assign(new EventEmitter(), { method, url, headers: { cookie } as Record<string, string>, socket: { destroy() {} } });
    const headers: Record<string, string | string[]> = {};
    let status = 200;
    const res = {
      headersSent: false,
      setHeader: (k: string, v: string) => (headers[k.toLowerCase()] = v),
      appendHeader: (k: string, v: string) => (headers[k.toLowerCase()] = [...((headers[k.toLowerCase()] as string[]) ?? []), v]),
      writeHead: (s: number, h: Record<string, string> = {}) => {
        status = s;
        Object.assign(headers, h);
      },
      end: (s?: string) => resolve({ status, headers, body: s && s.startsWith('{') ? JSON.parse(s) : null }),
    };
    mw(req as never, res as never, () => resolve({ status, headers, body: null }));
    queueMicrotask(() => req.emit('end'));
  });
}
