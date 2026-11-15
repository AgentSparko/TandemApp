// Tests for SRV-833 (the page part): after pairing, the bare engine and a model (lib/
// engine-after.ts, views/fleet/engine/after.ts), against the mock controller.
//   Gherkin (plan 13 item 10):
//   Given a paired engine with no model, When the wizard continues, Then "bare engine" is
//     preselected, And the user picks a model and sees its download progress, And no file was
//     edited by hand                                "bare engine preselected, a model picked..."
import { describe, expect, it } from 'vitest';
import { MOCK_ENGINES as M } from '../mock/enginehosts.ts';
import type { EngineView, JobProgress, PullPlan } from '../src/api/engines.ts';
import { AfterFlow, PROGRESS_EVERY_MS, STARTING, barOf, defaultModel, endOf, modelName, planWords, stepWords, viaWords } from '../src/lib/engine-after.ts';
import { afterView, engineSummary, type AfterHandlers } from '../src/views/fleet/engine/after.ts';
import { rig, text, type Rig } from './engine-rig.ts';

const ON: AfterHandlers = { pick: () => undefined, start: () => undefined, again: () => undefined, accept: () => undefined, reload: () => undefined, models: () => undefined };

async function paired(r: Rig, address: string = M.desk): Promise<EngineView> {
  const s = await r.api.pairStart({ address });
  return (await r.api.pairCode(s.pairing, M.code)).engine!;
}

/** the job followed to its end, a read every two seconds as the page does */
async function follow(r: Rig, f: AfterFlow, max = 60): Promise<JobProgress[]> {
  const seen: JobProgress[] = [];
  for (let i = 0; i < max && f.state.phase === 'installing'; i++) {
    r.wait(PROGRESS_EVERY_MS);
    await f.tick();
    if (f.state.progress) seen.push(f.state.progress);
  }
  return seen;
}

const prog = (o: Partial<JobProgress>): JobProgress => ({ state: 'running', finished: false, step: null, label: null, detail: null, downloading: false, steps_done: 0, steps: 7, message: null, ...o });
const plan = (o: Partial<PullPlan>): PullPlan => ({ ok: true, errors: [], warnings: [], download_gb: null, on_disk: null, profile: null, ...o });

describe('words and figures', () => {
  it('a plan in a sentence: what starting it costs', () => {
    expect(planWords(plan({ download_gb: 18.4, on_disk: false }))).toBe('18.4 GB to download to the engine host.');
    expect(planWords(plan({ download_gb: 46, on_disk: false }))).toBe('46 GB to download to the engine host.');
    expect(planWords(plan({ download_gb: 0, on_disk: true }))).toBe('Its weights are on the engine host’s disk already: nothing to download.');
    expect(planWords(plan({}))).toBe('The engine did not say how much it downloads.');
    expect(planWords(plan({ download_gb: 2, on_disk: false, warnings: ['The window is cut to 64k.'] }))).toBe('2 GB to download to the engine host. The window is cut to 64k.');
  });
  it('the bar: the download’s share while it downloads, else the steps done', () => {
    expect(barOf(prog({ downloading: true, step: 'fetch', detail: '9.2 of 18.4 GB', steps_done: 2 }))).toEqual({ value: 0.5, text: '9.2 of 18.4 GB', kind: 'download' });
    expect(barOf(prog({ downloading: true, detail: null, steps_done: 2 }))).toEqual({ value: 2 / 7, text: '2 of 7 steps done', kind: 'steps' });
    expect(barOf(prog({ steps_done: 5 }))).toMatchObject({ value: 5 / 7, kind: 'steps' });
    expect(barOf(prog({ state: 'done', finished: true, steps_done: 7 }))).toEqual({ value: 1, text: '7 of 7 steps done', kind: 'steps' });
    expect(barOf(prog({ steps: 0 })).value).toBe(0);
  });
  it('the step in the engine’s own words; how a job ended', () => {
    expect(stepWords(prog({ state: 'queued' }))).toBe('Waiting to start');
    expect(stepWords(prog({ step: 'fetch', label: 'Download the weights' }))).toBe('Download the weights');
    expect(stepWords(prog({ step: 'switch' }))).toBe('switch');
    expect(stepWords(prog({ state: 'done', finished: true }))).toBe('Done.');
    expect(stepWords(prog({ state: 'failed', finished: true, message: 'The download stopped.' }))).toBe('The download stopped.');
    expect([endOf(null), endOf(prog({})), endOf(prog({ state: 'done', finished: true })), endOf(prog({ state: 'failed', finished: true })), endOf(prog({ state: 'rolled_back', finished: true }))]).toEqual([null, null, 'done', 'failed', 'failed']);
  });
  it('the model the list starts on, its name, and how a host was paired', () => {
    const models = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B', recommended: true }];
    expect([defaultModel(models), defaultModel([{ id: 'a', name: 'A' }]), defaultModel([])]).toEqual(['b', 'a', null]);
    expect([modelName(models, 'b'), modelName(models, 'x'), modelName(null, null)]).toEqual(['B', 'x', 'the model']);
    expect([viaWords({ via: 'code' }), viaWords({ via: 'ssh' }), viaWords({ via: 'ssh' }, 'password'), viaWords({ via: 'ssh' }, 'key')]).toEqual(['by code', 'over SSH', 'with a password, once', 'with a pasted key']);
  });
});

describe('after pairing, against the controller', () => {
  it('bare engine preselected, a model picked, its download followed to the end; nothing edited by hand', async () => {
    const r = rig();
    const e = await paired(r);
    const calls = r.seen.length;
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    // the pairing's own answer says what is next: no second read of the engine
    expect([f.state.phase, f.state.engine?.wizard?.mode_choice, f.state.engine?.wizard?.needs_model]).toEqual(['pick', 'engine', true]);
    expect(f.state.models?.map((m) => m.id)).toEqual(['qwen3.8-27b', 'qwen3.8-flash-next', 'nemotron-3.5-bolt-74b-a9b']);
    expect([f.state.picked, f.state.plan?.download_gb, f.state.plan?.on_disk, f.state.planning]).toEqual(['qwen3.8-27b', 18.4, false, false]);
    expect(r.seen.length - calls).toBe(3);   // progress, models, the plan
    let t = text(afterView(f.state, ON, 'code'));
    expect(t).toContain('data-mode-choice="engine"');
    expect(t).toContain('Bare engine <span class="wz-pill">preselected</span>');
    expect(t).toContain('18.4 GB to download to the engine host.');
    expect(t).toContain('Use the bare engine and download Qwen3.8-27B');
    expect(t).toMatch(/data-act="start" \?disabled=false/);
    for (const m of f.state.models!) expect(t).toContain(`data-model=${m.id}>`);
    expect(t).toContain('<span class="wz-pill is-ok">recommended</span>');
    await f.start();
    // the bare engine was accepted with the same button: the mode call ran, then the install
    expect([f.state.mode?.mode, f.state.mode?.changed, f.state.phase, f.state.jobModel, f.state.progress?.state, f.state.error]).toEqual(['engine', true, 'installing', 'qwen3.8-27b', 'queued', null]);
    t = text(afterView(f.state, ON, 'code'));
    expect(t).toContain('Installing Qwen3.8-27B');
    expect(t).toContain('role="progressbar"');
    expect(t).toContain('data-mode-result=changed>');
    const seen = await follow(r, f);
    const dl = seen.filter((p) => p.downloading);
    expect(dl.length).toBeGreaterThan(2);
    expect(dl.every((p) => /^\d+\.\d of 18\.4 GB$/.test(p.detail ?? ''))).toBe(true);
    const shares = dl.map((p) => barOf(p).value);
    expect(shares).toEqual([...shares].sort((a, b) => a - b));
    expect(seen.map((p) => p.steps_done)).toEqual([...seen.map((p) => p.steps_done)].sort((a, b) => a - b));
    expect([f.state.phase, f.state.progress?.state, f.state.engine?.status?.engine.model, f.state.engine?.wizard?.next]).toEqual(['ready', 'done', 'Qwen3.8-27B', 'chat']);
    // every call was one of the controller's routes for this engine host: no file, no shell
    expect(r.seen.every((x) => !x.includes('"error"'))).toBe(true);
  });
  it('the download shows as GB so far on a bar a screen reader can read', async () => {
    const r = rig();
    const e = await paired(r);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    await f.start();
    r.wait(8000);
    await f.tick();
    const p = f.state.progress!;
    expect([p.downloading, p.step]).toEqual([true, 'fetch']);
    const t = text(afterView(f.state, ON));
    expect(t).toContain(`<span class="eh-prog-step" data-step=fetch>Download the weights</span><span class="num small" data-bar-text>${p.detail}</span>`);
    expect(t).toContain(`aria-valuetext=Download the weights: ${p.detail}>`);
    expect(t).toMatch(/aria-valuenow=\d+ /);
    expect(t).toContain(`${p.steps_done} of 7 steps done.`);
    expect(t).toContain('You can leave this page.');
  });
  it('weights already on the disk: nothing to download, and the button says start', async () => {
    const r = rig();
    const e = await paired(r);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    await f.pick('qwen3.8-flash-next');
    expect([f.state.plan?.on_disk, f.state.planError]).toEqual([true, null]);
    const t = text(afterView(f.state, ON));
    expect(t).toContain('nothing to download');
    expect(t).toContain('Use the bare engine and start Qwen3.8-Flash-Next');
    await f.start();
    await follow(r, f);
    expect([f.state.phase, f.state.engine?.status?.engine.model]).toEqual(['ready', 'Qwen3.8-Flash-Next']);
  });
  it('a model the engine cannot take says why in its words, and the button stays off', async () => {
    const r = rig();
    const e = await paired(r);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    await f.pick('nemotron-3.5-bolt-74b-a9b');
    expect([f.state.plan, f.state.planError?.code]).toEqual([null, 'setup_refused']);
    const t = text(afterView(f.state, ON));
    expect(t).toContain('not enough free disk for the weights: 46.2 GB needed, 31.0 GB free');
    expect(t).toContain('Change the choice as the message says.');
    expect(t).toMatch(/data-act="start" \?disabled=true/);
    await f.start();
    expect(f.state.phase).toBe('pick');
  });
  it('an install that fails says so in the job’s words and leads back to the list', async () => {
    const r = rig();
    const e = await paired(r);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    r.fleet.engines.failJob = true;
    await f.start();
    await follow(r, f);
    expect([f.state.phase, f.state.progress?.state]).toEqual(['failed', 'failed']);
    const t = text(afterView(f.state, ON));
    expect(t).toContain('Qwen3.8-27B was not installed');
    expect(t).toContain('The download stopped');
    expect(t).toContain('data-act="again"');
    await f.again();
    expect([f.state.phase, f.state.picked, f.state.plan?.download_gb, f.state.progress]).toEqual(['pick', 'qwen3.8-27b', 18.4, null]);
    await f.start();
    await follow(r, f);
    expect(f.state.phase).toBe('ready');
  });
  it('the page left and opened again follows the running install', async () => {
    const r = rig();
    const e = await paired(r);
    const first = new AfterFlow(r.api, e.id);
    await first.open(e);
    await first.start();
    r.wait(6000);
    const again = new AfterFlow(r.api, e.id);
    await again.open();
    expect([again.state.phase, again.state.job, again.state.jobModel, again.state.progress?.downloading]).toEqual(['installing', first.state.job, 'qwen3.8-27b', true]);
    await follow(r, again);
    expect(again.state.phase).toBe('ready');
  });
  it('a read that fails while the engine restarts is not the job’s end', async () => {
    const r = rig();
    const e = await paired(r);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    await f.start();
    r.wait(4000);
    r.fleet.engines.fail('progress', 'engine_down');
    await f.tick();
    expect([f.state.phase, f.state.progressError?.code]).toEqual(['installing', 'engine_down']);
    const t = text(afterView(f.state, ON));
    expect(t).toContain('The engine is not running.');
    expect(t).toContain('role=status data-finding=engine_down>');
    r.fleet.engines.fail('progress', null);
    await follow(r, f);
    expect([f.state.phase, f.state.progressError]).toEqual(['ready', null]);
    expect(STARTING.code).toBe('engine_loading');
  });
  it('an engine that serves a model already: no list, the bare engine accepted, then the first message', async () => {
    const r = rig();
    const e = await paired(r, M.tailnet);
    const f = new AfterFlow(r.api, e.id);
    await f.open(e);
    expect([f.state.phase, f.state.models]).toEqual(['serving', null]);
    const t = text(afterView(f.state, ON));
    expect(t).toContain('It serves Qwen3.8-Flash-Next already');
    expect(t).toContain('Use the bare engine and continue');
    await f.accept();
    // it ran in chat mode: the mode was set to the bare engine
    expect([f.state.mode?.changed, f.state.mode?.previous.mode, f.state.phase]).toEqual([true, 'chat', 'ready']);
  });
  it('what stops the step shows with its fix: the list, the start, an engine host that does not answer', async () => {
    const r = rig();
    const e = await paired(r);
    for (const code of ['setup_locked', 'token_refused', 'token_missing', 'http_blocked', 'engine_down', 'not_allowed']) {
      r.fleet.engines.fail('models', code);
      const f = new AfterFlow(r.api, e.id);
      await f.open(e);
      expect([f.state.phase, f.state.error?.code, f.state.models], code).toEqual(['pick', code, null]);
      const t = text(afterView(f.state, ON)).replace(/<\/?code>/g, '`');
      expect(t, code).toContain(f.state.error!.message);
      expect(t, code).toContain(f.state.error!.fix);
      expect(t, code).toContain('data-act="models-again"');
      r.fleet.engines.fail('models', null);
      await f.loadModels();
      expect([f.state.error, f.state.models?.length], code).toEqual([null, 3]);
    }
    for (const [route, code] of [['mode', 'busy'], ['mode', 'ctl_failed'], ['pull', 'busy'], ['pull', 'unknown_model']] as const) {
      const f = new AfterFlow(r.api, e.id);
      await f.open(e);
      r.fleet.engines.fail(route, code);
      await f.start();
      expect([f.state.phase, f.state.error?.code, f.state.busy], `${route} ${code}`).toEqual(['pick', code, null]);
      r.fleet.engines.fail(route, null);
    }
    r.fleet.engines.fail('engine', 'host_key_changed');
    const gone = new AfterFlow(r.api, e.id);
    await gone.open();
    expect([gone.state.phase, gone.state.error?.code, gone.state.engine?.alias]).toEqual(['unreadable', 'host_key_changed', 'spark-desk']);
    const t = text(afterView(gone.state, ON));
    expect(t).toContain('spark-desk does not answer');
    expect(t).toContain('<dt>Pinned before</dt>');
    expect(t).toContain('Check again');
    const none = new AfterFlow(r.api, 'eh-0000000000');
    await none.open();
    expect([none.state.phase, none.state.error?.code]).toEqual(['unreadable', 'not_found']);
  });
});

describe('the page', () => {
  it('the engine host as it answers: who it is, how it was paired, what it runs', async () => {
    const r = rig();
    const e = await paired(r);
    const t = text(engineSummary(e, 'code'));
    expect(t).toContain('spark-desk is paired');
    expect(t).toContain('by code');
    expect(t).toContain('data-engine-state=healthy>engine running</span>');
    expect(t).toContain(e.host_key!);
    expect(t).toContain(`${M.user}@${M.desk}`);
    expect(t).toContain('<dd data-engine-model>none yet</dd>');
    expect(t).toContain('<span class="num">0.16.0</span>');
    expect(text(engineSummary(e, 'password'))).toContain('with a password, once');
  });
  it('while it loads it says so without guessing', () => {
    const f = new AfterFlow(rig().api, 'eh-1');
    expect(text(afterView(f.state, ON))).toContain('aria-busy="true"');
  });
});
