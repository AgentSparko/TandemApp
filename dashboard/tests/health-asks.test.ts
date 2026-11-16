// Tests for SRV-835's later asks of the health page, against the mock controller: the upgrade's
// default release named in its plan, a fix's job read again (its later failure too), the plugins
// check naming its worst finding, and the menu's badge from the state view, which checks nothing.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FOLLOW_MS, FOLLOW_FOR_MS } from '../src/lib/health-flow.ts';
import { badgeOf, watchHealthBadge } from '../src/lib/menu-badge.ts';
import type { HealthStateView } from '../src/api/health.ts';
import { card, codes, dialog, rig, row, rowText, type HealthRig } from './health-rig.ts';

const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0));
async function seconds(r: HealthRig, n: number): Promise<void> {
  for (let i = 0; i < n; i++) {
    r.wait(1000);
    r.flow.tick();
    await settle();
  }
}

describe('the upgrade names its release', () => {
  it('no release typed: the plan is for the default, and the dialog says so', async () => {
    const r = rig('mixed');
    r.mock.control({ ref: 'v0.16.1' });
    await r.flow.load();
    const id = r.flow.view!.engines.find((e) => e.findings.some((f) => f.code === 'engine_too_old'))!.id;
    await r.flow.press(row(r, id, 'engine_too_old'), id);
    await r.flow.dialogGo();
    expect(r.bodies.at(-1)).toEqual({ action: 'upgrade' });
    const d = r.flow.dialog!;
    expect([d.phase, d.planRef, d.refSource, d.defaultRef]).toEqual(['plan', 'v0.16.1', 'default', 'v0.16.1']);
    expect(dialog(r)).toContain('data-plan-ref=default');
    expect(dialog(r)).toContain('the one this TandemApp is set to (TANDEM_ENGINE_REF)');
    // a typed release: asked, and the default named beside it
    r.flow.dialogBack();
    r.flow.dialogSet({ ref: 'v0.16.0' });
    await r.flow.dialogGo();
    expect([r.flow.dialog!.planRef, r.flow.dialog!.refSource]).toEqual(['v0.16.0', 'asked']);
    expect(dialog(r)).toContain('this TandemApp is set to v0.16.1');
  });
});

describe('a fix’s job, read again', () => {
  it('the upgrade’s job is followed by its id until it ends, and a later failure shows in the host’s words', async () => {
    const r = rig('mixed');
    await r.flow.load();
    const id = r.flow.view!.engines.find((e) => e.findings.some((f) => f.code === 'engine_too_old'))!.id;
    const key = row(r, id, 'engine_too_old').key;
    await r.flow.press(row(r, id, 'engine_too_old'), id);
    r.flow.dialogSet({ ref: 'v0.16.0' });
    await r.flow.dialogGo();
    await r.flow.dialogApply();
    r.flow.dialogClose();
    const job = r.flow.notes[key]?.job;
    expect(job).toMatch(/^\d{8}T\d{6}-[0-9a-f]{6}$/);
    await seconds(r, FOLLOW_MS / 1000);
    expect(r.paths.at(-1)).toBe(`GET /v1/fleet/health/${id}/jobs/${job}`);
    expect(rowText(r, id, 'engine_too_old')).toContain('data-note=working');
    // the host's job fails while it waits
    r.mock.failJob(id, 'the new release did not start; rolled back to 0.15.2');
    await seconds(r, FOLLOW_MS / 1000);
    const t = rowText(r, id, 'engine_too_old');
    expect(t).toContain('data-note=still');
    expect(t).toContain('Upgrade did not finish.');
    expect(t).toContain('The upgrade job failed: the new release did not start; rolled back to 0.15.2.');
    expect(r.flow.following).not.toContain(id);
  });

  it('a job a newer one replaced: 404 job_not_found, and the target is read again', async () => {
    const r = rig('mixed');
    await r.flow.load();
    await expect(r.api.job('spark-rack', '20261116T090000-abcdef')).rejects.toMatchObject({ status: 404 });
    const a = await r.send('GET', '/v1/fleet/health/spark-rack/jobs/20261116T090000-abcdef');
    expect((a.body as { error: { code: string } }).error.code).toBe('job_not_found');
    // chatd's restart, read by its id
    r.mock.control({ controller: { chatd: 'down' } });
    await r.flow.load({ fresh: true });
    const fix = await r.api.fix('controller', { action: 'restart_chatd' });
    const got = await r.api.job('controller', fix.job!.id!);
    expect([got.ended, got.job.state]).toEqual([true, 'done']);
  });

  it('a start that clears its finding is followed by reading the target, and the follow stops in time', async () => {
    const r = rig('mixed');
    await r.flow.load();
    await r.flow.press(row(r, 'spark-rack', 'engine_down'), 'spark-rack');
    await seconds(r, FOLLOW_FOR_MS / 1000);
    expect(codes(r, 'spark-rack')).toEqual([]);
    expect(r.flow.following).toEqual([]);
  });
});

describe('the plugins check', () => {
  it('names its worst finding, not the first', async () => {
    const r = rig('all');
    await r.flow.load();
    const c = r.flow.view!.controller.checks.find((x) => x.check === 'plugins');
    expect(c).toEqual({ check: 'plugins', ok: false, code: 'plugin_tampered' });
    expect(card(r, 'controller').state).toBe('error');
  });
});

describe('the menu’s badge', () => {
  it('the entry carries the badge, not the health page: the module reads the state view only', () => {
    const src = readFileSync(join(__dirname, '..', 'src', 'lib', 'menu-badge.ts'), 'utf8');
    expect(src).not.toMatch(/^import (?!type ).*health/m);
    expect(src).toContain("'/v1/fleet/health?view=state'");
    expect(readFileSync(join(__dirname, '..', 'src', 'app.ts'), 'utf8')).toContain("from './lib/menu-badge'");
  });

  it('the state view checks nothing: unknown first, then the kept answers counted', async () => {
    const quiet = rig('ok');
    const none = await quiet.api.state();
    expect([none.state, none.targets.checked, none.checked_at, none.pairings]).toEqual(['unknown', 0, null, 0]);
    expect(badgeOf(none)).toBeNull();
    // the refused pairing codes count before any target is checked: they need no check
    const r = rig('mixed');
    const first = await r.api.state();
    expect([first.targets.checked, first.checked_at]).toEqual([0, null]);
    expect(first.pairings).toBeGreaterThan(0);
    expect(first.counts.error + first.counts.warning + first.counts.info).toBe(first.pairings);
    await r.flow.load();
    const n = r.paths.length;
    const v = await r.api.state();
    expect(r.paths.length).toBe(n + 1);
    expect(v.state).toBe(r.flow.view!.state);
    expect(v.targets.checked).toBe(v.targets.total);
    const all = [r.flow.view!.controller, ...r.flow.view!.engines].flatMap((t) => t.findings).concat(r.flow.view!.pairings.map((p) => p.finding));
    expect(v.counts.error).toBe(all.filter((f) => f.severity === 'error').length);
    expect(v.counts.warning).toBe(all.filter((f) => f.severity === 'warning').length);
    expect(v.pairings).toBe(r.flow.view!.pairings.length);
    const b = badgeOf(v)!;
    expect(b).toEqual({ n: v.counts.error + v.counts.warning, tone: v.state === 'error' ? 'err' : 'warn', label: `Health: ${b.n} problems` });
    // nothing was checked again by it: the controller's kept answer is as old as before
    r.wait(30_000);
    expect((await r.api.state()).age_s).toBeGreaterThanOrEqual(30);
    const bad = await r.send('GET', '/v1/fleet/health?view=other');
    expect(bad.status).toBe(400);
  });

  it('the badge words: one problem, errors red, nothing when ok or unknown', () => {
    const v = (state: HealthStateView['state'], error: number, warning: number): HealthStateView => ({ contract_version: '1.0', state, counts: { error, warning, info: 2 }, targets: { total: 2, checked: 2 }, pairings: 0, checked_at: 1, age_s: 0 });
    expect(badgeOf(v('warning', 0, 1))).toEqual({ n: 1, tone: 'warn', label: 'Health: 1 problem' });
    expect(badgeOf(v('error', 2, 3))).toEqual({ n: 5, tone: 'err', label: 'Health: 5 problems' });
    expect(badgeOf(v('ok', 0, 0))).toBeNull();
    expect(badgeOf(v('unknown', 0, 0))).toBeNull();
    expect(badgeOf(null)).toBeNull();
  });

  it('the watcher reads the state, and a read that fails clears the badge', async () => {
    // the tests run without a DOM: the poll's visibility is stubbed
    const g = globalThis as { document?: unknown };
    const had = g.document;
    g.document = { visibilityState: 'visible', addEventListener: () => undefined, removeEventListener: () => undefined };
    const got: unknown[] = [];
    let fail = false;
    const stop = watchHealthBadge(
      (b) => got.push(b),
      async () => {
        if (fail) throw new Error('offline');
        return { contract_version: '1.0', state: 'error', counts: { error: 1, warning: 0, info: 0 }, targets: { total: 1, checked: 1 }, pairings: 0, checked_at: 1, age_s: 0 };
      },
      10,
    );
    await settle();
    expect(got).toEqual([{ n: 1, tone: 'err', label: 'Health: 1 problem' }]);
    fail = true;
    await new Promise((r) => setTimeout(r, 30));
    stop();
    g.document = had;
    expect(got.at(-1)).toBeNull();
  });
});
