// SRV-835: pictures of the health page against the mock's health scenarios, desktop 1440 px and a
// 360 px phone, dark and (the mixed page) light: the verdict and the cards, Start with what it
// leaves, Rotate, the dialog of Upgrade from its question to its job, every finding at once, the
// changed host key, the refused pairings, the first run, everything fine, the sign-in a fix asks
// for and the wait after too many fixes. Into PW_SHOTS (default screenshots/) as health-*.png.
// Each page is also checked for sideways scroll at both widths.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK, TOKEN, login, setMode } from './helpers';

test.skip(!IS_MOCK, 'reads the mock’s health scenarios');

const DIR = process.env.PW_SHOTS ?? 'screenshots';
const SIZES = { desktop: { width: 1440, height: 900 }, 360: { width: 360, height: 740 } } as const;

async function open(page: Page, scenario: string, o: { theme?: 'dark' | 'light'; fleet?: string; hash?: string } = {}): Promise<void> {
  const theme = o.theme ?? 'dark';
  await page.emulateMedia({ colorScheme: theme });
  await page.addInitScript((t) => localStorage.setItem('qse.theme', t), theme);
  await login(page, o.hash ?? '#/health', `?health=${scenario}${o.fleet ? `&fleet=${o.fleet}` : ''}&t=${Date.now()}`);
  await page.locator('qse-health [data-cards], qse-health .mon-signin-box').first().waitFor();
}

/** the elements that reach past the window's right edge */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const w = doc.clientWidth;
    const out: string[] = doc.scrollWidth > w + 1 ? [`the document scrolls: ${doc.scrollWidth} > ${w}`] : [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('.content *'))) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > w + 1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).replace(/\s+/g, '.')} right=${Math.round(r.right)} width=${Math.round(r.width)} in ${el.parentElement?.className ?? ''}`);
    }
    return out.slice(0, 14);
  });
}

async function shot(page: Page, name: string): Promise<void> {
  await page.waitForTimeout(250);
  expect.soft(await overflowing(page), `${name}: nothing reaches past the window`).toEqual([]);
  await page.locator('qse-health').screenshot({ path: `${DIR}/health-${name}.png` });
}
const card = (page: Page, id: string) => page.locator(`qse-health [data-card="${id}"]`);

for (const size of ['desktop', '360'] as const) {
  test(`${size}: the verdict and the cards, a card’s checks unfolded, the light theme`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'mixed');
    await expect(page.locator('[data-verdict]')).toContainText('Two things do not work.');
    await page.screenshot({ path: `${DIR}/health-1-mixed-top-${size}.png` });
    await shot(page, `1-mixed-${size}`);
    await card(page, 'spark-rack').locator('[data-checks] summary').click();
    await card(page, 'spark-rack').screenshot({ path: `${DIR}/health-1-card-unfolded-${size}.png` });
    await open(page, 'mixed', { theme: 'light' });
    await shot(page, `1-mixed-light-${size}`);
  });

  test(`${size}: Start and what it leaves, Rotate, a check again that clears`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'mixed');
    await card(page, 'spark-rack').locator('[data-act="start"]').click();
    await card(page, 'spark-rack').locator('[data-fixed]').waitFor();
    await expect(card(page, 'spark-rack').locator('[data-finding="engine_loading"]')).toBeVisible();
    await card(page, 'spark-rack').screenshot({ path: `${DIR}/health-2-started-${size}.png` });
    await card(page, 'gpu-box').locator('[data-act="rotate_token"]').click();
    await card(page, 'gpu-box').locator('[data-fixed]').waitFor();
    await card(page, 'controller').locator('[data-finding="backend_down"] [data-act="retry"]').click();
    await card(page, 'controller').locator('[data-fixed]').waitFor();
    await shot(page, `2-fixed-${size}`);
    // the page follows the engine until it serves
    await expect(card(page, 'spark-rack').locator('[data-finding]')).toHaveCount(0, { timeout: 30_000 });
    await expect(card(page, 'spark-rack')).toHaveAttribute('data-state', 'ok');
    await expect(page.locator('[data-verdict]')).toContainText('Everything works');
  });

  test(`${size}: Upgrade asks first: the release, the refusal without one, the plan, the job`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'mixed');
    await card(page, 'spark-attic').locator('[data-act="upgrade"]').click();
    const dlg = page.locator('dialog[data-dialog="fix"]');
    await expect(dlg).toBeVisible();
    await page.screenshot({ path: `${DIR}/health-3-upgrade-ask-${size}.png` });
    await dlg.locator('[data-act="dlg-go"]').click();
    await dlg.locator('[data-refusal="bad_request"]').waitFor();
    await page.screenshot({ path: `${DIR}/health-3-upgrade-refused-${size}.png` });
    await dlg.locator('[data-in="ref"]').fill('v0.16.0');
    await dlg.locator('[data-act="dlg-go"]').click();
    await dlg.locator('[data-plan]').waitFor();
    await page.screenshot({ path: `${DIR}/health-3-upgrade-plan-${size}.png` });
    await dlg.locator('[data-act="dlg-apply"]').click();
    await dlg.locator('.hl-dlg[data-phase="done"]').waitFor();
    await page.screenshot({ path: `${DIR}/health-3-upgrade-done-${size}.png` });
    await dlg.locator('[data-act="dlg-close"]').click();
    await expect(dlg).toBeHidden();
    await expect(card(page, 'spark-attic').locator('[data-note="working"]')).toBeVisible();
    await card(page, 'spark-attic').screenshot({ path: `${DIR}/health-3-upgrade-working-${size}.png` });
  });

  test(`${size}: every finding at once, the changed host key with its steps, the refused pairings`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'all');
    await page.screenshot({ path: `${DIR}/health-4-all-top-${size}.png` });
    await shot(page, `4-all-${size}`);
    const moved = card(page, 'spark-moved');
    await expect(moved.locator('[data-fingerprints]')).toBeVisible();
    // no button of the card connects: its only one shows how to pair again
    await expect(moved.locator('button[data-act]:not([data-act="recheck"])')).toHaveCount(1);
    await moved.locator('[data-act="re_pair"]').click();
    await moved.locator('[data-guide]').waitFor();
    await moved.screenshot({ path: `${DIR}/health-4-host-key-${size}.png` });
    await card(page, 'pairings').screenshot({ path: `${DIR}/health-4-pairings-${size}.png` });
    await card(page, 'plugins').screenshot({ path: `${DIR}/health-4-plugins-${size}.png` });
    await card(page, 'controller').screenshot({ path: `${DIR}/health-4-controller-${size}.png` });
    // the roll call jumps to a card
    await page.locator('[data-jump="gpu-box"]').click();
    await expect(card(page, 'gpu-box')).toBeInViewport();
    // too many fixes in a minute: the seventh waits
    const far = card(page, 'spark-far');
    for (let i = 0; i < 7; i++) {
      await far.locator('[data-act="retry"]').click();
      await expect(far.locator('[data-act="retry"]')).not.toHaveText('Working…');
    }
    await far.locator('[data-note="wait"]').waitFor();
    await expect(far.locator('[data-act="retry"]')).toBeDisabled();
    await far.screenshot({ path: `${DIR}/health-5-too-many-${size}.png` });
  });

  test(`${size}: the first run, everything fine, chatd silent, Chat on another machine`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'first', { fleet: 'empty' });
    await expect(page.locator('[data-verdict]')).toContainText('No engine is connected yet.');
    await card(page, 'controller').locator('[data-act="add_engine"]').click();
    await card(page, 'controller').locator('[data-guide]').waitFor();
    await shot(page, `6-first-${size}`);
    await open(page, 'ok');
    await expect(page.locator('[data-verdict]')).toContainText('Everything works.');
    await shot(page, `6-ok-${size}`);
    await open(page, 'silent');
    await shot(page, `6-silent-${size}`);
    await open(page, 'external');
    await shot(page, `6-external-${size}`);
    // a link to one card opens the page on it
    await open(page, 'mixed', { hash: '#/health/gpu-box' });
    await expect(card(page, 'gpu-box')).toHaveAttribute('data-focus', 'true');
    await expect(card(page, 'gpu-box')).toBeInViewport();
  });

  test(`${size}: a fix that needs the admin password brings the sign-in; signed in, it runs`, async ({ page, request }) => {
    await page.setViewportSize(SIZES[size]);
    await setMode(request, 'ok', { fleetPassword: true });
    try {
      await open(page, 'mixed');
      await card(page, 'spark-rack').locator('[data-act="start"]').click();
      await card(page, 'spark-rack').locator('[data-note="sign_in"]').waitFor();
      await page.locator('qse-health .mon-signin-box').waitFor();
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: `${DIR}/health-7-sign-in-${size}.png` });
      await page.locator('qse-health .mon-signin-box input[type="password"]').fill(TOKEN);
      await page.locator('qse-health .mon-signin-box button[type="submit"]').click();
      await expect(page.locator('qse-health .mon-signin-box')).toHaveCount(0);
      await card(page, 'spark-rack').locator('[data-act="start"]').click();
      await card(page, 'spark-rack').locator('[data-fixed]').waitFor();
    } finally {
      await setMode(request, 'ok', { fleetPassword: false });
    }
  });
}

test('the menu, the wizard and the settings area lead to Health', async ({ page }) => {
  await login(page, '#/add-engine', `?health=mixed&t=${Date.now()}`);
  await page.locator('qse-engine-wizard [data-health-link] a').click();
  await page.locator('qse-health [data-cards]').waitFor();
  await page.goto('./#/app');
  await page.locator('qse-settings [data-health-link] a').click();
  await page.locator('qse-health [data-cards]').waitFor();
  await expect(page.locator('.lpanel .nav-i[data-section="health"]')).toHaveClass(/is-on/);
});
