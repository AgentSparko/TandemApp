// VIS-284: pictures of the plugins pages against the mock's demo registry, desktop 1440 px and a
// 360 px phone, dark and (the list) light: the installed list, an install from its form to its
// report (refused, then passed) and its result, a plugin set up through the review with its second
// confirm, its page while it runs with a field's error, its panel in the sandboxed frame with a
// bridge call and a refusal, a plugin the host gave up on, a removal a hook holds up, a panel that
// cannot be shown, an engine-stats package and an upgrade. Into PW_SHOTS (default screenshots/) as
// plugins-*.png. Each page is also checked for sideways scroll at both widths.
import { expect, test, type Page } from '@playwright/test';
import { IS_MOCK, login } from './helpers';

test.skip(!IS_MOCK, 'reads the mock’s demo plugins');

const DIR = process.env.PW_SHOTS ?? 'screenshots';
const SIZES = { desktop: { width: 1440, height: 900 }, 360: { width: 360, height: 740 } } as const;

/** the mock's registry as seeded, whatever a test before left */
async function reset(page: Page): Promise<void> {
  const res = await page.request.post('/__mock/plugins', { data: { seed: 'demo', fail: null, panel: null, sandbox: true, trust: true } });
  expect(res.ok()).toBeTruthy();
}

async function open(page: Page, hash: string, theme: 'dark' | 'light' = 'dark'): Promise<void> {
  await page.emulateMedia({ colorScheme: theme });
  await page.addInitScript((t) => localStorage.setItem('qse.theme', t), theme);
  await login(page, hash, `?t=${Date.now()}`);
  await page.locator('qse-plugins').waitFor();
}

/** the elements that reach past the window's right edge, outside a box that scrolls on purpose
 *  (the settings area's tab strip, a block of code, the log) */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const w = doc.clientWidth;
    const out: string[] = doc.scrollWidth > w + 1 ? [`the document scrolls: ${doc.scrollWidth} > ${w}`] : [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('.content *'))) {
      if (el.closest('.st-tabs') || el.closest('.hk-pre') || el.closest('.plg-log') || el.closest('.plg-err')) continue;
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > w + 1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).replace(/\s+/g, '.')} right=${Math.round(r.right)} width=${Math.round(r.width)} in ${el.parentElement?.className ?? ''}`);
    }
    return out.slice(0, 14);
  });
}

async function shot(page: Page, name: string, o: { window?: boolean } = {}): Promise<void> {
  await page.waitForTimeout(250);
  expect.soft(await overflowing(page), `${name}: nothing reaches past the window`).toEqual([]);
  if (o.window) await page.screenshot({ path: `${DIR}/plugins-${name}.png` });
  else await page.locator('qse-settings').screenshot({ path: `${DIR}/plugins-${name}.png` });
}

for (const size of ['desktop', '360'] as const) {
  test(`${size}: the installed list, dark and light`, async ({ page }) => {
    await page.setViewportSize(SIZES[size]);
    await reset(page);
    await open(page, '#/app/plugins');
    await page.locator('[data-plugin="hello-tool"] [data-state="running"]').waitFor();
    await expect(page.locator('[data-plugin]')).toHaveCount(5);
    await page.screenshot({ path: `${DIR}/plugins-1-list-top-${size}.png` });
    await shot(page, `1-list-${size}`);
    await open(page, '#/app/plugins', 'light');
    await page.locator('[data-plugin="hello-tool"] [data-state="running"]').waitFor();
    await shot(page, `1-list-light-${size}`);
  });

  test(`${size}: an install from its form to its report, set up through the review, then its page and its panel`, async ({ page }) => {
    test.setTimeout(120_000);
    await page.setViewportSize(SIZES[size]);
    await reset(page);
    await open(page, '#/app/plugins/install');
    await page.locator('[data-install-steps]').waitFor();
    await shot(page, `2-install-${size}`);
    // what the form says before anything is sent
    await page.locator('[data-card="git"]').click();
    await page.locator('[data-in="url"]').fill('https://git.example.com/acme/tandem-plugins.git');
    await page.locator('[data-in="commit"]').fill('main');
    await page.locator('[data-act="inspect"]').click();
    await expect(page.locator('[data-field="commit"] [data-invalid]')).toContainText('A branch or a tag can move');
    await shot(page, `2-install-git-errors-${size}`);
    // a refused source: the reasons, and no install button
    await page.locator('[data-card="folder"]').click();
    await page.locator('[data-in="path"]').fill('/srv/plugins/broken-tool');
    await page.locator('[data-act="inspect"]').click();
    await page.locator('[data-inspect-head="refused"]').waitFor();
    await expect(page.locator('[data-act="install-go"]')).toHaveCount(0);
    await expect(page.locator('[data-check="tools"]')).toContainText('The tool greet does not say destructiveHint.');
    await shot(page, `2-install-refused-${size}`);
    // one that passes: the sha256, the level and the capabilities stand before the button
    await page.locator('[data-in="path"]').fill('/srv/plugins/usage-board');
    await page.locator('[data-act="inspect"]').click();
    await page.locator('[data-inspect-head="ok"]').waitFor();
    await expect(page.locator('[data-sha]')).toBeVisible();
    await expect(page.locator('[data-level="sandboxed"]')).toBeVisible();
    await expect(page.locator('[data-step="asks"] [data-capability]')).toHaveCount(4);
    await shot(page, `2-install-report-${size}`);
    await page.locator('[data-act="install-go"]').click();
    await page.locator('[data-installed="usage-board"]').waitFor();
    await shot(page, `2-install-done-${size}`);
    await page.locator('[data-act="install-done"]').click();
    await page.locator('[data-plugin-page="usage-board"]').waitFor();
    await page.locator('[data-setup]').waitFor();
    await shot(page, `3-plugin-new-${size}`);
    // one button stages the switch and every grant; the review asks twice for the risky ones
    await page.locator('[data-act="set-up"]').click();
    await expect(page.locator('[data-bar-count]')).toContainText('5 staged changes');
    await shot(page, `3-plugin-staged-${size}`);
    await page.locator('[data-act="review"]').click();
    await page.locator('[data-plan]').waitFor();
    await shot(page, `4-review-${size}`, { window: true });
    await page.locator('[data-act="sheet-apply"]').click();
    await page.locator('[data-second-plugin]').waitFor();
    await expect(page.locator('[data-second-plugin]')).toContainText('Read every prompt and answer as it is.');
    await shot(page, `4-review-second-${size}`, { window: true });
    await page.locator('[data-act="sheet-apply-second"]').click();
    await page.locator('[data-result="applied"]').waitFor();
    await page.locator('[data-act="sheet-done"]').click();
    await expect(page.locator('[data-panel="status"]')).toHaveAttribute('data-state', 'running', { timeout: 20_000 });
    // a field's error shows under it, and nothing of it is staged
    await page.locator('[data-in="refresh_s"]').fill('9000');
    await expect(page.locator('[data-key="settings.refresh_s"] [data-invalid]')).toContainText('At most 600.');
    await expect(page.locator('[data-bar]')).toHaveCount(0);
    await page.locator('[data-act="item-add"]').click();
    await shot(page, `3-plugin-running-${size}`);
    // its panel: a sandboxed frame, a bridge call answered, another refused
    await page.locator('[data-name="board"] [data-act="panel-open"]').click();
    const frame = page.locator('iframe[data-panel-frame="board"]');
    await frame.waitFor();
    await expect(frame).toHaveAttribute('sandbox', 'allow-scripts allow-forms');
    const inside = page.frameLocator('iframe[data-panel-frame="board"]');
    await inside.locator('button[data-call="plugin.info"]').click();
    await expect(inside.locator('#out')).toContainText('"id": "usage-board"');
    await inside.locator('button[data-call="engine.control"]').click();
    await expect(inside.locator('#out')).toContainText('refused');
    await expect(page.locator('[data-bridge]')).toHaveAttribute('data-calls', '2');
    await shot(page, `5-panel-${size}`);
  });

  test(`${size}: a plugin the host gave up on, a removal a hook holds up, a panel that cannot be shown, an engine-stats package, an upgrade`, async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize(SIZES[size]);
    await reset(page);
    await open(page, '#/app/plugins/p/flaky-export');
    await page.locator('[data-plugin-page="flaky-export"]').waitFor();
    await page.locator('[data-log]').waitFor();
    await shot(page, `6-plugin-failed-${size}`);
    await open(page, '#/app/plugins/p/audit-hook');
    await page.locator('[data-plugin-page="audit-hook"]').waitFor();
    await page.locator('[data-act="remove-ask"]').click();
    await page.locator('[data-act="remove-go"]').click();
    await page.locator('[data-panel="remove"] [data-error="referenced"]').waitFor();
    await shot(page, `7-remove-referenced-${size}`);
    await open(page, '#/app/plugins/p/hello-tool/panel/greeter');
    await page.locator('[data-panel-finding]').waitFor();
    await expect(page.locator('iframe')).toHaveCount(0);
    await shot(page, `5-panel-finding-${size}`);
    await open(page, '#/app/plugins/p/tandemllm-vllm-plugin');
    await page.locator('[data-engine-stats]').waitFor();
    await shot(page, `8-engine-stats-${size}`);
    await open(page, '#/app/plugins/install?upgrade=hello-tool');
    await page.locator('[data-install-steps]').waitFor();
    await page.locator('[data-card="wheel"]').click();
    await page.locator('[data-in="path"]').fill('/srv/dist/hello_tool-0.2.0-py3-none-any.whl');
    await page.locator('[data-act="inspect"]').click();
    await page.locator('[data-move="upgrade"]').waitFor();
    await shot(page, `9-upgrade-${size}`);
  });
}
