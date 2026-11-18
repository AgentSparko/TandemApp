// Tests for SRV-890: which pages each role sees, and the role a new account gets, against the mock
// controller, without a browser (the flow, the Views page's template, the menu).
//
//   Gherkin (SRV-890):
//   An admin hides pages       "Given an admin on Users & access, Views, when they hide Hardware
//                               from viewers, then auth.views.viewer lists every other page, and a
//                               signed-in viewer's menu has no Hardware"
//   Every page again is "all"  "Given a role with one page hidden, when the admin shows it again,
//                               then the setting is all, not a list"
//   A viewer's Chat            "Given a viewer, the Chat switch writes auth.chat_min_role: one
//                               switch, never two"
//   The default role           "Given the default role operator, then Add a user picks operator
//                               first, and an account added without a role is an operator"
//   A hidden page's link       "Given a role shown Chat and API alone, then the menu holds Chat,
//                               API and Settings' Account, and a hidden page's link goes to Chat"
//   Owners                     "An owner's views are every page, whatever the settings"

import { afterEach, describe, expect, it } from 'vitest';
import { ALL_SECTIONS, MENU_ONLY, viewsValue } from '../src/lib/access-flow.ts';
import { MENU, firstVisible, menuNow, setRights, setViews, visible } from '../src/lib/menu.ts';
import { SECTIONS } from '../mock/auth.ts';
import { dialog, open, page, panelText, rig, rowText } from './access-rig.ts';

const ids = (): string[] => menuNow().flatMap((g) => g.items.map((i) => i.id));

afterEach(() => {
  setRights(null);
  setViews(null);
});

describe('Gherkin: views per role (SRV-890)', () => {
  it('An admin hides pages: Hardware leaves the viewer setting and the viewer menu', async () => {
    const r = rig('sam');
    await open(r, 'views');
    expect(page(r)).toContain('data-panel=default-role');
    expect(rowText(panelText(page(r), 'views-monitor'), 'hardware')).toContain('aria-pressed=true data-role=viewer');
    await r.flow.showSection('viewer', 'hardware', 'Hardware', false);
    expect(r.flow.failed).toBeNull();
    const want = ALL_SECTIONS.filter((s) => s !== 'hardware' && s !== 'chat').join(',');
    expect(r.mock.setting('auth.views.viewer', 'all')).toBe(want);
    expect(rowText(panelText(page(r), 'views-monitor'), 'hardware')).toContain('aria-pressed=false data-role=viewer');
    r.as('vic');
    const me = (await r.api.me())!;
    expect(me.views).not.toContain('hardware');
    setRights(me.permissions);
    setViews(me.views);
    expect(ids()).not.toContain('hardware');
    expect(ids()).toContain('overview');
  });

  it('Every page again is "all", not a list', async () => {
    const r = rig('sam');
    await open(r, 'views');
    await r.flow.showSection('operator', 'dev', 'Dev', false);
    expect(r.mock.setting('auth.views.operator', 'all')).not.toBe('all');
    await r.flow.showSection('operator', 'dev', 'Dev', true);
    expect(r.mock.setting('auth.views.operator', 'all')).toBe('all');
    expect(viewsValue('viewer', null, 'chat', false)).toBe('all');
  });

  it("A viewer's Chat is auth.chat_min_role: one switch, the owner's", async () => {
    const adm = rig('sam');
    await open(adm, 'views');
    expect(rowText(panelText(page(adm), 'views-chat'), 'chat')).toContain('data-role=viewer ?disabled=true');
    expect(page(adm)).toContain('An owner switches a viewer’s Chat');
    const r = rig('kim');
    await open(r, 'views');
    expect(r.flow.shows('viewer', 'chat')).toBe(false);
    await r.flow.showSection('viewer', 'chat', 'Chat', true);
    expect(r.mock.setting('auth.chat_min_role', 'operator')).toBe('viewer');
    expect(r.mock.setting('auth.views.viewer', 'all')).toBe('all');
    expect(r.flow.shows('viewer', 'chat')).toBe(true);
    r.as('vic');
    expect((await r.api.me())!.views).toContain('chat');
  });

  it('The default role: Add a user picks it first; no role sent is that role', async () => {
    const r = rig('sam');
    await open(r, 'views');
    await r.flow.setDefaultRole('operator');
    await open(r, 'users');
    r.flow.show({ kind: 'user-new' });
    expect(dialog(r)).toContain('<option value=operator ?selected=true>');
    expect(dialog(r)).toContain('<option value=viewer ?selected=false>');
    const made = await r.api.addUser({ username: 'newbie' });
    expect(made.user.role).toBe('operator');
  });

  it("A hidden page's link goes to the first page shown; Account stays", () => {
    setRights(['self', 'read', 'chat']);
    setViews(['chat', 'api']);
    expect(ids()).toEqual(['chat', 'api', 'account']);
    expect(visible('hardware')).toBe(false);
    expect(visible('account')).toBe(true);
    expect(firstVisible()).toBe('chat');
    setViews([]);
    expect(firstVisible()).toBe('account');
    expect(menuNow().map((g) => g.label)).toEqual(['Settings']);
  });

  it("An owner's views are every page, whatever the settings", async () => {
    const r = rig('sam');
    await open(r, 'views');
    await r.flow.showSection('admin', 'conductor', 'Conductor', false);
    r.as('kim');
    expect((await r.api.me())!.views).toBeNull();
    r.as('sam');
    expect((await r.api.me())!.views).not.toContain('conductor');
  });

  it('The page lists every menu section, marks the menu-only ones, and matches the server', async () => {
    expect(ALL_SECTIONS).toEqual(SECTIONS);
    expect(MENU.flatMap((g) => g.items.map((i) => i.id))).toEqual(SECTIONS);
    const r = rig('sam');
    await open(r, 'views');
    const t = page(r);
    for (const s of ALL_SECTIONS) expect(t).toContain(`data-row=${s}>`);
    expect(t.match(/data-menu-only/g)).toHaveLength(MENU_ONLY.length);
  });

  it('An operator sees no Views page', async () => {
    const r = rig('ola');
    await open(r, 'views');
    expect(r.flow.stop).toBe('not-allowed');
  });
});
