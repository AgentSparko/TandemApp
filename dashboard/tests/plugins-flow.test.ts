// Tests for VIS-284 (the test matrix of VIS-285): the plugins pages' flow against the mock
// controller, without a browser. A page here is what the element holds: the flow on the settings
// area's draft, the change-set flow that applies what it stages, and the templates drawn from
// their state.
//
//   Gherkin (VIS-284):
//   Install shows what is granted   "Install shows what is granted: a plugin source, ..."
//   Generated form                  "Generated form: a plugin with a settings schema ..."
//   Refused install                 "Refused install: a plugin with a bad manifest ..."
//   Acceptance criteria:
//   the list shows name, version, kind, trust level and status for every plugin
//   the install dialog shows hash, level and capabilities before confirm     (in Install shows what is granted)
//   the settings form is generated from the schema with validation; secrets are set by name and
//     never displayed again
//   a panel renders in a sandboxed iframe and a failing panel shows a finding
//   a refused install shows the reason
//   the pages work at 360 px width                                           (the narrow layout's rules)
// and the states around them: setting a plugin up in one review, the second confirm of a risky
// grant and of the host, pairing's rights, a plugin's states (stopped by hand, restarting, given
// up, no sandbox), its log, what it adds, an upgrade and the way back, a removal a hook holds up,
// its files checked, and the page's side of a panel's bridge.

import { readFileSync } from 'node:fs';
import { nothing } from 'lit';
import { describe, expect, it } from 'vitest';
import { GIT_COMMIT, GIT_URL } from '../mock/plugins.ts';
import type { FormField } from '../src/lib/schema-form.ts';
import { installView, type InstallHandlers } from '../src/views/plugins/install.ts';
import { listView } from '../src/views/plugins/list.ts';
import { panelView, sandboxOf } from '../src/views/plugins/panel.ts';
import { pluginGone, pluginView, type PluginHandlers } from '../src/views/plugins/plugin.ts';
import { runSheet, type SheetHandlers } from '../src/views/settings/sheet.ts';
import { text } from './engine-rig.ts';
import { applyStaged, install, pages, rig, type Pages } from './plugins-rig.ts';

const noop = (): void => undefined;
const INSTALL: InstallHandlers = { edit: noop, inspect: noop, downgrade: noop, install: noop, done: noop, again: noop };
const PLUGIN: PluginHandlers = { enable: noop, setUp: noop, act: noop, grant: noop, trust: noop, undo: noop, type: noop, tidy: noop, put: noop, reset: noop, secretField: noop, secretType: noop, secretRemove: noop, logs: noop, follow: noop, askRemove: noop, remove: noop };
const SHEET: SheetHandlers = { apply: noop, back: noop, close: noop, again: noop, when: noop, note: noop, restore: noop, cancelRestart: noop };

const installPage = (p: Pages): string => {
  const s = p.flow.install!;
  const id = s.report?.manifest?.name ?? s.upgrade;
  return text(installView({ state: s, blocked: p.flow.installBlocked(), replaces: p.flow.info?.plugins.find((x) => x.id === id) ?? null, signIn: nothing }, INSTALL));
};
const pluginPage = (p: Pages, writable = true): string => {
  const f = p.flow;
  const id = f.one!.id;
  return text(
    pluginView(
      { one: f.one!, shown: { enabled: f.enabled, granted: (c) => f.granted(c), trust: f.trust, staged: (key) => p.draft.staged('plugins', id, key)?.kind ?? null, value: (prop) => f.value(prop), textAt: (path, fld) => f.textAt(path, fld), errorAt: (path) => f.errorAt(path), isSet: (prop) => f.isSet(prop) }, writable, now: p.r.now(), signIn: nothing },
      PLUGIN,
    ),
  );
};
const listPage = (p: Pages): string => text(listView({ info: p.flow.info!, statuses: p.flow.statuses, staged: (id) => p.draft.count('plugins', id), now: p.r.now() }));
const panelPage = (p: Pages): string => text(panelView(p.flow.panel!, '', { reload: noop }));
const sheet = (p: Pages): string => text(runSheet(p.sheet.state, null, { get: () => undefined, left: () => null, cancelling: null, refused: null }, p.sheet.blocked(), SHEET));
/** what is staged for one plugin, key by key */
const sets = (p: Pages, id: string): Record<string, unknown> => (p.draft.changes().find((c) => c.scope === 'plugins' && c.target === id) as { set?: Record<string, unknown> } | undefined)?.set ?? {};
/** the reads a page starts and does not wait for (its log) have ended */
const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0));
/** one panel of the page, by its data-panel name */
const part = (page: string, name: string): string => page.split(`data-panel="${name}"`)[1]?.split('data-panel="')[0] ?? '';
const field = (p: Pages, ...path: string[]): FormField => {
  let fs = p.flow.one!.plugin!.settings_form!.fields;
  let f: FormField | undefined;
  for (const step of path) {
    f = fs.find((x) => x.path[x.path.length - 1] === step);
    fs = f?.fields ?? [];
  }
  return f!;
};
const installs = (p: Pages): string[] => p.r.paths.filter((x) => x === 'POST /v1/fleet/plugins');

async function check(p: Pages, source: { type?: 'folder' | 'wheel' | 'git'; path?: string; url?: string; commit?: string; subdir?: string }): Promise<boolean> {
  p.flow.startInstall();
  p.flow.editSource({ type: 'folder', ...source });
  return p.flow.inspect();
}

/** turn a plugin on through the review, and let it start */
async function turnOn(p: Pages, id: string): Promise<void> {
  await p.flow.open(id);
  await settle();
  p.flow.setEnabled(true);
  await applyStaged(p);
  p.r.wait(2000);
  await p.flow.refresh();
  await settle();
}

describe('Gherkin: the plugins pages (VIS-284)', () => {
  it('Install shows what is granted: a plugin source, the user starts the install: the page shows the sha256, the requested level and capabilities before it asks to confirm', async () => {
    const p = pages();
    // Given a plugin source
    p.flow.startInstall();
    p.flow.editSource({ type: 'folder', path: '/srv/plugins/usage-board' });
    let page = installPage(p);
    expect(page).toContain('data-step="source"');
    // nothing to confirm yet: the source is checked first
    expect(page).not.toContain('data-act="install-go"');
    expect(p.flow.installBlocked()).toBe('Check the source first.');
    // When the user starts the install
    expect(await p.flow.inspect()).toBe(true);
    const r = p.flow.install!.report!;
    page = installPage(p);
    // Then the page shows the sha256 of its files, in full
    expect(r.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(page).toContain(`data-sha=${r.sha256}`);
    expect(page).toContain(`<code class="num">${r.sha256}</code>`);
    expect(page).toContain('9 files, 47 KB');
    expect(page).toContain('The signature holds for key');
    // the requested level
    expect(page).toContain('data-level=sandboxed');
    expect(page).toContain('Where it runs: sandboxed');
    // and each capability, the riskiest first, in plain words with what is asked twice
    expect([...page.matchAll(/data-capability=(\S+) data-risk=(\w+)/g)].map((m) => `${m[1]} ${m[2]}`)).toEqual(['prompts:full high', 'network:api.example.com:443 high', 'network:192.168.1.40:9100 elevated', 'settings:read low']);
    for (const words of ['Read every prompt and answer as it is', 'Connect to api.example.com:443', 'A machine outside the LAN', 'Connect to 192.168.1.40:9100', 'A machine on the LAN', 'Read TandemApp’s settings']) expect(page).toContain(words);
    expect(page.split('data-chip=confirm').length - 1).toBe(2);
    expect(page).toContain('Installing grants none of these');
    expect(page).toContain('2 of them are asked a second time');
    // what it adds, and its manifest as it is
    for (const name of ['usage_today', 'reset_counters', 'board', 'count']) expect(page).toContain(`data-name=${name}`);
    expect(page).toContain('can delete or overwrite');
    expect(page).toContain('"name": "usage-board"');
    // all of it before it asks to confirm: the install button comes last
    const go = page.indexOf('data-act="install-go"');
    for (const before of [`data-sha=${r.sha256}`, 'data-level=sandboxed', 'data-capability=prompts:full', 'data-step="adds"']) {
      expect(page.indexOf(before), before).toBeGreaterThan(0);
      expect(page.indexOf(before), before).toBeLessThan(go);
    }
    expect(page).toContain('Install Usage board 1.4.0');
    expect(p.flow.installBlocked()).toBeNull();
    // nothing is installed by looking
    expect(p.r.reg.rows['usage-board']).toBeUndefined();
    expect(installs(p)).toEqual([]);
    expect(p.r.paths).toContain('POST /v1/fleet/plugins/inspect');

    // the confirm: installed turned off, and nothing it asks for is granted
    const done = await p.flow.commit();
    expect([done!.action, done!.plugin.enabled, done!.plugin.capabilities.granted, done!.plugin.sha256]).toEqual(['installed', false, [], r.sha256]);
    page = installPage(p);
    expect(page).toContain('data-installed=usage-board');
    expect(page).toContain('Usage board 1.4.0 is installed, and turned off');
    expect(page).toContain('Set it up');
  });

  it('Install shows what is granted: a git commit that asks to run on the host shows that level, and a form edit after the check takes the report back', async () => {
    const p = pages();
    expect(await check(p, { type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' })).toBe(true);
    let page = installPage(p);
    expect(page).toContain('data-level=host');
    expect(page).toContain('Where it runs: on the host');
    expect(page).toContain('outside any sandbox');
    expect(page).toContain(`commit <span class="num">${GIT_COMMIT}</span>`);
    expect([...page.matchAll(/data-capability=(\S+) data-risk=(\w+)/g)].map((m) => `${m[1]} ${m[2]}`)).toEqual(['engine:control high', 'prompts:meta low']);
    expect(page).toContain('Not available to plugins yet');
    // the level and engine:control are each asked twice
    expect(page.split('data-chip=confirm').length - 1).toBe(2);
    expect(page).toContain('data-warning');
    // the report is of the source that was checked: another source has none
    p.flow.editSource({ subdir: 'other' });
    page = installPage(p);
    expect(p.flow.install!.report).toBeNull();
    expect(page).not.toContain('data-act="install-go"');
  });

  it('Generated form: a plugin with a settings schema: the detail page shows a form with a field per property and validation messages', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    // Given a plugin with a settings schema
    await p.flow.open('audit-hook');
    await settle();
    let page = part(pluginPage(p), 'settings');
    // Then the detail page shows a form with a field per property
    expect([...page.matchAll(/data-key=settings\.(\w+) data-staged=(\w+) data-control=(\w+)/g)].map((m) => m.slice(1).join(' '))).toEqual(['keep_days no number', 'fields no list']);
    expect(page).toContain('How long a line is kept.');
    expect(page).toContain('.value=14');
    expect(page).toContain('1 to 365');
    expect(page).toContain('Default 30');
    // a list of a few named values is a set of checkboxes
    expect([...page.matchAll(/data-option=(\w+)/g)].map((m) => m[1])).toEqual(['id', 'model', 'tokens', 'ms', 'status']);
    // and validation messages: a value out of range, then a text that is no number
    const keep = field(p, 'keep_days');
    p.flow.type(['keep_days'], keep, '400');
    page = part(pluginPage(p), 'settings');
    expect(page).toContain('data-invalid');
    expect(page).toContain('At most 365.');
    expect(page).toContain('.value=400');
    expect(p.draft.empty).toBe(true);
    p.flow.type(['keep_days'], keep, '4x');
    expect(part(pluginPage(p), 'settings')).toContain('A whole number, like 16.');
    expect(p.draft.empty).toBe(true);
    // a value that holds is staged, marked, and can be taken back
    p.flow.type(['keep_days'], keep, '45');
    p.flow.tidy(['keep_days']);
    expect(sets(p, 'audit-hook')).toEqual({ 'settings.keep_days': 45 });
    page = part(pluginPage(p), 'settings');
    expect(page).toContain('data-key=settings.keep_days data-staged=set');
    expect(page).not.toContain('data-invalid');
    expect(page).toContain('data-act="undo"');
    // the value it holds, typed again, is no change
    p.flow.type(['keep_days'], keep, '14');
    expect(p.draft.empty).toBe(true);
    p.flow.type(['keep_days'], keep, '45');
    p.flow.put(['fields'], ['id', 'model']);
    expect(sets(p, 'audit-hook')).toEqual({ 'settings.keep_days': 45, 'settings.fields': ['id', 'model'] });
    // the change goes through the settings interface: planned, applied, versioned
    await applyStaged(p);
    expect(p.sheet.state.result!.state).toBe('applied');
    expect(p.r.reg.rows['audit-hook'].settings).toEqual({ keep_days: 45, fields: ['id', 'model'] });
    expect(p.r.paths).toContain('POST /v1/settings/apply');
    page = part(pluginPage(p), 'settings');
    expect(page).toContain('.value=45');
    expect(page).not.toContain('data-staged=set');
    const versions = await p.r.settings.versions('plugins', 'audit-hook');
    expect(versions.versions[0].keys).toEqual(['settings.fields', 'settings.keep_days']);
    // a set value can go back to the schema's default
    expect(page).toContain('data-act="reset"');
    p.flow.reset('keep_days');
    expect(p.draft.changes()).toEqual([{ scope: 'plugins', target: 'audit-hook', unset: ['settings.keep_days'] }]);
    expect(part(pluginPage(p), 'settings')).toContain('.value=30');
    await applyStaged(p);
    expect(p.r.reg.rows['audit-hook'].settings).toEqual({ fields: ['id', 'model'] });
  });

  it('Generated form: nested objects, enums, a nullable value and a list item by item each get their control and their message', async () => {
    const p = pages();
    const id = await install(p, '/srv/plugins/usage-board');
    await p.flow.open(id);
    await settle();
    let page = part(pluginPage(p), 'settings');
    expect([...page.matchAll(/data-key=settings\.(\w+) data-staged=\w+ data-control=(\w+)/g)].map((m) => m.slice(1).join(' '))).toEqual(['refresh_s number', 'currency choice', 'export group', 'hide_users list']);
    expect(page).toContain('Refresh every (s)');
    expect(page).toContain('data-required');
    // a group's fields, each with its own control
    expect([...page.matchAll(/data-field=(export\.\w+) data-control=(\w+)/g)].map((m) => m.slice(1).join(' '))).toEqual(['export.enabled switch', 'export.url text']);
    // a nested text against its pattern
    const url = field(p, 'export', 'url');
    p.flow.type(['export', 'url'], url, 'ftp://collector');
    page = part(pluginPage(p), 'settings');
    expect(page).toContain('An address that starts with http:// or https://.');
    expect(p.draft.empty).toBe(true);
    p.flow.type(['export', 'url'], url, 'https://collector.example.com/in');
    p.flow.tidy(['export', 'url']);
    // a property is one key: the group is staged whole
    expect(sets(p, id)).toEqual({ 'settings.export': { enabled: false, url: 'https://collector.example.com/in' } });
    // emptied again, a nullable value is not set: back to what held, so no change
    p.flow.type(['export', 'url'], url, '');
    expect(p.draft.empty).toBe(true);
    p.flow.put(['export', 'enabled'], true);
    p.flow.type(['export', 'url'], url, 'https://collector.example.com/in');
    // an enum of two is a choice
    p.flow.put(['currency'], 'EUR');
    // a required number left empty
    const refresh = field(p, 'refresh_s');
    p.flow.type(['refresh_s'], refresh, '');
    expect(part(pluginPage(p), 'settings')).toContain('A number is needed here.');
    p.flow.type(['refresh_s'], refresh, '3');
    expect(part(pluginPage(p), 'settings')).toContain('At least 5.');
    p.flow.type(['refresh_s'], refresh, '60');
    // a list item by item: an empty one, one twice, then one taken out
    const item = field(p, 'hide_users').item!;
    p.flow.put(['hide_users', 0], '');
    page = part(pluginPage(p), 'settings');
    expect(page).toContain('data-item=0');
    expect(page).toContain('This cannot be empty.');
    expect('settings.hide_users' in sets(p, id)).toBe(false);
    p.flow.type(['hide_users', 0], item, 'guest');
    p.flow.put(['hide_users', 1], 'guest');
    expect(part(pluginPage(p), 'settings')).toContain('Each item once.');
    expect('settings.hide_users' in sets(p, id)).toBe(false);
    p.flow.put(['hide_users', 1], undefined);
    expect(sets(p, id)).toEqual({ 'settings.export': { enabled: true, url: 'https://collector.example.com/in' }, 'settings.currency': 'EUR', 'settings.refresh_s': 60, 'settings.hide_users': ['guest'] });
    await applyStaged(p);
    expect(p.sheet.state.result!.state).toBe('applied');
    expect(p.r.reg.rows[id].settings).toEqual({ export: { enabled: true, url: 'https://collector.example.com/in' }, currency: 'EUR', refresh_s: 60, hide_users: ['guest'] });
    // what the controller refuses lands in the review, in its own words
    p.draft.set('plugins', id, 'settings.refresh_s', 9000);
    await p.sheet.review(p.draft.changes());
    expect([p.sheet.state.error!.type, p.sheet.state.error!.facts.key]).toEqual(['bad_value', 'settings.refresh_s']);
    expect(sheet(p)).toContain('settings.refresh_s: at most 600');
  });

  it('Refused install: a plugin with a bad manifest: the page shows the refusal reason and nothing is installed', async () => {
    const p = pages();
    const before = Object.keys(p.r.reg.rows);
    // Given a plugin with a bad manifest
    expect(await check(p, { path: '/srv/plugins/broken-tool' })).toBe(true);
    const page = installPage(p);
    // Then the page shows the refusal reason: plainly, with what to change, and verbatim
    expect(page).toContain('data-inspect-head=refused');
    expect(page).toContain('Broken tool 0.1.0 cannot be installed');
    expect(page).toContain('2 checks refused it. Nothing was installed and nothing in it ran.');
    expect(page).toContain('data-check=tools data-check-state=refused');
    expect(page).toContain('The tool greet does not say destructiveHint.');
    expect(page).toContain('State readOnlyHint, destructiveHint, idempotentHint and openWorldHint as true or false on every tool');
    expect(page).toContain('contributes.tools[0].annotations.destructiveHint');
    expect(page).toContain("tool 'greet': annotations.destructiveHint is missing: state readOnlyHint, destructiveHint, idempotentHint and openWorldHint as booleans");
    expect(page).toContain('The tool greet would take arguments outside its schema.');
    expect(page).toContain("tool 'greet': inputSchema does not say additionalProperties false");
    // the checks that passed are told too
    expect([...page.matchAll(/data-check=(\w+) data-check-state=(\w+)/g)].map((m) => `${m[1]} ${m[2]}`)).toEqual(['files pass', 'signature pass', 'manifest pass', 'tools refused', 'contributions pass', 'paths pass', 'asks pass', 'settings pass', 'compat pass']);
    // and nothing is installed: there is no install button, the flow does not send one
    expect(page).not.toContain('data-act="install-go"');
    expect(page).toContain('data-installable="false"');
    expect(page).toContain('Nothing was installed');
    expect(page).not.toContain('data-step="asks"');
    expect(p.flow.installBlocked()).toBe('The checks refused it.');
    expect(await p.flow.commit()).toBeNull();
    expect(installs(p)).toEqual([]);
    expect(Object.keys(p.r.reg.rows)).toEqual(before);
    // a source that changed between the check and the install is refused by the install itself
    const raw = await p.r.send('POST', '/v1/fleet/plugins', { source: { type: 'folder', path: '/srv/plugins/broken-tool' } });
    expect([raw.status, (raw.body as { error: { type: string; errors: unknown[] } }).error.type, (raw.body as { error: { errors: unknown[] } }).error.errors.length]).toEqual([422, 'plugin_refused', 2]);
    expect(Object.keys(p.r.reg.rows)).toEqual(before);
  });

  it('Refused install: no manifest, an unknown field and a TandemApp that is too old each say their reason, and what stayed unchecked', async () => {
    const p = pages();
    expect(await check(p, { path: '/srv/plugins/no-manifest' })).toBe(true);
    let page = installPage(p);
    expect(page).toContain('This source cannot be installed');
    expect(page).toContain('There is no plugin.json at the plugin’s root.');
    expect([...page.matchAll(/data-check=(\w+) data-check-state=(\w+)/g)].map((m) => `${m[1]} ${m[2]}`)).toEqual(['files pass', 'signature pass', 'manifest refused', 'tools skipped', 'contributions skipped', 'paths skipped', 'asks skipped', 'settings skipped', 'compat skipped']);
    expect(page).toContain('Not checked: it needs a valid manifest first.');

    expect(await check(p, { path: '/srv/plugins/odd-fields' })).toBe(true);
    page = installPage(p);
    expect(page).toContain('plugin.json has a field plugin-v1 does not know: autostart.');
    expect(page).toContain('An unknown field is refused, never ignored.');
    expect(page).toContain('data-check=compat data-check-state=skipped');

    expect(await check(p, { path: '/srv/plugins/too-new' })).toBe(true);
    page = installPage(p);
    expect(page).toContain('data-check=compat data-check-state=refused');
    expect(page).toContain('It is written for another TandemApp: it needs >=99.0, this is 0.16.0.');
    expect(page).toContain('Install a version of the plugin for this TandemApp, or update TandemApp.');
    expect(installs(p)).toEqual([]);
  });
});

describe('the list shows name, version, kind, trust level and status for every plugin', () => {
  it('one row a plugin: its name and version, its kind, where it came from, its trust level, its signature and its state', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.load();
    const page = listPage(p);
    expect(page).toContain('data-plugins=5');
    expect(page).toContain('This TandemApp is <span class="num">0.16.0</span>');
    const row = (id: string): string => page.split(`data-plugin=${id} `)[1].split('data-plugin=')[0];
    const want: [string, string, string, string, string, string][] = [
      ['hello-tool', 'Hello tool', '0.1.0', 'plugin', 'sandboxed', 'running'],
      ['audit-hook', 'Audit hook', '0.1.0', 'plugin', 'sandboxed', 'running'],
      ['classify-provider', 'Keyword classifier', '0.1.0', 'plugin', 'sandboxed', 'off'],
      ['flaky-export', 'Flaky export', '0.3.1', 'plugin', 'sandboxed', 'failed'],
      ['tandemllm-vllm-plugin', 'vLLM stats', '0.1.0', 'engine-stats', 'sandboxed', 'recorded'],
    ];
    for (const [id, name, version, kind, trust, state] of want) {
      const r = row(id);
      expect(r, id).toContain(`<b class="plg-item-n">${name}</b>`);
      expect(r, id).toContain(`data-version>${version}</span>`);
      expect(r.startsWith(`data-kind=${kind} `), id).toBe(true);
      if (kind === 'plugin') expect(r, id).toContain(`data-chip=trust>${trust}</span>`);
      else expect(r, id).not.toContain('data-chip=trust');
      expect(r, id).toContain(`data-state=${state}>`);
      expect(r, id).toContain('data-chip=signature>not signed</span>');
      expect(r, id).toContain('data-source=folder');
      expect(r, id).toContain(`href=#/app/plugins/p/${id}`);
    }
    expect(row('tandemllm-vllm-plugin')).toContain('data-chip=kind>engine stats</span>');
    expect(row('hello-tool')).toContain('data-chip=kind>plugin</span>');
    expect(row('hello-tool')).toContain('1 tool');
    expect(row('audit-hook')).toContain('1 hook');
    // what a plugin still waits for shows on its row
    expect(row('classify-provider')).toContain('3 not granted');
    expect(row('hello-tool')).not.toContain('not granted');
    // a staged change marks its row
    p.draft.set('plugins', 'classify-provider', 'enabled', true);
    expect(listPage(p).split('data-plugin=classify-provider ')[1].split('data-plugin=')[0]).toContain('1 staged');
  });

  it('a plugin whose files changed, one that asks for the host, and a list with nothing in it', async () => {
    const p = pages();
    expect(await check(p, { type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' })).toBe(true);
    await p.flow.commit();
    p.r.reg.control({ tamper: 'hello-tool' });
    await p.flow.load();
    let page = listPage(p);
    expect(page.split('data-plugin=hello-tool ')[1].split('data-plugin=')[0]).toContain('data-chip=integrity>files changed</span>');
    const host = page.split('data-plugin=host-metrics ')[1];
    expect(host).toContain('data-chip=trust>asks for the host: not allowed yet</span>');
    expect(host).toContain('data-source=git');
    expect(host).toContain(GIT_COMMIT.slice(0, 12));

    const empty = pages(rig({ plugins: false }));
    await empty.flow.load();
    page = listPage(empty);
    expect(page).toContain('data-plugins-none');
    expect(page).toContain('No plugin yet');
    expect(page).toContain('href=#/app/plugins/install');
    // how a plugin of your own starts is on the page
    expect(page).toContain('python tools/plugin_check.py path/to/your-plugin');
  });
});

describe('setting a plugin up: one review for the switch and what it asks for', () => {
  it('a plugin that is off says what it waits for; Turn it on stages the switch and every grant, the review lists them, and it runs', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.open('classify-provider');
    await settle();
    let page = pluginPage(p);
    expect(page).toContain('data-setup');
    expect(page).toContain('Keyword classifier is turned off');
    expect(page).toContain('It asks for 3 capabilities that are not granted, has 1 secret without a value.');
    expect(page).toContain('data-panel="status" data-state=disabled');
    // its provider is not registered while it is off
    expect(part(page, 'adds')).toContain('data-name=keywords data-live=false');
    p.flow.setUp();
    expect(sets(p, 'classify-provider')).toEqual({ enabled: true, 'capabilities.prompts:redacted': true, 'capabilities.secrets:API_KEY': true, 'capabilities.network:192.168.1.30:8720': true });
    page = pluginPage(p);
    expect(page).not.toContain('data-setup');
    expect(part(page, 'status')).toContain('Staged: the plugin host may start it once the change is applied.');
    expect(part(page, 'may').split('grant staged').length - 1).toBe(3);
    // one grant can be taken back before the review
    p.flow.undo('capabilities.network:192.168.1.30:8720');
    expect(Object.keys(sets(p, 'classify-provider')).sort()).toEqual(['capabilities.prompts:redacted', 'capabilities.secrets:API_KEY', 'enabled']);
    p.flow.grant('network:192.168.1.30:8720', true);
    // its secret is set by name
    const secret = 'sk-classify-0123456789abcdef';
    p.flow.secretField('API_KEY', true);
    p.flow.secretType('API_KEY', secret);
    page = part(pluginPage(p), 'secrets');
    expect(page).toContain('data-key=secrets.API_KEY data-staged=set');
    expect(page).toContain('A new value is staged. It is sent when the change set is applied.');
    // the review: every grant is told, none of these is asked twice
    await p.sheet.review(p.draft.changes());
    const plan = p.sheet.state.plan!;
    expect(plan.parts[0].changes.map((c) => c.key)).toEqual(['capabilities.network:192.168.1.30:8720', 'capabilities.prompts:redacted', 'capabilities.secrets:API_KEY', 'enabled', 'secrets.API_KEY']);
    expect(plan.grant_keys).toEqual([]);
    const s = sheet(p);
    expect(s).toContain('Plugin: classify-provider');
    expect(s).toContain('No restart of TandemApp: the plugin host starts the plugin again with what changed.');
    expect(s).toContain('Enabled: the plugin host may start classify-provider (its own code, with the capabilities granted to it).');
    expect(s).toContain('a new key');
    expect(s).not.toContain(secret);
    await applyStaged(p);
    expect(p.sheet.state.result!.state).toBe('applied');
    p.r.wait(2000);
    await p.flow.refresh();
    await settle();
    page = pluginPage(p);
    expect(page).toContain('data-panel="status" data-state=running');
    expect(part(page, 'adds')).toContain('data-name=keywords data-live=true');
    expect(part(page, 'adds')).toContain('href=#/app/hooks/providers');
    expect(part(page, 'may')).toContain('Granted by admin');
    // the secret is stored, and never displayed again: not on the page, not in any answer
    const sec = part(page, 'secrets');
    expect(sec).toContain('data-secret=stored');
    expect(sec).toContain('A value is stored, ending in cdef');
    expect(sec).toContain('It is never shown.');
    expect(sec).not.toContain('type="password"');
    expect(page).not.toContain(secret);
    expect(p.r.seen.join('\n')).not.toContain(secret);
    expect(p.r.reg.rows['classify-provider'].secrets.API_KEY.value).toBe(secret);
    // removed by name too
    p.flow.secretRemove('API_KEY');
    expect(p.draft.changes()).toEqual([{ scope: 'plugins', target: 'classify-provider', unset: ['secrets.API_KEY'] }]);
    expect(part(pluginPage(p), 'secrets')).toContain('The stored value is removed when the change set is applied.');
    await applyStaged(p);
    expect(p.r.reg.rows['classify-provider'].secrets).toEqual({});
    expect(part(pluginPage(p), 'secrets')).toContain('No value is stored.');
  });

  it('a secret whose capability is not granted says that the plugin does not get it; a plugin with nothing to grant is one switch', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.open('classify-provider');
    await settle();
    expect(part(pluginPage(p), 'secrets')).toContain('data-secret-ungranted');
    p.flow.grant('secrets:API_KEY', true);
    expect(part(pluginPage(p), 'secrets')).not.toContain('data-secret-ungranted');

    const q = pages();
    await q.flow.open('hello-tool');
    await settle();
    const page = pluginPage(q);
    expect(page).not.toContain('data-setup');
    expect(part(page, 'may')).toContain('It asks for no capability');
    expect(page).not.toContain('data-panel="secrets"');
  });
});

describe('a risky grant is asked a second time', () => {
  it('prompts:full and a host outside the LAN: the review asks again, in plain words, and keeps who confirmed', async () => {
    const p = pages();
    const id = await install(p, '/srv/plugins/usage-board');
    await p.flow.open(id);
    await settle();
    let may = part(pluginPage(p), 'may');
    expect([...may.matchAll(/data-capability=(\S+) data-risk=(\w+)/g)].map((m) => `${m[1]} ${m[2]}`)).toEqual(['prompts:full high', 'network:api.example.com:443 high', 'network:192.168.1.40:9100 elevated', 'settings:read low']);
    expect(may.split('Not granted').length - 1).toBe(4);
    // a grant on the LAN alone is not asked twice
    p.flow.grant('network:192.168.1.40:9100', true);
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.plan!.confirm_grant).toBeNull();
    p.sheet.close();
    p.flow.grant('prompts:full', true);
    p.flow.grant('network:api.example.com:443', true);
    may = part(pluginPage(p), 'may');
    expect(may).toContain('The review asks a second time before it is granted.');
    await p.sheet.review(p.draft.changes());
    const plan = p.sheet.state.plan!;
    expect(plan.grant_keys).toEqual(['plugins/usage-board:capabilities.network:api.example.com:443', 'plugins/usage-board:capabilities.prompts:full']);
    expect(plan.confirm_grant).toMatch(/^[0-9a-f]{32}$/);
    expect(sheet(p)).toContain('Continue');
    await p.sheet.apply();
    expect(p.sheet.state.second).toBe(true);
    const s = sheet(p);
    expect(s).toContain('data-second-plugin=usage-board');
    expect(s).toContain('usage-board would get 2 grants that are asked twice');
    expect(s).toContain('Read every prompt and answer as it is.');
    expect(s).toContain('Connect to api.example.com:443.');
    expect(s).toContain('A machine outside the LAN');
    expect(s).toContain('Apply, and grant it');
    // the hooks' own second question is not there: no hook is in this plan
    expect(s).not.toContain('data-second-grant');
    // nothing is granted before the second confirm
    expect(p.r.reg.rows[id].granted).toEqual({});
    await p.sheet.apply();
    expect(p.sheet.state.result!.state).toBe('applied');
    const g = p.r.reg.rows[id].granted;
    expect(Object.keys(g).sort()).toEqual(['network:192.168.1.40:9100', 'network:api.example.com:443', 'prompts:full']);
    expect([g['prompts:full'].confirmed_by, g['network:192.168.1.40:9100'].confirmed_by]).toEqual(['admin', null]);
    p.draft.clear();
    await p.flow.read();
    may = part(pluginPage(p), 'may');
    expect(may).toContain('confirmed a second time by admin');
    // an apply without the second confirm is refused by the controller itself
    p.flow.grant('prompts:full', false);
    await applyStaged(p);
    const raw = await p.r.send('POST', '/v1/settings/plan', { changes: [{ scope: 'plugins', target: id, set: { 'capabilities.prompts:full': true } }] });
    const again = await p.r.send('POST', '/v1/settings/apply', { changes: [{ scope: 'plugins', target: id, set: { 'capabilities.prompts:full': true } }], confirm: (raw.body as { confirm: string }).confirm });
    expect([again.status, (again.body as { error: { type: string } }).error.type]).toEqual([428, 'grant_confirm_required']);
    expect(p.r.reg.rows[id].granted['prompts:full']).toBeUndefined();
  });

  it('the host: a plugin that asks for it does not start until it is allowed, and allowing it is asked twice', async () => {
    const p = pages();
    expect(await check(p, { type: 'git', url: GIT_URL, commit: GIT_COMMIT, subdir: 'host-metrics' })).toBe(true);
    await p.flow.commit();
    p.flow.closeInstall();
    await turnOn(p, 'host-metrics');
    let page = pluginPage(p);
    expect(page).toContain('data-panel="status" data-state=refused');
    expect(part(page, 'status')).toContain('It asks to run on the host, which was not granted');
    expect(part(page, 'trust')).toContain('data-trust-asked=host data-trust=sandboxed');
    expect(part(page, 'trust')).toContain('Allow it on the host');
    expect(part(page, 'trust')).toContain('data-chip=signature>not signed</span>');
    p.flow.setTrust('host');
    expect(sets(p, 'host-metrics')).toEqual({ trust: 'host' });
    expect(part(pluginPage(p), 'trust')).toContain('data-trust-asked=host data-trust=host');
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.plan!.grant_keys).toEqual(['plugins/host-metrics:trust']);
    await p.sheet.apply();
    const s = sheet(p);
    expect(s).toContain('host-metrics would get a grant that is asked twice');
    expect(s).toContain('Run on the host, outside any sandbox.');
    await p.sheet.apply();
    expect(p.sheet.state.result!.state).toBe('applied');
    p.draft.clear();
    p.r.wait(2000);
    await p.flow.refresh();
    await settle();
    page = pluginPage(p);
    expect(page).toContain('data-panel="status" data-state=running');
    expect(part(page, 'status')).toContain('a host process');
    expect(part(page, 'adds')).toContain('data-name=echo data-live=true');
    // setting it back to the sandbox takes it down again, without a second question
    p.flow.setTrust('sandboxed');
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.plan!.confirm_grant).toBeNull();
  });

  it('without pairing’s rights the review says so, and nothing is granted or turned on', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.open('classify-provider');
    await settle();
    p.r.mock.control({ trust: false });
    p.flow.setUp();
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.plan).toBeNull();
    expect(p.sheet.state.error!.type).toBe('trust_required');
    const s = sheet(p);
    expect(s).toContain('Turning a plugin on, and granting it anything, needs pairing’s rights.');
    expect(s).toContain('Nothing was changed.');
    expect([p.r.reg.rows['classify-provider'].enabled, p.r.reg.rows['classify-provider'].granted]).toEqual([false, {}]);
    // a change that grants nothing needs no such rights
    p.draft.clear();
    p.flow.put(['fallback'], 'chat');
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.plan!.changed).toBe(true);
    // installing and removing take them too
    expect(await check(p, { path: '/srv/plugins/usage-board' })).toBe(true);
    expect(await p.flow.commit()).toBeNull();
    expect(p.flow.install!.refused!.type).toBe('unauthorized');
    expect(installPage(p)).toContain('This needs pairing’s rights.');
    expect(p.r.reg.rows['usage-board']).toBeUndefined();
  });
});

describe('a plugin’s state, its controls and its log', () => {
  it('running; stopped by hand it stays stopped; started and restarted at once; its restarts counted', async () => {
    const p = pages();
    await p.flow.open('hello-tool');
    await settle();
    let st = part(pluginPage(p), 'status');
    expect(pluginPage(p)).toContain('data-panel="status" data-state=running');
    expect(st).toContain('data-state=running>');
    expect(st).toContain('Runs as a container (tandem-plugin-hello-tool). What it adds is registered.');
    expect(st).toContain('data-fact=restarts');
    // start is not offered while it runs
    expect(st).toMatch(/data-act=start \?disabled=true title=It runs already\./);
    expect(st).toMatch(/data-act=stop \?disabled=false/);
    expect(await p.flow.act('stop')).toBe(true);
    await settle();
    expect([p.flow.one!.status!.state, p.flow.one!.status!.held]).toEqual(['stopped', true]);
    st = part(pluginPage(p), 'status');
    expect(st).toContain('Stopped by hand. It stays stopped until you start it.');
    expect(st).toMatch(/data-act=start \?disabled=false/);
    expect(part(pluginPage(p), 'adds')).toContain('data-name=greet data-live=false');
    // the clock does not start it again
    p.r.wait(60_000);
    await p.flow.refresh();
    expect(p.flow.one!.status!.state).toBe('stopped');
    expect(await p.flow.act('start')).toBe(true);
    await settle();
    expect(p.flow.one!.status!.state).toBe('starting');
    expect(part(pluginPage(p), 'status')).toContain('waits for its first answer');
    p.r.wait(2000);
    await p.flow.refresh();
    await settle();
    expect(p.flow.one!.status!.state).toBe('running');
    expect(await p.flow.act('restart')).toBe(true);
    await settle();
    expect(p.flow.one!.status!.restarts).toBe(1);
    // the log: the plugin host's lines, newest last, read again on demand and at another length
    p.r.wait(2000);
    await p.flow.readLogs();
    const log = part(pluginPage(p), 'log');
    expect(p.flow.one!.logs.lines!.at(-1)).toContain('[host] registered 1 tool(s)');
    expect(log).toContain('[host] stopped by hand: held until a start by hand');
    expect(log).toContain('[host] restarted by hand');
    expect(log).toContain('class=plg-log-h');
    expect(log).toContain('data-act="log-reload"');
    await p.flow.readLogs(50);
    expect(p.r.paths.at(-1)).toBe('GET /v1/fleet/plugins/hello-tool/logs?tail=50');
    expect(p.flow.one!.logs.tail).toBe(50);
    // kept read while the page shows
    p.flow.followLogs(true);
    await settle();
    const n = p.r.paths.filter((x) => x.includes('/logs')).length;
    await p.flow.refresh();
    expect(p.r.paths.filter((x) => x.includes('/logs')).length).toBe(n + 1);
  });

  it('a plugin that keeps exiting: restarting with its last error, then given up and turned off, with what to do', async () => {
    const p = pages();
    await p.flow.open('hello-tool');
    await settle();
    p.r.reg.control({ crash: 'hello-tool' });
    await p.flow.refresh();
    await settle();
    expect(p.flow.one!.status!.state).toBe('backoff');
    let st = part(pluginPage(p), 'status');
    expect(st).toContain('data-state=restarting>');
    expect(st).toContain('The plugin host starts it again in 1 s; after five failures within an hour it gives up.');
    expect(st).toContain('data-last-error');
    expect(st).toContain('ECONNREFUSED');
    p.r.wait(30_000);
    await p.flow.refresh();
    await settle();
    expect(p.flow.one!.status!.state).toBe('failed');
    expect(p.flow.one!.plugin!.enabled).toBe(false);
    const page = pluginPage(p);
    // the state says what to do: no second call to turn it on above it
    expect(page).not.toContain('data-setup');
    st = part(page, 'status');
    expect(st).toContain('data-state=failed>');
    expect(st).toContain('It failed 5 times within an hour, so the plugin host gave up and turned it off. Read its log, then turn it on again.');
    expect(st).toContain('.checked=false');
    await p.flow.readLogs();
    const log = part(pluginPage(p), 'log');
    expect(log).toContain('gave up and disabled hello-tool');
    expect(log).toContain('class=plg-log-e');
    // start needs it on again
    expect(await p.flow.act('start')).toBe(false);
    expect(p.flow.one!.act.refused!.type).toBe('disabled');
    expect(part(pluginPage(p), 'status')).toContain('The plugin is turned off.');
  });

  it('no sandbox runtime on the machine: unavailable, and why; a hook whose payload is not granted is told as not registered', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    p.r.reg.control({ sandbox: false });
    await p.flow.open('hello-tool');
    await settle();
    await p.flow.act('restart');
    await settle();
    expect(p.flow.one!.status!.state).toBe('unavailable');
    expect(part(pluginPage(p), 'status')).toContain('This machine has no sandbox runtime (Docker, Podman or Kubernetes), so a sandboxed plugin cannot start.');
    p.r.reg.control({ sandbox: true });

    // audit-hook's hook needs prompts:meta: taken back, the plugin runs on and its hook is not registered
    await p.flow.open('audit-hook');
    await settle();
    expect(part(pluginPage(p), 'adds')).toContain('data-name=finished data-live=true');
    p.flow.grant('prompts:meta', false);
    expect(part(pluginPage(p), 'may')).toContain('taken back');
    await applyStaged(p);
    p.r.wait(2000);
    await p.flow.refresh();
    await settle();
    const page = pluginPage(p);
    expect(part(page, 'adds')).toContain('data-name=finished data-live=false');
    expect(part(page, 'status')).toContain('data-notes');
    expect(part(page, 'status')).toContain('The hook finished needs prompts:meta, which is not granted.');
  });

  it('an engine-stats package is recorded and not run: no switch, no log, its install line; a plugin that is gone says so', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.open('tandemllm-vllm-plugin');
    await settle();
    const page = pluginPage(p);
    expect(page).toContain('data-kind=engine-stats');
    for (const name of ['status', 'log', 'trust', 'may', 'settings']) expect(page).not.toContain(`data-panel="${name}"`);
    expect(page).toContain('data-engine-stats');
    expect(page).toContain('pip install plugins/vllm');
    expect(p.r.paths.some((x) => x.includes('/status') || x.includes('/logs'))).toBe(false);

    await p.flow.open('nope');
    expect(p.flow.one!.missing).toBe(true);
    expect(text(pluginGone('nope'))).toContain('No plugin nope');
  });
});

describe('an upgrade, the way back, and the same version again', () => {
  it('another version from a wheel: the page says what it replaces and what is new; what still fits is kept, the rest is dropped and listed', async () => {
    const p = pages();
    await p.flow.open('hello-tool');
    await settle();
    p.flow.type(['greeting'], field(p, 'greeting'), 'Hi');
    p.flow.put(['excited'], true);
    await applyStaged(p);
    expect(p.r.reg.rows['hello-tool'].settings).toEqual({ greeting: 'Hi', excited: true });
    // something staged for the old version
    p.flow.put(['excited'], false);
    await p.flow.load();
    // the upgrade starts from where it came from
    p.flow.startInstall({ id: 'hello-tool', source: p.flow.one!.plugin!.source });
    expect([p.flow.install!.form.type, p.flow.install!.form.path, p.flow.install!.upgrade]).toEqual(['folder', '/srv/plugins/hello-tool', 'hello-tool']);
    expect(installPage(p)).toContain('Upgrade Hello tool');
    p.flow.editSource({ type: 'wheel', path: '/srv/dist/hello_tool-0.2.0-py3-none-any.whl' });
    expect(await p.flow.inspect()).toBe(true);
    let page = installPage(p);
    expect(page).toContain('Hello tool 0.2.0 passed every check');
    expect(page).toContain('It replaces the installed version 0.1.0.');
    expect(page).toContain('data-move="upgrade"');
    expect(page).toContain('Upgrade to 0.2.0');
    expect(page).toContain('sha256 of the wheel');
    expect(page).toContain('data-capability=settings:read data-risk=low');
    expect(page).toContain('data-name=greeter');
    const done = await p.flow.commit();
    expect([done!.action, done!.from, done!.kept, done!.dropped]).toEqual(['upgraded', '0.1.0', ['settings.greeting'], ['settings.excited']]);
    expect(p.r.paths).toContain('POST /v1/fleet/plugins');
    page = installPage(p);
    expect(page).toContain('Hello tool went from 0.1.0 to 0.2.0');
    expect(page).toContain('data-kept');
    expect(page).toContain('data-dropped');
    expect(page).toContain('settings.excited');
    // a capability that is new starts ungranted, and what was staged for the old version is gone
    expect(done!.plugin.capabilities).toEqual({ requested: ['settings:read'], granted: [], grants: {} });
    expect(p.draft.empty).toBe(true);
    expect(p.r.reg.rows['hello-tool'].enabled).toBe(true);
  });

  it('an older version is asked for on purpose; the same version with other files is refused; the same files again change nothing', async () => {
    const p = pages();
    await p.flow.load();
    expect(await check(p, { path: '/srv/plugins/hello-tool-0.0.9' })).toBe(true);
    let page = installPage(p);
    expect(page).toContain('data-move="downgrade"');
    expect(page).toContain('is older than the installed');
    expect(p.flow.installBlocked()).toBe('Say that you want the older version.');
    expect(await p.flow.commit()).toBeNull();
    p.flow.setDowngrade(true);
    expect(p.flow.installBlocked()).toBeNull();
    const done = await p.flow.commit();
    expect([done!.action, done!.from, done!.plugin.version]).toEqual(['upgraded', '0.1.0', '0.0.9']);
    // the controller refuses the way back when it is not asked for
    const raw = await p.r.send('POST', '/v1/fleet/plugins', { source: { type: 'folder', path: '/srv/plugins/hello-tool-0.0.9' }, upgrade: true });
    expect(raw.status).toBe(200);
    const q = pages();
    const down = await q.r.send('POST', '/v1/fleet/plugins', { source: { type: 'folder', path: '/srv/plugins/hello-tool-0.0.9' }, upgrade: true });
    expect([down.status, (down.body as { error: { type: string } }).error.type]).toEqual([409, 'downgrade']);
    const exists = await q.r.send('POST', '/v1/fleet/plugins', { source: { type: 'wheel', path: '/srv/dist/hello_tool-0.2.0-py3-none-any.whl' } });
    expect([exists.status, (exists.body as { error: { type: string; installed: string; version: string } }).error]).toEqual([409, expect.objectContaining({ type: 'exists', installed: '0.1.0', version: '0.2.0' })]);

    expect(await check(q, { path: '/srv/plugins/hello-tool-changed' })).toBe(true);
    page = installPage(q);
    expect(page).toContain('Hello tool 0.1.0 is installed with other files');
    expect(q.flow.installBlocked()).toBe('A changed plugin carries a new version.');
    expect(page).toMatch(/data-act="install-go" \?disabled=true/);

    expect(await check(q, { path: '/srv/plugins/hello-tool' })).toBe(true);
    page = installPage(q);
    expect(page).toContain('Hello tool 0.1.0 is installed already');
    expect(q.flow.installBlocked()).toBe('This version is installed already.');
    expect(installs(q).length).toBe(2);
  });
});

describe('a refused install shows the reason', () => {
  it('a branch, a short id, a path that is not a full one: told before anything is sent', async () => {
    const p = pages();
    expect(await check(p, { type: 'git', url: GIT_URL, commit: 'main' })).toBe(false);
    expect(p.flow.install!.errors.commit).toContain('A branch or a tag can move, so neither is taken.');
    expect(installPage(p)).toContain('Paste the full commit id: 40 hex digits (git rev-parse HEAD).');
    p.flow.editSource({ commit: '3f2a9c1' });
    expect(p.flow.install!.errors.commit).toContain('That is a short id (7 of 40 digits).');
    p.flow.editSource({ url: 'http://git.example.com/x.git', commit: GIT_COMMIT });
    expect(p.flow.install!.errors).toEqual({ url: 'An https, ssh or file URL. Plain http and git:// are not taken.' });
    expect(await check(p, { path: 'plugins/hello-tool' })).toBe(false);
    expect(p.flow.install!.errors.path).toBe('A full path from the root, like /srv/plugins/hello-tool.');
    expect(await check(p, { type: 'wheel', path: '/srv/dist/hello.zip' })).toBe(false);
    expect(p.flow.install!.errors.path).toBe('A wheel’s name ends in .whl.');
    expect(p.r.paths).toEqual([]);
    // an upper-case commit id is the same commit
    expect(await check(p, { type: 'git', url: GIT_URL, commit: GIT_COMMIT.toUpperCase(), subdir: 'host-metrics' })).toBe(true);
  });

  it('what the controller refuses of a source lands on its field, with what to do', async () => {
    const p = pages();
    expect(await check(p, { path: '/srv/plugins/nope' })).toBe(false);
    expect([p.flow.install!.step, p.flow.install!.refused!.type, p.flow.install!.errors.path]).toEqual(['form', 'not_found', 'no folder /srv/plugins/nope']);
    let page = installPage(p);
    expect(page).toContain('Nothing is there on the controller’s machine.');
    expect(page).toContain('The path is read on the machine TandemApp’s controller runs on, not on this device.');
    expect(page).toContain('no folder /srv/plugins/nope');
    expect(await check(p, { type: 'wheel', path: '/srv/dist/broken.whl' })).toBe(false);
    expect(installPage(p)).toContain('That file is not a wheel TandemApp can read.');
    expect(await check(p, { type: 'git', url: GIT_URL, commit: 'a'.repeat(40) })).toBe(false);
    expect(p.flow.install!.errors.commit).toContain('is not a commit of');
    expect(installPage(p)).toContain('The repository has no such commit.');
    expect(await check(p, { type: 'git', url: 'https://git.example.com/other.git', commit: GIT_COMMIT })).toBe(false);
    page = installPage(p);
    expect(page).toContain('git could not fetch the commit.');
    expect(page).toContain('Could not resolve host');
    expect(await check(p, { type: 'git', url: 'https://git.example.com/acme/with-submodule.git', commit: GIT_COMMIT })).toBe(false);
    expect(installPage(p)).toContain('The commit holds a submodule, whose code is not in the commit.');
    expect(await check(p, { type: 'git', url: GIT_URL, commit: GIT_COMMIT })).toBe(false);
    expect(installPage(p)).toContain('There is no plugin.json there.');
    expect(installs(p)).toEqual([]);
  });
});

describe('removing a plugin, and checking its files', () => {
  it('a hook names it: the page says so before the question, the controller refuses with the hooks, and it goes once they are gone', async () => {
    const p = pages(rig({ plugins: 'demo' }));
    await p.flow.load();
    await p.flow.open('audit-hook');
    await settle();
    let page = pluginPage(p);
    expect(page).toContain('data-panel="remove" data-removable=false');
    expect(part(page, 'remove')).toContain('It cannot go yet: a hook names it.');
    expect(part(page, 'remove')).toContain('href=#/app/hooks/hook/audit-to-disk');
    expect(part(page, 'adds')).toContain('data-references=1');
    p.flow.setEnabled(false);
    p.flow.askRemove(true);
    expect(await p.flow.remove()).toBe(false);
    expect(p.flow.one!.remove.refused!.type).toBe('referenced');
    page = part(pluginPage(p), 'remove');
    expect(page).toContain('data-error=referenced');
    expect(page).toContain('1 hook still names this plugin, so it stays.');
    expect(page).toContain('Remove those hooks, or point them at something else. Then remove the plugin.');
    expect(page).toContain('Audit to disk');
    expect(page).toContain('Try again');
    expect(p.r.reg.rows['audit-hook']).toBeDefined();
    // the hook is removed: now it goes, with what was staged for it
    p.r.reg.control({ unref: 'audit-hook' });
    expect(await p.flow.remove()).toBe(true);
    expect(p.r.reg.rows['audit-hook']).toBeUndefined();
    expect(p.flow.one).toBeNull();
    expect(p.flow.info!.plugins.map((x) => x.id)).not.toContain('audit-hook');
    expect(p.draft.empty).toBe(true);
    // its keys and versions went with it
    const o = await p.r.settings.overview();
    expect(o.scopes.find((s) => s.scope === 'plugins')!.targets.map((t) => t.id)).not.toContain('audit-hook');
  });

  it('its files held against the sha256 of the install: unchanged, then changed, with both hashes', async () => {
    const p = pages();
    await p.flow.open('hello-tool');
    await settle();
    let files = part(pluginPage(p), 'files');
    expect(pluginPage(p)).toContain('data-panel="files" data-integrity=ok');
    expect(files).toContain(`data-sha=${p.flow.one!.plugin!.sha256}`);
    expect(files).toContain('data-chip=integrity>files unchanged</span>');
    expect(files).toContain('href=#/app/plugins/install?upgrade=hello-tool');
    expect(await p.flow.act('verify')).toBe(true);
    expect(p.flow.one!.act.verified!.integrity).toBe('ok');
    expect(part(pluginPage(p), 'files')).toContain('Checked just now.');
    p.r.reg.control({ tamper: 'hello-tool' });
    expect(await p.flow.act('verify')).toBe(true);
    await settle();
    const page = pluginPage(p);
    files = part(page, 'files');
    expect(page).toContain('data-panel="files" data-integrity=mismatch');
    expect(files).toContain('data-chip=integrity>files changed</span>');
    expect(files).toContain('sha256 of the files now');
    expect(page).toContain('data-integrity-alert');
    // turning it on again is refused in the review
    p.flow.setEnabled(false);
    await applyStaged(p);
    p.flow.setEnabled(true);
    await p.sheet.review(p.draft.changes());
    expect(p.sheet.state.error!.type).toBe('bad_value');
    expect(sheet(p)).toContain('its tree is mismatch since the install');
  });
});

describe('a panel renders in a sandboxed iframe and a failing panel shows a finding', () => {
  it('the frame: sandboxed, never with the page’s origin, at the panel’s own address, with what the bridge offers it', async () => {
    const p = pages();
    const id = await install(p, '/srv/plugins/usage-board');
    await turnOn(p, id);
    expect(part(pluginPage(p), 'adds')).toContain('data-name=board data-live=true');
    expect(part(pluginPage(p), 'adds')).toMatch(/class="btn btn-sm btn-primary" href=#\/app\/plugins\/p\/usage-board\/panel\/board data-act=panel-open/);
    await p.flow.openPanel(id, 'board');
    expect(p.flow.panel!.step).toBe('ready');
    const page = panelPage(p);
    expect(page).toContain('data-panel-ready');
    expect(page).toMatch(/<iframe class="plg-frame" data-panel-frame=board title=Usage board, a panel of Usage board sandbox=allow-scripts allow-forms referrerpolicy="no-referrer" src=\/plugin-panels\/usage-board\/board\/>/);
    expect(page).not.toContain('allow-same-origin');
    expect(page).toContain('plugin.info');
    expect(page).toContain('call its own tools that only read');
    expect(page).not.toContain('settings.read');
    expect(page).toContain('It has not asked for anything yet.');
    // whatever the controller names, the frame gets scripts and forms and nothing more
    expect(sandboxOf('allow-scripts allow-same-origin allow-forms allow-top-navigation allow-popups')).toBe('allow-scripts allow-forms');
    expect(sandboxOf('')).toBe('');
  });

  it('a failing panel is a finding with what to do, never an empty frame', async () => {
    const p = pages();
    const id = await install(p, '/srv/plugins/usage-board');
    // the plugin is off: its panel is not served
    await p.flow.openPanel(id, 'board');
    expect(p.flow.panel!.step).toBe('failed');
    let page = panelPage(p);
    expect(page).toContain('data-panel-finding');
    expect(page).toContain('The plugin is not running, so its panel is not served.');
    expect(page).toContain('Turn it on, then open the panel again.');
    expect(page).not.toContain('<iframe');
    expect(page).toContain('data-act="panel-reload"');
    await turnOn(p, id);
    // stopped by hand
    await p.flow.act('stop');
    await settle();
    await p.flow.openPanel(id, 'board');
    expect(panelPage(p)).toContain('Start it on its page, then open the panel again.');
    await p.flow.act('start');
    p.r.wait(2000);
    await settle();
    // the plugin lists the panel, and its file is not there
    p.r.reg.control({ panel: 'usage-board/board' });
    await p.flow.openPanel(id, 'board');
    page = panelPage(p);
    expect(p.flow.panel!.step).toBe('failed');
    expect(page).toContain('The panel did not load.');
    expect(page).toContain('That is a fault in the plugin: tell its author.');
    expect(page).toContain('/plugin-panels/usage-board/board/ answered 404');
    expect(page).not.toContain('<iframe');
    p.r.reg.control({ panel: null });
    // a panel it does not have, and a plugin that is gone
    await p.flow.openPanel(id, 'nope');
    expect(panelPage(p)).toContain('Usage board has no panel nope.');
    await p.flow.openPanel('gone', 'board');
    expect(panelPage(p)).toContain('No plugin gone is installed.');
    // loaded again once it is back
    await p.flow.openPanel(id, 'board');
    expect(p.flow.panel!.step).toBe('ready');
    expect(panelPage(p)).toContain('src=/plugin-panels/usage-board/board/?load=');
  });

  it('the page’s side of the bridge: only the methods this plugin was given are relayed; the rest is refused here; the answer carries the call’s id', async () => {
    const p = pages();
    const id = await install(p, '/srv/plugins/usage-board');
    await turnOn(p, id);
    await p.flow.openPanel(id, 'board');
    const sent = (): number => p.r.paths.filter((x) => x === 'POST /v1/fleet/plugins/usage-board/bridge').length;
    // a call it may make: relayed, answered with its id
    expect(await p.flow.relay({ type: 'tandem-bridge', id: 1, method: 'plugin.info' })).toEqual({ type: 'tandem-bridge-result', id: 1, result: { id: 'usage-board', version: '1.4.0', title: 'Usage board', capabilities: [] } });
    const usage = await p.flow.relay({ type: 'tandem-bridge', id: 'u-2', method: 'tools.call', params: { tool: 'usage_today', arguments: {} } });
    expect([usage!.id, usage!.error]).toEqual(['u-2', undefined]);
    expect(JSON.stringify(usage!.result)).toContain('ada');
    expect(sent()).toBe(2);
    // a method it was not given, and one that does not exist: refused on the page, nothing is sent
    const read = await p.flow.relay({ type: 'tandem-bridge', id: 3, method: 'settings.read' });
    expect(read).toEqual({ type: 'tandem-bridge-result', id: 3, error: { type: 'refused', message: 'the bridge does not offer settings.read to this plugin' } });
    expect((await p.flow.relay({ type: 'tandem-bridge', id: 4, method: 'engine.control', params: { action: 'restart' } }))!.error!.type).toBe('refused');
    expect((await p.flow.relay({ type: 'tandem-bridge', id: 5, method: 'fs.read', params: { path: '/etc/passwd' } }))!.error!.type).toBe('refused');
    expect((await p.flow.relay({ type: 'tandem-bridge', id: 6, method: 'plugin.info', params: ['x'] }))!.error).toEqual({ type: 'bad_request', message: 'params: an object' });
    expect((await p.flow.relay({ type: 'tandem-bridge', id: 7, method: 'tools.call', params: { tool: 'usage_today', arguments: { blob: 'x'.repeat(70_000) } } }))!.error!.type).toBe('bad_request');
    expect(sent()).toBe(2);
    // what is not a bridge call gets no answer at all
    for (const junk of [null, 'tandem-bridge', 42, { type: 'other', id: 1, method: 'plugin.info' }, { type: 'tandem-bridge', method: 'plugin.info' }, { type: 'tandem-bridge', id: {}, method: 'plugin.info' }]) expect(await p.flow.relay(junk)).toBeNull();
    expect(sent()).toBe(2);
    // a tool that changes things: relayed, refused by the controller, and journaled there
    const reset = await p.flow.relay({ type: 'tandem-bridge', id: 8, method: 'tools.call', params: { tool: 'reset_counters', arguments: {} } });
    expect(reset!.error).toEqual({ type: 'refused', message: 'a panel may call only read-only tools; reset_counters is not' });
    expect(sent()).toBe(3);
    const page = panelPage(p);
    expect(page).toContain('data-bridge data-calls=8');
    expect(page).toContain('data-call-ok=false');
    expect(page).toContain('the bridge does not offer settings.read to this plugin');
    await p.flow.open(id);
    await settle();
    const st = part(pluginPage(p), 'status');
    expect(st).toContain('data-refusals=1');
    expect(st).toContain('The tool reset_counters (not read-only) from its panel');
    // once settings:read is granted, the bridge offers it and the page relays it
    p.flow.grant('settings:read', true);
    await applyStaged(p);
    p.r.wait(2000);
    await p.flow.openPanel(id, 'board');
    expect(p.flow.panel!.panel!.bridge.methods).toEqual(['plugin.info', 'plugin.status', 'settings.read', 'tools.call']);
    expect(await p.flow.relay({ type: 'tandem-bridge', id: 9, method: 'settings.read' })).toEqual({ type: 'tandem-bridge-result', id: 9, result: { settings: { refresh_s: 30, currency: 'USD', export: { enabled: false, url: null }, hide_users: [] } } });
    // a panel that is not showing relays nothing
    p.flow.closePanel();
    expect(await p.flow.relay({ type: 'tandem-bridge', id: 10, method: 'plugin.info' })).toBeNull();
  });
});

describe('the pages work at 360 px width', () => {
  const css = readFileSync(new URL('../src/styles/plugins.css', import.meta.url), 'utf8');
  const narrow = css.slice(css.indexOf('@container mon (max-width: 640px)'));

  it('the narrow layout stacks every grid of the pages into one column, and keeps the controls a thumb can hit', () => {
    expect(narrow.length).toBeGreaterThan(800);
    // a plugin's row, a capability, what it adds, a journal's line, a group of settings and the sha256 stack
    for (const sel of ['.plg-item', '.plg-cap', '.plg-row', '.plg-journal > li', '.plg-group', '.plg-sha']) {
      const rule = narrow.split(`\n  ${sel} {`)[1]?.split('}')[0] ?? '';
      expect(rule, sel).toContain('grid-template-columns: minmax(0, 1fr);');
    }
    // start, stop and restart share the width; the buttons that decide are full-size targets
    expect(narrow).toMatch(/\.plg-controls \.btn \{[^}]*flex: 1;[^}]*min-height: 40px;/);
    expect(narrow).toMatch(/\.plg-setup \.btn,\n  \.plg-install \.st-acts \.btn \{[^}]*width: 100%;[^}]*min-height: 42px;/);
    expect(narrow).toMatch(/\.plg-item-a \{\n    min-height: 44px;/);
    // a grant's switch moves under its words
    expect(narrow).toMatch(/\.plg-cap-e \{[^}]*flex-direction: row;/);
    // the frame keeps most of the screen and is not dragged by a finger
    expect(narrow).toMatch(/\.plg-frame-w \{\n    height: 68vh;\n    resize: none;/);
  });

  it('nothing in the pages has a fixed width that a phone cannot hold, and long ids, paths and hashes wrap', () => {
    const wide = [...css.matchAll(/(?:^|\s)(?:min-)?width: (\d+)px/g)].map((m) => Number(m[1])).filter((n) => n > 300);
    expect(wide).toEqual([]);
    // the one grid with a wide minimum is the settings group, which is one column when narrow
    const mins = [...css.matchAll(/minmax\((\d+)px/g)].map((m) => Number(m[1]));
    expect(Math.max(...mins)).toBeLessThanOrEqual(240);
    for (const sel of ['.plg-item-n', '.plg-sha > code', '.plg-row-b', '.plg-item-m', '.plg-cap-b code', '.plg-err']) expect(css.split(`\n${sel} {`)[1].split('}')[0], sel).toContain('overflow-wrap: anywhere');
    // the log scrolls inside its own box, never the page
    expect(css.split('\n.plg-log {')[1].split('}')[0]).toContain('overflow: auto');
    // the pages share the hooks pages' and the settings pages' rows, which have their own narrow rules
    const hooks = readFileSync(new URL('../src/styles/hooks.css', import.meta.url), 'utf8');
    expect(hooks.slice(hooks.indexOf('@container mon (max-width: 640px)'))).toMatch(/\.hk-cards,\n  \.hk-grid \{\n    grid-template-columns: minmax\(0, 1fr\);/);
  });
});
