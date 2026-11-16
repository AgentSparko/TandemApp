// Tests for VIS-282 (the test matrix of VIS-283): the hooks pages' flows against the mock
// controller, without a browser. A page here is what the element holds: the two flows on the
// settings area's draft, the change-set flow that applies what they stage, and the templates drawn
// from their state.
//
//   Gherkin (VIS-282):
//   Reorder                    "Reorder: two hooks on one stage, ..."
//   Trace                      "Trace: a request that passed three hooks ..."
//   Replay without bodies      "Replay without bodies: a request recorded at payload level meta ..."
//   Acceptance criteria:
//   the list shows mode, kind, scope, priority, failure policy and breaker state per hook
//   reordering changes priorities and is audited                          (in Reorder)
//   the trace view lists every stage and hook of a request with times and diffs
//   replay and playground work on recorded samples and never send traffic to the engine
//   forms are generated from the hook's schema and show validation errors
//   all pages work at 360 px width                                        (the narrow layout's rules)
// and the states around them: a hook added (the secret once, its verification code), the full
// payload's second question on the hooks' API and in the review, a conflict, the breaker, a
// rotation, a removal, and the decision providers' pages.

import { readFileSync } from 'node:fs';
import { nothing } from 'lit';
import { describe, expect, it } from 'vitest';
import { capUse, pipeline } from '../src/lib/hooks.ts';
import { addView, type AddHandlers } from '../src/views/hooks/add.ts';
import { hooksNav } from '../src/views/hooks/bits.ts';
import { hookGone, hookView, type HookHandlers } from '../src/views/hooks/hook.ts';
import { pipelineView, type PipelineHandlers } from '../src/views/hooks/pipeline.ts';
import { addProviderView, providerGone, providerView, providersView, type ProviderHandlers } from '../src/views/hooks/providers.ts';
import { traceView, type TraceHandlers } from '../src/views/hooks/trace.ts';
import { runSheet, type SheetHandlers } from '../src/views/settings/sheet.ts';
import { text } from './engine-rig.ts';
import { T0, applyStaged, pages, rig, type Pages } from './hooks-rig.ts';

const noop = (): void => undefined;
const PIPE: PipelineHandlers = { mode: noop, move: noop, drag: noop };
const ADD: AddHandlers = { edit: noop, pick: noop, create: noop, unconfirm: noop, lang: noop, done: noop };
const HOOK: HookHandlers = { stage: noop, type: noop, tidy: noop, undo: noop, edit: noop, editField: noop, save: noop, cancelEdit: noop, play: noop, resetEvent: noop, test: noop, secret: noop, rotate: noop, lang: noop, askRemove: noop, remove: noop, reload: noop };
const TRACE: TraceHandlers = { show: noop, reload: noop, replay: noop };
const PROV: ProviderHandlers = { stage: noop, hours: noop, share: noop, undo: noop, question: noop, map: noop, tidy: noop, keyField: noop, key: noop, removeKey: noop, undoKey: noop, probe: noop, edit: noop, editField: noop, save: noop, cancelEdit: noop, setTest: noop, test: noop, reload: noop, askRemove: noop, remove: noop };
const SHEET: SheetHandlers = { apply: noop, back: noop, close: noop, again: noop, when: noop, note: noop, restore: noop, cancelRestart: noop };

const pipe = (p: Pages, writable = true): string => text(pipelineView({ table: p.flow.table!, stops: pipeline(p.flow.table!, p.flow.shown), cap: capUse(p.flow.table!, p.flow.shown), writable, drag: null }, PIPE));
const addPage = (p: Pages): string => text(addView({ state: p.flow.add!, table: p.flow.table!, builtins: p.flow.builtins, here: 0, lang: 'python', signIn: nothing }, ADD));
const hookPage = (p: Pages, id: string, lang: 'python' | 'node' = 'python'): string => text(hookView({ hook: p.flow.hook(id)!, table: p.flow.table!, one: p.flow.one!, writable: true, now: p.r.now(), lang, signIn: nothing }, HOOK));
const tracePage = (p: Pages): string => text(traceView(p.flow.trace, TRACE));
const provList = (p: Pages): string => text(providersView({ info: p.prov.info!, list: p.prov.shown, health: p.prov.health, report: p.prov.report, reportRefused: nothing, hours: p.prov.hours, writable: true }, PROV));
const provPage = (p: Pages, id: string): string => text(providerView({ provider: p.prov.provider(id)!, info: p.prov.info!, list: p.prov.shown, health: p.prov.health[id], report: p.prov.report, reportRefused: nothing, hours: p.prov.hours, one: p.prov.one!, hooks: p.flow.list ?? [], writable: true, signIn: nothing }, PROV));
/** the ids of the hooks of one stop, in the order the page lists them */
const rowsAt = (html: string, stage: string): string[] => [...html.split(`data-stage=${stage} `)[1].split('data-stage=')[0].matchAll(/data-hook=(\S+)/g)].map((m) => m[1]);
const sheet = (p: Pages): string => text(runSheet(p.sheet.state, null, { get: () => undefined, left: () => null, cancelling: null, refused: null }, p.sheet.blocked(), SHEET));

/** what is staged for the hooks scope, key by key */
const sets = (p: Pages): Record<string, unknown> => (p.draft.changes()[0] as { set?: Record<string, unknown> }).set ?? {};

/** a webhook that caps the temperature just before the engine */
async function addCap(p: Pages, o: { priority?: string; endpoint?: string; mode?: 'on' | 'shadow' } = {}): Promise<string> {
  if (!p.flow.table) await p.flow.load();
  p.flow.startAdd('request.before_engine');
  p.flow.editAdd({ name: 'Temperature cap', role: 'transform', endpoint: o.endpoint ?? 'http://hooks.lan:9300/cap', mode: o.mode ?? 'on', priority: o.priority ?? '10' });
  const h = await p.flow.create();
  p.flow.closeAdd();
  p.r.reg.hooks[h!.id].answer = { action: 'modify', patch: { temperature: 0.3 } };
  return h!.id;
}

describe('Gherkin: the hooks pages (VIS-282)', () => {
  it('Reorder: two hooks on one stage, the second moved above the first: the priorities change through the settings interface, are audited, and the trace shows the new order', async () => {
    const p = pages();
    await p.flow.load();
    // Given two hooks on one stage: audit (priority 50) runs before export (200) when a request is finished
    expect(rowsAt(pipe(p), 'request.finished')).toEqual(['audit', 'export']);
    const before = p.r.reg.request(p.r.now());
    await p.flow.readTrace(before);
    expect(p.flow.trace.trace!.stages.find((s) => s.stage === 'request.finished')!.calls.map((c) => c.hook)).toEqual(['audit', 'export']);
    // When the user moves the second above the first
    const next = p.flow.move('request.finished', 'observer', 1, 0);
    // Then the priorities change: staged as a key of the settings interface, nothing written yet
    expect(next).toEqual({ export: 40 });
    expect(p.draft.changes()).toEqual([{ scope: 'hooks', target: 'controller', set: { 'hooks.export.priority': 40 } }]);
    expect((await p.r.hooks.get('export')).priority).toBe(200);
    const staged = pipe(p);
    expect(rowsAt(staged, 'request.finished')).toEqual(['export', 'audit']);
    expect(staged).toContain('data-hook=export data-mode=shadow data-priority=40');
    expect(staged.split('data-hook=export')[2]).toContain('>changed<');
    // a hook has one priority: it moved at its other stop too, and the page says so
    expect(staged).toContain('it moves at every stop it runs at');
    await applyStaged(p);
    expect([p.sheet.state.result!.state, (await p.r.hooks.get('export')).priority, p.draft.empty]).toEqual(['applied', 40, true]);
    expect(rowsAt(pipe(p), 'request.finished')).toEqual(['export', 'audit']);
    // and is audited: the change set's rows name the key, and it is a version that can be restored
    const audit = (await p.r.settings.audit({ change_set: p.sheet.state.result!.change_set })).rows;
    expect(audit.map((r) => [r.action, r.scope, r.keys, r.result])).toEqual([['settings.change_set', null, ['hooks.export.priority'], 'applied'], ['settings.apply', 'hooks', ['hooks.export.priority'], 'applied']]);
    expect((await p.r.settings.versions('hooks')).versions[0]).toMatchObject({ current: true, keys: ['hooks.export.priority'] });
    // and the trace shows the new order: the next request passes export first
    p.r.wait(5000);
    const after = p.r.reg.request(p.r.now());
    await p.flow.readTrace(after);
    expect(p.flow.trace.trace!.stages.find((s) => s.stage === 'request.finished')!.calls.map((c) => c.hook)).toEqual(['export', 'audit']);
    const shown = tracePage(p);
    expect(shown.indexOf('data-trace-hook=export')).toBeLessThan(shown.indexOf('data-trace-hook=audit'));
  });

  it('Reorder: the arrows say what a move does, the ends cannot move further, and a move back leaves no change', async () => {
    const p = pages();
    await p.flow.load();
    const html = pipe(p);
    expect(html).toContain('aria-label=Move Export to the archive up: it then runs before Audit log');
    expect(html).toContain('aria-label=Audit log runs first already');
    expect(html).toContain('aria-label=Export to the archive runs last already');
    // a stop with one hook has no order to change
    expect(html.split('data-stage=request.received ')[1].split('data-stage=')[0]).not.toContain('data-act="move-up"');
    p.flow.move('request.finished', 'observer', 1, 0);
    expect(p.draft.count()).toBe(1);
    p.flow.move('request.finished', 'observer', 0, 1);
    expect(p.flow.shown.find((h) => h.id === 'export')!.priority).toBeGreaterThan(50);
    expect(rowsAt(pipe(p), 'request.finished')).toEqual(['audit', 'export']);
    // without the settings interface nothing can be moved or switched
    const locked = pipe(p, false);
    expect(locked).toContain('data-readonly');
    expect(locked).not.toContain('draggable=true');
  });

  it('Trace: a request that passed three hooks lists each stage, hook, time and result', async () => {
    const p = pages();
    await addCap(p);
    // Given a request that passed three hooks: the cap before the engine, audit and export at its end
    const id = p.r.reg.request(p.r.now());
    // When the user opens its trace
    await p.flow.readTrace(id);
    const t = p.flow.trace.trace!;
    // the open request is read from the controller's trace of it (SRV-849)
    expect(p.r.paths).toContain(`GET /v1/fleet/hooks/calls?request=${id}`);
    // Then each stage, hook, time and result is listed
    expect(t.stages.map((s) => [s.stage, s.calls.map((c) => c.hook)])).toEqual([['request.before_engine', ['temperature-cap']], ['response.after_engine', ['export']], ['request.finished', ['audit', 'export']]]);
    expect([t.calls, t.hooks.length, t.refused]).toEqual([4, 3, null]);
    const html = tracePage(p);
    for (const s of ['Just before the engine', 'request.before_engine', 'The whole answer', 'The request is finished', 'Temperature cap', 'Audit log', 'Export to the archive', 'changed temperature', '4 calls of 3 hooks at 3 stops']) expect(html, s).toContain(s);
    for (const c of t.stages.flatMap((s) => s.calls)) expect(html).toContain(`${c.duration_ms < 10 ? c.duration_ms.toFixed(1) : Math.round(c.duration_ms)} ms`);
    expect(html).toContain(`The hooks on its path took ${t.path_ms < 10 ? t.path_ms.toFixed(1) : Math.round(t.path_ms)} ms.`);
    // the request is in the list to pick from, marked as the one that is open
    expect(html).toContain(`data-recent-request=${id}`);
    expect(p.flow.trace.recent!.map((r) => [r.request, r.calls, r.hooks])).toEqual([[id, 4, 3]]);
  });

  it('the trace view lists every stage and hook of a request with times and diffs: a shadow call shows what it would have changed, as field names and sizes', async () => {
    const p = pages();
    const cap = await addCap(p, { mode: 'shadow' });
    p.r.reg.hooks[cap].answer = { action: 'modify', patch: { temperature: 0.3, messages: [{ role: 'user', content: 'a secret sentence' }], seed: 7, stop: null, top_p: 0.9, top_k: 40, min_p: 0.1, max_tokens: 64 } };
    const id = p.r.reg.request(p.r.now());
    await p.flow.readTrace(id);
    const html = tracePage(p);
    expect(html).toContain('would change max_tokens, messages, min_p, seed, stop, temperature, top_k, top_p');
    for (const s of ['<span class="num">messages</span> <span class="muted">replaced, 47 B</span>', '<span class="num">seed</span> <span class="muted">added, 1 B</span>', '<span class="num">stop</span> <span class="muted">removed</span>']) expect(html).toContain(s);
    // a large diff is folded: six fields show, the rest opens
    expect(html).toContain('data-diff=8');
    expect(html).toContain('and 2 more fields');
    // never a value: neither the journal nor the page holds the text
    expect(html).not.toContain('a secret sentence');
    expect(p.r.seen.join('')).not.toContain('a secret sentence');
    // a request a guard refused says so; one that is too long ago says why nothing shows
    const refused = p.r.reg.request(p.r.now(), { model: 'big-1', user: 'guest-7' });
    await p.flow.readTrace(refused);
    expect(tracePage(p)).toContain('No big model for guests refused this request at A request arrives: ask the admin for this model. The client got 403 hook_denied.');
    expect(p.flow.trace.recent!.find((r) => r.request === refused)!.refused).toBe(true);
    await p.flow.readTrace('chatcmpl-px-000000000000000000000000');
    const none = tracePage(p);
    expect(none).toContain('No hook call is kept for this request');
    expect(none).toContain('keeps the last 500 calls of each hook in memory');
    expect(none).not.toContain('data-panel="replay"');
  });

  it('Replay without bodies: a request recorded at payload level meta: the page says the body was not kept and offers the playground instead', async () => {
    const p = pages();
    await p.flow.load();
    // Given a request recorded at payload level meta
    await p.r.settings.apply([{ scope: 'hooks', set: { 'hooks.export.payload': 'meta' } }], { confirm: (await p.r.settings.plan([{ scope: 'hooks', set: { 'hooks.export.payload': 'meta' } }])).confirm! });
    const id = p.r.reg.request(p.r.now());
    await p.flow.readTrace(id);
    const closed = tracePage(p);
    expect(closed).toContain('data-replay-available="false"');
    expect(closed).toContain('data-act="replay-start"');
    expect(closed).not.toContain('data-replay-note');
    // When the user starts a replay
    const asked = p.r.paths.length;
    p.flow.openReplay(true);
    // Then the page says the body was not kept and offers the playground instead
    const html = tracePage(p);
    expect(html).toContain('This request cannot be replayed');
    expect(html).toContain('The body of this request was not kept. Its hooks ran at the payload level meta: they got ids, sizes and counts, never the text.');
    expect(html).toContain('This TandemApp has no replay yet either, so nothing was run and nothing went to the engine.');
    for (const s of ['data-act="replay-playground" data-hook=audit data-stage=request.finished', 'data-act="replay-playground" data-hook=export data-stage=response.after_engine', '#/app/hooks/hook/export?test=response.after_engine']) expect(html).toContain(s);
    // nothing was sent anywhere: a replay that cannot run runs nothing
    expect(p.r.paths.length).toBe(asked);
    // the offer leads to that hook's playground at that stage
    await p.flow.open('export', 'response.after_engine');
    expect(p.flow.one!.play.stage).toBe('response.after_engine');
    expect(hookPage(p, 'export')).toContain('id="hk-playground"');
  });
});

describe('the pipeline', () => {
  it('the list shows mode, kind, scope, priority, failure policy and breaker state per hook', async () => {
    const p = pages();
    const cap = await addCap(p);
    p.r.reg.control({ behave: { [cap]: 'timeout' } });
    for (let i = 0; i < 5; i++) p.r.reg.request(p.r.now());
    await p.flow.load();
    const html = pipe(p);
    const row = (id: string) => html.split(`data-hook=${id} `)[1].split('</li>')[0];
    const facts = (id: string) => Object.fromEntries([...row(id).matchAll(/data-fact="(\w+)">(.*?)<\/span>(?=\s*(?:<span|<\/p>|\$|$))/gs)].map((m) => [m[1], m[2].replace(/<[^>]+>/g, '')]));
    expect(row('guard')).toContain('data-mode=on data-priority=10 data-breaker=closed');
    expect(facts('guard')).toMatchObject({ kind: 'built-in deny', scope: 'runs on models big-*; users guest*', priority: 'priority 10', policy: 'fails closed' });
    expect(facts('export')).toMatchObject({ kind: 'webhook to archive.example.com', scope: 'runs on every request', priority: 'priority 200', policy: 'fails open' });
    expect(row('export')).toContain('data-mode=shadow');
    expect(facts('audit')).toMatchObject({ kind: 'webhook to audit.lan', policy: 'failures dropped' });
    // the mode is the row's one control, the one that holds pressed
    expect(row('export')).toMatch(/aria-pressed=true[^>]*data-mode-to=shadow/);
    expect(row('guard')).toMatch(/aria-pressed=true[^>]*data-mode-to=on/);
    // the breaker that opened after five timeouts shows on its row, with what it means
    expect(row(cap)).toContain('data-breaker=open');
    expect(row(cap)).toContain('>breaker open<');
    expect(row(cap)).toMatch(/It failed too often and is skipped for \d+ s more/);
    expect(row('guard')).toContain('>breaker closed<');
  });

  it('every stage of the path is a stop in order, with what a hook may do there and its budget; a stop the controller does not call yet says so', async () => {
    const p = pages(rig({ hooks: false }));
    await p.flow.load();
    const html = pipe(p);
    const stages = Object.keys(p.flow.table!.stages);
    expect([...html.matchAll(/data-stage=(\S+) data-hooks/g)].map((m) => m[1])).toEqual(stages);
    expect(html).toContain('The 15 stops a request can make, in order.');
    for (const s of ['A request arrives', 'After auth, before routing.', 'A hook may change it, refuse it.', '150 ms shared', 'beside the request', 'data-stage=tool.before_exec data-hooks=0 data-wired=false']) expect(html, s).toContain(s);
    expect(html.match(/>not called yet</g)).toHaveLength(stages.filter((s) => !p.flow.table!.stages[s].wired).length);
    // nothing registered: the path is still there, with a way in at every stop
    expect(html).toContain('data-hooks-none');
    expect(html).toContain('No hook runs on a request’s path. TandemApp relays every request byte for byte');
    expect(html.match(/data-act="add-here"/g)).toHaveLength(15);
    expect(html).toContain('#/app/hooks/new?stage=route.select');
    expect(text(hooksNav('pipeline', { hooks: 0 }))).toContain('data-act="hook-new"');
    expect(text(hooksNav('providers'))).toContain('data-act="provider-new"');
  });

  it('the time budgets: what the hooks on the path use before the first token, and at their stop', async () => {
    const p = pages();
    const cap = await addCap(p);
    for (let i = 0; i < 20; i++) p.r.reg.request(p.r.now() + i * 100);
    await p.flow.load();
    const p95 = p.flow.hook(cap)!.metrics.p95_ms!;
    const html = pipe(p);
    expect(html).toContain(`data-use=${p95} data-budget=800`);
    expect(html).toContain(`data-use=${p95} data-budget=250`);
    expect(html).toContain('Each hook’s p95, added up over the stages that count.');
    expect(html).toContain('data-cap data-over=false');
    // staged into shadow it uses none of it; the guard that is left on the path was never called
    p.flow.setMode(cap, 'shadow');
    const quiet = pipe(p);
    expect(quiet).toContain('data-use=0 data-budget=800');
    expect(quiet).toContain('One hook has no call measured yet.');
    expect(quiet).not.toContain('hk-use-fill');
  });

  it('the row’s control stages the mode; switching on a hook that is switched off altogether switches it on too', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.setMode('export', 'on');
    p.flow.setMode('audit', 'off');
    expect(p.draft.changes()[0]).toMatchObject({ set: { 'hooks.export.mode': 'on', 'hooks.audit.mode': 'off' } });
    await applyStaged(p);
    expect((await p.r.hooks.list()).hooks.map((h) => [h.id, h.mode])).toEqual([['audit', 'off'], ['export', 'on'], ['guard', 'on']]);
    await p.r.settings.apply([{ scope: 'hooks', set: { 'hooks.guard.enabled': false } }], { confirm: (await p.r.settings.plan([{ scope: 'hooks', set: { 'hooks.guard.enabled': false } }])).confirm! });
    await p.flow.load();
    expect(pipe(p).split('data-hook=guard ')[1]).toMatch(/^data-mode=off/);
    p.flow.setMode('guard', 'shadow');
    expect(sets(p)).toEqual({ 'hooks.guard.mode': 'shadow', 'hooks.guard.enabled': true });
  });

  it('a controller that refuses the list shows why, and the page reads again', async () => {
    const p = pages();
    p.r.reg.control({ fail: 'list:unavailable' });
    await p.flow.load();
    expect([p.flow.list, p.flow.refused!.type]).toEqual([null, 'unavailable']);
    p.r.reg.control({ fail: null });
    await p.flow.load();
    expect([p.flow.list!.length, p.flow.refused]).toEqual([3, null]);
  });
});

describe('add a hook', () => {
  it('forms are generated from the hook’s schema and show validation errors', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.startAdd();
    let html = addPage(p);
    // the stops, roles, kinds and payload levels are the stage table's own
    expect([...html.matchAll(/data-stage-pick=(\S+)>/g)].map((m) => m[1])).toEqual(Object.keys(p.flow.table!.stages));
    expect(html).toContain('data-need-stage');
    // a stop that is chosen stands alone; the list comes back when another is wanted
    p.flow.editAdd({ stage: 'route.select' });
    html = addPage(p);
    expect(html).toContain('data-stage-chosen=route.select');
    expect(html).not.toContain('data-stage-pick=');
    p.flow.pickStage();
    expect(addPage(p)).toContain('data-stage-pick=route.select');
    p.flow.editAdd({ stage: 'route.select' });
    html = addPage(p);
    expect([...html.matchAll(/data-card=(\w+)>/g)].map((m) => m[1]).filter((c) => ['transform', 'guard', 'decider', 'observer'].includes(c))).toEqual(['decider', 'observer']);
    expect([...html.split('data-cards=payload')[1].matchAll(/data-card=(\w+)>/g)].map((m) => m[1]).slice(0, 3)).toEqual(p.flow.table!.payloads);
    expect(html).toContain('MCP, In-process: not in this version.');
    // each payload level says in plain words what leaves TandemApp
    for (const s of ['Facts only', 'Never a word of a prompt or an answer.', 'Text with secrets removed', 'replaced by [redacted:kind]', 'Everything as written', 'A destination outside the LAN asks a second time']) expect(html).toContain(s);
    // nothing is refused until the user tries; then every field says what is wrong with it
    expect(html).not.toContain('data-invalid');
    expect(await p.flow.create()).toBeNull();
    html = addPage(p);
    expect(Object.keys(p.flow.add!.errors).sort()).toEqual(['endpoint', 'name', 'role']);
    for (const s of ['Pick what it does there.', 'The URL TandemApp calls.', 'Give it a name.', '3 things need a correction above.']) expect(html).toContain(s);
    expect(p.r.paths.some((x) => x.startsWith('POST'))).toBe(false);
    // an error leaves as its field is put right
    p.flow.editAdd({ role: 'decider', name: 'Vision to the cloud' });
    expect(Object.keys(p.flow.add!.errors)).toEqual(['endpoint']);
    // a built-in: only the ones that fit the role at this stop, with their own options
    p.flow.editAdd({ kind: 'builtin' });
    expect([p.flow.add!.form.target, p.flow.add!.errors]).toEqual(['route', {}]);
    html = addPage(p);
    expect(html).toContain('Sends the requests in its scope to a backend, a model or a profile.');
    for (const f of ['options.backend', 'options.model', 'options.profile']) expect(html).toContain(`data-field=${f}`);
    // a stop that takes another role: the role and the built-in that no longer fit are dropped
    p.flow.editAdd({ stage: 'request.received' });
    expect([p.flow.add!.form.role, p.flow.add!.form.target]).toEqual(['', '']);
    p.flow.editAdd({ role: 'guard' });
    expect(p.flow.add!.form.target).toBe('deny');
    p.flow.editAdd({ options: { reason: 'ask the admin' }, scope: { ...p.flow.add!.form.scope, users: 'guest*' }, timeout: '9999' });
    expect(await p.flow.create()).toBeNull();
    expect(addPage(p)).toContain('At most the stage’s budget, 150 ms.');
    p.flow.editAdd({ timeout: '' });
    const h = await p.flow.create();
    expect([h!.id, h!.kind, h!.target, h!.options, h!.scope, h!.mode, h!.role]).toEqual(['vision-to-the-cloud', 'builtin', 'deny', { reason: 'ask the admin' }, { users: ['guest*'] }, 'shadow', 'guard']);
    // a built-in has no secret to show
    const made = addPage(p);
    expect(made).toContain('data-created=vision-to-the-cloud');
    expect(made).toContain('data-no-secret');
    expect(made).not.toContain('data-secret-once');
  });

  it('what the controller refuses lands on its field', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.startAdd('request.before_engine');
    p.flow.editAdd({ name: 'Cap', role: 'transform', endpoint: 'http://hooks.lan/cap#v2' });
    expect(await p.flow.create()).toBeNull();
    expect([p.flow.add!.refused!.type, p.flow.add!.errors.endpoint]).toEqual(['invalid', 'endpoint: no #fragment in the URL']);
    const html = addPage(p);
    expect(html).toContain('The URL is not accepted.');
    expect(html).toContain('data-field=endpoint');
    p.r.reg.control({ fail: 'create:kind_unavailable' });
    p.flow.editAdd({ endpoint: 'http://hooks.lan/' });
    await p.flow.create();
    expect(addPage(p)).toContain('That kind of hook does not run in this version.');
  });

  it('a webhook’s secret is shown once, with copy and the code that checks a signature', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.startAdd('request.before_engine');
    p.flow.editAdd({ name: 'Temperature cap', role: 'transform', endpoint: 'http://hooks.lan:9300/cap', scope: { ...p.flow.add!.form.scope, models: 'qwen*' } });
    const h = await p.flow.create();
    const secret = p.flow.add!.created!.secret!;
    expect([h!.id, h!.mode, secret.startsWith('hks_')]).toEqual(['temperature-cap', 'shadow', true]);
    const html = addPage(p);
    for (const s of ['data-secret-once', `data-secret-value>${secret}<`, 'Copy the signing secret now', 'never shows it again', 'data-copy', 'data-verify=python', 'X-Tandem-Signature', 'hmac.compare_digest', 'data-lang=node', 'data-act="created-test"', 'Called on a copy of real requests.']) expect(html, s).toContain(s);
    expect(text(addView({ state: p.flow.add!, table: p.flow.table!, builtins: p.flow.builtins, here: 0, lang: 'node', signIn: nothing }, ADD))).toContain('crypto.timingSafeEqual');
    // it is on the pipeline at once, and its switches are settings keys
    expect(rowsAt(pipe(p), 'request.before_engine')).toEqual(['temperature-cap']);
    expect((await p.r.settings.schema('hooks')).keys.map((k) => k.key)).toContain('hooks.temperature-cap.mode');
    // once dismissed the secret is gone from the page, and no later answer holds it
    p.flow.closeAdd();
    p.r.seen.length = 0;
    await p.flow.load();
    await p.flow.open('temperature-cap');
    expect(p.flow.add).toBeNull();
    expect(hookPage(p, 'temperature-cap')).not.toContain(secret);
    expect(p.r.seen.join('')).not.toContain(secret);
    expect(hookPage(p, 'temperature-cap')).toContain(`A secret is stored, ending in ${secret.slice(-4)}. It is never shown again.`);
  });

  it('the full payload outside the LAN is asked a second time: confirm_needed, then the same request again with its confirm', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.startAdd('response.after_engine');
    p.flow.editAdd({ name: 'Archive', role: 'observer', endpoint: 'https://archive.example.com/in', payload: 'full' });
    // without pairing's rights: the sign-in, nothing made
    p.r.reg.control({ trust: false });
    expect(await p.flow.create()).toBeNull();
    expect([p.flow.add!.refused!.type, p.flow.add!.confirm]).toEqual(['unauthorized', null]);
    expect(addPage(p)).toContain('This needs TandemApp’s admin password.');
    p.r.reg.control({ trust: true });
    expect(await p.flow.create()).toBeNull();
    expect([p.flow.add!.confirm!.destination, p.flow.add!.confirm!.token.length, p.flow.add!.refused]).toEqual(['archive.example.com', 32, null]);
    let html = addPage(p);
    for (const s of ['data-confirm-needed', 'Prompts and answers leave the LAN', '>archive.example.com<', 'Add it, and send the full payload', 'data-act="confirm-back"']) expect(html).toContain(s);
    expect(html).not.toContain('data-act="add-create"');
    expect((await p.r.hooks.list()).hooks.some((h) => h.name === 'Archive')).toBe(false);
    // Back: the question goes, the form stays; any change of the form asks again
    p.flow.editAdd({});
    expect([p.flow.add!.confirm, addPage(p).includes('data-act="add-create"')]).toEqual([null, true]);
    await p.flow.create();
    const sent = p.r.paths.length;
    const h = await p.flow.create(true);
    expect([p.r.paths.length - sent, h!.payload, h!.grant!.destination, h!.grant!.confirmed_by]).toEqual([1, 'full', 'archive.example.com', 'admin']);
    html = addPage(p);
    expect(html).toContain('data-created=archive');
    // on the LAN the full payload needs no second question
    p.flow.closeAdd();
    p.flow.startAdd('response.after_engine');
    p.flow.editAdd({ name: 'Near', role: 'observer', endpoint: 'http://hooks.lan:9300/all', payload: 'full' });
    expect((await p.flow.create())!.grant).toMatchObject({ lan: true, confirmed_by: null });
  });
});

describe('one hook’s page', () => {
  it('status, breaker, figures and recent calls; a hook that is gone says so', async () => {
    const p = pages();
    const cap = await addCap(p);
    for (let i = 0; i < 12; i++) p.r.reg.request(p.r.now() + i * 50);
    p.r.reg.control({ behave: { [cap]: 'error' } });
    for (let i = 0; i < 3; i++) p.r.reg.request(p.r.now() + 1000 + i * 50);
    await p.flow.open(cap);
    const html = hookPage(p, cap);
    expect(html).toContain(`data-hook-page=${cap}`);
    for (const s of ['Temperature cap', '>Called<', 'Called on real requests. What it answers is applied.', 'Calls go through. 3 in a row failed; five open the breaker.', '3 failed, 0 timed out, 0 skipped', '12</span> modify', '3</span> error', 'Just before the engine', 'http://hooks.lan:9300/cap', '>on the LAN<', 'data-calls=15', 'changed temperature', 'the hook answered HTTP 500', 'Remove this hook']) expect(html, s).toContain(s);
    expect(html.match(/data-outcome=error/g)).toHaveLength(3);
    // every call links to its request's trace
    expect(html).toContain(`#/app/hooks/trace?request=${encodeURIComponent(p.flow.one!.calls![0].request!)}`);
    await p.flow.open('nobody');
    expect(p.flow.one!.missing).toBe(true);
    expect(text(hookGone('nobody'))).toContain('data-hook-gone=nobody');
  });

  it('its switches are settings rows: staged, checked as they are typed, applied through the review', async () => {
    const p = pages();
    const cap = await addCap(p);
    await p.flow.open(cap);
    p.flow.type('priority', 'first');
    p.flow.type('timeout_ms', '99999');
    expect(p.flow.one!.invalid).toEqual({ priority: 'A whole number, -100000 to 100000.', timeout_ms: 'A whole number of milliseconds, 1 to 30000. Empty: the stage’s budget.' });
    expect(p.draft.empty).toBe(true);
    let html = hookPage(p, cap);
    expect(html.match(/data-invalid/g)).toHaveLength(2);
    expect(html).toContain('aria-invalid=true');
    p.flow.type('priority', '5');
    p.flow.type('timeout_ms', '120');
    p.flow.type('scope.models', 'qwen*, big-*');
    p.flow.type('scope.users', 'guest*');
    p.flow.stage(cap, 'policy', 'closed');
    p.flow.stage(cap, 'enabled', false);
    expect(p.flow.one!.invalid).toEqual({});
    expect(sets(p)).toEqual({ [`hooks.${cap}.priority`]: 5, [`hooks.${cap}.timeout_ms`]: 120, [`hooks.${cap}.scope`]: { models: ['qwen*', 'big-*'], users: ['guest*'] }, [`hooks.${cap}.policy`]: 'closed', [`hooks.${cap}.enabled`]: false });
    html = hookPage(p, cap);
    expect(html.match(/data-staged=yes/g)).toHaveLength(4);
    for (const s of ['>Not called at all<', 'Switched off: it is not called, whatever its mode.', 'Refuse the request. The client gets 503 hook_unavailable.', 'data-act="undo"']) expect(html).toContain(s);
    await applyStaged(p);
    const now = await p.r.hooks.get(cap);
    expect([now.priority, now.timeout_ms, now.scope, now.policy, now.enabled, now.version]).toEqual([5, 120, { models: ['qwen*', 'big-*'], users: ['guest*'] }, 'closed', false, 2]);
    // emptied again: the keys go back to their defaults
    p.flow.type('timeout_ms', '');
    p.flow.type('scope.models', '');
    p.flow.type('scope.users', ' ');
    p.flow.stage(cap, 'policy', null);
    expect(p.draft.changes()[0]).toMatchObject({ unset: expect.arrayContaining([`hooks.${cap}.timeout_ms`, `hooks.${cap}.scope`, `hooks.${cap}.policy`]) });
    await applyStaged(p);
    expect((await p.r.hooks.get(cap)).scope).toEqual({});
  });

  it('what it sees: the full payload staged on its page goes through the review’s second confirm, and the grant shows afterwards', async () => {
    const p = pages();
    await p.flow.load();
    await p.flow.open('export');
    p.flow.stage('export', 'payload', 'full');
    const html = hookPage(p, 'export');
    expect(html).toContain('It would go to archive.example.com, outside the LAN: the review asks a second time. Granting it needs pairing’s rights.');
    await p.sheet.review(p.draft.changes());
    await p.sheet.apply();
    expect([p.sheet.state.second, p.sheet.state.plan!.grant_keys]).toEqual([true, ['hooks/controller:hooks.export.payload']]);
    expect(sheet(p)).toContain('Prompts and answers leave the LAN');
    await p.sheet.apply();
    p.draft.clear();
    await p.flow.open('export');
    expect(hookPage(p, 'export')).toContain('Granted by admin');
    expect(hookPage(p, 'export')).toContain('for archive.example.com, outside the LAN; confirmed by admin.');
  });

  it('where it runs and how it is called are saved on the hooks’ API; a conflict reads it again and says so', async () => {
    const p = pages();
    const cap = await addCap(p);
    await p.flow.open(cap);
    p.flow.startEdit();
    expect(hookPage(p, cap)).toContain('data-editing');
    // only the stops that take its role can be ticked
    expect(hookPage(p, cap)).toMatch(/data-in="stage-route\.select"[^>]*\?disabled=true|takes no transform/);
    p.flow.editEdit({ name: '', stages: [], endpoint: 'nowhere' });
    expect(await p.flow.save()).toBe(false);
    expect(p.flow.one!.edit!.errors).toEqual({ name: 'A name of 1 to 80 characters.', stages: 'It runs at one stage at least.', endpoint: 'A URL that starts with http:// or https://.' });
    p.flow.editEdit({ name: 'Cap', stages: ['request.before_engine', 'request.received'], endpoint: 'http://hooks.lan:9300/cap2' });
    // it changed on another screen meanwhile
    p.r.reg.control({ race: cap });
    expect(await p.flow.save()).toBe(false);
    const o = p.flow.one!;
    expect([o.notice, o.edit!.name, (await p.r.hooks.get(cap)).name]).toEqual(['This hook changed on another screen while you were editing it, so nothing was saved. The form shows it as it stands now.', 'Temperature cap', 'Temperature cap']);
    p.flow.editEdit({ name: 'Cap', stages: ['request.before_engine', 'request.received'], endpoint: 'http://hooks.lan:9300/cap2' });
    expect(await p.flow.save()).toBe(true);
    const h = p.flow.hook(cap)!;
    // the stops are kept in the path's order, whatever order they were ticked in
    expect([h.name, h.stages, h.endpoint, p.flow.one!.edit]).toEqual(['Cap', ['request.received', 'request.before_engine'], 'http://hooks.lan:9300/cap2', null]);
    expect(rowsAt(pipe(p), 'request.received')).toEqual([cap, 'guard']);
    // nothing changed: nothing is sent
    p.flow.startEdit();
    const sent = p.r.paths.length;
    expect([await p.flow.save(), p.r.paths.length]).toEqual([true, sent]);
  });

  it('a full hook’s new endpoint outside the LAN asks first', async () => {
    const p = pages();
    await p.flow.load();
    p.flow.startAdd('response.after_engine');
    p.flow.editAdd({ name: 'Near', role: 'observer', endpoint: 'http://hooks.lan:9300/all', payload: 'full' });
    await p.flow.create();
    p.flow.closeAdd();
    await p.flow.open('near');
    p.flow.startEdit();
    p.flow.editEdit({ endpoint: 'https://other.example.org/all' });
    expect(await p.flow.save()).toBe(false);
    expect(p.flow.one!.edit!.confirm).toMatchObject({ destination: 'other.example.org' });
    const html = hookPage(p, 'near');
    expect(html).toContain('Save, and send the full payload there');
    expect(html).toContain('>other.example.org<');
    expect(await p.flow.save(true)).toBe(true);
    expect(p.flow.hook('near')!.grant).toMatchObject({ destination: 'other.example.org', lan: false, confirmed_by: 'admin' });
    expect(hookPage(p, 'near')).toContain('>outside the LAN<');
  });

  it('replay and playground work on recorded samples and never send traffic to the engine', async () => {
    const p = pages();
    const cap = await addCap(p);
    await p.flow.open(cap);
    let html = hookPage(p, cap);
    // the event is the contract's own sample for the stop
    expect(p.flow.one!.play).toMatchObject({ stage: 'request.before_engine', result: null });
    expect(JSON.parse(p.flow.one!.play.raw)).toMatchObject({ params: { temperature: 0.2, max_tokens: 256 }, route: { backend: 'local' } });
    for (const s of ['Nothing is applied or counted, and nothing goes to an engine.', 'data-play-result=none', 'As if at <b>Just before the engine</b>']) expect(html).toContain(s);
    const calls = p.flow.hook(cap)!.metrics.calls;
    await p.flow.test();
    html = hookPage(p, cap);
    expect(p.flow.one!.play.result).toMatchObject({ outcome: 'ok', result: { action: 'modify', patch: { temperature: 0.3 } }, diff: { temperature: { op: 'replace', bytes: 3 } } });
    for (const s of ['data-play-result=ok', 'and would change one field.', '<li>temperature: replaced, 3 B</li>', 'Nothing was applied or counted.', 'data-raw-result']) expect(html, s).toContain(s);
    // nothing counted: the figures stand, and the journal has it as a test
    expect([(await p.r.hooks.get(cap)).metrics.calls, p.flow.one!.calls![0].mode]).toEqual([calls, 'test']);
    expect(html).toContain('data-call-mode=test');
    // the sample sends no event of its own; a changed one is sent, a broken one is not
    expect(p.r.paths.filter((x) => x.includes('/test')).length).toBe(1);
    p.flow.play({ raw: '{"params": {"temperature": 1.5}' });
    expect(p.flow.one!.play.invalid).toMatch(/^Not valid JSON yet/);
    await p.flow.test();
    expect(p.r.paths.filter((x) => x.includes('/test')).length).toBe(1);
    expect(hookPage(p, cap)).toMatch(/data-act="play-send" \?disabled=true|Not valid JSON yet/);
    p.flow.resetEvent();
    expect(p.flow.one!.play.invalid).toBeNull();
    // every way a call can end is explained
    p.r.reg.control({ behave: { [cap]: 'timeout' } });
    await p.flow.test();
    expect(hookPage(p, cap)).toContain('No answer within its 250 ms.');
    p.r.reg.control({ behave: { [cap]: 'error' } });
    await p.flow.test();
    expect(hookPage(p, cap)).toContain('The call failed.');
    p.r.reg.control({ behave: { [cap]: 'ok' } });
    p.r.reg.hooks[cap].answer = { action: 'deny', reason: 'nope' };
    await p.flow.test();
    expect(hookPage(p, cap)).toContain('It answered, but not with something this stage allows.');
    await p.flow.open('guard');
    await p.flow.test();
    expect(hookPage(p, 'guard')).toContain('and would refuse the request.');
    expect(hookPage(p, 'guard')).toContain('The client would read: ask the admin for this model');
    // a hook at two stops picks the one to try
    await p.flow.open('export');
    expect(hookPage(p, 'export')).toContain('data-in="play-stage"');
    p.flow.play({ stage: 'request.finished' });
    expect(JSON.parse(p.flow.one!.play.raw)).toHaveProperty('row.status', 200);
    // all of it stayed on the hooks' API and the settings: nothing went to an engine
    expect(p.r.paths.every((x) => / \/v1\/(fleet\/hooks|settings)/.test(x))).toBe(true);
    expect(p.r.paths.some((x) => /chat\/completions|\/v1\/models|engine/.test(x))).toBe(false);
  });

  it('rotating the secret shows the new one once; the old one signs beside it for the grace', async () => {
    const p = pages();
    await p.flow.load();
    await p.flow.open('audit');
    expect(hookPage(p, 'audit')).toContain('data-secret-step=idle');
    p.flow.secret({ step: 'ask' });
    let html = hookPage(p, 'audit');
    for (const s of ['data-rotate', 'How long may the old one still sign beside it?', 'At once', 'One hour', 'One day', 'Rotate, and show the new secret']) expect(html).toContain(s);
    p.r.reg.control({ trust: false });
    await p.flow.rotate();
    expect([p.flow.one!.secret.step, p.flow.one!.secret.refused!.type]).toEqual(['ask', 'unauthorized']);
    p.r.reg.control({ trust: true });
    await p.flow.rotate();
    const s = p.flow.one!.secret;
    expect([s.step, s.value!.startsWith('hks_'), p.flow.hook('audit')!.secret!.previous_until]).toEqual(['shown', true, T0 + 3_600_000]);
    html = hookPage(p, 'audit', 'node');
    for (const x of [`data-secret-value>${s.value}<`, 'This is the new secret.', 'data-verify=node', 'data-act="secret-saved"', 'The previous one still signs beside it for 60 min.']) expect(html, x).toContain(x);
    p.flow.secret({ step: 'idle' });
    expect(p.flow.one!.secret.value).toBeNull();
    expect(hookPage(p, 'audit')).not.toContain('hks_');
    // a built-in has no secret, so no panel
    await p.flow.open('guard');
    expect(hookPage(p, 'guard')).not.toContain('data-panel="secret"');
  });

  it('removing a hook asks first, and what was staged for it goes with it', async () => {
    const p = pages();
    await p.flow.load();
    await p.flow.open('export');
    p.flow.stage('export', 'mode', 'on');
    p.flow.stage('audit', 'mode', 'shadow');
    p.flow.askRemove(true);
    expect(hookPage(p, 'export')).toContain('Remove Export to the archive?');
    expect(hookPage(p, 'export')).toContain('It is taken out of its 2 stops now, not staged.');
    expect(await p.flow.remove()).toBe(true);
    expect([p.flow.list!.map((h) => h.id), p.flow.one, p.draft.changes()]).toEqual([['audit', 'guard'], null, [{ scope: 'hooks', target: 'controller', set: { 'hooks.audit.mode': 'shadow' } }]]);
    // what is left of the draft still plans: no key of the hook that is gone is in it
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.error).toBeNull();
  });
});

describe('the decision providers', () => {
  it('the list: who answers what, each provider’s mode and health badge, and the report with its arms side by side', async () => {
    const p = pages(rig({ hooks: 'demo' }));
    await p.prov.load();
    const html = provList(p);
    const who = (q: string) => html.split(`data-question=${q} `)[1].split('</li>')[0];
    expect(who('classify')).toContain('data-answers=classes');
    expect(who('packages')).toContain('data-answers=builtin');
    expect(who('packages')).toContain('watching in shadow:');
    expect(who('memory_intent')).toContain('for 30 % of sessions');
    expect(who('deliverable')).toContain('the built-in decider');
    const row = (id: string) => html.split(`data-provider=${id} `)[1].split('</li>')[0];
    expect(row('classes')).toContain('data-mode=on data-enabled=true data-health=ok');
    expect(row('intent')).toMatch(/data-mode=ab data-enabled=true data-health=failing/);
    expect(row('intent')).toContain('answers 30 % by session');
    expect(row('own')).toContain('watches in shadow');
    expect(row('classes')).toMatch(/ready in \d+ ms, checked/);
    expect(row('intent')).toContain('>failing<');
    // the report: on, shadow and both sides of the A/B, side by side
    expect(html).toContain('data-report=classes data-arms=on');
    expect(html).toContain('data-report=own data-arms=shadow');
    expect(html).toContain('data-report=intent data-arms=ab_b ab_a');
    const intent = html.split('data-report=intent')[1].split('data-report=')[0];
    for (const s of ['A/B, the provider', 'A/B, the built-in', 'answered by the built-in', 'fell back to the built-in']) expect(intent, s).toContain(s);
    expect(html.split('data-report=own')[1]).toMatch(/data-arm=shadow data-agreement=\d+(\.\d)?>/);
    expect(html.split('data-report=own')[1]).toMatch(/\d+(\.\d)? % agree \(\d+ of \d+\)/);
    expect(html).toContain('from this controller’s memory');
    await p.prov.readReport(1);
    expect(p.prov.hours).toBe(1);
    expect(provList(p)).toMatch(/aria-pressed=true data-hours=1 /);
  });

  it('a provider’s page: health with each endpoint’s probe, its questions with endpoint, model, time limit and key hint, and why the built-in answered instead', async () => {
    const p = pages(rig({ hooks: 'demo' }));
    await p.flow.load();
    await p.prov.open('intent');
    let html = provPage(p, 'intent');
    expect(html).toContain('data-provider-page=intent');
    for (const s of ['data-health=failing', 'data-endpoint=http://intent.lan:8721 data-up=true', '>ready<', 'data-ab', 'Share it answers (%)', 'data-sticky-to=session', 'data-sticky-to=user', 'data-sticky-to=request', 'data-question=memory_intent data-answered=true', 'data-question=classify data-answered=false']) expect(html, s).toContain(s);
    expect(html).toMatch(/Last call: memory_intent, .*failed \(timeout\)/);
    // the fallback reasons, counted, each with what it means
    expect(html).toContain('data-fallbacks=1');
    expect(html).toMatch(/data-reason=timeout><b class="num hk-fall-n">\d+×<\/b>/);
    expect(html).toContain('No answer in time');
    // a probe now: the endpoint is asked again, not the answer kept for 10 s
    p.r.wait(4000);
    await p.prov.probe('intent', true);
    expect(p.r.paths[p.r.paths.length - 1]).toBe('GET /v1/fleet/decision-providers/intent/health?force=1');
    // the key shows as its last four characters, never more
    await p.prov.open('classes');
    html = provPage(p, 'classes');
    expect(html).toContain('API key: A key is stored, ending in 0003. It is never shown.');
    expect(html).toContain('Its own key: No key is stored.');
    expect(html).not.toContain('sk-mock');
    expect(p.r.seen.join('')).not.toContain('sk-mock');
    // a provider through a hook names the hook and links to it
    await p.prov.open('adapter');
    html = provPage(p, 'adapter');
    expect(html).toContain('#/app/hooks/hook/classes-adapter');
    expect(html).toContain('data-endpoint=hook:classes-adapter');
    expect(html).not.toContain('data-key=');
    await p.prov.open('nobody');
    expect(p.prov.one!.missing).toBe(true);
    expect(text(providerGone('nobody'))).toContain('data-provider-gone=nobody');
  });

  it('mode, A/B share, sticky key, question mapping and keys are settings keys: staged, then applied through the review', async () => {
    const p = pages();
    await p.flow.load();
    await p.prov.open('classes');
    p.prov.stage('classes', 'mode', 'ab');
    p.prov.share('classes', '140');
    expect(p.prov.one!.invalid.ab_percent).toBe('A share from 0 to 100.');
    p.prov.share('classes', '25');
    p.prov.stage('classes', 'sticky', 'user');
    p.prov.map('classes', 'classify', 'model', 'classes-v2');
    p.prov.map('classes', 'classify', 'endpoint', 'not a url');
    p.prov.map('classes', 'classify', 'timeout_ms', '250');
    expect(p.prov.one!.invalid).toEqual({ 'classify.endpoint': 'A URL that starts with http:// or https://.' });
    p.prov.map('classes', 'classify', 'endpoint', 'http://classes.lan:8720');
    p.prov.question('classes', 'memory_intent', true);
    p.prov.keyField('classes', 'judge', true);
    p.prov.key('classes', 'judge', 'sk-judge-key-4321');
    p.prov.removeKey('classes', '*');
    const html = provPage(p, 'classes');
    for (const s of ['data-key=judge data-key-state=set', 'Its own key: A new key is staged.', 'data-key=* data-key-state=unset', 'API key: The stored key is removed when the change set is applied.', 'data-key-later=memory_intent', 'answers 25 % by user', '>changed<']) expect(html, s).toContain(s);
    // the typed key is in its password field and nowhere else on the page
    expect(html.split('sk-judge-key-4321')).toHaveLength(2);
    expect(html).toMatch(/type="password"[^>]*\.value=sk-judge-key-4321/);
    expect(p.draft.changes()).toEqual([{ scope: 'hooks', target: 'controller', set: { 'providers.classes.mode': 'ab', 'providers.classes.ab_percent': 25, 'providers.classes.sticky': 'user', 'providers.classes.questions': { classify: { model: 'classes-v2', timeout_ms: 250, endpoint: 'http://classes.lan:8720' }, judge: { model: 'judge-v2' }, memory_intent: {} }, 'providers.classes.questions.judge.api_key': 'sk-judge-key-4321' }, unset: ['providers.classes.api_key'] }]);
    await applyStaged(p);
    expect(p.sheet.state.result!.state).toBe('applied');
    const now = await p.r.providers.get('classes');
    expect([now.mode, now.ab_percent, now.sticky, now.questions.classify, now.questions.judge.key?.last4, now.key?.key_set, Object.keys(now.questions)]).toEqual(['ab', 25, 'user', { model: 'classes-v2', endpoint: 'http://classes.lan:8720', timeout_ms: 250, key: { key_set: false, last4: null, sha256_6: null, set_at: null } }, '4321', false, ['classify', 'judge', 'memory_intent']]);
    expect(p.r.seen.join('')).not.toContain('sk-judge-key-4321');
    // the question that is applied now takes its own key; one less question takes its key along
    await p.prov.open('classes');
    expect(provPage(p, 'classes')).not.toContain('data-key-later');
    p.prov.question('classes', 'judge', false);
    expect(sets(p)).toEqual({ 'providers.classes.questions': { classify: { model: 'classes-v2', timeout_ms: 250, endpoint: 'http://classes.lan:8720' }, memory_intent: {} } });
    // a provider answers one question at least
    p.draft.clear();
    await p.prov.open('own');
    p.prov.question('own', 'memory_gate', false);
    expect(p.draft.empty).toBe(true);
  });

  it('two providers that would answer one question: said on the page before the review refuses it', async () => {
    const p = pages();
    await p.prov.load();
    await p.prov.open('own');
    p.prov.question('own', 'classify', true);
    p.prov.stage('own', 'mode', 'on');
    expect(provPage(p, 'own')).toContain('Classes answers classify already. One provider answers a question, on or in A/B.');
    expect(provList(p)).toContain('Classes and Own decider would both answer');
    await p.sheet.review(p.draft.changes());
    expect([p.sheet.state.plan, p.sheet.state.error!.type]).toEqual([null, 'bad_value']);
    // the other one goes to shadow in the same change set: it applies
    p.prov.stage('classes', 'mode', 'shadow');
    expect(provList(p)).not.toContain('data-conflict');
    await applyStaged(p);
    expect((await p.r.providers.list()).providers.map((x) => [x.id, x.mode])).toEqual([['classes', 'shadow'], ['own', 'on']]);
  });

  it('add a provider: the systemone wire, a hook as the adapter, or the built-in', async () => {
    const p = pages();
    await p.flow.load();
    await p.prov.load();
    p.prov.startAdd();
    const view = () => text(addProviderView({ state: p.prov.add!, info: p.prov.info!, list: p.prov.shown, hooks: p.flow.list ?? [], signIn: nothing }, { edit: noop, create: noop }));
    let html = view();
    expect([...html.split('data-cards=kind')[1].split('</div>')[0].matchAll(/data-card=([\w-]+)>/g)].map((m) => m[1])).toEqual(['systemone-http', 'hook', 'builtin']);
    for (const s of ['Your own service', 'POST /v1/systemone', 'tools/systemone_example.py', 'A hook as the adapter', 'The built-in decider', 'In shadow', 'Start here.', 'Classes answers it now.']) expect(html, s).toContain(s);
    expect(await p.prov.create()).toBeNull();
    expect(Object.keys(p.prov.add!.errors).sort()).toEqual(['endpoint', 'name', 'questions']);
    expect(view()).toContain('3 things need a correction above.');
    p.prov.editAdd({ name: 'My decider', endpoint: 'http://decider.example:8720', api_key: 'sk-my-own-key-7777', questions: ['classify', 'packages'], mode: 'on' });
    expect(p.prov.add!.errors.questions).toContain('classify is answered by Classes');
    p.prov.editAdd({ mode: 'ab', ab_percent: '20', sticky: 'user', questions: ['packages'] });
    expect(view()).toContain('data-sticky-to=user');
    const made = await p.prov.create();
    expect([made!.id, made!.mode, made!.ab_percent, made!.sticky, made!.key?.last4, Object.keys(made!.questions), p.prov.add]).toEqual(['my-decider', 'ab', 20, 'user', '7777', ['packages'], null]);
    expect(p.r.seen.join('')).not.toContain('sk-my-own-key-7777');
    expect((await p.r.settings.schema('hooks')).keys.map((k) => k.key)).toContain('providers.my-decider.api_key');
    // a hook as the adapter: none fits yet, so the page leads to adding one
    p.prov.startAdd();
    p.prov.editAdd({ kind: 'hook', name: 'Through a hook', questions: ['deliverable'] });
    html = view();
    expect(html).toContain('data-no-adapter');
    expect(html).toContain('#/app/hooks/new?stage=decision.ask');
    expect(await p.prov.create()).toBeNull();
    p.flow.startAdd('decision.ask');
    p.flow.editAdd({ name: 'Adapter', role: 'decider', endpoint: 'http://hooks.lan:9300/ask', mode: 'on' });
    await p.flow.create();
    p.flow.closeAdd();
    expect(view()).toContain('<option value=adapter');
    p.prov.editAdd({ hook_id: 'adapter' });
    expect((await p.prov.create())!.hook_id).toBe('adapter');
    // the built-in: nothing to connect
    p.prov.startAdd();
    p.prov.editAdd({ kind: 'builtin', name: 'Own judge', questions: ['judge'] });
    expect(view()).toContain('data-builtin-note');
    expect((await p.prov.create())!.kind).toBe('builtin');
    // what the controller refuses shows with what to do
    p.prov.startAdd();
    p.prov.editAdd({ kind: 'builtin', name: 'Second', questions: ['deliverable'] });
    p.r.reg.control({ fail: 'p-create:question_taken' });
    expect(await p.prov.create()).toBeNull();
    expect(view()).toContain('Another provider answers one of these questions already.');
  });

  it('a test call, a change of its connection with a conflict, and its removal', async () => {
    const p = pages();
    await p.flow.load();
    await p.prov.open('classes');
    expect(p.prov.one!.test.question).toBe('classify');
    p.prov.setTest({ question: 'judge', text: 'The build failed.' });
    await p.prov.test();
    let html = provPage(p, 'classes');
    expect(html).toContain('data-test-result=ok');
    expect(html).toMatch(/It answered “\w+” in \d+ ms, \d+ % sure\./);
    expect(html).toContain('Model judge-v2');
    expect(html).toContain('class="hk-prob"');
    p.r.reg.control({ provider: { classes: 'http_401' } });
    await p.prov.test();
    html = provPage(p, 'classes');
    expect(html).toContain('data-test-result=failed');
    expect(html).toContain('It did not answer judge: http 401.');
    expect(html).toContain('The endpoint refused the key. Set the right API key.');
    expect((await p.r.providers.calls('classes')).calls).toEqual([]);
    // its connection is saved on the providers' API; a conflict reads it again
    p.prov.startEdit();
    p.prov.editEdit({ name: '', endpoint: 'x', model: 'two words', timeout: '0' });
    expect(await p.prov.save()).toBe(false);
    expect(Object.keys(p.prov.one!.edit!.errors).sort()).toEqual(['endpoint', 'model', 'name', 'timeout_ms']);
    p.prov.editEdit({ name: 'Classes two', endpoint: 'http://decider.lan:8721', model: 'auto', timeout: '300' });
    p.r.reg.control({ race: 'classes' });
    expect(await p.prov.save()).toBe(false);
    expect([p.prov.one!.notice!.startsWith('This provider changed on another screen'), p.prov.one!.edit!.name]).toEqual([true, 'Classes']);
    p.prov.editEdit({ name: 'Classes two', endpoint: 'http://decider.lan:8721', timeout: '300' });
    expect(await p.prov.save()).toBe(true);
    const now = await p.r.providers.get('classes');
    expect([now.name, now.endpoint, now.timeout_ms, now.key?.last4]).toEqual(['Classes two', 'http://decider.lan:8721', 300, '0003']);
    // removed: its staged changes go with it
    p.prov.stage('classes', 'mode', 'shadow');
    p.prov.askRemove(true);
    expect(provPage(p, 'classes')).toContain('Remove Classes two?');
    expect(await p.prov.remove()).toBe(true);
    expect([p.prov.info!.providers.map((x) => x.id), p.draft.empty, p.prov.one]).toEqual([['own'], true, null]);
  });

  it('with no provider the page says who answers now and how to start; replayed answers are said', async () => {
    const p = pages(rig({ hooks: false }));
    await p.prov.load();
    let html = provList(p);
    expect(html).toContain('data-providers-none');
    expect(html.match(/the built-in decider/g)).toHaveLength(6);
    expect(html).not.toContain('data-panel="report"');
    p.r.reg.control({ replay: true });
    await p.prov.load();
    html = provList(p);
    expect(html).toContain('data-replay-on');
  });
});

describe('all pages work at 360 px width', () => {
  const css = readFileSync(new URL('../src/styles/hooks.css', import.meta.url), 'utf8');
  const narrow = css.slice(css.indexOf('@container mon (max-width: 640px)'));

  it('the narrow layout stacks every grid of the pages into one column, and keeps the controls a thumb can hit', () => {
    expect(narrow.length).toBeGreaterThan(1000);
    // the rail narrows; a hook's row, a provider's row, the cards, the fields, the playground, a
    // journal's row, the trace's list, who answers what, an endpoint and the report stack
    for (const sel of ['.hk-row,\n  .hk-prov', '.hk-cards,\n  .hk-grid', '.hk-play', '.hk-call,\n  .hk-tcall .hk-call', '.hk-recent-r', '.hk-who-r,\n  .hk-ep', '.hk-report-r', '.hk-kv > div', '.hk-facts']) {
      const rule = narrow.split(`${sel} {`)[1]?.split('}')[0] ?? '';
      expect(rule, sel).toMatch(/grid-template-columns: (minmax\(0, 1fr\)|repeat\(2, minmax\(0, 1fr\)\)|repeat\(auto-fit, minmax\(130px, 1fr\)\)|minmax\(0, 1fr\) auto);/);
    }
    expect(narrow).toMatch(/\.hk-stop,\n  \.hk-step \{\n    grid-template-columns: 24px minmax\(0, 1fr\);/);
    // the mode control and the arrows are full-size targets
    expect(narrow).toMatch(/\.hk-mode \.seg-btn,[^}]*min-height: 40px;/);
    expect(narrow).toMatch(/\.hk-order \.btn \{[^}]*min-width: 44px;[^}]*min-height: 40px;/);
    // the report's arms are named when they stack, since their header row is gone
    expect(narrow).toMatch(/\.hk-report-h \{\n    display: none;/);
    expect(narrow).toMatch(/\.hk-arm-l \{\n    display: block;/);
  });

  it('nothing in the pages has a fixed width that a phone cannot hold, and long ids wrap', () => {
    // no rule sets a width or a min-width in pixels above what 360 px minus the gutters leaves
    const wide = [...css.matchAll(/(?:^|\s)(?:min-)?width: (\d+)px/g)].map((m) => Number(m[1])).filter((n) => n > 300);
    expect(wide).toEqual([]);
    const mins = [...css.matchAll(/minmax\((\d+)px/g)].map((m) => Number(m[1]));
    expect(Math.max(...mins)).toBeLessThanOrEqual(210);
    for (const sel of ['.hk-name', '.hk-title', '.hk-secret-v code', '.hk-call-what']) expect(css.split(`${sel} {`)[1].split('}')[0], sel).toContain('overflow-wrap: anywhere');
    // motion stops under reduced motion
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\n  \.hk-use-fill \{\n    transition: none;/);
  });
});
