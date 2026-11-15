// Tests for VIS-278: the setup wizard is the first page of a TandemApp with no engine (lib/
// engine-wizard.ts, views/fleet/engine-wizard.ts, the shell's landing, the menu and the route).
//   Gherkin (plan 13 item 6):
//   Given a TandemApp with no engine, When the user opens the page, Then the wizard opens at
//     "Add engine"                                  "the first open lands on the wizard..."
// and the whole first run through the mock controller, from nothing to the message Chat sends.
import { describe, expect, it } from 'vitest';
import { MOCK_ENGINES as M } from '../mock/enginehosts.ts';
import { AfterFlow, PROGRESS_EVERY_MS } from '../src/lib/engine-after.ts';
import { landing } from '../src/lib/controller-host.ts';
import { FindFlow } from '../src/lib/engine-find.ts';
import { PairFlow } from '../src/lib/engine-pair.ts';
import { FIRST_DEFAULT, STEP_LABEL, WIZARD_STEPS, engineHash, firstRun, headOf, stepOf, stepper } from '../src/lib/engine-wizard.ts';
import { MENU, crumb, menuItem, sectionOf, subPages } from '../src/lib/menu.ts';
import { FIRST_MAX, chatHash, firstMessage, parseHash, routeHash } from '../src/lib/router.ts';
import { PATHS } from '../src/lib/start.ts';
import { pageContext, type PageData } from '../src/ui/page-context.ts';
import { chatView } from '../src/views/fleet/engine/first.ts';
import { rig, text } from './engine-rig.ts';

describe('where the first open goes', () => {
  it('the first open lands on the wizard, which opens at Add engine; a member or a route given keeps the page asked for', () => {
    expect(landing('', [])).toBe('add-engine');
    expect(landing('#', [])).toBe('add-engine');
    expect(landing('#/', [])).toBe('add-engine');
    expect(landing('#/usage', [])).toBeNull();
    expect(landing('#/start', [])).toBeNull();
    expect(landing('', [{ id: 'eh-1' }])).toBeNull();
    expect(landing('', [{ id: 'backend.bench', class: 'backend' }])).toBeNull();
    // the route with nothing below it is the first step
    expect(parseHash('#/add-engine')).toMatchObject({ view: 'add-engine', path: [] });
    expect(stepper('find')[0]).toEqual({ id: 'find', label: 'Add engine', n: 1, state: 'on' });
  });
  it('is a page of its own under Settings, before Add Spark; Get started names it first', () => {
    expect(MENU.find((g) => g.label === 'Settings')!.items.map((i) => i.id)).toEqual(['start', 'add-engine', 'add-spark', 'app']);
    expect(menuItem('add-engine').label).toBe('Add engine');
    expect(sectionOf('add-engine')).toBe('add-engine');
    expect(crumb('add-engine')).toEqual({ group: 'Settings', label: 'Add engine' });
    expect(subPages(parseHash('#/add-engine'))).toEqual([]);
    expect(PATHS[0]).toMatchObject({ id: 'engine', href: '#/add-engine' });
    // the host with TandemNode keeps its own wizard
    expect(PATHS.find((p) => p.id === 'spark')!.href).toBe('#/add-spark');
    expect(parseHash('#/add-spark').view).toBe('add-spark');
  });
  it('the steps after pairing live at the engine host’s own route, so a reload opens them again', () => {
    expect(engineHash('eh-0a1b2c3d4e')).toBe('#/add-engine/eh-0a1b2c3d4e');
    const r = parseHash('#/add-engine/eh-0a1b2c3d4e');
    expect([r.view, r.path]).toEqual(['add-engine', ['eh-0a1b2c3d4e']]);
    expect(routeHash(r)).toBe('#/add-engine/eh-0a1b2c3d4e');
    expect(parseHash(engineHash('spark desk')).path).toEqual(['spark desk']);
  });
  it('has a context of its own before the page registers its own', () => {
    const data = { route: { view: 'add-engine', path: [], params: new URLSearchParams() }, fleet: null, summary: null, meta: { version: null, code: null }, layouts: [], theme: 'dark', toggleTheme: () => undefined } as unknown as PageData;
    const c = pageContext('add-engine', data);
    expect(c.title).toBe('Add engine');
    expect(text(c.body)).toContain('<li>Add engine</li><li>Pair</li><li>Model</li><li>First message</li>');
  });
});

describe('the steps', () => {
  it('four of them, the other ways standing in for Pair', () => {
    expect(WIZARD_STEPS.map((s) => STEP_LABEL[s])).toEqual(['Add engine', 'Pair', 'Model', 'First message']);
    expect([stepOf('find'), stepOf('pair'), stepOf('connect'), stepOf('after'), stepOf('chat')]).toEqual(['find', 'pair', 'pair', 'model', 'chat']);
    expect(stepper('connect').map((s) => s.state)).toEqual(['done', 'on', 'todo', 'todo']);
    expect(stepper('after').map((s) => s.state)).toEqual(['done', 'done', 'on', 'todo']);
    expect(stepper('chat').map((s) => s.state)).toEqual(['done', 'done', 'done', 'on']);
    expect(stepper('pair').map((s) => s.n)).toEqual([1, 2, 3, 4]);
  });
  it('a TandemApp with nothing in it is set up; one with a member adds an engine', () => {
    expect([firstRun([]), firstRun(null), firstRun(undefined), firstRun([{ id: 'desk' }])]).toEqual([true, true, true, false]);
    expect(headOf(true).title).toBe('Set up TandemApp');
    expect(headOf(false).title).toBe('Add engine');
    for (const h of [headOf(true), headOf(false)]) expect(h.sub).toMatch(/code it printed/);
  });
  it('the first message goes to Chat in the route and comes out as typed', () => {
    const hash = chatHash('  Hello! What can you do? 100% & more  ');
    expect(hash).toBe('#/chat?first=Hello!%20What%20can%20you%20do%3F%20100%25%20%26%20more');
    const r = parseHash(hash);
    expect([r.view, firstMessage(r.params)]).toEqual(['chat', 'Hello! What can you do? 100% & more']);
    expect(firstMessage(parseHash(chatHash(FIRST_DEFAULT)).params)).toBe(FIRST_DEFAULT);
    expect(chatHash('   ')).toBe('#/chat');
    expect(firstMessage(new URLSearchParams())).toBeNull();
    expect(firstMessage(new URLSearchParams('first=%20%20'))).toBeNull();
    expect(firstMessage(parseHash(chatHash('x'.repeat(FIRST_MAX + 50))).params)).toHaveLength(FIRST_MAX);
  });
});

describe('the last step', () => {
  it('names the engine and its model and hands one message to the Chat tab', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.tailnet });
    const e = (await r.api.pairCode(s.pairing, M.code)).engine!;
    const t = text(chatView(e, FIRST_DEFAULT, { text: () => undefined, send: () => undefined }, 'code'));
    expect(t).toContain('Send a first message');
    expect(t).toContain(`${e.alias} serves Qwen3.8-Flash-Next.`);
    expect(t).toContain('data-in="first"');
    expect(t).toMatch(/data-act="send" \?disabled=false/);
    expect(t).toContain('Send in Chat');
    expect(t).toContain(`${e.alias} is paired`);
    expect(t).toContain('href="#/add-engine"');
    expect(text(chatView(e, '   ', { text: () => undefined, send: () => undefined }))).toMatch(/data-act="send" \?disabled=true/);
    // Chat on, or not read: the same step
    expect(text(chatView(e, FIRST_DEFAULT, { text: () => undefined, send: () => undefined }, 'code', true))).toContain('Send in Chat');
  });

  it('OPS-288: with Chat off (a new TandemApp) the step offers the Playground and the switch, not the Chat tab', async () => {
    const r = rig();
    const s = await r.api.pairStart({ address: M.tailnet });
    const e = (await r.api.pairCode(s.pairing, M.code)).engine!;
    const t = text(chatView(e, FIRST_DEFAULT, { text: () => undefined, send: () => undefined }, 'code', false));
    expect(t).toContain('Try the engine');
    expect(t).toContain('data-chat="off"');
    expect(t).toContain('href="#/playground"');
    expect(t).toContain('href="#/app"');
    expect(t).not.toContain('Send in Chat');
    expect(t).not.toContain('data-in="first"');
    expect(t).toContain(`${e.alias} serves Qwen3.8-Flash-Next.`);
  });
});

describe('the first run, from nothing to the first message', () => {
  it('no engine: Add engine finds it, the code pairs it, the bare engine gets a model, Chat gets the message', async () => {
    const r = rig();
    // 1  the page opens on a TandemApp with no member: the wizard, at Add engine
    const nodes = () => (r.fleet.handle('GET', '/v1/fleet/nodes', new URLSearchParams(), null, r.now()).body as { nodes: { id: string }[] }).nodes;
    expect(landing('', nodes())).toBe('add-engine');
    const find = new FindFlow(r.api);
    await find.open();
    const desk = find.state.view!.engines.find((e) => e.address === M.desk)!;
    // 2  click the engine: its fingerprint to compare, and the code
    const picked = await find.pick(desk);
    const pair = new PairFlow(r.api, picked!.start, picked!.name, () => undefined, r.now);
    expect(pair.state.start.fingerprint).toBe(desk.fingerprint);
    pair.setCode(M.code);
    const paired = await pair.submit();
    const id = paired!.engine!.id;
    expect(parseHash(engineHash(id)).path).toEqual([id]);
    expect(nodes().map((n) => n.id)).toEqual([id]);
    // 3  the bare engine preselected, a model picked, its download followed
    const after = new AfterFlow(r.api, id);
    await after.open(paired!.engine);
    expect([after.state.engine?.wizard?.mode_choice, after.state.picked]).toEqual(['engine', 'qwen3.8-27b']);
    await after.start();
    for (let i = 0; i < 60 && after.state.phase === 'installing'; i++) {
      r.wait(PROGRESS_EVERY_MS);
      await after.tick();
    }
    expect([after.state.phase, after.state.engine?.wizard?.next]).toEqual(['ready', 'chat']);
    // 4  the first message, handed to the Chat tab
    expect(firstMessage(parseHash(chatHash(FIRST_DEFAULT)).params)).toBe(FIRST_DEFAULT);
    // the next open has a member: no wizard in the way
    expect(landing('', nodes())).toBeNull();
    // and nothing that passed was a secret
    expect(r.seen.join('\n')).not.toContain(M.code);
  });
});
