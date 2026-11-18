// SRV-889: the smart API key dialog against the mock controller with accounts: API, Endpoint and
// keys shows the endpoint; New API key walks purpose, models, expiry and review; a key for one
// backend shows on the page with its backends; a monitoring key skips the models step; an empty
// pick is held; a phone's width has no sideways scroll.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK } from './helpers';

test.skip(!IS_MOCK, 'reads the mock’s accounts and backends');

const reset = async (page: Page): Promise<void> => {
  await page.request.post('/__mock/fleet', { data: { reset: true } });
  // the backends of the mock as seeded (vLLM, SGLang, DeepSeek): what the picker lists
  await page.request.post('/__mock/backends', { data: { reset: true } });
};
const tk = (page: Page) => page.locator('dialog[data-dialog="token"]');

async function api(page: Page, who = 'ola'): Promise<void> {
  await page.goto(`./?auth=${who}&t=${Date.now()}#/api`);
  await expect(page.locator('qse-endpoint [data-endpoint]')).toBeVisible();
}

test.beforeEach(async ({ page }) => reset(page));

test('the endpoint and a key for one backend', async ({ page }) => {
  await api(page);
  await expect(page.locator('qse-endpoint [data-endpoint]')).toContainText('/v1');
  await page.locator('[data-act="key-new"]').click();
  await expect(tk(page)).toBeVisible();
  await tk(page).locator('[data-purpose="agent"]').click();
  await tk(page).locator('input[name="label"]').fill('laptop opencode');
  await tk(page).locator('[data-act="tk-next"]').click();
  // an empty pick is held
  await tk(page).locator('[data-run="pick"]').click();
  await tk(page).locator('[data-act="tk-next"]').click();
  await expect(tk(page).locator('[data-dialog-error]')).toContainText('Pick at least one backend');
  const first = tk(page).locator('[data-picker] li[data-backend]').first();
  await expect(first).toBeVisible();
  const id = (await first.getAttribute('data-backend'))!;
  await first.locator('.tk-brow input[type="checkbox"]').check();
  await expect(tk(page).locator('[data-count]')).toContainText('1 backend');
  await tk(page).locator('[data-act="tk-next"]').click();
  await tk(page).locator('[data-act="tk-next"]').click();
  await expect(tk(page).locator('[data-summary]')).toContainText('This key calls the OpenAI-compatible API');
  await tk(page).locator('[data-act="tk-create"]').click();
  await expect(tk(page).locator('[data-secret="token"]')).toHaveText(/^tdm-/);
  await expect(tk(page).locator('.tk-setup')).toContainText('OPENAI_BASE_URL');
  await tk(page).locator('[data-act="tk-done"]').click();
  await expect(page.locator(`qse-endpoint [data-keys]`)).toContainText('laptop opencode');
  await expect(page.locator(`qse-endpoint [data-keys]`)).toContainText(`backends ${id}`);
});

test('picking some models of a backend', async ({ page }) => {
  await api(page);
  await page.locator('[data-act="key-new"]').click();
  await tk(page).locator('[data-act="tk-next"]').click();
  await tk(page).locator('[data-run="pick"]').click();
  const b = tk(page).locator('[data-picker] li[data-backend]').filter({ has: page.locator('[data-models]') }).first();
  await b.locator('[data-models]').click();
  const m = b.locator('input[data-model]').first();
  await m.check();
  const name = (await m.getAttribute('data-model'))!;
  await expect(tk(page).locator('[data-count]')).toContainText('1 model');
  await tk(page).locator('[data-act="tk-next"]').click();
  await tk(page).locator('[data-act="tk-next"]').click();
  await tk(page).locator('[data-act="tk-create"]').click();
  await tk(page).locator('[data-act="tk-done"]').click();
  await expect(page.locator('qse-endpoint [data-keys]')).toContainText(name);
});

test('a monitoring key skips the models step', async ({ page }) => {
  await api(page, 'kim');
  await page.locator('[data-act="key-new"]').click();
  await tk(page).locator('[data-purpose="monitoring"]').click();
  await expect(tk(page).locator('.tk-steps li')).toHaveCount(3);
  await tk(page).locator('[data-act="tk-next"]').click();
  await expect(tk(page).locator('select[name="days"]')).toBeVisible();
});

test('a phone: no sideways scroll in the dialog', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await api(page);
  await page.locator('[data-act="key-new"]').click();
  await tk(page).locator('[data-act="tk-next"]').click();
  await tk(page).locator('[data-run="pick"]').click();
  const wide = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(wide).toBe(false);
});
