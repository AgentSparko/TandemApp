// 6.3b (OPS-234, VIS-E19): App settings beyond the controller's host, in the mock fleet: this
// controller's service and release, the sign-in panel (SEC-28: links to the account pages; the
// controller's single admin password is gone), the permissions per
// host (a Mac's Local Network checked and denied with its way around; on a Spark: does not apply),
// the configuration file (Export downloads it, Import shows the plan and applies it: the alias the
// file renamed is the Spark's), and the phone. The host, Move and Take stay as they were. Shots in
// e2e-artifacts/app63b/. Mock tier only.
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { IS_MOCK, setMode } from './helpers';

const SHOTS = 'e2e-artifacts/app63b';

async function fresh(request: APIRequestContext, scenario: string): Promise<void> {
  expect((await request.post('/__mock/fleet', { data: { reset: true, scenario } })).ok()).toBeTruthy();
}
async function open(page: Page, scenario: string, extra = ''): Promise<void> {
  await page.goto(`./?fleet=${scenario}${extra}#/app`);
  await expect(page.locator('qse-app-settings [data-panel="password"]')).toBeVisible({ timeout: 10_000 });
}
const shot = (page: Page, name: string) => page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
const noSideScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);

test.describe('App settings: password, permissions, the configuration file', () => {
  test.skip(!IS_MOCK, 'the fleet is the mock’s');
  test.beforeEach(async ({ request }) => {
    await setMode(request, 'ok');
  });
  test.afterEach(async ({ request }) => {
    await setMode(request, 'ok');
    await fresh(request, 'fleet4');
  });

  test('a Mac controller: every section, the earlier ones kept; Local Network checked and denied', async ({ page, request }, info) => {
    test.skip(info.project.name !== 'chromium-desktop', 'the desk');
    await fresh(request, 'gold3');
    await open(page, 'gold3', '&lan=deny');
    // kept: the host cards, the API URL, Move, the sleep warning
    await expect(page.locator('[data-host="here"]')).toContainText('This Mac');
    await expect(page.locator('[data-api-url] code')).toContainText('/v1');
    await expect(page.locator('[data-act="move"]')).toBeVisible();
    await expect(page.locator('[data-sleep-warning]')).toBeVisible();
    // this controller: launchd, the release, Update fleet
    await expect(page.locator('[data-service="launchd"]')).toContainText('launchd agent');
    await expect(page.locator('[data-panel="service"] [data-act="update-fleet"]')).toHaveAttribute('href', '#/sparks?update=1');
    // the permissions: four rows; Local Network asked, then denied with its way around
    const perms = page.locator('[data-panel="permissions"] [data-perm]');
    await expect(perms).toHaveCount(4);
    const ln = page.locator('[data-perm="local_network"]');
    await expect(ln.locator('.app-state')).toHaveText('Not asked yet');
    await ln.locator('[data-act="perm-local_network"]').click();
    await expect(ln.locator('.app-state')).toHaveText('Denied');
    await expect(ln.locator('[data-fallback]')).toContainText('Run the controller on a Spark');
    await expect(ln).toContainText('System Settings → Privacy & Security → Local Network');
    await expect(page.locator('[data-perm="keys"] [data-file-state="ok"]')).toHaveCount(1);
    await expect(page.locator('[data-perm="node_sudo"]')).toContainText('never stored');
    await shot(page, 'mac-desk');
  });

  test('the sign-in: each account’s, on the account pages; no admin password here', async ({ page, request }, info) => {
    test.skip(info.project.name !== 'chromium-desktop', 'the desk');
    await fresh(request, 'fleet4');
    await open(page, 'fleet4');
    const panel = page.locator('[data-panel="password"]');
    await expect(panel.locator('[data-act="account"]')).toHaveAttribute('href', '#/account');
    await expect(panel.locator('[data-act="access"]')).toHaveAttribute('href', '#/access');
    await expect(panel.locator('input')).toHaveCount(0);
    await shot(page, 'sign-in');
  });

  test('export, then import the file with one Spark renamed: the plan, then the Spark has the name', async ({ page, request }, info) => {
    test.skip(info.project.name !== 'chromium-desktop', 'the desk');
    await fresh(request, 'quad');
    await open(page, 'quad');
    // a Spark: Local Network does not apply
    await expect(page.locator('[data-perm="local_network"] .app-state')).toHaveText('Does not apply');
    await expect(page.locator('[data-service="systemd"]')).toContainText('systemd user unit');
    const [dl] = await Promise.all([page.waitForEvent('download'), page.locator('[data-act="export"]').click()]);
    expect(dl.suggestedFilename()).toMatch(/^tandemapp-fleet-config-\d{4}-\d\d-\d\d\.json$/);
    await expect(page.locator('[data-config-done]')).toContainText('Exported 4 Sparks');
    const doc = JSON.parse(readFileSync((await dl.path())!, 'utf8'));
    expect(doc.secrets).toBe('excluded');
    const target = doc.nodes[1];
    target.alias = 'Renamed';
    await page.locator('[data-in="import-file"]').setInputFiles({ name: 'fleet.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(doc)) });
    const dlg = page.locator('dialog[data-dialog="import"]');
    await expect(dlg.locator('[data-import-summary]')).toHaveText('1 changed');
    await expect(dlg.locator(`[data-import="node:${target.id}"]`)).toHaveAttribute('data-op', 'change');
    await expect(dlg.locator('[data-import-from]')).toContainText('This controller’s own export');
    await expect(dlg.locator('[data-import-identity]')).toContainText('certificate');
    await shot(page, 'import-plan');
    // replace: re-planned
    await dlg.locator('[data-mode="replace"]').check();
    await expect(dlg.locator('[data-import-summary]')).toHaveText('1 changed');
    await dlg.locator('[data-mode="merge"]').check();
    await dlg.locator('[data-act="import-go"]').click();
    await expect(dlg).not.toBeVisible();
    await expect(page.locator('[data-config-done]')).toHaveText('Imported: 1 changed.');
    const nodes = (await (await request.get('/v1/fleet/nodes')).json()).nodes as { id: string; alias: string }[];
    expect(nodes.find((n) => n.id === target.id)?.alias).toBe('Renamed');
    // a file that is not an export is refused before anything is sent
    await page.locator('[data-in="import-file"]').setInputFiles({ name: 'x.json', mimeType: 'application/json', buffer: Buffer.from('{"kind":"tandemapp-export"}') });
    await expect(page.locator('[data-config-error]')).toContainText('Take the fleet');
  });

  test('the phone: one column, no side scroll, the import plan as a sheet', async ({ page, request }, info) => {
    test.skip(info.project.name !== 'webkit-phone', 'the phone layout');
    await fresh(request, 'gold3');
    await open(page, 'gold3');
    expect(await noSideScroll(page)).toBeTruthy();
    const perms = page.locator('[data-perm]');
    const a = await perms.nth(0).boundingBox();
    const b = await perms.nth(1).boundingBox();
    expect(b!.y).toBeGreaterThan(a!.y + a!.height - 1);
    const pwBox = await page.locator('[data-in="pw-next"]').boundingBox();
    expect(pwBox!.width).toBeGreaterThan(150);
    await shot(page, 'mac-phone');
    const doc = (await (await request.get('/v1/fleet/app/export')).json()) as { nodes: { alias: string }[] };
    doc.nodes[0].alias = 'Phone renamed';
    await page.locator('[data-in="import-file"]').setInputFiles({ name: 'fleet.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(doc)) });
    const dlg = page.locator('dialog[data-dialog="import"]');
    await expect(dlg.locator('[data-import-summary]')).toHaveText('1 changed');
    expect(await noSideScroll(page)).toBeTruthy();
    await shot(page, 'import-phone');
    await dlg.locator('[data-act="import-cancel"]').click();
  });
});
