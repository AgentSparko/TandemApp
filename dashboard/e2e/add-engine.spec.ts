// The setup wizard in the mock fleet (VIS-278; SRV-827, SRV-829, SRV-831, SRV-833), a TandemApp
// with no member (`?fleet=empty`) and the engine hosts of mock/enginehosts.ts. The Gherkin of the
// first run (plan 13 items 6 to 10): the first page is the wizard, at Add engine; an advertised
// engine is listed with its name and fingerprint, and an address can be typed (a tailnet one too);
// the code pairs, a wrong one says so and how to get a new one; the other ways (a password once, a
// key line to paste); after pairing the bare engine is preselected and a model shows its download;
// then the first message goes to Chat. Besides: the sign-in on a 401, a host key that does not
// match, the keyboard, the phone and both themes. Mock tier only.
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { IS_MOCK, TOKEN, setMode } from './helpers';

const SHOTS = process.env.PW_SHOTS ?? 'e2e-artifacts/vis278';
const CODE = '482913';
const DESK = '192.0.2.80:8000';
const RACK = '192.0.2.81:8000';
const ATTIC = '192.0.2.82:8000';
const LAB = '192.0.2.84:8000';
const TAILNET = 'gpu-box.tailnet.example';

async function fresh(request: APIRequestContext, lan: 'allow' | 'deny' = 'allow'): Promise<void> {
  expect((await request.post('/__mock/fleet', { data: { reset: true, scenario: 'empty', lan } })).ok()).toBeTruthy();
}

const wizard = (page: Page) => page.locator('qse-engine-wizard');
const atStep = (page: Page, step: string) => expect(wizard(page).locator('.eh-body')).toHaveAttribute('data-at', step, { timeout: 10_000 });
const row = (page: Page, key: string) => page.locator(`.eh-item[data-engine="${key}"]`);
const noSideScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
/** a phone project is any with a narrow window, whatever engine draws it */
const kind = (page: Page) => ((page.viewportSize()?.width ?? 1440) < 700 ? 'phone' : 'desktop');

async function shot(page: Page, name: string): Promise<void> {
  await expect(page.locator('main.content')).not.toHaveClass(/is-entering/, { timeout: 5_000 });
  await page.screenshot({ path: `${SHOTS}/${name}-${kind(page)}.png`, fullPage: true });
}

/** the first open of a TandemApp with no member: the wizard, its list searched */
async function open(page: Page, query = ''): Promise<void> {
  await page.goto(`./?fleet=empty${query}`);
  await expect(page).toHaveURL(/#\/add-engine$/, { timeout: 10_000 });
  await atStep(page, 'find');
}

async function pairDesk(page: Page): Promise<void> {
  await row(page, DESK).locator('button.eh-row').click();
  await atStep(page, 'pair');
  await page.locator('#eh-code').fill(CODE);
  await page.locator('[data-act="pair"]').click();
  await atStep(page, 'after');
}

test.describe('The setup wizard: add an engine host', () => {
  test.skip(!IS_MOCK, 'the engine hosts are the mock’s');
  test.beforeEach(async ({ request }) => {
    await setMode(request, 'ok');
    await fresh(request);
  });
  test.afterEach(async ({ request }) => {
    await setMode(request, 'ok');
    expect((await request.post('/__mock/fleet', { data: { reset: true, scenario: 'fleet4', lan: 'allow' } })).ok()).toBeTruthy();
  });

  test('a TandemApp with no engine opens the wizard at Add engine, with what the LAN advertises', async ({ page }) => {
    await open(page);
    const head = page.locator('.page-head[data-page="add-engine"]');
    await expect(head.locator('h1')).toHaveText('Set up TandemApp');
    await expect(head).toHaveAttribute('data-first-run', 'true');
    await expect(page.locator('.eh-stepper .wz-step.is-on')).toHaveText(/Add engine/);
    await expect(page.locator('.eh-stepper .wz-step')).toHaveCount(4);
    // what to look for on the engine host: the three things its install printed
    for (const id of ['code', 'fingerprint', 'address']) await expect(page.locator(`[data-panel="guide"] [data-slip="${id}"]`)).toBeVisible();
    // each engine with its name, version, address and fingerprint
    await expect(page.locator('.eh-item')).toHaveCount(4);
    await expect(row(page, DESK)).toContainText('spark-desk');
    await expect(row(page, DESK)).toContainText('0.16.0');
    await expect(row(page, DESK)).toContainText(DESK);
    await expect(row(page, DESK).locator('.eh-row-fp')).toHaveText(/^SHA256:[A-Za-z0-9+/]{43}$/);
    await expect(page.locator('[data-found-words]')).toHaveText('4 engines answered.');
    // the address field is there from the start, and the other ways one link away
    await expect(page.locator('[data-in="address"]')).toBeVisible();
    await expect(page.locator('[data-ways] [data-act="other-ways"]')).toBeVisible();
    await expect(page.locator('[data-else] a[href="#/start"]')).toBeVisible();
    await expect(page.locator('[data-else] a[href="#/add-spark"]')).toBeVisible();
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'find');
    // light theme
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    await shot(page, 'find-light');
    // a route given is kept: no wizard in the way
    await page.goto('./?fleet=empty#/app');
    await expect(page).toHaveURL(/#\/app$/);
  });

  test('click the engine, type the code: a wrong code says so, the right one pairs, the bare engine gets a model, Chat gets the message', async ({ page }) => {
    test.setTimeout(120_000);
    await open(page, '&chat=mock');
    const fp = (await row(page, DESK).locator('.eh-row-fp').textContent())!.trim();
    await row(page, DESK).locator('button.eh-row').click();
    await atStep(page, 'pair');
    await expect(page.locator('.eh-stepper .wz-step.is-on')).toHaveText(/Pair/);
    // the fingerprint to compare is the one the list showed; the code field has the focus
    await expect(page.locator('[data-panel="pair"] .eh-fp')).toHaveAttribute('data-fp', fp);
    await expect(page.locator('[data-slip="address"]')).toContainText(DESK);
    await expect(page.locator('#eh-code')).toBeFocused();
    await expect(page.locator('[data-act="pair"]')).toBeDisabled();
    await shot(page, 'pair');
    // a pasted fingerprint is compared
    await page.locator('[data-in="fp-paste"]').fill(`Fingerprint: ${fp}`);
    await expect(page.locator('[data-panel="pair"] .eh-fp')).toHaveAttribute('data-compare', 'same');
    // a wrong code
    await page.locator('#eh-code').fill('000000');
    await page.keyboard.press('Enter');
    const wrong = page.locator('[data-finding="wrong_code"]');
    await expect(wrong).toContainText('The code does not match: 4 tries left.');
    await expect(wrong).toContainText('after five wrong tries run tandem pair there again.');
    await expect(page.locator('[data-tries]')).toHaveText('4 tries left');
    await expect(page.locator('#eh-code')).toHaveValue('');
    await expect(page.locator('#eh-code')).toBeFocused();
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'pair-wrong-code');
    // the right code: paired, the steps after pairing at the engine host's own route
    await page.locator('#eh-code').fill('482 913');
    await page.locator('[data-act="pair"]').click();
    await atStep(page, 'after');
    await expect(page).toHaveURL(/#\/add-engine\/eh-[0-9a-f]{10}$/);
    await expect(page.locator('[data-panel="engine"]')).toContainText('spark-desk is paired');
    await expect(page.locator('[data-panel="engine"]')).toContainText(fp);
    await expect(page.locator('[data-engine-state]')).toHaveText('engine running');
    await expect(page.locator('[data-engine-model]')).toHaveText('none yet');
    // the bare engine preselected; the engine's models; the picked one's plan
    await expect(page.locator('[data-panel="mode"]')).toHaveAttribute('data-mode-choice', 'engine');
    await expect(page.locator('[data-choice="engine"]')).toContainText('preselected');
    await expect(page.locator('.eh-model')).toHaveCount(3);
    await expect(page.locator('.eh-model[data-model="qwen3.8-27b"] input')).toBeChecked();
    await expect(page.locator('[data-plan]')).toHaveText('18.4 GB to download to the engine host.');
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'model');
    // a model the engine cannot take says why; weights on the disk need no download
    await page.locator('.eh-model[data-model="nemotron-3.5-bolt-74b-a9b"]').click();
    await expect(page.locator('[data-finding="setup_refused"]')).toContainText('not enough free disk');
    await expect(page.locator('[data-act="start"]')).toBeDisabled();
    await page.locator('.eh-model[data-model="qwen3.8-flash-next"]').click();
    await expect(page.locator('[data-plan]')).toHaveAttribute('data-plan', 'on-disk');
    await page.locator('.eh-model[data-model="qwen3.8-27b"]').click();
    await expect(page.locator('[data-plan]')).toHaveAttribute('data-plan', 'download');
    // the install: its step, the GB so far on the bar
    await page.locator('[data-act="start"]').click();
    await expect(page.locator('[data-panel="install"]')).toBeVisible();
    await expect(page.locator('[data-mode-result]')).toHaveAttribute('data-mode-result', 'changed');
    await expect(page.locator('.eh-prog-step[data-step="fetch"]')).toHaveText('Download the weights', { timeout: 15_000 });
    await expect(page.locator('[data-bar-text]')).toHaveText(/^\d+\.\d of 18\.4 GB$/);
    await expect(page.locator('[data-panel="install"] [role="progressbar"]')).toHaveAttribute('aria-valuetext', /Download the weights: \d+\.\d of 18\.4 GB/);
    await shot(page, 'download');
    // a reload opens the same step, the download still followed
    await page.reload();
    await atStep(page, 'after');
    await expect(page.locator('[data-panel="install"]')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.page-head[data-page="add-engine"] h1')).toHaveText('Set up TandemApp');
    // its end: the first message
    await expect(wizard(page).locator('.eh-body')).toHaveAttribute('data-at', 'chat', { timeout: 60_000 });
    await expect(page.locator('.eh-stepper .wz-step.is-on')).toHaveText(/First message/);
    await expect(page.locator('[data-panel="first-message"]')).toContainText('serves Qwen3.8-27B');
    await expect(page.locator('[data-engine-model]')).toHaveText('Qwen3.8-27B');
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'first-message');
    await page.locator('[data-in="first"]').fill('Hello from the setup wizard');
    await page.locator('[data-act="send"]').click();
    // Chat takes it: a new chat with the message in it, its own route
    await expect(page).toHaveURL(/#\/chat\/[^?]+$/, { timeout: 15_000 });
    await expect(page.locator('qse-chat')).toContainText('Hello from the setup wizard', { timeout: 15_000 });
    // the next open has a member: no wizard in the way
    await page.goto('./?fleet=empty');
    await expect(page.locator('.shell')).toBeVisible();
    await expect(page).not.toHaveURL(/add-engine/);
  });

  test('five wrong codes spend it: the page says how to get a new one and takes it', async ({ page }) => {
    await open(page);
    await row(page, DESK).locator('button.eh-row').click();
    await atStep(page, 'pair');
    for (let i = 0; i < 5; i++) {
      await page.locator('#eh-code').fill('000000');
      await page.locator('[data-act="pair"]').click();
      await expect(page.locator(i < 4 ? `[data-finding="wrong_code"] >> text=${4 - i} tries left` : '[data-finding="code_spent"]')).toBeVisible();
    }
    const spent = page.locator('[data-finding="code_spent"]');
    await expect(spent).toContainText('This code cannot be used any more');
    await expect(spent).toContainText('Run tandem pair on the engine host again for a new code.');
    await expect(page.locator('#eh-code')).toBeDisabled();
    await shot(page, 'pair-spent');
    await page.locator('[data-act="renew"]').click();
    await expect(page.locator('#eh-code')).toBeEnabled();
    await expect(page.locator('[data-tries]')).toHaveText('5 tries left');
    await page.locator('#eh-code').fill(CODE);
    await page.locator('[data-act="pair"]').click();
    await atStep(page, 'after');
  });

  test('no code showing, an engine too old, a fingerprint that is not the advertised one: each says what to do', async ({ page }) => {
    await open(page);
    // no code showing on the engine host
    await row(page, RACK).locator('button.eh-row').click();
    const none = row(page, RACK).locator('[data-finding="no_window"]');
    await expect(none).toContainText('No pairing code is showing on the engine host.');
    await expect(none).toContainText('Run tandem pair on the engine host and type the code it prints.');
    // another fingerprint than advertised: both shown, nothing pairs
    await row(page, LAB).locator('button.eh-row').click();
    const mism = row(page, LAB).locator('[data-finding="host_key_mismatch"]');
    await expect(mism.locator('.eh-facts dt')).toHaveText(['Shown here', 'The engine host has', 'It said']);
    await expect(mism).toContainText('do not pair');
    await expect(mism.locator('button')).toHaveCount(0);
    await atStep(page, 'find');
    // too old for a code: the other ways
    await row(page, ATTIC).locator('button.eh-row').click();
    await expect(row(page, ATTIC).locator('[data-finding="pairing_unsupported"]')).toContainText('too old to pair by code');
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'find-refusals');
    // `tandem pair` ran on the first one: the same row pairs now
    await none.locator('[data-act="again"]').click();
    await atStep(page, 'pair');
    await expect(page.locator('[data-panel="pair"] h2')).toHaveText('Pair with spark-rack');
    // Cancel goes back; the list is there again
    await page.locator('.eh-foot [data-act="cancel"]').click();
    await atStep(page, 'find');
    await expect(page.locator('.eh-item')).toHaveCount(4);
  });

  test('no engine found: the search is not allowed, and a tailnet address is typed', async ({ page, request }) => {
    await fresh(request, 'deny');
    await open(page, '&lan=deny');
    await expect(page.locator('[data-panel="found"]')).toHaveAttribute('data-lan', 'denied');
    const denied = page.locator('[data-panel="found"] [data-finding="lan_denied"]');
    await expect(denied).toContainText('This TandemApp may not use the local network, so it found no engine.');
    await expect(denied).toContainText("type the engine's address");
    await expect(page.locator('.eh-item')).toHaveCount(0);
    await shot(page, 'find-denied');
    // an address that does not answer says so with its fix
    await page.locator('[data-in="address"]').fill('192.0.2.90');
    await page.locator('[data-act="check"]').click();
    await expect(page.locator('[data-panel="by-address"] [data-finding="unreachable"]')).toContainText('TandemApp cannot reach the engine at 192.0.2.90:8000.');
    // nothing typed says what is missing
    await page.locator('[data-in="address"]').fill('');
    await page.locator('[data-act="check"]').click();
    await expect(page.locator('[data-form-error]')).toContainText('Type the engine’s address');
    // a tailnet name, the Enter key
    await page.locator('[data-in="address"]').fill(TAILNET);
    await page.keyboard.press('Enter');
    await atStep(page, 'pair');
    await expect(page.locator('[data-slip="address"]')).toContainText(`${TAILNET}:8000`);
    await page.locator('#eh-code').fill(CODE);
    await page.keyboard.press('Enter');
    await atStep(page, 'after');
    // it serves a model already: no list, the bare engine accepted, then the first message
    await expect(page.locator('[data-panel="serving"]')).toContainText('It serves Qwen3.8-Flash-Next already');
    await expect(page.locator('[data-panel="models"]')).toHaveCount(0);
    await page.locator('[data-act="accept"]').click();
    await atStep(page, 'chat');
    await expect(page.locator('[data-mode-result]')).toHaveCount(0);
    await expect(page.locator('[data-panel="first-message"]')).toContainText('gpu-box serves Qwen3.8-Flash-Next');
  });

  test('other ways, password once: the fingerprint first, a wrong password says so, the right one pairs and is kept nowhere', async ({ page }) => {
    await open(page);
    await row(page, ATTIC).locator('button.eh-row').click();
    await row(page, ATTIC).locator('[data-act="other-ways"]').click();
    await atStep(page, 'connect');
    await expect(page.locator('.eh-stepper .wz-step.is-on')).toHaveText(/Pair/);
    // the address came along; the focus is on the next field
    await expect(page.locator('[data-in="host"]')).toHaveValue('192.0.2.82');
    await expect(page.locator('[data-in="user"]')).toBeFocused();
    await expect(page.locator('[data-way-btn="password"]')).toHaveAttribute('aria-checked', 'true');
    // nothing typed says what is missing
    await page.locator('[data-act="read"]').click();
    await expect(page.locator('[data-panel="connect"] [data-form-error]')).toContainText('Type the user name');
    await page.locator('[data-in="user"]').fill('spark');
    await page.keyboard.press('Enter');
    const hk = page.locator('.eh-hostkey');
    await expect(hk).toHaveAttribute('data-hostkey', 'new');
    await expect(hk).toContainText('Check the fingerprint of 192.0.2.82');
    await expect(hk.locator('.eh-fp-v')).toContainText(/SHA256:[A-Za-z0-9+/]{43}/);
    await expect(page.locator('[data-act="connect"]')).toBeDisabled();
    await page.locator('[data-in="password"]').fill('not-the-password');
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'other-ways-password');
    await page.locator('[data-act="connect"]').click();
    await expect(page.locator('[data-finding="auth_failed"]')).toContainText('The engine host did not accept that user name and password.');
    // the field is empty again: the password is never read back
    await expect(page.locator('[data-in="password"]')).toHaveValue('');
    await page.locator('[data-in="password"]').fill('spark-pass');
    await page.locator('[data-act="connect"]').click();
    await atStep(page, 'after');
    await expect(page.locator('[data-panel="engine"]')).toContainText('with a password, once');
    expect(await page.content()).not.toContain('spark-pass');
  });

  test('other ways, paste a key: the line to add, the fingerprint confirmed, then the page waits for the first login', async ({ page }) => {
    test.setTimeout(90_000);
    await open(page);
    await page.locator('[data-ways] [data-act="other-ways"]').click();
    await atStep(page, 'connect');
    await expect(page.locator('[data-in="host"]')).toBeFocused();
    await page.locator('[data-way-btn="key"]').click();
    await page.locator('[data-in="host"]').fill('spark@192.0.2.82');
    await page.locator('[data-act="line"]').click();
    const line = page.locator('[data-line]');
    await expect(line).toHaveText(/^command="\/bin\/bash \/home\/spark\/TandemLLM\/bin\/tandem remote --key-id tandemapp-[0-9a-f]{10}",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 \S+ tandemapp-[0-9a-f]{10}$/);
    await expect(page.locator('[data-key-step="line"]')).toContainText('~/.ssh/authorized_keys');
    await expect(page.locator('[data-key-step="line"] [data-copy]')).toHaveCount(2);
    await expect(page.locator('[data-guessed]')).toContainText('/home/spark/TandemLLM');
    // a fingerprint that does not match keeps the button off
    const fp = (await page.locator('.eh-hostkey .eh-fp').getAttribute('data-fp'))!;
    await page.locator('[data-in="fp-paste"]').fill(fp.replace(/.$/, '/'));
    await expect(page.locator('[data-act="confirm-key"]')).toBeDisabled();
    await page.locator('[data-in="fp-paste"]').fill(fp);
    await page.locator('[data-act="confirm-key"]').click();
    // not yet: the controller's reason, and it asks again by itself
    const wait = page.locator('[data-key-step="wait"]');
    await expect(wait.locator('[data-finding="key_not_installed"]')).toContainText("The engine host does not take TandemApp's key yet.");
    await expect(wait.locator('[data-polling]')).toContainText('It asks again every 3 seconds');
    expect(await noSideScroll(page)).toBe(true);
    await shot(page, 'other-ways-key');
    // the mock's user pastes the line a few seconds later: the page goes on by itself
    await expect(wizard(page).locator('.eh-body')).toHaveAttribute('data-at', 'after', { timeout: 30_000 });
    await expect(page.locator('[data-panel="engine"]')).toContainText('with a pasted key');
  });

  test('a host key the fleet pinned differently: both fingerprints, and no button connects anyway', async ({ page }) => {
    await open(page);
    await page.locator('[data-ways] [data-act="other-ways"]').click();
    await page.locator('[data-in="host"]').fill('192.0.2.85');
    await page.locator('[data-in="user"]').fill('spark');
    await page.locator('[data-act="read"]').click();
    const f = page.locator('[data-finding="host_key_changed"]');
    await expect(f.locator('.eh-facts dt')).toHaveText(['Pinned before', 'Offered now']);
    await expect(f).toContainText('otherwise do not connect');
    for (const act of ['connect', 'read', 'confirm-key', 'line']) await expect(page.locator(`[data-act="${act}"]`)).toHaveCount(0);
    await shot(page, 'other-ways-changed');
    await page.locator('[data-act="back"]').click();
    await atStep(page, 'find');
  });

  test('a controller with an admin password: the refusal brings its sign-in, and the step goes on after it', async ({ page, request }) => {
    await setMode(request, 'ok', { fleetPassword: true });
    await page.goto('./?fleet=empty#/add-engine');
    await atStep(page, 'find');
    await expect(page.locator('[data-panel="found"] [data-finding="sign_in"]')).toContainText('TandemApp’s admin password is needed for this.');
    const card = page.locator('qse-engine-wizard qse-setup-signin');
    await expect(card).toBeVisible();
    await shot(page, 'sign-in');
    await card.locator('.signin-input').fill(TOKEN);
    await card.locator('button[type="submit"]').click();
    // signed in: the search runs again by itself
    await expect(page.locator('.eh-item')).toHaveCount(4, { timeout: 10_000 });
    await expect(page.locator('[data-finding="sign_in"]')).toHaveCount(0);
    await expect(card).toHaveCount(0);
    await pairDesk(page);
  });

  test('the keyboard alone: from the list to the code and on', async ({ page, isMobile }) => {
    test.skip(isMobile, 'a keyboard');
    await open(page);
    await row(page, DESK).locator('button.eh-row').focus();
    await page.keyboard.press('Enter');
    await atStep(page, 'pair');
    await expect(page.locator('#eh-code')).toBeFocused();
    await page.keyboard.type(CODE);
    await page.keyboard.press('Enter');
    await atStep(page, 'after');
    // the new step's heading has the focus, so a screen reader says where the page is
    await expect(page.locator('#eh-models-t')).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(page.locator('.eh-model input:checked')).toBeFocused();
  });
});
