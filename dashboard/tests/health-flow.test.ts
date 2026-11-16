// Tests for SRV-835: the health page against the mock controller, without a browser. A page here
// is what the element holds: the flow (src/lib/health-flow.ts) and the templates drawn from it
// (src/views/health/view.ts).
//
//   Gherkin (SRV-835):
//   Stopped engine      "Given a stopped engine, when the user opens Health, then it reads 'The
//                        engine is not running' with a Start button; when the user presses Start,
//                        then the engine starts and the finding clears"
//   Changed host key    "Given a changed host key, then both fingerprints are shown and no button
//                        connects anyway"
//   Token refused       "Given the engine refuses the stored token, when the user presses Rotate,
//                        then a new token is fetched through SSH and the finding clears"
//   Acceptance criteria:
//   each finding exists with its code, one sentence, and its button or the stated absence of one
//   Start, Rotate, Upgrade and retry are tested end to end
//   a changed host key shows both fingerprints and offers no button that connects   (in the Gherkin)
//   a failed fix keeps the finding and shows the new reason
//   findings are ordered so that a blocking one (SSH) is listed before dependent ones
//   no finding or fix output contains a secret
// and the states around them: the verdict, check again and its limit, a card's own check again,
// the sign-in for a read and for a fix, too many fixes, the guides, the dialog of Upgrade, a job
// that is followed, a finding that clears by itself, the refused pairings, the plugins, a read
// that fails, and the narrow layout's rules.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { html } from 'lit';
import { describe, expect, it } from 'vitest';
import { HEALTH_FINDINGS, HEALTH_SCENARIOS, START_S, UPGRADE_IDLE_S } from '../mock/health.ts';
import { FIX_WAIT_MS, FOLLOW_MS, NOTE_KEEP_MS, POLL_MS, REFRESH_GAP_MS } from '../src/lib/health-flow.ts';
import { MENU, crumb, sectionOf } from '../src/lib/menu.ts';
import { parseHash } from '../src/lib/router.ts';
import { healthView } from '../src/views/health/view.ts';
import { text } from './engine-rig.ts';
import { HANDLERS, acts, card, cardText, cards, codes, dialog, page, rig, row, rowText, type HealthRig } from './health-rig.ts';

const SRC = join(__dirname, '..', 'src');
/** the calls a promise started with `void` makes end before the test looks again */
const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0));
/** the page's clock moves a second at a time, as the element's does */
async function seconds(r: HealthRig, n: number): Promise<void> {
  for (let i = 0; i < n; i++) {
    r.wait(1000);
    r.flow.tick();
    await settle();
  }
}
const posts = (r: HealthRig): string[] => r.paths.filter((p) => p.startsWith('POST'));

describe('Gherkin: a health page that says what is wrong and fixes what it can (SRV-835)', () => {
  it('Stopped engine: Health reads "The engine is not running" with a Start button; Start starts the engine and the finding clears', async () => {
    // Given a stopped engine
    const r = rig('mixed');
    // When the user opens Health
    await r.flow.load();
    // Then it reads "The engine is not running" with a Start button
    const t = rowText(r, 'spark-rack', 'engine_down');
    expect(t).toContain('The engine is not running.');
    expect(t).toContain('Start it.');
    expect(acts(t)).toEqual(['start']);
    expect(t).toContain('data-changes=true');
    expect(t).toContain('>Start</button>');
    expect(cardText(r, 'spark-rack')).toContain('Needs a fix');
    // When the user presses Start
    const pressed = r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    // (the call is under way: the row says so and its button waits)
    expect(r.flow.busy['spark-rack']).toMatchObject({ action: 'start' });
    expect(rowText(r, 'spark-rack', 'engine_down')).toContain('data-busy=start');
    expect(rowText(r, 'spark-rack', 'engine_down')).toContain('Starting the engine over SSH, then checking again…');
    // Start pressed twice sends one fix
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    await pressed;
    expect(posts(r)).toEqual(['POST /v1/fleet/health/spark-rack/fix']);
    expect(r.bodies).toEqual([{ action: 'start' }]);
    // Then the engine starts and the finding clears
    expect(codes(r, 'spark-rack')).toEqual(['engine_loading']);
    const after = cardText(r, 'spark-rack');
    expect(after).toContain('data-fixed');
    expect(after).toContain('Started. The engine loads its model now; this page checks again by itself.');
    expect(after).toContain('Before: The engine is not running.');
    expect(after).toContain('Starting');
    expect(r.flow.busy['spark-rack']).toBeUndefined();
    // the page reads the engine host again by itself until it serves
    expect(r.flow.following).toEqual(['spark-rack']);
    await seconds(r, START_S + FOLLOW_MS / 1000);
    expect(codes(r, 'spark-rack')).toEqual([]);
    expect(card(r, 'spark-rack').state).toBe('ok');
    expect(cardText(r, 'spark-rack')).toContain('Works');
    expect(r.paths.filter((p) => p === 'GET /v1/fleet/health/spark-rack?fresh=1').length).toBeGreaterThanOrEqual(2);
    // settled: nothing is asked any more
    await seconds(r, 1);
    expect(r.flow.following).toEqual([]);
  });

  it('Changed host key: both fingerprints are shown and no button connects anyway', async () => {
    // Given a changed host key
    const r = rig('all');
    await r.flow.load();
    const f = row(r, 'spark-moved', 'host_key_changed').finding;
    const t = rowText(r, 'spark-moved', 'host_key_changed');
    // Then both fingerprints are shown
    expect(t).toContain('data-fingerprints');
    expect(t).toContain(`<dt>Pinned at pairing</dt><dd class="num hl-wrap">${f.facts.pinned}</dd>`);
    expect(t).toContain(`<dt>Offered now</dt><dd class="num hl-wrap">${f.facts.offered}</dd>`);
    expect(f.facts.pinned).not.toBe(f.facts.offered);
    expect(t).toContain('TandemApp does not connect to this host while the two differ.');
    // and no button connects anyway: the one button shows how to pair again and changes nothing
    expect(acts(t)).toEqual(['re_pair']);
    expect(t).toContain('data-changes=false');
    expect(t).toContain('>How to pair again</button>');
    expect(t).not.toMatch(/connect anyway/i);
    expect(t).not.toMatch(/data-act="?(retry|start|rotate_token|upgrade)/);
    expect(cardText(r, 'spark-moved')).toContain('Needs a fix');
    // the steps: pair again by code, which proves the new key
    await r.flow.press(row(r, 'spark-moved', 'host_key_changed'), 'spark-moved');
    expect(r.bodies).toEqual([{ action: 're_pair' }]);
    const steps = rowText(r, 'spark-moved', 'host_key_changed');
    expect(steps).toContain('data-guide=add_engine');
    expect(steps.match(/<li>/g)).toHaveLength(3);
    expect(steps).toContain('On the engine host, run `tandem pair`');
    expect(steps).toContain('if they differ, do not pair.');
    expect(acts(steps)).toEqual(['re_pair', 'guide-open']);
    expect(steps).toContain('href=#/add-engine ');
    // the finding is still there: nothing was connected
    expect(codes(r, 'spark-moved')).toEqual(['host_key_changed']);
    // a page that forged another action for it draws none
    r.flow.view!.engines[2].findings[0].action = { id: 'retry', label: 'Connect anyway', changes: true, confirm: false };
    expect(acts(rowText(r, 'spark-moved', 'host_key_changed')).filter((a) => a !== 'guide-open')).toEqual([]);
  });

  it('Token refused: Rotate fetches a new token through SSH and the finding clears', async () => {
    // Given the engine refuses the stored token
    const r = rig('mixed');
    await r.flow.load();
    const t = rowText(r, 'gpu-box', 'token_refused');
    expect(t).toContain('The engine refused TandemApp\'s HTTP token.');
    expect(t).toContain('Rotate the token; TandemApp does it over SSH.');
    expect(acts(t)).toEqual(['rotate_token']);
    expect(t).toContain('>Rotate the token</button>');
    const was = r.flow.view!.engines[2].http!.token.last4;
    // When the user presses Rotate
    const pressed = r.flow.press(row(r, 'gpu-box', 'token_refused'), 'gpu-box');
    expect(rowText(r, 'gpu-box', 'token_refused')).toContain('Fetching a new token over SSH and storing it sealed…');
    await pressed;
    expect(r.bodies).toEqual([{ action: 'rotate_token' }]);
    // Then a new token is fetched through SSH and the finding clears
    expect(codes(r, 'gpu-box')).toEqual([]);
    const now = r.flow.view!.engines[2].http!.token.last4;
    expect(now).not.toBe(was);
    const after = cardText(r, 'gpu-box');
    expect(after).toContain(`A new token is in place. It ends in ${now}.`);
    expect(after).toContain('Works');
    expect(card(r, 'gpu-box').state).toBe('ok');
  });
});

describe('Acceptance criteria (SRV-835)', () => {
  it('each finding exists with its code, one sentence, and its button or the stated absence of one', async () => {
    const drawn = new Set<string>();
    for (const s of HEALTH_SCENARIOS) {
      const r = rig(s, { fleet: s === 'first' ? 'empty' : 'fleet4' });
      await r.flow.load();
      for (const c of cards(r)) {
        for (const x of c.rows) {
          const f = x.finding;
          const t = rowText(r, c.id, f.code);
          const first = c.rows.find((y) => y.finding.code === f.code) === x;
          if (!first) continue;
          drawn.add(f.code);
          expect(t, f.code).toContain(`data-finding=${f.code} data-severity=${f.severity}`);
          expect(t, f.code).toContain(f.sentence);
          expect(t, f.code).toContain(f.fix);
          expect(f.sentence + f.fix, f.code).not.toMatch(/[{}]/);
          const want = HEALTH_FINDINGS[f.code][1];
          // a refused pairing is no target: its way on is Add engine; a host key that does not match gets none;
          // a plugin's finding without a fix links to its page, which the plugins pages (VIS-284) hold
          const button = c.kind === 'pairings' ? (want && f.code !== 'host_key_mismatch' ? ['open-add-engine'] : []) : want ? [want] : c.kind === 'plugins' && f.facts.plugin ? ['open-plugin'] : [];
          if (button[0] === 'open-plugin') expect(t, f.code).toContain(`href=#/app/plugins/p/${encodeURIComponent(String(f.facts.plugin))}`);
          expect(acts(t), f.code).toEqual(button);
        }
      }
    }
    expect([...drawn].sort()).toEqual(Object.keys(HEALTH_FINDINGS).sort());
  });

  it('Start, Rotate, Upgrade and retry run end to end; Upgrade is two calls, the second with the plan’s confirm', async () => {
    const r = rig('mixed');
    await r.flow.load();
    // retry: the backend that was down answers again
    expect(codes(r, 'controller')).toContain('backend_down');
    await r.flow.press(row(r, 'controller', 'backend_down'), 'TandemApp');
    expect(codes(r, 'controller')).not.toContain('backend_down');
    expect(cardText(r, 'controller')).toContain('Checked again: this passes now.');
    // Start and Rotate
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    await r.flow.press(row(r, 'gpu-box', 'token_refused'), 'gpu-box');
    expect([codes(r, 'spark-rack'), codes(r, 'gpu-box')]).toEqual([['engine_loading'], []]);
    // Upgrade: the button opens the dialog, nothing is sent yet
    const before = posts(r).length;
    expect(rowText(r, 'spark-attic', 'engine_too_old')).toContain('>Upgrade…</button>');
    await r.flow.press(row(r, 'spark-attic', 'engine_too_old'), 'spark-attic');
    expect([r.flow.dialog?.phase, posts(r).length]).toEqual(['ask', before]);
    expect(dialog(r)).toContain('Upgrade the engine on spark-attic');
    expect(dialog(r)).toContain('Nothing changes until you confirm that plan.');
    // no release typed and none set on this TandemApp: the controller's refusal, in the dialog
    await r.flow.dialogGo();
    expect([r.flow.dialog?.phase, r.flow.dialog?.error?.code]).toEqual(['ask', 'bad_request']);
    expect(dialog(r)).toContain('ref is the engine release to upgrade to (a tag such as v0.16.0)');
    // the first call: the plan
    r.flow.dialogSet({ ref: ' v0.16.0 ' });
    await r.flow.dialogGo();
    expect(r.bodies.at(-1)).toEqual({ action: 'upgrade', ref: 'v0.16.0' });
    const plan = r.flow.dialog!.plan!;
    expect([r.flow.dialog!.phase, r.flow.dialog!.planRef]).toEqual(['plan', 'v0.16.0']);
    expect(plan.confirm).toMatch(/^[0-9a-f]{16}$/);
    const d = dialog(r);
    expect(d).toContain('data-plan');
    expect(d).toContain('<dt>From</dt><dd><b>0.15.2</b>');
    expect(d).toContain('<dt>To</dt><dd><b>0.16.0</b>');
    expect(d).toContain('data-plan-steps');
    expect(d).toContain('data-plan-warnings');
    expect(d).toContain('>Upgrade when idle</button>');
    // nothing was upgraded by the plan
    expect(codes(r, 'spark-attic')).toEqual(['engine_too_old']);
    r.flow.dialogSet({ when: 'now' });
    expect(dialog(r)).toContain('>Upgrade now</button>');
    r.flow.dialogSet({ when: 'idle' });
    // the second call: the same action with the plan's confirm
    await r.flow.dialogApply();
    expect(r.bodies.at(-1)).toEqual({ action: 'upgrade', confirm: plan.confirm, when: 'idle', ref: 'v0.16.0' });
    expect([r.flow.dialog!.phase, r.flow.dialog!.note?.kind]).toEqual(['done', 'working']);
    expect(dialog(r)).toContain('The upgrade waits until the engine is idle, then runs. This page checks again by itself.');
    expect(dialog(r)).toContain('The upgrade job waits for the engine to be idle.');
    r.flow.dialogClose();
    expect(r.flow.dialog).toBeNull();
    // the row carries the job until the engine host answers with the new version
    expect(rowText(r, 'spark-attic', 'engine_too_old')).toContain('data-note=working');
    await seconds(r, UPGRADE_IDLE_S + 12);
    expect(codes(r, 'spark-attic')).toEqual([]);
    expect(cardText(r, 'spark-attic')).toContain('This passes now.');
    expect(r.flow.view!.engines[3].engine_version).toBe('0.16.0');
    // everything TandemApp could fix is fixed
    expect(r.flow.view!.state).toBe('warning');
    expect(page(r)).toContain('Everything works, four things worse than they should.');
  });

  it('a failed fix keeps the finding and shows the new reason', async () => {
    const r = rig('mixed');
    r.mock.host('spark-rack').startFails = true;
    await r.flow.load();
    expect(rowText(r, 'spark-rack', 'engine_down')).toContain('<dt>It said</dt><dd class="num hl-wrap">it was stopped on purpose</dd>');
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    // the finding stays
    expect(codes(r, 'spark-rack')).toEqual(['engine_down']);
    const t = rowText(r, 'spark-rack', 'engine_down');
    // with the new reason, and what the fix's job said
    expect(t).toContain('<dt>It said</dt><dd class="num hl-wrap">it ended with an error; its log says why</dd>');
    expect(t).not.toContain('it was stopped on purpose');
    expect(t).toContain('data-note=still');
    expect(t).toContain('Start did not fix it. What it says now is above.');
    expect(t).toContain('The start job failed: the engine ended while it loaded the model.');
    // Start is still offered
    expect(acts(t)).toEqual(['start', 'dismiss']);
    // a fix the controller could not run at all says why in the controller's words
    const m = rig('mixed');
    await m.flow.load();
    m.mock.fail('start', 'ssh_unreachable');
    await m.flow.press(row(m, 'spark-rack', 'engine_down'), 'spark-rack');
    const refused = rowText(m, 'spark-rack', 'engine_down');
    expect(refused).toContain('data-note=refused');
    expect(refused).toContain('role=alert');
    expect(refused).toContain('TandemApp cannot reach SSH on 192.0.2.81 port 22.');
    expect(codes(m, 'spark-rack')).toEqual(['engine_down']);
    m.flow.dismiss(row(m, 'spark-rack', 'engine_down').key);
    expect(rowText(m, 'spark-rack', 'engine_down')).not.toContain('data-note');
  });

  it('findings are ordered so that a blocking one (SSH) is listed before dependent ones; with the transport down only the SSH finding appears', async () => {
    const r = rig('ok');
    Object.assign(r.mock.host('spark-desk'), { engine: 'down', api: '2.0' });
    Object.assign(r.mock.host('spark-rack'), { ssh: 'unreachable', http: 'silent', token: 'refused', model: null, api: '1.7' });
    await r.flow.load();
    // what blocks first: the engine's state before its version
    expect(codes(r, 'spark-desk')).toEqual(['engine_down', 'engine_api_unsupported']);
    const t = cardText(r, 'spark-desk');
    expect(t.indexOf('data-finding=engine_down')).toBeLessThan(t.indexOf('data-finding=engine_api_unsupported'));
    // SSH down: no engine finding can be known, so none is shown
    expect(codes(r, 'spark-rack')).toEqual(['ssh_unreachable']);
    const rack = cardText(r, 'spark-rack');
    expect(rack).toContain('<dt>Tried</dt><dd class="num hl-wrap">spark@192.0.2.81 port 22</dd>');
    expect(rack).toContain('its HTTP port 8000 does not answer either');
    expect([...rack.matchAll(/data-check=(\w+) data-ok=(\w+) title/g)].map((m) => `${m[1]}:${m[2]}`)).toEqual(['ssh:false', 'host_key:null', 'engine:null', 'version:null', 'model:null', 'http:null', 'token:null']);
    expect(rack).toContain('No check passed, 6 not run');
    // the trace marks the stop that failed and the ones that did not run
    expect(rack).toContain('<li class="is-error" data-check=ssh ');
    expect(rack).toContain('<li class="is-skip" data-check=token ');
  });

  it('no finding or fix output contains a secret', async () => {
    const r = rig('mixed');
    await r.flow.load();
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    await r.flow.press(row(r, 'gpu-box', 'token_refused'), 'gpu-box');
    await r.flow.press(row(r, 'spark-attic', 'engine_too_old'), 'spark-attic');
    r.flow.dialogSet({ ref: 'v0.16.0' });
    await r.flow.dialogGo();
    await r.flow.dialogApply();
    const all = r.seen.join('\n');
    // a token is an object of hints in every answer, never a string
    expect(all).not.toMatch(/"token"\s*:\s*"/);
    expect(all).not.toMatch(/"(password|private_key|secret|pairing_code)"/);
    for (const m of all.matchAll(/"token":(\{[^}]*\})/g)) expect(Object.keys(JSON.parse(m[1])).sort()).toEqual(['last4', 'rotated_at', 'set', 'sha256_6']);
    // the page shows of a token its last four characters at most
    const shown = page(r) + dialog(r);
    const last4 = r.flow.view!.engines.map((e) => e.http!.token.last4!);
    for (const e of r.flow.view!.engines) expect(shown).not.toContain(e.http!.token.sha256_6!);
    expect(last4.every((x) => x.length === 4)).toBe(true);
    // the page sends nothing but the action, the release, the plan's confirm and when
    for (const b of r.bodies) expect(Object.keys(b as object).every((k) => ['action', 'ref', 'confirm', 'when'].includes(k))).toBe(true);
  });
});

describe('the head: the verdict, and Check again', () => {
  it('the first read shows as checking; then the verdict in words, the roll call and a card a target', async () => {
    const r = rig('mixed');
    const first = page(r);
    expect(first).toContain('data-state=loading');
    expect(first).toContain('Checking…');
    expect(first).toContain('data-loading');
    await r.flow.load();
    const p = page(r);
    expect(p).toContain('data-page="health" data-state=error');
    expect(p).toContain('<span>Two things do not work.</span>');
    expect(p).toContain('Six more work worse than they should. TandemApp can fix three of them itself. Checked: this TandemApp and four engine hosts.');
    expect(p).toContain('Checked just now.');
    expect([...p.matchAll(/data-jump=([\w-]+)/g)].map((m) => m[1])).toEqual(['controller', 'spark-desk', 'spark-rack', 'gpu-box', 'spark-attic', 'plugins', 'pairings']);
    expect([...p.matchAll(/data-card=([\w-]+) data-kind=(\w+) data-state=(\w+)/g)].map((m) => m.slice(1).join(' '))).toEqual([
      'controller controller warning', 'spark-desk engine ok', 'spark-rack engine error', 'gpu-box engine error', 'spark-attic engine warning', 'plugins plugins warning', 'pairings pairings warning',
    ]);
    // a card that works shows no finding, its checks folded
    const desk = cardText(r, 'spark-desk');
    expect(desk).not.toContain('data-finding');
    expect(desk).toContain('<details class="hl-fold" data-checks>');
    expect(desk).toContain('7 checks passed');
    expect(desk).toContain('TandemApp reaches the host over SSH');
    expect(desk).toContain('data-reach');
  });
  it('everything works; and the first run offers Add engine', async () => {
    const ok = rig('ok');
    await ok.flow.load();
    expect(page(ok)).toContain('data-state=ok');
    expect(page(ok)).toContain('<span>Everything works.</span>');
    // three cards need no roll call
    const few = rig('first', { fleet: 'empty' });
    await few.flow.load();
    const p = page(few);
    expect(p).toContain('<span>No engine is connected yet.</span>');
    expect(p).toContain('data-setup=true');
    expect(p).toContain('href="#/add-engine" data-act="setup">Add engine</a>');
    expect(p).not.toContain('data-roll');
    expect(cardText(few, 'controller')).toContain('Waits for an engine');
    // its finding's button shows the steps
    await few.flow.press(row(few, 'controller', 'no_engine'), 'TandemApp');
    const t = rowText(few, 'controller', 'no_engine');
    expect(t).toContain('data-guide=add_engine');
    expect(t.match(/<li>/g)).toHaveLength(2);
    expect(t).toContain('>Open Add engine</a>');
  });
  it(`Check again asks for a fresh check, at most once every ${REFRESH_GAP_MS / 1000} seconds`, async () => {
    const r = rig('ok');
    await r.flow.load();
    r.wait(5000);
    await r.flow.refresh();
    expect(r.paths).toEqual(['GET /v1/fleet/health', 'GET /v1/fleet/health?fresh=1']);
    expect([r.flow.limited, r.flow.refreshLeft()]).toEqual([false, 3]);
    expect(page(r)).toContain('Checked just now. Again in 3 s.');
    // pressed again at once: nothing is asked
    await r.flow.refresh();
    expect(r.paths).toHaveLength(2);
    r.wait(REFRESH_GAP_MS);
    expect(r.flow.refreshLeft()).toBe(0);
    await r.flow.refresh();
    expect(r.paths).toHaveLength(3);
  });
  it('a fresh check that comes too soon after another answers what the controller had, and the page says so', async () => {
    const r = rig('ok');
    await r.flow.load();
    r.wait(1000);
    await r.flow.refresh();
    expect(r.flow.limited).toBe(true);
    expect(r.flow.view!.controller).toMatchObject({ cached: true, limited: true });
    expect(page(r)).toContain('Checked a moment ago: this is that answer.');
    expect(page(r)).toContain('data-limited');
    r.wait(REFRESH_GAP_MS);
    await r.flow.refresh();
    expect(r.flow.limited).toBe(false);
  });
  it('a card’s own Check again reads that target alone; the ages grow on the page’s clock', async () => {
    const r = rig('mixed');
    await r.flow.load();
    r.wait(8000);
    expect(cardText(r, 'spark-rack')).toContain('checked 8 s ago');
    await r.flow.recheck('spark-rack');
    expect(r.paths.at(-1)).toBe('GET /v1/fleet/health/spark-rack?fresh=1');
    expect(cardText(r, 'spark-rack')).toContain('checked just now');
    expect(cardText(r, 'spark-desk')).toContain('checked 8 s ago');
    expect(page(r)).toContain('Checked 8 s ago.');
    // the pairings are no target: their card checks everything again
    await r.flow.recheck(null);
    expect(r.paths.at(-1)).toBe('GET /v1/fleet/health?fresh=1');
  });
  it(`the page reads again by itself every ${POLL_MS / 1000} s: a finding that clears by itself goes`, async () => {
    const r = rig('all');
    r.mock.host('spark-loading').readyAt = r.now() / 1000 + 30;
    await r.flow.load();
    expect(codes(r, 'spark-loading')).toEqual(['engine_loading']);
    expect(cardText(r, 'spark-loading')).toContain('<li class="is-info" data-check=engine ');
    r.wait(POLL_MS * 2);
    await r.flow.load({ quiet: true });
    expect(codes(r, 'spark-loading')).toEqual([]);
    expect(r.flow.refreshing).toBe(false);
  });
});

describe('sign-in and limits', () => {
  it('a read the controller refuses with 401 shows the sign-in, and reads once signed in', async () => {
    const r = rig('mixed');
    r.locked.add('read');
    await r.flow.load();
    expect([r.flow.view, r.flow.signIn, r.flow.error]).toEqual([null, 'read', null]);
    const p = page(r, html`<div data-signin>the sign-in card</div>`);
    expect(p).toContain('data-state=locked');
    expect(p).toContain('Sign in to see the health checks.');
    expect(p).toContain('data-signin');
    expect(p).not.toContain('data-card');
    r.locked.clear();
    await r.flow.signedIn();
    expect(r.flow.signIn).toBeNull();
    expect(page(r)).toContain('Two things do not work.');
  });
  it('a fix whose rights need the admin password: the row says so, the card comes up, and after the sign-in the button works', async () => {
    const r = rig('mixed');
    await r.flow.load();
    // Start needs the Setup Guide's rights; a check again and the guides need a read's
    r.locked.add('setup');
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    expect(r.flow.signIn).toBe('start');
    const t = rowText(r, 'spark-rack', 'engine_down');
    expect(t).toContain('data-note=sign_in');
    expect(t).toContain('TandemApp’s admin password is needed for this.');
    expect(page(r, html`<div data-signin></div>`)).toContain('data-signin');
    expect(codes(r, 'spark-rack')).toEqual(['engine_down']);
    // the reads still work: Check again of a finding is not held back
    await r.flow.press(row(r, 'controller', 'backend_down'), 'TandemApp');
    expect(codes(r, 'controller')).not.toContain('backend_down');
    r.locked.clear();
    await r.flow.signedIn();
    expect(rowText(r, 'spark-rack', 'engine_down')).not.toContain('data-note');
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    expect(codes(r, 'spark-rack')).toEqual(['engine_loading']);
    // pairing's rights for the token and an upgrade, the conductor's writes for chatd
    const m = rig('all');
    await m.flow.load();
    m.locked.add('fleet_trust').add('conductor_write');
    await m.flow.press(row(m, 'gpu-box', 'token_refused'), 'gpu-box');
    expect(m.flow.signIn).toBe('rotate_token');
    await m.flow.press(row(m, 'controller', 'chatd_down'), 'TandemApp');
    expect(m.flow.signIn).toBe('restart_chatd');
    await m.flow.press(row(m, 'spark-old', 'engine_too_old'), 'spark-old');
    m.flow.dialogSet({ ref: 'v0.16.0' });
    await m.flow.dialogGo();
    expect([m.flow.signIn, m.flow.dialog?.phase, m.flow.dialog?.error?.code]).toEqual(['upgrade', 'ask', 'sign_in']);
  });
  it('too many fixes in a minute: the controller’s sentence, the card’s buttons wait a minute, then work again', async () => {
    const r = rig('all');
    await r.flow.load();
    for (let i = 0; i < 6; i++) await r.flow.press(row(r, 'spark-far', 'ssh_unreachable'), 'spark-far');
    expect(rowText(r, 'spark-far', 'ssh_unreachable')).toContain('Checked again: still the same.');
    const sent = posts(r).length;
    await r.flow.press(row(r, 'spark-far', 'ssh_unreachable'), 'spark-far');
    expect(posts(r).length).toBe(sent + 1);
    const t = rowText(r, 'spark-far', 'ssh_unreachable');
    expect(t).toContain('data-note=wait');
    expect(t).toContain('spark-far had too many fixes in the last minute.');
    expect(t).toContain('Wait a minute, then try again.');
    expect(t).toContain('The buttons of this card work again in 60 s.');
    expect(r.flow.waitLeft('spark-far')).toBe(FIX_WAIT_MS / 1000);
    // the button waits: pressing it sends nothing; another card's buttons do not wait
    await r.flow.press(row(r, 'spark-far', 'ssh_unreachable'), 'spark-far');
    expect(posts(r).length).toBe(sent + 1);
    expect(r.flow.waitLeft('spark-odd')).toBe(0);
    await seconds(r, 20);
    expect(rowText(r, 'spark-far', 'ssh_unreachable')).toContain('work again in 40 s.');
    await seconds(r, 41);
    expect(r.flow.waitLeft('spark-far')).toBe(0);
    expect(rowText(r, 'spark-far', 'ssh_unreachable')).not.toContain('data-note=wait');
    await r.flow.press(row(r, 'spark-far', 'ssh_unreachable'), 'spark-far');
    expect(posts(r).length).toBe(sent + 2);
  });
  it('a read that fails keeps what the page had and says what happened; with nothing read yet it says that nothing is known', async () => {
    const r = rig('mixed');
    await r.flow.load();
    r.offline = true;
    r.wait(5000);
    await r.flow.refresh();
    expect(r.flow.error?.code).toBe('offline');
    const p = page(r);
    expect(p).toContain('data-refusal=offline');
    expect(p).toContain('TandemApp did not answer.');
    expect(p).toContain('data-card=spark-rack');
    r.offline = false;
    r.wait(REFRESH_GAP_MS);
    await r.flow.refresh();
    expect(r.flow.error).toBeNull();
    const none = rig('mixed');
    none.offline = true;
    await none.flow.load();
    const n = page(none);
    expect(n).toContain('data-state=unread');
    expect(n).toContain('The health checks did not answer.');
    expect(n).toContain('this is not a finding about an engine');
    expect(n).toContain('data-act="refresh"');
    expect(n).not.toContain('data-card');
  });
  it('a fix the target no longer needs: the controller says so and the card reads what is there now', async () => {
    const r = rig('mixed');
    await r.flow.load();
    // the engine was started somewhere else since the page read
    r.mock.host('spark-rack').engine = 'healthy';
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    await settle();
    expect(r.paths.at(-1)).toBe('GET /v1/fleet/health/spark-rack?fresh=1');
    expect(codes(r, 'spark-rack')).toEqual([]);
  });
});

describe('guides, pairings, plugins, chatd', () => {
  it('a guide is asked once, then shown and hidden in place; no model opens the engine host’s model step', async () => {
    const r = rig('all');
    await r.flow.load();
    const x = () => row(r, 'spark-empty', 'no_model');
    expect(rowText(r, 'spark-empty', 'no_model')).toContain('aria-expanded=false');
    await r.flow.press(x(), 'spark-empty');
    const t = rowText(r, 'spark-empty', 'no_model');
    expect(t).toContain('aria-expanded=true');
    expect(t).toContain('data-guide=model');
    expect(t).toContain('href=#/add-engine/spark-empty ');
    expect(t).toContain('>Open the model step</a>');
    await r.flow.press(x(), 'spark-empty');
    expect(rowText(r, 'spark-empty', 'no_model')).not.toContain('data-guide');
    await r.flow.press(x(), 'spark-empty');
    expect(rowText(r, 'spark-empty', 'no_model')).toContain('data-guide=model');
    expect(posts(r)).toHaveLength(1);
  });
  it('the refused pairing codes stand under Add engine: where, when, and the way on; a host key that does not match gets no button', async () => {
    const r = rig('all');
    await r.flow.load();
    const c = cardText(r, 'pairings');
    expect(c).toContain('>Add engine</h2>');
    expect(c).toContain('Pairing codes an engine host refused in the last 10 minutes');
    expect(codes(r, 'pairings')).toEqual(['host_key_mismatch', 'wrong_code', 'code_spent', 'no_window', 'rate_limited']);
    const wrong = rowText(r, 'pairings', 'wrong_code');
    expect(wrong).toContain('The code does not match: 3 tries left.');
    expect(wrong).toContain('<span class="num hl-wrap">192.0.2.84:8000</span>, refused 2 min ago');
    expect(wrong).toContain('href="#/add-engine" data-act="open-add-engine">Open Add engine</a>');
    const mism = rowText(r, 'pairings', 'host_key_mismatch');
    expect(mism).toContain('<dt>Shown when pairing</dt>');
    expect(mism).toContain('<dt>The engine host has</dt>');
    expect(mism).toContain('Nothing was paired.');
    expect(acts(mism)).toEqual([]);
    expect(acts(rowText(r, 'pairings', 'rate_limited'))).toEqual([]);
    expect(rowText(r, 'pairings', 'rate_limited')).toContain('<dt>Wait</dt><dd class="">30 seconds</dd>');
    // nothing of a pairing is sent to a target
    await r.flow.press(row(r, 'pairings', 'wrong_code'), 'Add engine');
    expect(posts(r)).toEqual([]);
  });
  it('the plugins have their card: each that does not run, worst first, and their check', async () => {
    const r = rig('all');
    await r.flow.load();
    expect(codes(r, 'plugins')).toEqual(['plugin_tampered', 'plugin_failed', 'plugin_no_sandbox', 'plugin_untrusted']);
    const c = cardText(r, 'plugins');
    expect(c).toContain('The plugin calendar changed since its install: tool.py is not the file that was installed');
    expect(c).toContain('Install it again from its source; do not enable a plugin whose files changed under it.');
    // one check is no line of checks: the card's state says it, as bad as the worst plugin
    expect(c).not.toContain('data-trace');
    expect(c).toContain('Needs a fix');
    const ok = rig('ok');
    await ok.flow.load();
    expect(cardText(ok, 'plugins')).toContain('All run');
    expect(cardText(ok, 'plugins')).toContain('Every plugin that is on runs');
  });
  it('Restart chatd: restarted and answering; a restart that fails keeps the finding with the job’s words', async () => {
    const r = rig('all');
    await r.flow.load();
    expect(rowText(r, 'controller', 'chatd_down')).toContain('>Restart chatd</button>');
    const pressed = r.flow.press(row(r, 'controller', 'chatd_down'), 'TandemApp');
    expect(rowText(r, 'controller', 'chatd_down')).toContain('Restarting chatd and waiting for it to answer, up to 45 seconds…');
    await pressed;
    expect(codes(r, 'controller')).not.toContain('chatd_down');
    expect(cardText(r, 'controller')).toContain('Restarted. chatd answers.');
    // the line goes after a while
    await seconds(r, NOTE_KEEP_MS / 1000 + 1);
    expect(cardText(r, 'controller')).not.toContain('Restarted. chatd answers.');
    const f = rig('silent');
    f.mock.controller.restartFails = true;
    await f.flow.load();
    await f.flow.press(row(f, 'controller', 'chatd_silent'), 'TandemApp');
    const t = rowText(f, 'controller', 'chatd_silent');
    expect(t).toContain('Restart chatd did not fix it. What it says now is above.');
    expect(t).toContain('The restart job failed: chatd ended at once: its port 8016 is in use.');
  });
  it('the dialog: Back from the plan, another release; it does not close while a call runs', async () => {
    const r = rig('all');
    await r.flow.load();
    await r.flow.press(row(r, 'spark-old', 'engine_too_old'), 'spark-old');
    r.flow.dialogSet({ ref: 'v0.16.0' });
    const going = r.flow.dialogGo();
    expect(r.flow.dialog?.phase).toBe('planning');
    expect(dialog(r)).toContain('Asking spark-old for its plan…');
    r.flow.dialogClose();
    expect(r.flow.dialog).not.toBeNull();
    await going;
    expect(r.flow.dialog?.phase).toBe('plan');
    r.flow.dialogBack();
    expect([r.flow.dialog?.phase, r.flow.dialog?.plan]).toEqual(['ask', null]);
    r.flow.dialogSet({ ref: 'v0.17.0' });
    await r.flow.dialogGo();
    expect(dialog(r)).toContain('<dt>To</dt><dd><b>0.17.0</b>');
    r.flow.dialogClose();
    expect(r.flow.dialog).toBeNull();
    // closed without the apply: nothing was upgraded
    expect(r.bodies.every((b) => !(b as { confirm?: string }).confirm)).toBe(true);
    expect(codes(r, 'spark-old')).toEqual(['engine_too_old']);
  });
});

describe('where the page lives', () => {
  it('Health is a page of Monitor, at #/health; a target’s id below it opens its card', () => {
    expect(MENU.find((g) => g.label === 'Monitor')!.items.map((i) => i.id)).toContain('health');
    expect([sectionOf('health'), crumb('health')]).toEqual(['health', { group: 'Monitor', label: 'Health' }]);
    expect(parseHash('#/health')).toMatchObject({ view: 'health', path: [] });
    expect(parseHash('#/health/spark-rack').path).toEqual(['spark-rack']);
  });
  it('the card the route names is marked; no other is', async () => {
    const r = rig('mixed');
    await r.flow.load();
    expect(page(r)).not.toContain('data-focus=true');
    const named = text(healthView(r.flow, HANDLERS, { focus: 'gpu-box' }));
    expect(named.match(/data-focus=true/g)).toHaveLength(1);
    expect(named).toContain('data-card=gpu-box data-kind=engine data-state=error data-focus=true');
  });
  it('it is a chunk of its own, and the wizard and the settings area link to it', () => {
    const pages = readFileSync(join(SRC, 'views', 'fleet-pages.ts'), 'utf8');
    expect(pages).toContain("import('./health/area')");
    expect(pages).not.toMatch(/^import .*health/m);
    expect(readFileSync(join(SRC, 'app.ts'), 'utf8')).not.toMatch(/qse-health|health\/area|lib\/health/);
    expect(readFileSync(join(SRC, 'views', 'fleet', 'engine-wizard.ts'), 'utf8')).toMatch(/data-health-link>.*#\/health/s);
    expect(readFileSync(join(SRC, 'views', 'settings', 'area.ts'), 'utf8')).toContain('data-health-link>Something does not work? <a href="#/health">Health</a>');
  });
  it('the page works on a narrow column: the trace takes the row, a finding’s button the full width, nothing is set in a fixed width', () => {
    const css = readFileSync(join(SRC, 'styles', 'health.css'), 'utf8');
    expect(css).toMatch(/@container mon \(max-width: 820px\) \{[^@]*\.hl-trace \{\s*width: 100%;/);
    expect(css).toMatch(/@container mon \(max-width: 620px\) \{[^@]*\.hl-row \{\s*grid-template-columns: 20px minmax\(0, 1fr\);/);
    expect(css).toMatch(/\.hl-act \{\s*flex: 1 1 auto;\s*justify-content: center;\s*min-height: 42px;/);
    expect(css).toMatch(/\.hl-fps > div,\s*\.hl-plan > div \{\s*grid-template-columns: minmax\(0, 1fr\);/);
    // every value that can be long wraps: fingerprints, addresses, paths, commands
    expect(css).toMatch(/\.hl-wrap \{\s*overflow-wrap: anywhere;/);
    expect(css).not.toMatch(/(?<!max-|min-)width: \d{3,}px/);
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
  });
});
