// Tests for SRV-835: what the health page says, without a browser (src/lib/health.ts). The verdict
// over everything that was checked, the cards and their order, a card's trace, the words for a
// check, a job and what a fix left, the facts worth a line, and the floor under a host key that is
// not the one expected.
import { describe, expect, it } from 'vitest';
import { healthFinding, actionView } from '../mock/health.ts';
import type { HealthFix, HealthTarget, HealthView } from '../src/api/health.ts';
import { actionOf, ago, cardTarget, cardsOf, factsOf, fingerprintsOf, foldWords, guideLink, isGuide, isHostKeyStop, jobLive, jobWords, merged, normalized, noteOf, numberWord, reachOf, signInWhat, stateWords, traceOf, verdict, withTarget, worstFirst } from '../src/lib/health.ts';
import { card, cards, rig } from './health-rig.ts';

const FP_A = 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
const FP_B = 'SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';

async function view(scenario: Parameters<typeof rig>[0], fleet: 'fleet4' | 'empty' = 'fleet4'): Promise<HealthView> {
  const r = rig(scenario, { fleet });
  await r.flow.load();
  return r.flow.view!;
}

describe('the verdict: how it stands, in words', () => {
  it('everything works', async () => {
    const v = verdict(await view('ok'));
    expect([v.state, v.title]).toEqual(['ok', 'Everything works.']);
    expect(v.sub).toBe('Checked: this TandemApp and two engine hosts.');
    expect([v.errors, v.warnings, v.fixable, v.setup]).toEqual([0, 0, 0, false]);
  });
  it('things that do not work are counted in words, then what works worse, then what TandemApp can fix itself', async () => {
    const v = verdict(await view('mixed'));
    expect([v.state, v.title]).toEqual(['error', 'Two things do not work.']);
    expect(v.sub).toBe('Six more work worse than they should. TandemApp can fix three of them itself. Checked: this TandemApp and four engine hosts.');
    expect([v.errors, v.warnings, v.fixable]).toEqual([2, 6, 3]);
  });
  it('one warning alone reads as working; a fix that is the user’s to make is said to be', async () => {
    const v = verdict(await view('external'));
    expect([v.state, v.title]).toEqual(['warning', 'Everything works, one thing worse than it should.']);
    expect(v.sub).toBe('The fix is yours to make: the card says how. Checked: this TandemApp and its engine host.');
  });
  it('the first run says that no engine is connected and what to do', async () => {
    const v = verdict(await view('first', 'empty'));
    expect([v.state, v.setup, v.title]).toEqual(['ok', true, 'No engine is connected yet.']);
    expect(v.sub).toBe('Install the engine on its host, then pair it here. Checked: this TandemApp.');
  });
  it('one thing, and the fix is TandemApp’s', async () => {
    const r = rig('ok');
    r.mock.host('spark-rack').engine = 'down';
    await r.flow.load();
    const v = verdict(r.flow.view!);
    expect(v.title).toBe('One thing does not work.');
    expect(v.sub).toBe('TandemApp can fix it itself. Checked: this TandemApp and two engine hosts.');
  });
  it('a count reads as a word up to twelve', () => {
    expect([0, 1, 2, 7, 12, 13, 40].map(numberWord)).toEqual(['no', 'one', 'two', 'seven', 'twelve', '13', '40']);
  });
});

describe('the cards', () => {
  it('this TandemApp, each engine host, the plugins, and the refused pairing codes when there are some', async () => {
    const c = cardsOf(await view('mixed'));
    expect(c.map((x) => [x.id, x.kind, x.title, x.state])).toEqual([
      ['controller', 'controller', 'TandemApp', 'warning'],
      ['spark-desk', 'engine', 'spark-desk', 'ok'],
      ['spark-rack', 'engine', 'spark-rack', 'error'],
      ['gpu-box', 'engine', 'gpu-box', 'error'],
      ['spark-attic', 'engine', 'spark-attic', 'warning'],
      ['plugins', 'plugins', 'Plugins', 'warning'],
      ['pairings', 'pairings', 'Add engine', 'warning'],
    ]);
    expect(cardsOf(await view('ok')).map((x) => x.id)).toEqual(['controller', 'spark-desk', 'spark-rack', 'plugins']);
  });
  it('the plugins’ findings and their check are the Plugins card’s, the rest the controller’s', async () => {
    const c = cardsOf(await view('all'));
    const ctl = c.find((x) => x.id === 'controller')!;
    const plug = c.find((x) => x.id === 'plugins')!;
    expect(ctl.rows.every((r) => !r.finding.code.startsWith('plugin_'))).toBe(true);
    expect(ctl.checks.map((k) => k.check)).toEqual(['chatd', 'sandbox', 'memory_cgroup', 'disk', 'backends', 'engines']);
    expect(plug.rows.map((r) => r.finding.code)).toEqual(['plugin_tampered', 'plugin_failed', 'plugin_no_sandbox', 'plugin_untrusted']);
    expect(plug.checks.map((k) => k.check)).toEqual(['plugins']);
    // a plugin's fix goes to the controller: the plugins are no target of their own
    expect(plug.rows.every((r) => r.target === 'controller')).toBe(true);
    expect([cardTarget(ctl), cardTarget(plug), cardTarget(c.find((x) => x.kind === 'engine')!), cardTarget(c.find((x) => x.id === 'pairings')!)]).toEqual(['controller', 'controller', 'spark-far', null]);
  });
  it('two findings of one code are two rows (two backends down)', async () => {
    const ctl = cardsOf(await view('all'))[0];
    const down = ctl.rows.filter((r) => r.finding.code === 'backend_down');
    expect(down.map((r) => r.finding.facts.backend)).toEqual(['DeepSeek', 'rack-gpu']);
    expect(new Set(ctl.rows.map((r) => r.key)).size).toBe(ctl.rows.length);
  });
  it('a card’s state in words; its meta names how it is reached', async () => {
    const c = cardsOf(await view('all'));
    const words = Object.fromEntries(c.map((x) => [x.id, stateWords(x)]));
    expect(words).toMatchObject({ controller: 'Needs a fix', 'spark-serving': 'Works, but worse', 'spark-loading': 'Starting', 'spark-far': 'Needs a fix', plugins: 'Needs a fix', pairings: 'Pairing refused' });
    const ok = cardsOf(await view('ok'));
    expect(ok.map(stateWords)).toEqual(['Works', 'Works', 'Works', 'All run']);
    expect(ok[1].meta).toEqual([{ text: '192.0.2.80', mono: true }, { text: 'engine 0.16.0', mono: false }, { text: 'serves Qwen3.8-27B', mono: false }]);
    expect(ok[0].meta.map((m) => m.text)).toEqual(['This machine', 'Chat runs here']);
    const first = cardsOf(await view('first', 'empty'));
    expect([stateWords(first[0]), first[0].meta[1].text]).toEqual(['Waits for an engine', 'Chat is off']);
  });
});

describe('the order of a card’s findings', () => {
  it('worst first; within a severity the controller’s order stays, so what blocks comes before what depends on it', async () => {
    const r = rig('ok');
    // the engine is down and speaks another major: both errors, the engine's state first
    Object.assign(r.mock.host('spark-desk'), { engine: 'down', api: '2.0' });
    // an older engine (a warning, found first) whose port is blocked (an error, found later)
    Object.assign(r.mock.host('spark-rack'), { api: '1.7', http: 'silent' });
    await r.flow.load();
    expect(card(r, 'spark-desk').rows.map((x) => x.finding.code)).toEqual(['engine_down', 'engine_api_unsupported']);
    expect(r.flow.view!.engines[1].findings.map((f) => f.code)).toEqual(['engine_too_old', 'http_blocked']);
    expect(card(r, 'spark-rack').rows.map((x) => x.finding.code)).toEqual(['http_blocked', 'engine_too_old']);
  });
  it('worstFirst is stable', () => {
    const f = (code: string, severity: 'error' | 'warning' | 'info') => ({ finding: { ...healthFinding('disk_low'), code, severity } });
    expect(worstFirst([f('a', 'info'), f('b', 'warning'), f('c', 'error'), f('d', 'warning'), f('e', 'error')]).map((x) => x.finding.code)).toEqual(['c', 'e', 'b', 'd', 'a']);
  });
  it('the controller’s findings of the worst scenario: chatd and the disk first', async () => {
    const ctl = cardsOf(await view('all'))[0];
    expect(ctl.rows.map((r) => `${r.finding.severity} ${r.finding.code}`)).toEqual(['error chatd_down', 'error disk_low', 'warning sandbox_no_image', 'warning no_memory_cgroup', 'warning backend_down', 'warning backend_down', 'warning harness_unsupported']);
  });
});

describe('the trace: a card’s checks in the order they ran', () => {
  it('a stopped engine: the line holds up to the engine and the checks behind it did not run', async () => {
    const r = rig('mixed');
    await r.flow.load();
    const t = traceOf(card(r, 'spark-rack'));
    expect(t.map((s) => `${s.short}:${s.state}`)).toEqual(['SSH:ok', 'Host key:ok', 'Engine:error', 'Version:ok', 'Model:skip', 'Port:skip', 'Token:skip']);
    expect(t[2].says).toBe('The engine is not running.');
    expect(t[4].says).toBe('The model was not checked: an earlier check has to pass first');
    expect(foldWords(t)).toBe('3 checks passed, 3 not run');
  });
  it('the transport down: only SSH is known, nothing behind it', async () => {
    const r = rig('all');
    await r.flow.load();
    const far = traceOf(card(r, 'spark-far'));
    expect(far.map((s) => s.state)).toEqual(['error', 'skip', 'skip', 'skip', 'skip', 'skip', 'skip']);
    expect(foldWords(far)).toBe('No check passed, 6 not run');
    // SSH is cut off, the engine still serves: the stop is a warning and its port and token passed
    const serving = traceOf(card(r, 'spark-serving'));
    expect(serving.map((s) => s.state)).toEqual(['warning', 'skip', 'skip', 'skip', 'skip', 'ok', 'ok']);
    // an engine that loads will pass by itself
    expect(traceOf(card(r, 'spark-loading')).find((s) => s.check === 'engine')!.state).toBe('info');
  });
  it('a check that does not apply says why; every check has its words', async () => {
    const first = cardsOf(await view('first', 'empty'))[0];
    const t = traceOf(first);
    expect(t.map((s) => `${s.check}:${s.state}`)).toEqual(['chatd:skip', 'sandbox:skip', 'memory_cgroup:skip', 'disk:ok', 'backends:ok', 'engines:info']);
    expect(t[0].says).toBe('Chat’s service was not checked: Chat is switched off here');
    const ext = traceOf(cardsOf(await view('external'))[0]);
    expect(ext[1].says).toBe('The sandbox runtime was not checked: Chat runs on another machine');
    const ok = cardsOf(await view('ok'));
    expect(traceOf(ok[0]).map((s) => s.says)).toEqual(['Chat’s service runs and answers', 'The container runtime answers and holds the sandbox image', 'The kernel caps a sandbox’s memory', 'There is enough free disk space', 'Every backend that is on answers', 'An engine is connected']);
    expect(traceOf(ok[1]).map((s) => s.says)).toEqual(['TandemApp reaches the host over SSH', 'The host key is the one pinned at pairing', 'The engine runs', 'The engine’s version fits this TandemApp', 'The engine serves a model', 'The engine’s port answers from this machine', 'The engine takes TandemApp’s token']);
    expect(foldWords(traceOf(ok[1]))).toBe('7 checks passed');
    expect(foldWords(traceOf(ok[3]))).toBe('1 check passed');
  });
});

describe('a host key that is not the one expected', () => {
  it('both fingerprints, to compare line by line', () => {
    const f = healthFinding('host_key_changed', { pinned: FP_A, offered: FP_B });
    expect(fingerprintsOf(f)).toEqual([{ label: 'Pinned at pairing', value: FP_A, mono: true }, { label: 'Offered now', value: FP_B, mono: true }]);
    const m = healthFinding('host_key_mismatch', { expected: FP_A, offered: FP_B });
    expect(fingerprintsOf(m)!.map((x) => x.label)).toEqual(['Shown when pairing', 'The engine host has']);
    expect(fingerprintsOf(healthFinding('engine_down'))).toBeNull();
    expect([isHostKeyStop('host_key_changed'), isHostKeyStop('host_key_mismatch'), isHostKeyStop('not_paired')]).toEqual([true, true, false]);
  });
  it('its only button shows how to pair again: an action that would do anything else is not drawn', () => {
    const f = healthFinding('host_key_changed', { pinned: FP_A, offered: FP_B });
    expect(actionOf(f)).toEqual({ id: 're_pair', label: 'How to pair again', changes: false, confirm: false });
    for (const forged of ['retry', 'start', 'rotate_token', 'upgrade'] as const) expect(actionOf({ ...f, action: actionView(forged) }), forged).toBeNull();
    expect(actionOf({ ...f, action: { id: 're_pair', label: 'Connect anyway', changes: true, confirm: false } })).toBeNull();
    // any other finding keeps what the controller gave it
    expect(actionOf(healthFinding('engine_down'))!.id).toBe('start');
    expect(actionOf(healthFinding('not_allowed'))).toBeNull();
  });
  it('is shown as something that does not work, also where the controller lowered it to a warning', async () => {
    const r = rig('all');
    await r.flow.load();
    // the engine's port still answers there; the controller keeps it an error all the same (no token
    // went there, so that it serves is not proven), and the page holds to that for one that does not
    expect(JSON.parse(r.seen[0]).engines.find((e: HealthTarget) => e.id === 'spark-moved').findings[0].severity).toBe('error');
    const c = card(r, 'spark-moved');
    expect([c.state, c.rows[0].finding.severity, stateWords(c)]).toEqual(['error', 'error', 'Needs a fix']);
    const lowered: HealthView = { ...r.flow.view!, state: 'ok', controller: { ...r.flow.view!.controller, findings: [], state: 'ok' }, pairings: [], engines: [{ ...r.flow.view!.engines[2], state: 'warning', findings: [healthFinding('host_key_changed', { pinned: FP_A, offered: FP_B }, null, { severity: 'warning' })] }] };
    const n = normalized(lowered);
    expect([n.state, n.engines[0].state, n.engines[0].findings[0].severity]).toEqual(['error', 'error', 'error']);
    // a view with nothing to raise is the same object's parts
    const ok = await view('ok');
    expect(normalized(ok).engines[0]).toBe(ok.engines[0]);
  });
});

describe('a finding’s facts', () => {
  it('SSH that cannot be reached shows the login tried; the other side’s words when the sentence lacks them', () => {
    const f = healthFinding('ssh_unreachable', { host: '192.0.2.80', port: 22, user: 'spark' }, 'Connection timed out');
    expect(f.sentence).toBe('TandemApp cannot reach SSH on 192.0.2.80 port 22.');
    expect(factsOf(f)).toEqual([{ label: 'Tried', value: 'spark@192.0.2.80 port 22', mono: true }, { label: 'It said', value: 'Connection timed out', mono: true }]);
    expect(factsOf(healthFinding('http_blocked', { address: '192.0.2.80', port: 8000 }))).toEqual([{ label: 'Tried', value: '192.0.2.80:8000', mono: true }]);
    // the tries left are in the sentence already
    expect(factsOf(healthFinding('wrong_code', { tries_left: 3 }))).toEqual([]);
    expect(factsOf(healthFinding('rate_limited', { retry_after: 30 }))).toEqual([{ label: 'Wait', value: '30 seconds', mono: false }]);
    // a detail the sentence already holds is not said twice
    expect(factsOf(healthFinding('backend_down', { backend: 'DeepSeek' }, 'timed out after 5 s'))).toEqual([]);
    expect(factsOf(healthFinding('engine_down', {}, 'it was stopped on purpose'))).toEqual([{ label: 'It said', value: 'it was stopped on purpose', mono: true }]);
  });
  it('how a target is reached; a token is its last four characters at most', async () => {
    const v = await view('ok');
    const e = reachOf(v.engines[0]);
    expect(e.map((x) => x.label)).toEqual(['SSH login', 'Host key pinned', 'Engine port', 'Token', 'Engine API', 'Mode']);
    expect(e[0].value).toBe('spark@192.0.2.80 port 22');
    expect(e[3].value).toMatch(/^stored, ends in [0-9a-f]{4}$/);
    expect(reachOf(v.controller)).toEqual([{ label: 'Chat', value: 'Chat runs here', mono: false }, { label: 'Sandboxes', value: 'containers on a runtime socket', mono: false }, { label: 'Sandbox socket', value: '/run/user/1000/podman/podman.sock', mono: true }]);
    expect(reachOf({ ...v.controller, sandbox_backend: 'kubernetes', sandbox_socket: null })).toEqual([{ label: 'Chat', value: 'Chat runs here', mono: false }, { label: 'Sandboxes', value: 'pods in the cluster', mono: false }]);
    expect(reachOf({ ...v.engines[0], http: { port: 8000, token: { set: false, last4: null, sha256_6: null, rotated_at: null } } })[3].value).toBe('none stored');
  });
});

describe('time, jobs and what a fix left', () => {
  it('how long ago', () => {
    expect([0, 4, 12, 89, 90, 600, 5400, 7200].map(ago)).toEqual(['just now', 'just now', '12 s ago', '89 s ago', '2 min ago', '10 min ago', '2 h ago', '2 h ago']);
  });
  it('a job in a sentence', () => {
    expect(jobWords(null)).toBeNull();
    expect(jobWords({ kind: 'start', state: 'acting', phase: 'start' })).toBe('The start job runs (start).');
    expect(jobWords({ kind: 'upgrade', state: 'waiting', when: { mode: 'idle' } })).toBe('The upgrade job waits until the engine is idle.');
    expect(jobWords({ kind: 'upgrade', state: 'waiting', waiting_for: 'two requests to end' })).toBe('The upgrade job waits for two requests to end.');
    expect(jobWords({ action: 'restart', state: 'failed', message: 'its port is in use' })).toBe('The restart job failed: its port is in use.');
    expect(jobWords({ kind: 'start', state: 'done' })).toBe('The start job is done.');
    expect([jobLive({ state: 'waiting' }), jobLive({ state: 'acting' }), jobLive({ state: 'done' }), jobLive(null)]).toEqual([true, true, false, false]);
  });
  it('a note says what the fix left: cleared, under way, or still there', async () => {
    const v = await view('mixed');
    const target = v.engines[1];
    const was = target.findings[0];
    const fix = (o: Partial<HealthFix>): HealthFix => ({ contract_version: '1.0', id: target.id, action: 'start', done: true, needs_confirm: false, job: null, result: null, guide: null, target: { ...target, findings: [] }, finding: null, cleared: true, ...o });
    const start = actionView('start')!;
    expect(noteOf(fix({}), start, was, 5)).toMatchObject({ kind: 'cleared', text: 'Started. The engine runs.', target: 'spark-rack', was: 'The engine is not running.', codes: ['engine_down'], at: 5 });
    expect(noteOf(fix({ target: { ...target, findings: [healthFinding('engine_loading', { state: 'loading' })] } }), start, was, 5).text).toBe('Started. The engine loads its model now; this page checks again by itself.');
    expect(noteOf(fix({ cleared: false, finding: was, job: { kind: 'start', state: 'failed', message: 'out of memory' } }), start, was, 5)).toMatchObject({ kind: 'still', text: 'Start did not fix it. What it says now is above.', more: 'The start job failed: out of memory.' });
    expect(noteOf(fix({ cleared: false, finding: was }), actionView('retry')!, was, 5)).toMatchObject({ kind: 'still', text: 'Checked again: still the same.', more: null });
    expect(noteOf(fix({}), actionView('retry')!, was, 5).text).toBe('Checked again: this passes now.');
    expect(noteOf(fix({ result: { rotated: true, stored: true, hint: { set: true, last4: 'a1f4' } } }), actionView('rotate_token')!, was, 5).text).toBe('A new token is in place. It ends in a1f4.');
    expect(noteOf(fix({}), actionView('restart_chatd')!, was, 5).text).toBe('Restarted. chatd answers.');
    const up = actionView('upgrade')!;
    expect(noteOf(fix({ cleared: false, finding: was, job: { kind: 'upgrade', state: 'waiting', when: { mode: 'idle' } } }), up, was, 5)).toMatchObject({ kind: 'working', text: 'The upgrade waits until the engine is idle, then runs. This page checks again by itself.' });
    expect(noteOf(fix({ cleared: false, finding: was, job: { kind: 'upgrade', state: 'acting' } }), up, was, 5).text).toBe('The upgrade runs. This page checks again by itself.');
  });
  it('a guide’s button goes to Add engine, or to one engine host’s model step', () => {
    expect(guideLink({ open: 'model', engine: 'spark desk', address: null, pinned: null, offered: null, steps: [] })).toEqual({ href: '#/add-engine/spark%20desk', label: 'Open the model step' });
    expect(guideLink({ open: 'add_engine', engine: 'x', address: '192.0.2.80', pinned: null, offered: null, steps: [] })).toEqual({ href: '#/add-engine', label: 'Open Add engine' });
    expect([isGuide(actionView('re_pair')!), isGuide(actionView('pick_model')!), isGuide(actionView('add_engine')!), isGuide(actionView('retry')!), isGuide(actionView('start')!)]).toEqual([true, true, true, false, false]);
  });
  it('the sign-in card names whose rights a fix needs', () => {
    expect((['read', 'start', 'rotate_token', 'upgrade', 'restart_chatd'] as const).map(signInWhat)).toEqual(['The health checks', 'Fixes that start an engine', 'Fixes that rotate a token or upgrade an engine', 'Fixes that rotate a token or upgrade an engine', 'Fixes that restart chatd']);
  });
});

describe('the answer the page holds', () => {
  it('one target replaced, and the worst state read again', async () => {
    const v = await view('mixed');
    const rack = { ...v.engines[1], findings: [], state: 'ok' as const, checked_at: v.engines[1].checked_at + 5 };
    const gpu = { ...v.engines[2], findings: [], state: 'ok' as const, checked_at: v.engines[2].checked_at + 5 };
    const one = withTarget(v, rack);
    expect([one.engines[1].state, one.state]).toEqual(['ok', 'error']);
    expect(withTarget(one, gpu).state).toBe('warning');
  });
  it('an answer older than the one held does not replace it: a read that began before a fix may land after it', async () => {
    const v = await view('mixed');
    const newer = { ...v.engines[1], findings: [], state: 'ok' as const, checked_at: v.engines[1].checked_at + 5 };
    const held = withTarget(v, newer);
    // the older check of the same target
    expect(withTarget(held, v.engines[1]).engines[1]).toBe(newer);
    // a whole answer that still has the older check keeps the newer one of that target
    const next = merged(held, v);
    expect(next.engines[1]).toBe(newer);
    expect(next.engines[2]).toEqual(v.engines[2]);
    expect(merged(null, v).state).toBe('error');
  });
  it('the cards read from what the flow holds', async () => {
    const r = rig('ok');
    await r.flow.load();
    expect(cards(r)).toHaveLength(4);
  });
});
