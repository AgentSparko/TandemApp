// SRV-875: a walk through the account and access pages against the mock controller with accounts
// (`?auth=<who>`, mock/auth.ts), desktop 1440 px and a 360 px phone, dark and (two pages) light:
// every page as an owner, a token made and shown once, a user added with a one-time code, what a
// viewer sees and a direct link without the right, an exposure switch with its question, local
// mode, a session that ends in the middle and the same view after the sign-in, the two settings
// pages, the keyboard through a dialog, and a health finding's link. Into PW_SHOTS (default
// screenshots/) as access-*.png. Each page is also checked for sideways scroll at both widths.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK } from './helpers';

test.skip(!IS_MOCK, 'reads the mock’s accounts');

const DIR = process.env.PW_SHOTS ?? 'screenshots';
const SIZES = { desktop: { width: 1440, height: 900 }, 360: { width: 360, height: 740 } } as const;
const PAGES = [
  ['account', '#/account'],
  ['my-tokens', '#/account/tokens'],
  ['users', '#/access'],
  ['tokens', '#/access/tokens'],
  ['exposure', '#/access/exposure'],
  ['sessions', '#/access/sessions'],
  ['audit', '#/access/audit'],
] as const;

/** the page as `who`, on a mock whose accounts are as seeded */
async function open(page: Page, who: string, hash: string, o: { theme?: 'dark' | 'light'; query?: string } = {}): Promise<void> {
  const theme = o.theme ?? 'dark';
  await page.emulateMedia({ colorScheme: theme });
  await page.addInitScript((t) => localStorage.setItem('qse.theme', t), theme);
  await page.goto(`./?auth=${who}${o.query ?? ''}&t=${Date.now()}${hash}`);
}
const reset = async (page: Page): Promise<void> => void (await page.request.post('/__mock/fleet', { data: { reset: true } }));
const at = (page: Page, id: string) => page.locator(`qse-access [data-access="${id}"]`);
const dlg = (page: Page) => page.locator('dialog[data-dialog="access"]');

/** the elements that reach past the window's right edge */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const w = doc.clientWidth;
    const out: string[] = doc.scrollWidth > w + 1 ? [`the document scrolls: ${doc.scrollWidth} > ${w}`] : [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('.content *, dialog[open] *'))) {
      // the strips of pages (access, settings) scroll by themselves, on purpose
      if (el.closest('.ac-tabs, .st-tabs')) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > w + 1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).replace(/\s+/g, '.')} right=${Math.round(r.right)} width=${Math.round(r.width)} in ${el.parentElement?.className ?? ''}`);
    }
    return out.slice(0, 14);
  });
}
async function shot(page: Page, name: string): Promise<void> {
  await page.waitForTimeout(250);
  expect.soft(await overflowing(page), `${name}: nothing reaches past the window`).toEqual([]);
  await page.screenshot({ path: `${DIR}/access-${name}.png`, fullPage: true });
}

test.beforeEach(async ({ page }) => reset(page));

for (const size of ['desktop', '360'] as const) {
  test(`${size}: every page as an owner, two of them in the light theme`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    for (const [id, hash] of PAGES) {
      await open(page, 'kim', hash);
      await at(page, id).waitFor();
      await expect(page.locator('qse-access .panel').first()).toBeVisible();
      await expect(page.locator('qse-access [data-unread], qse-access [data-stop]')).toHaveCount(0);
      await shot(page, `1-${id}-${size}`);
    }
    for (const [id, hash] of [PAGES[2], PAGES[4]]) {
      await open(page, 'kim', hash, { theme: 'light' });
      await at(page, id).waitFor();
      await shot(page, `1-${id}-light-${size}`);
    }
  });

  test(`${size}: a token is shown once, then only its last 4 characters`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'ola', '#/account/tokens');
    await page.locator('[data-act="token-new"]').click();
    // SRV-889: the smart dialog (views/tokens/token-dialog.ts)
    const tk = page.locator('dialog[data-dialog="token"]');
    await expect(tk).toBeVisible();
    await tk.locator('input[name="label"]').fill('desk');
    await shot(page, `2-token-form-${size}`);
    for (let i = 0; i < 3; i++) await tk.locator('[data-act="tk-next"]').click();
    await expect(tk.locator('[data-summary]')).toContainText('This key');
    await tk.locator('[data-act="tk-create"]').click();
    const code = tk.locator('[data-secret="token"]');
    await expect(code).toHaveText(/^tdm-[0-9a-f]{8}-/);
    const token = (await code.textContent())!;
    await expect(tk).toContainText('Copy the key now. It is shown this once.');
    await expect(tk.locator('.tk-setup > div')).toHaveCount(4);
    await shot(page, `2-token-once-${size}`);
    await tk.locator('[data-act="tk-done"]').click();
    await expect(tk).toBeHidden();
    await expect(page.locator('qse-access')).toContainText(`...${token.slice(-4)}`);
    await expect(page.locator('qse-access')).not.toContainText(token);
    // nothing of it in the address, and nothing kept in this browser
    expect(page.url()).not.toContain(token.slice(13));
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }))).not.toContain(token.slice(13));
  });

  test(`${size}: a user is added with a one-time code; the last owner is held`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'kim', '#/access');
    await at(page, 'users').waitFor();
    await expect(page.locator('[data-last-owner]')).toHaveCount(1);
    await page.locator('[data-act="user-new"]').click();
    await dlg(page).locator('input[name="username"]').fill('eve');
    await dlg(page).locator('select[name="role"]').selectOption('operator');
    await dlg(page).locator('[data-act="dlg-go"]').click();
    await expect(dlg(page).locator('[data-secret="code"]')).toBeVisible();
    await expect(dlg(page)).toContainText('This code is shown once. Copy it now.');
    await shot(page, `3-user-code-${size}`);
    await dlg(page).locator('[data-act="dlg-go"]').click();
    await expect(page.locator('qse-access')).toContainText('one-time code');
  });

  test(`${size}: a viewer sees Account alone, no create button, and "Not allowed" on a direct link`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'vic', '#/account/tokens');
    await at(page, 'my-tokens').waitFor();
    await expect(page.locator('[data-no-create]')).toHaveText('An admin must allow viewers to create tokens.');
    await expect(page.locator('[data-act="token-new"]')).toHaveCount(0);
    await expect(page.locator('[data-section="account"]').first()).toBeAttached();
    await expect(page.locator('[data-section="access"]')).toHaveCount(0);
    await shot(page, `4-viewer-tokens-${size}`);
    for (const hash of ['#/access', '#/access/tokens', '#/access/exposure']) {
      await open(page, 'vic', hash);
      await expect(page.locator('qse-access [data-stop="not-allowed"]')).toContainText('Not allowed');
    }
    await shot(page, `4-viewer-not-allowed-${size}`);
    // once an admin allows it, the button is there
    await page.request.post('/__mock/auth', { data: { viewers: 'on' } });
    await open(page, 'vic', '#/account/tokens');
    await expect(page.locator('[data-act="token-new"]')).toBeVisible();
  });

  test(`${size}: an exposure switch asks first; local mode asks for the password`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'kim', '#/access/exposure');
    await at(page, 'exposure').waitFor();
    await page.locator('[data-panel="exposure-anthropic"] [data-value="network"]').click();
    await expect(dlg(page)).toContainText('No device gets in without a token.');
    await shot(page, `5-exposure-ask-${size}`);
    await dlg(page).locator('[data-act="dlg-go"]').click();
    await expect(page.locator('[data-panel="exposure-anthropic"] [data-value="network"]')).toHaveAttribute('aria-pressed', 'true');
    await open(page, 'local', '#/access/exposure');
    await expect(page.locator('qse-access [data-password-first]')).toBeVisible();
    await shot(page, `5-exposure-local-${size}`);
    await open(page, 'local', '#/access');
    await expect(page.locator('qse-access [data-stop="password-first"]')).toBeVisible();
    // #/account is the step that sets the first password
    await open(page, 'local', '#/account');
    await expect(page.locator('qse-auth [data-auth="account"]')).toContainText('Set a password');
    await shot(page, `5-local-account-${size}`);
  });

  test(`${size}: signed out in the middle: the sign-in appears, and the same view after it`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'sam', '#/access/tokens');
    await at(page, 'tokens').waitFor();
    await page.request.post('/__mock/auth', { data: { revoke: true } });
    // the next request of the page gets a 401
    await page.locator('[data-panel="review"] [data-act="confirm"]').first().click();
    const form = page.locator('qse-auth [data-auth="signin"]');
    await expect(form).toBeVisible();
    expect(page.url()).toContain('#/access/tokens');
    await shot(page, `6-signed-out-${size}`);
    await form.locator('input[name="username"]').fill('sam');
    await form.locator('input[name="password"]').fill('the password of sam');
    await form.locator('button[type="submit"]').click();
    await at(page, 'tokens').waitFor();
    expect(page.url()).toContain('#/access/tokens');
    await expect(page.locator('[data-panel="review"]')).toBeVisible();
  });

  test(`${size}: the Sign-in and API settings pages, and a health finding’s link`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await open(page, 'kim', '#/app/signin');
    await expect(page.locator('[data-settings-tab="signin"]')).toBeVisible();
    await expect(page.locator('qse-settings')).toContainText('Idle days before a session ends');
    await shot(page, `7-settings-signin-${size}`);
    await open(page, 'kim', '#/app/api');
    await expect(page.locator('qse-settings')).toContainText('Viewers create inference tokens');
    await shot(page, `7-settings-api-${size}`);
    await open(page, 'kim', '#/health');
    const link = page.locator('qse-health [data-finding="tokens_need_review"] [data-act="open-page"]');
    await expect(link).toHaveText('Open All tokens');
    await link.click();
    await at(page, 'tokens').waitFor();
  });
}

test('desktop: the keyboard opens a dialog, moves through it and closes it', async ({ page }) => {
  await page.setViewportSize(SIZES.desktop);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await open(page, 'kim', '#/account/tokens');
  // SRV-889: Create a token opens the smart dialog
  const tk = page.locator('dialog[data-dialog="token"]');
  await page.locator('[data-act="token-new"]').focus();
  await page.keyboard.press('Enter');
  await expect(tk).toBeVisible();
  // the name has the focus; Tab stays inside the dialog
  await expect(tk.locator('input[name="label"]')).toBeFocused();
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => !!document.activeElement?.closest('dialog[open]') || document.activeElement === document.body)).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(tk).toBeHidden();
  // Enter from a field goes on: an empty name is held, a name goes to the next step
  await page.locator('[data-act="token-new"]').click();
  await tk.locator('input[name="label"]').fill('');
  await page.keyboard.press('Enter');
  await expect(tk.locator('[data-dialog-error]')).toHaveText('Give the key a name.');
  await tk.locator('input[name="label"]').fill('by keyboard');
  await page.keyboard.press('Enter');
  await expect(tk.locator('.tk-steps li.is-on')).toContainText('Models');
});
