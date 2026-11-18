// The proxy's Backends screen and Setup flow (P1, contract 1.3) on the mock engine: Setup on an
// empty registry, add a backend after a test connection, the write-only key and its hint, a field
// the environment locks, delete asks first, the routing rules with the env rule locked, a user's
// key shown once, a model's price and switches, the backend filter on Usage, a relayed request's
// drawer measured at the proxy, and the screen at 390 px.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK, login, noHorizontalOverflow, setMode } from './helpers';

test.skip(!IS_MOCK, 'the backends spec runs on the mock tier (@mock)');

async function open(page: Page, hash = '#/backends', mock: Record<string, unknown> = { reset: true }): Promise<void> {
  await setMode(page.request, 'ok');
  await page.request.post('/__mock/backends', { data: mock });
  await login(page, hash);
  if (hash.startsWith('#/backends')) await expect(page.locator('qse-backends')).toBeVisible();
}

/** Add a backend: pick a provider, then the whole form (More settings) */
async function moreSettings(page: Page, preset: string): Promise<void> {
  await page.locator('#connect select[data-provider]').selectOption(preset);
  await page.locator('#connect [data-more]').click();
}

test('an empty registry opens on Setup, and a backend is tested and saved @mock', async ({ page }) => {
  await open(page, '#/backends', { reset: true, empty: true });
  await expect(page.locator('#setup')).toBeVisible();
  await moreSettings(page, 'deepseek');
  const form = page.locator('#backend-form');
  await expect(form.locator('[data-field="url"] input')).toHaveValue('https://api.deepseek.com/v1');
  await form.locator('[data-field="key"] input').fill('sk-fixture-ABCDEFGH1234');
  await form.getByRole('button', { name: 'Test connection' }).click();
  await expect(form.locator('[data-test="ok"]')).toContainText('2 models');
  await form.getByRole('button', { name: 'Save backend' }).click();
  await expect(page.locator('#backend-detail')).toBeVisible();
  await expect(page.locator('#backend-detail')).toContainText('…1234');
  await expect(page.locator('body')).not.toContainText('sk-fixture-ABCDEFGH1234');
});

test('a failing test says why, before anything is saved @mock', async ({ page }) => {
  await open(page, '#/backends?screen=setup');
  await moreSettings(page, 'custom-openai');
  const form = page.locator('#backend-form');
  await form.locator('[data-field="url"] input').fill('http://down.local:1/v1');
  await form.getByRole('button', { name: 'Test connection' }).click();
  await expect(form.locator('[data-test="fail"]')).toContainText('refused');
});

test('a field the environment sets is locked and names its variable @mock', async ({ page }) => {
  await open(page);
  const card = page.locator('.bk-card[data-backend="deepseek"]');
  await expect(card).toContainText('up');
  await card.getByRole('button', { name: 'Edit' }).click();
  const url = page.locator('[data-field="url"]');
  await expect(url).toContainText('set by TANDEM_BACKEND_DEEPSEEK_URL');
  await expect(url.locator('input')).toBeDisabled();
  await expect(page.locator('[data-field="key"]')).toContainText('ends in');
  await page.getByRole('button', { name: 'Cancel' }).click();
  // delete asks first, and the env-declared backend refuses
  await page.locator('.bk-card[data-backend="vllm1"]').getByRole('button', { name: 'Edit' }).click();
  let asked = false;
  page.once('dialog', (d) => {
    asked = true;
    void d.accept();
  });
  await page.getByRole('button', { name: 'Delete' }).click();
  await expect(page.locator('.bk-card[data-backend="vllm1"]')).toHaveCount(0);
  expect(asked).toBe(true);
});

test('routing rules keep the env rule; the keys of before point to Access @mock', async ({ page }) => {
  await open(page, '#/backends?screen=routing');
  await expect(page.locator('.bk-rule').first()).toContainText('set by TANDEM_ROUTE_1');
  await page.getByRole('button', { name: 'Add rule' }).click();
  await page.locator('.bk-rule').nth(1).locator('input.mono').fill('Qwen/*');
  await page.getByRole('button', { name: 'Save rules' }).click();
  await expect(page.locator('.view-backends [role="status"]')).toContainText('rules saved');
  await page.getByRole('tab', { name: 'Users' }).click();
  // SEC-28: API tokens replace these keys; the panel lists the old ones and points to Access
  await expect(page.locator('#users a[href="#/access"]')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Make a key' })).toHaveCount(0);
});

test('a model gets a price from the list and its switches @mock', async ({ page }) => {
  await open(page, '#/backends?backend=deepseek');
  const row = page.locator('tr[data-model="deepseek-reasoner"]');
  await row.locator('input[aria-label="deepseek-reasoner input price"]').fill('0.55');
  await row.locator('input[aria-label="deepseek-reasoner output price"]').fill('2.19');
  await row.getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('.view-backends [role="status"]')).toContainText('deepseek-reasoner saved');
  await page.locator('tr[data-model="deepseek-chat"] .bk-row-tools button').first().click();
  await expect(page.locator('.bk-features')).toContainText('packages');
});

test('usage filters by backend; a relayed request is measured at the proxy @mock', async ({ page }) => {
  await open(page, '#/usage');
  await expect(page.locator('[data-filter="backend"] select')).toBeVisible();
  await page.locator('[data-filter="backend"] select').selectOption('deepseek');
  await expect(page.locator('.chip', { hasText: 'backend DeepSeek' })).toBeVisible();
  await expect(page.locator('[data-fact="cost"]')).toContainText('$0.04');
  await page.goto('./#/dev');
  const tag = page.locator('.proxy-tag[data-backend="deepseek"]').first();
  await expect(tag).toBeVisible();
  await tag.click();
  await expect(page.locator('[data-measured="proxy"]').first()).toContainText('Measured at the proxy');
});

test('the Backends screen fits a phone @mock', async ({ page }, info) => {
  test.skip(!info.project.name.includes('phone'), 'phone layout');
  await open(page);
  await expect(page.locator('.bk-card').first()).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBe(true);
  await page.locator('.bk-card[data-backend="vllm1"]').getByRole('button', { name: 'Edit' }).click();
  // 0.3.36: the memory writer's four fields stack in one column at 390 px
  const wr = page.locator('[data-section="writer"]');
  await expect(wr).toBeVisible();
  await wr.locator('[data-field="harness.writer"] select').selectOption('shadow');
  await expect(wr.locator('[data-shadow-log]')).toBeVisible();
  const boxes = await wr.locator('.bk-field').evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().left)));
  expect(new Set(boxes).size).toBe(1);
  expect(await noHorizontalOverflow(page)).toBe(true);
  await page.getByRole('button', { name: 'Cancel' }).click();
  await page.goto('./#/backends?screen=setup');
  expect(await noHorizontalOverflow(page)).toBe(true);
  await page.goto('./#/backends?backend=deepseek&model=deepseek-chat');
  await expect(page.locator('.bk-feature[data-group="packages"]')).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBe(true);
});

test('a model shows what each switch resolves to, what it loses here and what it cost @mock', async ({ page }) => {
  await open(page, '#/backends?backend=vllm1&model=Qwen%2FQwen3.5-4B');
  const loops = page.locator('.bk-feature[data-group="loops"]');
  await expect(loops).toContainText('inherit → on');
  await expect(loops.locator('[data-degraded]')).toContainText('a close is a second request');
  await page.goto('./#/backends?backend=deepseek&model=deepseek-chat');
  const pk = page.locator('.bk-feature[data-group="packages"]');
  await expect(pk).toContainText('inherit → off');
  await expect(pk.locator('[data-degraded]')).toContainText('costs input tokens every turn');
  await expect(page.locator('.bk-feature[data-group="loops"] [data-degraded]')).toContainText('never closed');
  await expect(page.locator('.bk-feature[data-group="class"] [data-degraded]')).toContainText('24 h: 41 requests');
  // the backend's own harness switch and its profile are on the form
  await page.goto('./#/backends');
  await page.locator('.bk-card[data-backend="vllm1"]').getByRole('button', { name: 'Edit' }).click();
  await expect(page.locator('[data-field="harness.mode"] select')).toHaveValue('on');
  await expect(page.locator('[data-field="harness.profile"] select')).toHaveValue('coding');
});

test('the memory writer of a backend: the switches on its form, the shadow log, the mode of each model @mock', async ({ page }) => {
  await open(page, '#/backends?backend=vllm1');
  const facts = page.locator('#backend-detail [data-writer]');
  await expect(facts).toContainText('auto');
  await expect(facts).toContainText('today 3 of 48 runs');
  await expect(facts.locator('li[data-model="Qwen/Qwen3.5-4B"]')).toContainText('→ on');
  // the form: mode, spend cap, runs, prompt cap; a bad value is refused before anything is saved
  await page.goto('./#/backends');
  await page.locator('.bk-card[data-backend="vllm1"]').getByRole('button', { name: 'Edit' }).click();
  const wr = page.locator('[data-section="writer"]');
  await expect(wr.locator('[data-field="harness.writer"] select')).toHaveValue('auto');
  await expect(wr.locator('[data-field="harness.writer"] .hint')).toContainText('auto: on');
  await expect(wr.locator('[data-field="harness.writer_runs_day"] input')).toHaveValue('48');
  await expect(wr.locator('[data-field="harness.writer_prompt_tokens"] input')).toHaveValue('32000');
  await wr.locator('[data-field="harness.writer_runs_day"] input').fill('-1');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('.bk-form [role="alert"]')).toContainText('runs a day');
  await expect(wr.locator('[data-field="harness.writer_runs_day"]')).toHaveClass(/is-bad/);
  await wr.locator('[data-field="harness.writer_runs_day"] input').fill('5');
  await wr.locator('[data-field="harness.writer"] select').selectOption('shadow');
  await expect(wr.locator('[data-shadow-log]')).toContainText('/shadow/writer-<yyyy-mm-dd>.md');
  await expect(wr.locator('[data-field="harness.writer"] .hint')).toContainText('settings’ writer mode is on');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('.bk-card[data-backend="vllm1"] [data-writer-mode]')).toHaveText('writer shadow');
  // saved, and the harness mode and profile kept (the server merges the harness document)
  await page.locator('.bk-card[data-backend="vllm1"]').getByRole('button', { name: 'Edit' }).click();
  await expect(page.locator('[data-field="harness.writer"] select')).toHaveValue('shadow');
  await expect(page.locator('[data-field="harness.writer_runs_day"] input')).toHaveValue('5');
  await expect(page.locator('[data-field="harness.mode"] select')).toHaveValue('on');
  await expect(page.locator('[data-field="harness.profile"] select')).toHaveValue('coding');
  await page.getByRole('button', { name: 'Cancel' }).click();
  await page.goto('./#/backends?backend=vllm1');
  await expect(page.locator('#backend-detail [data-writer] li[data-model="Qwen/Qwen3.5-4B"]')).toContainText('→ shadow');
  await expect(page.locator('#backend-detail [data-writer] [data-shadow-log]')).toBeVisible();
});

test('a paid cloud model: auto is off, and the spend cap the environment sets is locked @mock', async ({ page }) => {
  await open(page, '#/backends?backend=deepseek');
  const facts = page.locator('#backend-detail [data-writer]');
  await expect(facts.locator('li[data-model="deepseek-chat"]')).toContainText('→ off');
  await expect(facts.locator('li[data-model="deepseek-chat"]')).toContainText('a paid cloud model');
  await expect(facts).toContainText('cap $0.1 a day');
  await page.goto('./#/backends');
  await page.locator('.bk-card[data-backend="deepseek"]').getByRole('button', { name: 'Edit' }).click();
  const cap = page.locator('[data-field="harness.writer_usd_day"]');
  await expect(cap).toContainText('set by TANDEM_BACKEND_DEEPSEEK_WRITER_USD_DAY');
  await expect(cap.locator('input[type="number"]')).toBeDisabled();
  await expect(cap.locator('input[type="number"]')).toHaveValue('0.1');
  await expect(page.locator('[data-field="harness.writer"] .hint')).toContainText('off for a paid model');
  // the lock names a long variable: it wraps, the page keeps the phone's width (a wider page moved
  // the bottom bar over Save)
  expect(await noHorizontalOverflow(page)).toBe(true);
  // another field still saves: the locked one is not sent
  await page.locator('[data-field="harness.writer"] select').selectOption('on');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.locator('.bk-card[data-backend="deepseek"] [data-writer-mode]')).toHaveText('writer on');
});

test('VIS-54: the proxy off is said without a 404 for /v1/config, at 1440 and 390 px @mock', async ({ page }) => {
  // UT1 (2026-10-06): the Backends tab on an engine without --backends logged a 404 for
  // /v1/config on every visit; the server now says `proxy.mode: off` in /v1/dashboard/system
  const config: string[] = [];
  const errors: string[] = [];
  page.on('request', (r) => { if (new URL(r.url()).pathname.endsWith('/v1/config')) config.push(r.url()); });
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width > 500 ? 900 : 844 });
    await open(page, '#/backends', { reset: true, off: true });
    errors.length = 0;                   // the sign-in screen's own session check is a 401
    await page.evaluate(() => { location.hash = '#/usage'; });
    await page.evaluate(() => { location.hash = '#/backends'; });
    await expect(page.locator('#proxy-off')).toContainText('The proxy is off on this server');
    await page.waitForTimeout(1500);
    expect(await noHorizontalOverflow(page)).toBe(true);
    expect(errors).toEqual([]);
  }
  expect(config).toEqual([]);
  await page.request.post('/__mock/backends', { data: { reset: true, off: false } });
});

test('a mixed front: the engine says off, the controller with accounts still adds a backend @mock', async ({ page }) => {
  // tandem.khaled.de 2026-11-18: /v1/dashboard/* reached the engine (proxy.mode off since engine API
  // 1.8) and /v1/config the controller; the page said "The proxy is off" and hid Add backend
  await setMode(page.request, 'ok');
  await page.request.post('/__mock/backends', { data: { reset: true, systemOff: true } });
  await page.goto(`./?auth=kim&t=${Date.now()}#/backends`);
  await expect(page.locator('qse-backends')).toBeVisible();
  await expect(page.locator('#proxy-off')).toHaveCount(0);
  await page.getByRole('button', { name: 'Add backend' }).click();
  await expect(page.locator('#connect select[data-provider]')).toBeVisible();
  // without accounts (an engine alone) the same `off` stays the quiet panel of VIS-54
  await page.request.post('/__mock/backends', { data: { reset: true, systemOff: true } });
  await page.goto(`./?auth=off&t=${Date.now()}#/backends`);
  await expect(page.locator('#proxy-off')).toContainText('The proxy is off on this server');
  await page.request.post('/__mock/backends', { data: { reset: true } });
});

test('a provider, a key and go: OpenRouter connects with its URL filled in and shows its name @mock', async ({ page }) => {
  await open(page, '#/backends?screen=setup', { reset: true, empty: true });
  const f = page.locator('#connect');
  await f.locator('select[data-provider]').selectOption('openrouter');
  await expect(f.locator('[data-field="url"] input')).toHaveValue('https://openrouter.ai/api/v1');
  await expect(f.locator('[data-protocol]')).toContainText('OpenAI-compatible');
  await expect(f.locator('[data-connect]')).toBeDisabled();            // its key is required
  await f.locator('[data-field="key"] input').fill('sk-or-test');
  await f.locator('[data-connect]').click();
  await expect(page.locator('#backend-detail')).toBeVisible();      // saved: its page opens
  await page.goto('./#/backends');
  const card = page.locator('.bk-card[data-backend="openrouter"]');
  await expect(card).toBeVisible();
  await expect(card.locator('[data-preset="openrouter"]')).toHaveText('OpenRouter');
  await expect(card).toContainText('cloud');
});

test('a provider with both protocols: the Anthropic base of Z.ai is one click @mock', async ({ page }) => {
  await open(page, '#/backends?screen=setup');
  const f = page.locator('#connect');
  await f.locator('select[data-provider]').selectOption('zai');
  await expect(f.locator('[data-field="url"] input')).toHaveValue('https://api.z.ai/api/paas/v4');
  await f.locator('[data-protocol-option="anthropic"]').click();
  await expect(f.locator('[data-field="url"] input')).toHaveValue('https://api.z.ai/api/anthropic');
  await expect(f.locator('[data-protocol-option="anthropic"]')).toHaveAttribute('aria-checked', 'true');
  expect(await noHorizontalOverflow(page)).toBe(true);
});

test('a refused key saves nothing until Save anyway @mock', async ({ page }) => {
  await open(page, '#/backends?screen=setup', { reset: true, empty: true });
  const f = page.locator('#connect');
  await f.locator('select[data-provider]').selectOption('deepseek');
  await f.locator('[data-field="key"] input').fill('wrong-key');
  await f.locator('[data-connect]').click();
  await expect(f.locator('[data-connect-error]')).toContainText('refused the API key');
  await page.goto('./#/backends?screen=setup');
  await page.goto('./#/backends');
  await expect(page.locator('.bk-card[data-backend="deepseek"]')).toHaveCount(0);
  await page.goto('./#/backends?screen=setup');
  await f.locator('select[data-provider]').selectOption('deepseek');
  await f.locator('[data-field="key"] input').fill('wrong-key');
  await f.locator('[data-connect]').click();
  await f.locator('[data-confirm]').click();
  await expect(page.locator('#backend-detail')).toBeVisible();
  await page.goto('./#/backends');
  await expect(page.locator('.bk-card[data-backend="deepseek"]')).toBeVisible();
});

test('Custom (Anthropic-compatible): a URL and an optional token @mock', async ({ page }) => {
  await open(page, '#/backends?screen=setup', { reset: true, empty: true });
  const f = page.locator('#connect');
  await f.locator('select[data-provider]').selectOption('custom-anthropic');
  await expect(f.locator('[data-protocol]')).toContainText('Anthropic-compatible');
  await expect(f.locator('[data-connect]')).toBeDisabled();            // no URL yet
  await f.locator('[data-field="url"] input').fill('http://192.0.2.7:8080');
  await f.locator('[data-field="label"] input').fill('Lab Claude proxy');
  await f.locator('[data-connect]').click();
  await expect(page.locator('#backend-detail')).toBeVisible();
  await page.goto('./#/backends');
  const card = page.locator('.bk-card[data-backend="custom"]');
  await expect(card).toContainText('Lab Claude proxy');
});

test('PL1: a new backend has no in-flight limit from us; parallel is this engine\'s own setting @mock', async ({ page }) => {
  // a backend stored without a cap: the card shows no "/N", its page says no limit
  await open(page, '#/backends');
  const inflight = page.locator('.bk-card[data-backend="vllm1"] .bk-card-facts div:has(dt:text-is("in flight")) dd');
  await expect(inflight).toBeVisible();
  await expect(inflight).not.toContainText('/');
  await page.goto('./#/backends?backend=vllm1');
  await expect(page.locator('#backend-detail')).toContainText('(no limit)');
  // the engine's own line: its parallel requests and the models beside Qwen, never a backend's field
  await open(page, '#/backends', { reset: true, empty: true });
  const own = page.locator('[data-engine-own]');
  await expect(own).toContainText('2 requests at once');
  await expect(own).toContainText('laya-english');
  await expect(own).toContainText('qwen3.5-2b');
  await moreSettings(page, 'vllm');
  const form = page.locator('#backend-form');
  const cap = form.locator('[data-field="max_inflight"] input');
  await expect(cap).toHaveValue('');
  await expect(cap).toHaveAttribute('placeholder', 'no limit');
  await expect(form.locator('[data-field="parallel"]')).toHaveCount(0);
});
