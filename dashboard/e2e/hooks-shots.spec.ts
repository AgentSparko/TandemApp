// VIS-282: pictures of the hooks pages against the mock's demo registry, desktop 1440 px and a
// 360 px phone, dark and (the pipeline) light: the pipeline, add a hook and the secret it shows
// once, a hook's page, the trace with the replay note, the decision providers, one provider and add
// a provider. Into PW_SHOTS (default screenshots/) as hooks-*.png. Each page is also checked for
// sideways scroll at both widths.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK, login } from './helpers';

test.skip(!IS_MOCK, 'reads the mock’s demo hooks');

const DIR = process.env.PW_SHOTS ?? 'screenshots';
const SIZES = { desktop: { width: 1440, height: 900 }, 360: { width: 360, height: 740 } } as const;

async function open(page: Page, hash: string, theme: 'dark' | 'light' = 'dark'): Promise<void> {
  await page.emulateMedia({ colorScheme: theme });
  await page.addInitScript((t) => localStorage.setItem('qse.theme', t), theme);
  await login(page, hash, `?t=${Date.now()}`);
  await page.locator('qse-hooks').waitFor();
}

/** the elements that reach past the window's right edge, outside a strip that scrolls on purpose
 *  (the settings area's tab strip, a block of code) */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const w = doc.clientWidth;
    const out: string[] = doc.scrollWidth > w + 1 ? [`the document scrolls: ${doc.scrollWidth} > ${w}`] : [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('.content *'))) {
      if (el.closest('.st-tabs') || el.closest('.hk-pre')) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > w + 1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).replace(/\s+/g, '.')} right=${Math.round(r.right)} width=${Math.round(r.width)} in ${el.parentElement?.className ?? ''}`);
    }
    return out.slice(0, 14);
  });
}

async function shot(page: Page, name: string): Promise<void> {
  await page.waitForTimeout(250);
  expect.soft(await overflowing(page), `${name}: nothing reaches past the window`).toEqual([]);

  await page.locator('qse-settings').screenshot({ path: `${DIR}/hooks-${name}.png` });
}

for (const size of ['desktop', '360'] as const) {
  test(`${size}: the pipeline, a move staged, and the light theme`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, '#/app/hooks');
    await page.locator('[data-pipeline] [data-hook="audit"]').waitFor();
    await page.screenshot({ path: `${DIR}/hooks-1-pipeline-top-${size}.png` });
    await shot(page, `1-pipeline-${size}`);
    await page.locator('[data-stage="request.finished"] [data-hook="export"] [data-act="move-up"]').click();
    await expect(page.locator('[data-bar-count]')).toContainText('1 staged change');
    await shot(page, `1-pipeline-staged-${size}`);
    await page.locator('[data-act="discard"]').click();
    await open(page, '#/app/hooks', 'light');
    await page.locator('[data-pipeline] [data-hook="audit"]').waitFor();
    await shot(page, `1-pipeline-light-${size}`);
  });

  test(`${size}: add a hook, its errors, the secret once`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, '#/app/hooks/new?stage=request.before_engine');
    await page.locator('[data-add-form]').waitFor();
    await shot(page, `2-add-${size}`);
    await page.locator('[data-act="add-create"]').click();
    await expect(page.locator('[data-form-invalid]')).toBeVisible();
    await shot(page, `2-add-errors-${size}`);
    await page.locator('[data-card="transform"]').click();
    await page.locator('[data-in="endpoint"]').fill('http://hooks.lan:9300/shots');
    await page.locator('[data-in="name"]').fill('Shots cap');
    await page.locator('[data-act="add-create"]').click();
    await page.locator('[data-secret-once]').waitFor();
    await shot(page, `2-add-created-${size}`);
    await page.locator('[data-act="created-test"]').click();
    await page.locator('[data-panel="playground"]').waitFor();
    await page.locator('[data-act="play-send"]').click();
    await page.locator('[data-explain]').waitFor();
    await shot(page, `3-hook-new-tested-${size}`);
    // leave the mock as it was
    await page.locator('[data-act="remove-ask"]').click();
    await page.locator('[data-act="remove-go"]').click();
    await page.locator('[data-pipeline]').waitFor();
  });

  test(`${size}: a hook’s page, one whose breaker probes, the trace and the replay note`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, '#/app/hooks/hook/temperature-cap');
    await page.locator('[data-hook-page="temperature-cap"]').waitFor();
    await page.locator('[data-calls]').first().waitFor();
    await shot(page, `3-hook-${size}`);
    await open(page, '#/app/hooks/hook/pii-guard');
    await page.locator('[data-hook-page="pii-guard"]').waitFor();
    await page.locator('[data-calls]').first().waitFor();
    await page.locator('[data-act="play-send"]').click();
    await page.locator('[data-explain]').waitFor();
    await shot(page, `3-hook-breaker-${size}`);
    await open(page, '#/app/hooks/trace');
    await page.locator('[data-recent]').waitFor();
    await page.locator('[data-recent-request]').first().click();
    await page.locator('[data-trace]').waitFor();
    await page.locator('[data-act="replay-start"]').click();
    await page.locator('[data-replay-note]').waitFor();
    await shot(page, `4-trace-${size}`);
  });

  test(`${size}: the decision providers, one provider, add a provider`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, '#/app/hooks/providers');
    await page.locator('[data-report]').first().waitFor();
    await shot(page, `5-providers-${size}`);
    await open(page, '#/app/hooks/providers/p/intent');
    await page.locator('[data-provider-page="intent"]').waitFor();
    await page.locator('[data-fallbacks]').waitFor();
    await page.locator('[data-act="test-send"]').click();
    await page.locator('[data-panel="test"] [data-explain]').waitFor();
    await shot(page, `6-provider-${size}`);
    await open(page, '#/app/hooks/providers/new');
    await page.locator('[data-add-provider]').waitFor();
    await page.locator('[data-act="add-create"]').click();
    await shot(page, `7-provider-add-${size}`);
  });
}
