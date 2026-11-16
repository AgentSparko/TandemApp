// Tests for VIS-280 (the test matrix of VIS-281): the settings pages' flows against the mock
// controller, without a browser. A page here is what the area holds: the draft, the change-set
// flow and the restart watch on the page's own client, and the templates it draws from them.
//
//   Gherkin (VIS-280):
//   Toggle the conductor on      "Toggle the conductor on: a bare engine ..." (the engine's mode),
//                                and the controller's own conductor switch beside it
//   Scheduled restart is cancelled   "Scheduled restart is cancelled: ..."
//   Toggle off keeps data        "Toggle off keeps data: ..."
// and the states around them: a plan before every apply, the second confirm for a fingerprint key,
// a stale plan, another change set applying, a part that fails, the restore of an applied part,
// a refused plan, the sign-in, every restart end, the forms of each scope, versions and the audit.

import { html } from 'lit';
import { describe, expect, it } from 'vitest';
import type { Send } from '../src/api/engines.ts';
import { settingsApi, type Overview, type PartChange, type Scope } from '../src/api/settings.ts';
import { stagedWords } from '../src/lib/app-parts.ts';
import { Draft } from '../src/lib/settings-draft.ts';
import { ChangeSetFlow, RestartWatch, restartsEngine, restoreOf } from '../src/lib/settings-flow.ts';
import { MODE_KEY, fieldsOf, groupsOf, type Field } from '../src/lib/settings-form.ts';
import { fieldRow, groupPanel, type FormHandlers, type FormView } from '../src/views/settings/form.ts';
import { auditList, versionsPanel, type VersionsState } from '../src/views/settings/history.ts';
import { restartCard, restartLine, runSheet, type SheetHandlers } from '../src/views/settings/sheet.ts';
import { text } from './engine-rig.ts';
import { SECRETS, T0, rig, type Rig } from './settings-rig.ts';

const SHEET: SheetHandlers = { apply() {}, back() {}, close() {}, again() {}, when() {}, note() {}, restore() {}, cancelRestart() {} };
const FORM: FormHandlers = { edit() {}, blur() {}, set() {}, unset() {}, undo() {}, secret() {}, toggle() {} };
const view = (o: Partial<FormView> = {}): FormView => ({ raw: new Map(), invalid: new Map(), secrets: new Set(), open: new Set(), backends: ['engine', 'cloud'], ...o });

interface Page {
  r: Rig;
  draft: Draft;
  flow: ChangeSetFlow;
  watch: RestartWatch;
  overview: Overview;
  /** the restarts the watch saw end */
  ended: string[];
}

async function page(r: Rig = rig()): Promise<Page> {
  const ended: string[] = [];
  return { r, draft: new Draft(), flow: new ChangeSetFlow(r.api, () => undefined, r.now), watch: new RestartWatch(r.api, () => undefined, r.now, (x) => ended.push(x.id)), overview: await r.api.overview(), ended };
}
const fields = async (p: Page, scope: Scope, target = 'controller'): Promise<Field[]> => fieldsOf(await p.r.api.schema(scope, target), await p.r.api.values(scope, target), p.draft.part(scope, target));
const field = async (p: Page, scope: Scope, key: string, target = 'controller'): Promise<Field> => (await fields(p, scope, target)).find((f) => f.key === key)!;
const sheet = (p: Page): string => text(runSheet(p.flow.state, p.overview, p.watch, p.flow.blocked(), SHEET));
const card = (p: Page, id: string): string => text(restartCard(p.watch.get(id)!, p.watch, () => undefined));
const held = async (p: Page, key: string) => (await p.r.api.values('engine', 'eh-desk')).values.find((v) => v.key === key)!;
/** review the draft and apply it, the second confirm included; the restarts go to the watch */
async function applyDraft(p: Page): Promise<void> {
  await p.flow.review(p.draft.changes());
  await p.flow.apply();
  if (p.flow.state.second) await p.flow.apply();
  for (const x of p.flow.state.result?.parts ?? []) if (x.restart) p.watch.add(x.restart);
  if (p.flow.state.result?.state === 'applied') p.draft.clear();
}
const QUEUE: PartChange[] = [{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '32' } }];

describe('the Gherkin scenarios of VIS-280', () => {
  it('Toggle the conductor on: a bare engine, the plan with one restart and its time, then chat mode after one restart', async () => {
    const p = await page();
    // Given a bare engine
    const mode = await field(p, 'engine', MODE_KEY, 'eh-desk');
    expect([mode.value, mode.control, mode.locked, mode.label]).toEqual(['engine', 'choice', false, 'Mode']);
    const row = text(fieldRow(mode, view(), FORM));
    for (const s of ['Bare engine', 'Chat mode', 'The engine runs its own conductor and chat extension in front of its model.', 'restarts the engine']) expect(row).toContain(s);
    // When the user turns the conductor on
    p.draft.set('engine', 'eh-desk', MODE_KEY, 'chat', mode.held);
    const staged = text(fieldRow(await field(p, 'engine', MODE_KEY, 'eh-desk'), view(), FORM));
    expect(staged).toContain('data-staged=set');
    expect(staged).toContain('data-act="undo"');
    await p.flow.review(p.draft.changes());
    // Then the page shows the plan with one restart and its time
    const plan = sheet(p);
    for (const s of ['Review 1 change', 'One engine restart: spark-desk.', 'Engine: spark-desk', 'Restarts the engine on spark-desk, once.', 'When spark-desk restarts', 'Wait at most', 'It waits for the running requests to end, at most 10 minutes. Still busy then: nothing changes.', 'Apply 1 change']) expect(plan).toContain(s);
    expect(plan).toMatch(/QSE_MODE<\/span>\s*<span class="rs-val"><span class="rs-from">engine<\/span>.*<span class="rs-to">chat<\/span>/s);
    expect([restartsEngine(p.flow.state.plan), p.flow.state.plan!.parts[0].restart_needed, p.flow.blocked()]).toEqual([true, true, null]);
    // a plan writes nothing
    expect((await held(p, MODE_KEY)).value).toBe('engine');
    expect((await p.r.api.restarts()).restarts).toEqual([]);
    // And after confirm chat mode is set and the engine restarts once
    await p.flow.apply();
    const part = p.flow.state.result!.parts[0];
    expect([p.flow.state.phase, p.flow.state.result!.state, part.state]).toEqual(['result', 'applied', 'scheduled']);
    p.watch.add(part.restart!);
    const result = sheet(p);
    for (const s of ['Applied. One engine restart is on its way.', 'Applies with the engine’s restart', 'spark-desk restarts when it is idle', '10:00', 'left to wait for idle', 'data-act="restart-cancel"', 'Show it in the history']) expect(result).toContain(s);
    p.r.wait(7000);
    await p.watch.read();
    const acting = card(p, part.restart!.id);
    expect(acting).toContain('spark-desk is restarting');
    expect(acting).not.toContain('data-act="restart-cancel"');
    p.r.wait(8000);
    await p.watch.read();
    expect(card(p, part.restart!.id)).toContain('spark-desk runs the new settings');
    expect(p.ended).toEqual([part.restart!.id]);
    const now = await held(p, MODE_KEY);
    expect([now.value, now.source]).toEqual(['chat', 'managed']);
    // exactly one restart, and the mode is written as a key of its own
    expect((await p.r.api.restarts()).restarts.map((x) => x.state)).toEqual(['done']);
    expect(part.keys).toEqual([MODE_KEY]);
  });

  it('the controller’s own conductor switch: staged, a plan without a restart, on from the next request', async () => {
    const p = await page();
    const f = await field(p, 'app', 'conductor');
    expect([f.control, f.value, f.hints, f.reset, f.label]).toEqual(['switch', 'off', ['Default off', 'As installed'], null, 'Conductor']);
    p.draft.set('app', 'controller', 'conductor', 'on', f.held);
    expect([(await field(p, 'app', 'conductor')).staged, (await field(p, 'app', 'conductor')).value]).toEqual(['set', 'on']);
    expect([stagedWords('set', true), stagedWords('unset', false)]).toEqual(['switches on when applied', 'goes back to the install’s setting (off) when applied']);
    await p.flow.review(p.draft.changes());
    const plan = sheet(p);
    for (const s of ['No restart. Everything applies at once.', 'No restart: it applies at once.', 'Apply 1 change']) expect(plan).toContain(s);
    expect(plan).not.toContain('data-when');
    expect(p.r.parts.on.conductor).toBe(false);
    await p.flow.apply();
    expect([p.flow.state.result!.state, p.r.parts.on.conductor]).toEqual(['applied', true]);
    expect(sheet(p)).toContain('Every part holds now.');
    p.draft.clear();
    const on = await field(p, 'app', 'conductor');
    expect([on.value, on.hints, on.reset]).toEqual(['on', ['Default off', 'Set on this page'], 'Use the install’s setting']);
    // the conductor's documents were kept while it was off, and the page said so
    expect((await p.r.api.schema('conductor')).why).toBeNull();
  });

  it('Scheduled restart is cancelled: the countdown, Cancel, and the gateway serves again', async () => {
    const p = await page();
    p.r.mock.control({ busy_s: 300 });
    p.draft.set('engine', 'eh-desk', 'MAX_QUEUE', '32', await held(p, 'MAX_QUEUE'));
    await applyDraft(p);
    const id = p.flow.state.result!.parts[0].restart!.id;
    // Given a restart scheduled for idle with a countdown
    p.r.wait(18_000);
    await p.watch.read();
    const waiting = card(p, id);
    for (const s of ['spark-desk restarts when it is idle', '09:42', 'left to wait for idle', 'answered “try again in 30 s”', 'Waits at most 10 minutes', 'Cancel the restart', 'data-act="restart-cancel"', 'Engine job ']) expect(waiting).toContain(s);
    // the dock's line keeps the countdown and Cancel in reach wherever the page is scrolled to
    const line = text(restartLine(p.watch.get(id)!, p.watch, () => undefined, () => undefined));
    for (const s of ['spark-desk restarts when it is idle', '09:42', 'left to wait for idle', 'data-act="restart-cancel"', 'data-act="restart-show"']) expect(line).toContain(s);
    // between two reads the page counts on by itself
    p.r.wait(2000);
    expect(card(p, id)).toContain('09:40');
    expect(p.watch.active.map((x) => x.id)).toEqual([id]);
    // while it drains, the gateway answers 503 with Retry-After
    expect(p.r.mock.gateway('engine', p.r.now())).toMatchObject({ status: 503, headers: { 'Retry-After': '30' } });
    // When the user presses cancel
    await p.watch.cancel(id);
    // Then the engine stops draining and the gateway serves again
    expect([p.watch.get(id)!.state, p.watch.get(id)!.draining, p.watch.active, p.watch.refused, p.ended]).toEqual(['cancelled', false, [], null, [id]]);
    expect(p.r.mock.gateway('engine', p.r.now())).toBeNull();
    const after = card(p, id);
    for (const s of ['The restart of spark-desk was cancelled', 'The engine keeps the settings it runs, and its backends serve again.']) expect(after).toContain(s);
    expect(after).not.toContain('data-act="restart-cancel"');
    expect(text(restartLine(p.watch.get(id)!, p.watch, () => undefined, () => undefined))).not.toContain('data-act="restart-cancel"');
    expect((await held(p, 'MAX_QUEUE')).value).toBe('16');
    // a restart that has begun cannot be cancelled: the card says so and shows where it stands
    p.r.mock.control({ busy_s: 1 });
    await p.flow.review(QUEUE);
    await p.flow.apply();
    const late = p.flow.state.result!.parts[0].restart!;
    p.watch.add(late);
    p.r.wait(2000);
    await p.watch.cancel(late.id);
    expect([p.watch.refused!.id, p.watch.refused!.error.type, p.watch.get(late.id)!.state, p.watch.cancelling]).toEqual([late.id, 'too_late', 'restarting', null]);
    expect(card(p, late.id)).toContain('The restart has begun: it can no longer be cancelled.');
  });

  it('Toggle off keeps data: one restart back to the bare engine, the extension and its data stay', async () => {
    const p = await page();
    // Given the conductor is on
    p.draft.set('engine', 'eh-desk', MODE_KEY, 'chat', await held(p, MODE_KEY));
    await applyDraft(p);
    p.r.wait(20_000);
    await p.watch.read();
    const on = await field(p, 'engine', MODE_KEY, 'eh-desk');
    expect([on.value, on.reset]).toEqual(['chat', 'Use the engine’s own value']);
    // When the user turns it off
    expect(text(fieldRow(on, view(), FORM))).toContain('Back to the bare engine, the chat extension and its data stay on the engine host’s disk');
    p.draft.set('engine', 'eh-desk', MODE_KEY, 'engine', on.held);
    await p.flow.review(p.draft.changes());
    const plan = sheet(p);
    for (const s of ['Review 1 change', 'Restarts the engine on spark-desk, once.']) expect(plan).toContain(s);
    expect(plan).toMatch(/<span class="rs-from">chat<\/span>.*<span class="rs-to">engine<\/span>/s);
    await p.flow.apply();
    const res = p.flow.state.result!;
    p.watch.add(res.parts[0].restart!);
    p.r.wait(20_000);
    await p.watch.read();
    // Then the engine restarts once in engine mode
    expect((await p.r.api.restarts()).restarts.filter((x) => x.change_set === res.change_set).map((x) => x.state)).toEqual(['done']);
    expect((await held(p, MODE_KEY)).value).toBe('engine');
    // nothing else was touched: the version before is kept, to go back to
    expect((await p.r.api.versions('engine', 'eh-desk')).versions.find((v) => v.values?.[MODE_KEY] === 'chat')).toBeTruthy();
  });
});

describe('from the plan to the result', () => {
  it('a key that feeds the fingerprint: Continue opens a second confirm, and nothing is sent before it', async () => {
    const p = await page();
    p.draft.set('engine', 'eh-desk', 'QWEN38_PREFIX_CHUNK', '4096', await held(p, 'QWEN38_PREFIX_CHUNK'));
    await p.flow.review(p.draft.changes());
    const plan = sheet(p);
    for (const s of ['saved caches start cold', 'QWEN38_PREFIX_CHUNK feed the disk cache’s fingerprint.', '>Continue<']) expect(plan).toContain(s);
    expect(plan).not.toContain('Apply 1 change');
    await p.flow.apply();
    expect([p.flow.state.phase, p.flow.state.second]).toEqual(['plan', true]);
    expect((await p.r.api.restarts()).restarts).toEqual([]);
    const second = sheet(p);
    for (const s of ['One more confirm', 'Saved caches on spark-desk start cold', 'every saved prefix and every parked chat there misses once', 'Apply, and let the caches start cold', 'Back to the plan']) expect(second).toContain(s);
    p.flow.back();
    expect(p.flow.state.second).toBe(false);
    await p.flow.apply();
    await p.flow.apply();
    expect([p.flow.state.phase, p.flow.state.result!.parts[0].state]).toEqual(['result', 'scheduled']);
  });

  it('the settings moved under the plan: the new plan is shown to be checked, nothing was applied on the old one', async () => {
    const p = await page();
    await p.flow.review(QUEUE);
    const first = p.flow.state.plan!.confirm;
    p.r.mock.control({ move: true });
    await p.flow.apply();
    expect([p.flow.state.phase, p.flow.state.moved, p.flow.state.error!.type, p.flow.state.plan!.confirm === first]).toEqual(['plan', true, 'stale_plan', false]);
    expect(sheet(p)).toContain('The settings changed since this plan was read. This is the plan as it is now. Check it, then apply.');
    expect((await p.r.api.restarts()).restarts).toEqual([]);
    await p.flow.apply();
    expect([p.flow.state.phase, p.flow.state.error]).toEqual(['result', null]);
  });

  it('another change set applies: said so, and Apply works once it is done', async () => {
    const p = await page();
    await p.flow.review(QUEUE);
    p.r.mock.control({ busy: true });
    await p.flow.apply();
    expect([p.flow.state.phase, p.flow.state.error!.type]).toEqual(['plan', 'busy']);
    for (const s of ['Another change set is being applied right now.', 'Wait a moment, then apply again.', 'Apply 1 change']) expect(sheet(p)).toContain(s);
    p.r.mock.control({ busy: false });
    await p.flow.apply();
    expect(p.flow.state.phase).toBe('result');
  });

  it('a change the controller refuses to plan: its sentence, the fix, and the plan read again', async () => {
    const p = await page();
    await p.flow.review([{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: 'many' } }]);
    expect([p.flow.state.phase, p.flow.state.plan, p.flow.state.error!.type, p.flow.blocked()]).toEqual(['plan', null, 'bad_value', 'There is no plan yet.']);
    for (const s of ['This change set cannot be planned', 'Nothing was written.', 'MAX_QUEUE is a whole number', 'Correct the value, then review again.', 'Read the plan again']) expect(sheet(p)).toContain(s);
    // an engine host that does not answer
    p.r.mock.control({ unreachable: 'eh-desk' });
    await p.flow.review(QUEUE);
    for (const s of ['TandemApp could not reach the engine host.', 'Nothing was changed there.']) expect(sheet(p)).toContain(s);
    p.r.mock.control({ unreachable: null });
    await p.flow.again();
    expect(p.flow.state.plan!.changed).toBe(true);
    // what holds already: nothing to apply, and no Apply button
    await p.flow.review([{ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '16' } }]);
    expect([p.flow.state.plan!.changed, p.flow.blocked()]).toEqual([false, 'Nothing would change.']);
    expect(sheet(p)).toContain('Nothing to change');
    expect(sheet(p)).not.toContain('data-act="sheet-apply"');
  });

  it('a part fails: the set stops there, the applied part can be restored, the rest is planned again', async () => {
    const p = await page();
    p.r.mock.control({ hooks: true, fail: 'apply-engine:engine_unreachable' });
    p.draft.set('app', 'controller', 'chat', 'on', { value: 'off', set: false });
    p.draft.set('engine', 'eh-desk', 'MAX_QUEUE', '32', await held(p, 'MAX_QUEUE'));
    p.draft.set('hooks', 'controller', 'hooks.audit.mode', 'enforce', { value: 'observe', set: true });
    await applyDraft(p);
    const res = p.flow.state.result!;
    expect([res.state, res.parts.map((x) => x.state)]).toEqual(['partial', ['applied', 'failed', 'not_run']]);
    // the draft stays after a failure, to be planned again
    expect(p.draft.count()).toBe(3);
    const out = sheet(p);
    for (const s of ['Stopped at engine/eh-desk', 'What was applied before it stays applied and can be restored', 'Failed. Nothing changed here.', 'TandemApp could not reach the engine host.', 'Not run: the set stopped before it', 'data-act="part-restore"', 'Plan it again']) expect(out).toContain(s);
    expect(p.r.parts.on.chat).toBe(true);
    // planned again once the cause is gone: what holds is left alone
    p.r.mock.control({ fail: null });
    await p.flow.again();
    expect(p.flow.state.plan!.parts.map((x) => x.changed)).toEqual([false, true, true]);
    expect(sheet(p)).toContain('Nothing to change: it holds already.');
    // the applied part's restore is a change set of its own, planned before it writes
    const back = restoreOf(res.parts[0]);
    expect(back).toEqual([[{ scope: 'app', target: 'controller', restore: '1' }]]);
    await p.flow.review(back[0]);
    for (const s of ['Review 1 change', 'restores version 1', '<span class="rs-from">on</span>']) expect(sheet(p)).toContain(s);
    await p.flow.apply();
    expect([p.flow.state.result!.state, p.flow.state.result!.parts[0].kind, p.r.parts.on.chat]).toEqual(['applied', 'restore', false]);
    expect(sheet(p)).toContain('Restored');
  });

  it('when it restarts: idle with a wait, a set time (a past one is refused before anything is sent), now', async () => {
    const p = await page();
    await p.flow.review(QUEUE);
    p.flow.setWhen({ mode: 'at', at: '' });
    expect(p.flow.blocked()).toBe('Pick the day and the time.');
    expect(sheet(p)).toContain('data-act="sheet-apply" ?disabled=true');
    await p.flow.apply();
    expect(p.flow.state.phase).toBe('plan');
    p.flow.setWhen({ at: new Date(T0 - 60_000).toISOString() });
    expect(p.flow.blocked()).toBe('That time has passed. Pick a later one.');
    p.flow.setWhen({ at: new Date(T0 + 3_600_000).toISOString(), deadline_s: 1800 });
    p.flow.setNote('  before the evening  ');
    expect(p.flow.blocked()).toBeNull();
    await p.flow.apply();
    const rs = p.flow.state.result!.parts[0].restart!;
    expect([rs.state, rs.when, rs.deadline_s, rs.can_cancel]).toEqual(['scheduled', 'at=2026-11-16T10:00:00Z', 1800, true]);
    p.watch.add(rs);
    const waiting = card(p, rs.id);
    for (const s of ['Nothing is held back yet.', 'Cancel the restart']) expect(waiting).toContain(s);
    expect(waiting).not.toContain('left to wait for idle');
    await p.watch.cancel(rs.id);
    // now: no wait, no deadline
    await p.flow.review(QUEUE);
    p.flow.setWhen({ mode: 'now' });
    expect(sheet(p)).toContain('It restarts at once. Requests that run are cut off.');
    await p.flow.apply();
    expect([p.flow.state.result!.parts[0].restart!.state, p.flow.state.result!.parts[0].restart!.deadline_s]).toEqual(['restarting', null]);
  });

  it('a restart that fails says what the engine runs again; one that times out says nothing changed', async () => {
    const p = await page();
    p.r.mock.control({ restart: 'fail' });
    await p.flow.review(QUEUE);
    await p.flow.apply();
    const failed = p.flow.state.result!.parts[0].restart!;
    p.watch.add(failed);
    p.r.wait(12_000);
    await p.watch.read();
    const bad = card(p, failed.id);
    for (const s of ['spark-desk did not start with the new settings', 'It runs version 1 again and answers.', 'data-restored=1']) expect(bad).toContain(s);
    expect(p.ended).toEqual([failed.id]);
    p.r.mock.control({ restart: 'timeout' });
    await p.flow.review(QUEUE);
    p.flow.setWhen({ deadline_s: 120 });
    await p.flow.apply();
    const slow = p.flow.state.result!.parts[0].restart!;
    p.watch.add(slow);
    p.r.wait(121_000);
    await p.watch.read();
    for (const s of ['spark-desk stayed busy: nothing changed', 'The engine keeps its settings and serves as before.']) expect(card(p, slow.id)).toContain(s);
    expect((await held(p, 'MAX_QUEUE')).value).toBe('16');
  });

  it('a write without the admin session: the refusal asks for the sign-in, and nothing is lost', async () => {
    const r = rig();
    const send: Send = async (m, path, body) => (m === 'POST' ? { status: 401, body: null } : r.send(m, path, body));
    const flow = new ChangeSetFlow(settingsApi(send));
    await flow.review(QUEUE);
    expect([flow.state.error!.type, flow.state.error!.status, flow.state.changes]).toEqual(['unauthorized', 401, QUEUE]);
    const out = text(runSheet(flow.state, null, new RestartWatch(r.api), flow.blocked(), SHEET, html`SIGN-IN`));
    for (const s of ['This needs TandemApp’s admin password.', 'Sign in below, then try again.', 'SIGN-IN']) expect(out).toContain(s);
    // a controller without restarts to tell of: the watch stays quiet
    const watch = new RestartWatch(settingsApi(async () => ({ status: 404, body: { error: { type: 'not_found', message: 'no engine scope here' } } })));
    await watch.read();
    expect([watch.none, watch.list]).toEqual([true, []]);
  });
});

describe('each scope’s form', () => {
  it('backends: a secret is set or removed and never shown; the environment’s lock names its variable; the rules in order', async () => {
    const p = await page();
    const key = await field(p, 'backends', 'backends.cloud.key');
    const stored = text(fieldRow(key, view(), FORM));
    for (const s of ['A key is stored (its SHA-256 starts a1b2c3). It is never shown.', 'Replace the key…', 'Remove the key']) expect(stored).toContain(s);
    expect(stored).not.toContain('type="password"');
    // the field opens to type a new one; staged, the row says so and shows no value
    expect(text(fieldRow(key, view({ secrets: new Set([key.id]) }), FORM))).toContain('type="password"');
    p.draft.set('backends', 'controller', key.key, SECRETS[0], key.held, true);
    const staged = text(fieldRow(await field(p, 'backends', key.key), view(), FORM));
    expect(staged).toContain('A new key is staged. It is sent when the change set is applied.');
    expect(staged).not.toContain(SECRETS[0]);
    await p.flow.review(p.draft.changes());
    const plan = sheet(p);
    for (const s of ['a key is stored', 'a new key']) expect(plan).toContain(s);
    expect(plan).not.toContain(SECRETS[0]);
    await p.flow.apply();
    expect(sheet(p)).not.toContain(SECRETS[0]);
    for (const a of p.r.seen) expect(a.includes(SECRETS[0])).toBe(false);
    // a field the environment sets
    const url = await field(p, 'backends', 'backends.engine.url');
    expect(text(fieldRow(url, view(), FORM))).toContain('Set by TANDEM_BACKENDS in the environment. Change it there.');
    // the routing rules: the environment's rule keeps its place
    const routes = text(fieldRow(await field(p, 'backends', 'routes'), view(), FORM));
    for (const s of ['example-*', 'TANDEM_ROUTE_DEFAULT', 'Add a rule', 'The first rule that matches a model id decides.']) expect(routes).toContain(s);
    const groups = groupsOf('backends', await fields(p, 'backends'));
    expect(groups.map((g) => g.title)).toEqual(['Cloud', 'TandemLLM engine', 'Routing rules', 'Users’ keys']);
    // a fold draws its rows only while it is open
    expect(text(groupPanel(groups[0], view(), FORM, { fold: true }))).not.toContain('data-key=');
    expect(text(groupPanel(groups[0], view({ open: new Set([groups[0].id]) }), FORM, { fold: true }))).toContain('data-key=backends.cloud.url');
  });

  it('conductor: each document whole, stored or built in, a locked profile; an invalid text is said under the field', async () => {
    const p = await page();
    const router = await field(p, 'conductor', 'router');
    expect([router.control, router.hints, router.reset, router.chips]).toEqual(['json', ['Stored, version 1'], 'Back to the built-in one', []]);
    // a document's row needs no sentence that only says its label again
    expect([router.text, (await field(p, 'backends', 'backends.cloud.key')).text, (await field(p, 'app', 'conductor')).text]).toEqual([null, null, 'the conductor in front of the backends']);
    const safe = await field(p, 'conductor', 'profiles.safe');
    expect([safe.locked, safe.why, safe.label]).toEqual([true, 'Locked: it ships with TandemApp.', 'Profile safe']);
    expect((await p.r.api.schema('conductor')).why).toBe('the conductor is off: its documents are kept and read when it is switched on');
    const open = view({ open: new Set([router.id]), raw: new Map([[router.id, '{"rules": ']]), invalid: new Map([[router.id, 'Not valid JSON yet: Unexpected end of JSON input']]) });
    const row = text(fieldRow(router, open, FORM));
    for (const s of ['Edit as JSON', '{"rules": ', 'Not valid JSON yet: Unexpected end of JSON input', 'aria-invalid=true']) expect(row).toContain(s);
  });

  it('engines: a host over SSH is edited, one reached over HTTP only is read-only with the reason', async () => {
    const p = await page();
    const targets = p.overview.scopes.find((s) => s.scope === 'engine')!.targets;
    expect(targets.map((t) => [t.id, t.writable, t.transport])).toEqual([['eh-desk', true, 'ssh'], ['http:bench', false, 'http']]);
    expect(groupsOf('engine', await fields(p, 'engine', 'eh-desk')).map((g) => g.title)).toEqual(['How it runs', 'Applies at once', 'Needs a restart', 'Set on the engine host']);
    const schema = await p.r.api.schema('engine', 'http:bench');
    expect([schema.writable, schema.why]).toEqual([false, 'reached over HTTP only: change its settings on its own host (tandem remote settings), or pair it over SSH']);
    const ro = await fields(p, 'engine', 'http:bench');
    expect(ro.map((f) => [f.key, f.locked, f.value])).toEqual([['QSE_ALLOW_FROM', true, '192.0.2.0/24']]);
    expect(groupsOf('engine', ro).map((g) => g.id)).toEqual(['host']);
    expect(text(fieldRow(ro[0], view(), FORM))).toContain('?readonly=true');
    // an engine's secret is never read into the page
    const token = await field(p, 'engine', 'HF_TOKEN', 'eh-desk');
    expect([token.control, token.value, token.locked]).toEqual(['secret', null, true]);
    expect(text(fieldRow(token, view(), FORM))).not.toContain('data-act="secret-set"');
  });

  it('hooks: no key until a hook registers, then the same form', async () => {
    const p = await page();
    const none = await p.r.api.schema('hooks');
    expect([none.keys, none.why, none.writable]).toEqual([[], 'no hook is registered yet: the hooks scope has no key', true]);
    expect(await fields(p, 'hooks')).toEqual([]);
    p.r.mock.control({ hooks: true });
    const f = await fields(p, 'hooks');
    expect(f.map((x) => [x.label, x.control])).toEqual([['Audit: mode', 'choice'], ['Audit: priority', 'number'], ['Audit: token', 'secret'], ['Guard: on failure', 'choice']]);
    expect(f[3].chips.map((c) => c.text)).toEqual(['restarts the engine']);
    expect(groupsOf('hooks', f).map((g) => g.title)).toEqual(['Hook settings']);
  });
});

describe('what was', () => {
  it('a scope’s versions: what differs from now, a restore staged like any change', async () => {
    const p = await page();
    p.draft.set('app', 'controller', 'conductor', 'on', { value: 'off', set: false });
    await applyDraft(p);
    const state = async (o: Partial<VersionsState> = {}): Promise<VersionsState> => ({ scope: 'app', target: 'controller', view: await p.r.api.versions('app'), error: null, compare: null, values: (await p.r.api.values('app')).values, secrets: new Set(), staged: null, writable: true, ...o });
    const h = { open() {}, compare() {}, restore() {}, undo() {} };
    const list = text(versionsPanel(await state(), h));
    for (const s of ['Versions', '>v2<', '>v1<', 'holds now', 'Compare with now', 'data-act="version-restore"', 'as found before a change', p.flow.state.result!.change_set]) expect(list).toContain(s);
    const diff = text(versionsPanel(await state({ compare: '1' }), h));
    for (const s of ['Hide the difference', 'data-version-diff', '>conductor<', 'Now on the left, this version on the right.']) expect(diff).toContain(s);
    expect(diff).toMatch(/<span class="rs-from" title="now">on<\/span>.*<span class="rs-to" title="in this version">off<\/span>/s);
    // staged: the bar reviews it; the plan says what it brings back
    p.draft.restore('app', 'controller', '1');
    expect(text(versionsPanel(await state({ staged: p.draft.part('app', 'controller')!.restore }), h))).toContain('data-act="version-undo"');
    await p.flow.review(p.draft.changes());
    expect(p.flow.state.plan!.parts[0]).toMatchObject({ kind: 'restore', version: '1', changed: true });
    // not read yet, and none yet
    expect(text(versionsPanel(await state({ view: null }), h))).toContain('skeleton');
    expect(text(versionsPanel({ ...(await state()), scope: 'hooks', view: await p.r.api.versions('hooks') }, h))).toContain('No version yet.');
    // a conductor document's version carries no values: no compare, its plan shows the diff
    const docs = text(versionsPanel({ ...(await state()), scope: 'conductor', view: await p.r.api.versions('conductor'), values: [] }, h));
    expect(docs).toContain('router@1');
    expect(docs).not.toContain('data-act="version-compare"');
    expect(docs).toContain('A document’s restore shows its difference in the plan');
  });

  it('the audit: every part, the set and the restart’s end, one change set’s alone when it is named', async () => {
    const p = await page();
    p.draft.set('app', 'controller', 'conductor', 'on', { value: 'off', set: false });
    p.draft.set('engine', 'eh-desk', 'MAX_QUEUE', '32', await held(p, 'MAX_QUEUE'));
    await applyDraft(p);
    const cs = p.flow.state.result!.change_set;
    p.r.wait(20_000);
    await p.watch.read();
    await p.flow.review([{ scope: 'app', target: 'controller', set: { chat: 'on' } }]);
    await p.flow.apply();
    const all = (await p.r.api.audit()).rows;
    const one = (await p.r.api.audit({ change_set: cs })).rows;
    expect([all.length, one.length, one.every((x) => x.change_set === cs)]).toEqual([6, 4, true]);
    const h = { filter() {} };
    const out = text(auditList(one, null, p.overview, cs, h));
    for (const s of ['What changed', 'Restart ended', 'Engine: spark-desk', 'Change set ended', 'Applied', '>App<', 'MAX_QUEUE', cs, 'Show everything', '>done<', '>scheduled<']) expect(out).toContain(s);
    expect(text(auditList([], null, p.overview, '', h))).toContain('Nothing was changed through these settings yet.');
    expect(text(auditList([], null, p.overview, 'cs-000000000000', h))).toContain('Nothing is recorded for');
    expect(text(auditList(null, null, p.overview, '', h))).toContain('skeleton');
  });
});
