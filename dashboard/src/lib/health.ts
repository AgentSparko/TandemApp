// What the health page says (SRV-835): the verdict over everything that was checked, one card a
// target (this TandemApp, each engine host, the plugins, the pairing codes refused lately), its
// findings worst first, the checks as a trace in the order they ran, and the words for a check, a
// state, a job and what a fix left behind. The controller's own sentence and fix are shown as they
// come; the words here are the page's around them. Kept pure so it can be tested; the templates are
// views/health/view.ts, the calls lib/health-flow.ts.

import type { EngineFinding } from '../api/engines';
import { CONTROLLER, type ActionId, type CheckName, type FixJob, type HealthAction, type HealthCheck, type HealthFinding, type HealthFix, type HealthGuide, type HealthState, type HealthTarget, type HealthView, type RefusedPairing, type Severity } from '../api/health';

// ---- findings --------------------------------------------------------------------------------

const RANK: Record<Severity, number> = { error: 0, warning: 1, info: 2 };

/** Worst first; within a severity the controller's order stays: it lists what blocks before what
 *  depends on it (SSH before the engine, the engine before its port). */
export function worstFirst<T extends { finding: HealthFinding }>(rows: T[]): T[] {
  return rows.map((r, i) => ({ r, i })).sort((a, b) => RANK[a.r.finding.severity] - RANK[b.r.finding.severity] || a.i - b.i).map((x) => x.r);
}

export function stateOf(findings: readonly HealthFinding[]): HealthState {
  if (findings.some((f) => f.severity === 'error')) return 'error';
  return findings.some((f) => f.severity === 'warning') ? 'warning' : 'ok';
}

export const isPluginFinding = (f: Pick<HealthFinding, 'code'>): boolean => f.code.startsWith('plugin_');

/** A host key that is not the one expected: the page shows both and offers nothing that connects. */
export const isHostKeyStop = (code: string): boolean => code === 'host_key_changed' || code === 'host_key_mismatch';

/** Which one of its code a finding is, where a target has several: the backend or plugin it names. */
export const instanceOf = (f: Pick<HealthFinding, 'facts'>): string | null => {
  const x = f.facts.backend ?? f.facts.plugin;
  return x == null ? null : String(x);
};

/** A refused pairing code whose way on is a new code in Add engine (it is no target: no fix runs
 *  for it). A host key that does not match gets nothing; a code tried too often waits first. */
export const pairAgain = (code: string): boolean => code === 'wrong_code' || code === 'code_spent' || code === 'no_window';

/** An action that changes nothing and answers how: its button opens the steps. */
export const isGuide = (a: Pick<HealthAction, 'id' | 'changes'>): boolean => !a.changes && a.id !== 'retry';

/** The action a finding's button may run. A changed or mismatched host key gets the steps to pair
 *  again and nothing else, whatever the answer carries. */
export function actionOf(f: HealthFinding): HealthAction | null {
  const a = f.action;
  if (!a) return null;
  if (isHostKeyStop(f.code) && !(a.id === 're_pair' && !a.changes)) return null;
  return a;
}

export const SEVERITY_WORDS: Record<Severity, string> = { error: 'Does not work', warning: 'Works, but worse', info: 'Will pass by itself' };

// ---- the cards ---------------------------------------------------------------------------------

export type CardKind = 'controller' | 'engine' | 'plugins' | 'pairings';
export const PLUGINS = 'plugins';
export const PAIRINGS = 'pairings';

export interface Row {
  key: string;
  finding: HealthFinding;
  /** the target a fix of this finding is sent to; null: no target takes it (a refused pairing) */
  target: string | null;
  pairing: RefusedPairing | null;
}

export interface Meta {
  text: string;
  /** an address or a path: set in the fixed-width face */
  mono: boolean;
}

export interface Card {
  /** the target's id, or `plugins`, or `pairings` */
  id: string;
  kind: CardKind;
  title: string;
  meta: Meta[];
  state: HealthState;
  rows: Row[];
  checks: HealthCheck[];
  /** the target whose answer this card shows (the plugins are the controller's) */
  target: HealthTarget | null;
}

const rowKey = (card: string, f: HealthFinding, extra = ''): string => `${card}|${f.code}|${f.facts.backend ?? f.facts.plugin ?? ''}${extra}`;

const CHAT_WORDS = { own: 'Chat runs here', external: 'Chat runs on another machine', off: 'Chat is off' } as const;

function engineMeta(t: HealthTarget): Meta[] {
  const out: Meta[] = [];
  if (t.address) out.push({ text: t.address, mono: true });
  if (t.engine_version) out.push({ text: `engine ${t.engine_version}`, mono: false });
  if (t.model) out.push({ text: `serves ${t.model}`, mono: false });
  else if (t.engine_state === 'healthy') out.push({ text: 'no model yet', mono: false });
  return out;
}

/** One card a target: this TandemApp, each engine host, the plugins (the controller's plugin
 *  findings and its plugins check), and the refused pairing codes when there are some. */
export function cardsOf(v: HealthView): Card[] {
  const c = v.controller;
  const card = (id: string, kind: CardKind, title: string, meta: Meta[], rows: Row[], checks: HealthCheck[], target: HealthTarget | null): Card => {
    const sorted = worstFirst(rows);
    return { id, kind, title, meta, state: stateOf(sorted.map((r) => r.finding)), rows: sorted, checks, target };
  };
  const own = (f: HealthFinding, cardId: string, target: string): Row => ({ key: rowKey(cardId, f), finding: f, target, pairing: null });
  const out: Card[] = [
    card(
      CONTROLLER, 'controller', c.alias || 'TandemApp', [{ text: 'This machine', mono: false }, ...(c.chat ? [{ text: CHAT_WORDS[c.chat], mono: false }] : [])],
      c.findings.filter((f) => !isPluginFinding(f)).map((f) => own(f, CONTROLLER, CONTROLLER)),
      c.checks.filter((k) => k.check !== 'plugins'), c,
    ),
    ...v.engines.map((e) => card(e.id, 'engine', e.alias, engineMeta(e), e.findings.map((f) => own(f, e.id, e.id)), e.checks, e)),
    card(
      PLUGINS, 'plugins', 'Plugins', [{ text: 'Run by this TandemApp', mono: false }],
      c.findings.filter(isPluginFinding).map((f) => own(f, PLUGINS, CONTROLLER)),
      c.checks.filter((k) => k.check === 'plugins'), c,
    ),
  ];
  if (v.pairings.length)
    out.push(
      card(
        PAIRINGS, 'pairings', 'Add engine', [{ text: 'Pairing codes an engine host refused in the last 10 minutes', mono: false }],
        v.pairings.map((p) => ({ key: rowKey(PAIRINGS, p.finding, `${p.address}:${p.port}@${p.at}`), finding: p.finding, target: null, pairing: p })),
        [], null,
      ),
    );
  return out;
}

/** The target a card's Check again reads; null: everything (the pairings are no target). */
export const cardTarget = (c: Pick<Card, 'kind' | 'id'>): string | null => (c.kind === 'engine' ? c.id : c.kind === 'pairings' ? null : CONTROLLER);

/** A card's state in a few words. */
export function stateWords(c: Card): string {
  if (c.state === 'error') return c.kind === 'pairings' ? 'Pairing refused' : 'Needs a fix';
  if (c.state === 'warning') return c.kind === 'pairings' ? 'Pairing refused' : 'Works, but worse';
  const info = c.rows[0]?.finding.code;
  if (info === 'engine_loading') return 'Starting';
  if (info === 'no_engine') return 'Waits for an engine';
  return c.kind === 'plugins' ? 'All run' : 'Works';
}

// ---- the verdict -------------------------------------------------------------------------------

const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve'];
/** a count in words up to twelve, as a sentence reads */
export const numberWord = (n: number): string => WORDS[n] ?? String(n);
const cap = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1);

export interface Verdict {
  state: HealthState;
  /** the first run: nothing is connected yet */
  setup: boolean;
  title: string;
  sub: string;
  errors: number;
  warnings: number;
  /** the findings TandemApp can fix itself (a button that changes something) */
  fixable: number;
}

/** Everything the answer found, the refused pairings included. */
export const allFindings = (v: HealthView): HealthFinding[] => [...v.controller.findings, ...v.engines.flatMap((e) => e.findings), ...v.pairings.map((p) => p.finding)];

/** The page's first sentence: how it stands, in words, then what TandemApp can do about it and
 *  what was checked. */
export function verdict(v: HealthView): Verdict {
  const all = allFindings(v);
  const errors = all.filter((f) => f.severity === 'error').length;
  const warnings = all.filter((f) => f.severity === 'warning').length;
  const fixable = all.filter((f) => f.severity !== 'info' && actionOf(f)?.changes).length;
  const setup = v.setup.needs_setup;
  const n = errors + warnings;
  let title: string;
  if (errors) title = `${cap(numberWord(errors))} ${errors === 1 ? 'thing does' : 'things do'} not work.`;
  else if (warnings) title = warnings === 1 ? 'Everything works, one thing worse than it should.' : `Everything works, ${numberWord(warnings)} things worse than they should.`;
  else if (setup) title = 'No engine is connected yet.';
  else title = 'Everything works.';
  const sub: string[] = [];
  if (errors && warnings) sub.push(`${cap(numberWord(warnings))} more ${warnings === 1 ? 'works worse than it' : 'work worse than they'} should.`);
  if (fixable) sub.push(`TandemApp can fix ${n === 1 ? 'it' : fixable === n ? 'all of them' : `${numberWord(fixable)} of them`} itself.`);
  else if (n) sub.push(n === 1 ? 'The fix is yours to make: the card says how.' : 'The fixes are yours to make: each card says how.');
  if (setup) sub.push('Install the engine on its host, then pair it here.');
  const hosts = v.engines.length;
  sub.push(`Checked: this TandemApp${hosts ? ` and ${hosts === 1 ? 'its engine host' : `${numberWord(hosts)} engine hosts`}` : ''}.`);
  return { state: v.state, setup, title, sub: sub.join(' '), errors, warnings, fixable };
}

// ---- the checks ----------------------------------------------------------------------------------

/** a check in a word (the trace), as a passed sentence (the list), and as the thing it looks at */
const CHECKS: Record<CheckName, { short: string; ok: string; what: string }> = {
  ssh: { short: 'SSH', ok: 'TandemApp reaches the host over SSH', what: 'the SSH login' },
  host_key: { short: 'Host key', ok: 'The host key is the one pinned at pairing', what: 'the host key' },
  engine: { short: 'Engine', ok: 'The engine runs', what: 'the engine' },
  version: { short: 'Version', ok: 'The engine’s version fits this TandemApp', what: 'the engine’s version' },
  model: { short: 'Model', ok: 'The engine serves a model', what: 'the model' },
  http: { short: 'Port', ok: 'The engine’s port answers from this machine', what: 'the engine’s port' },
  token: { short: 'Token', ok: 'The engine takes TandemApp’s token', what: 'the token' },
  chatd: { short: 'Chat', ok: 'Chat’s service runs and answers', what: 'Chat’s service' },
  sandbox: { short: 'Sandbox', ok: 'The container runtime answers and holds the sandbox image', what: 'the sandbox runtime' },
  memory_cgroup: { short: 'Memory cap', ok: 'The kernel caps a sandbox’s memory', what: 'the memory cap' },
  disk: { short: 'Disk', ok: 'There is enough free disk space', what: 'the free disk space' },
  backends: { short: 'Backends', ok: 'Every backend that is on answers', what: 'the backends' },
  plugins: { short: 'Plugins', ok: 'Every plugin that is on runs', what: 'the plugins' },
  engines: { short: 'Engines', ok: 'An engine is connected', what: 'an engine' },
};

export type StopState = 'ok' | Severity | 'skip';

export interface Stop {
  check: CheckName;
  ok: boolean | null;
  state: StopState;
  short: string;
  /** what the check found, in a sentence */
  says: string;
}

/** Why a check did not run. */
export function skipReason(t: HealthTarget | null): string {
  if (!t || t.kind === 'engine') return 'an earlier check has to pass first';
  if (t.chat === 'off') return 'Chat is switched off here';
  if (t.chat === 'external') return 'Chat runs on another machine';
  return 'it does not apply on this machine';
}

/** A card's checks in the order they ran: passed, failed (as bad as its finding), or not run. */
export function traceOf(c: Pick<Card, 'checks' | 'rows' | 'target'>): Stop[] {
  return c.checks.map((k) => {
    const w = CHECKS[k.check] ?? { short: k.check, ok: `${k.check} passed`, what: k.check };
    if (k.ok === true) return { check: k.check, ok: true, state: 'ok', short: w.short, says: w.ok };
    if (k.ok === null) return { check: k.check, ok: null, state: 'skip', short: w.short, says: `${cap(w.what)} was not checked: ${skipReason(c.target)}` };
    // the rows are worst first: a check that stands for several findings (the plugins) is as bad as the worst
    const f = c.rows.find((r) => (k.check === 'plugins' ? isPluginFinding(r.finding) : r.finding.code === k.code))?.finding;
    return { check: k.check, ok: false, state: f?.severity ?? 'error', short: w.short, says: f?.sentence ?? `${cap(w.what)} did not pass` };
  });
}

/** The fold's summary: how many checks passed and how many did not run. */
export function foldWords(stops: readonly Stop[]): string {
  const ok = stops.filter((s) => s.state === 'ok').length;
  const skip = stops.filter((s) => s.state === 'skip').length;
  const passed = ok ? `${ok} ${ok === 1 ? 'check' : 'checks'} passed` : 'No check passed';
  return skip ? `${passed}, ${skip} not run` : passed;
}

// ---- a finding's facts ---------------------------------------------------------------------------

export interface Fact {
  label: string;
  value: string;
  mono: boolean;
}

/** The two fingerprints of a host key that is not the one expected, to compare line by line. */
export function fingerprintsOf(f: HealthFinding): [Fact, Fact] | null {
  if (f.code === 'host_key_changed' && f.facts.pinned && f.facts.offered)
    return [
      { label: 'Pinned at pairing', value: f.facts.pinned, mono: true },
      { label: 'Offered now', value: f.facts.offered, mono: true },
    ];
  if (f.code === 'host_key_mismatch' && f.facts.expected && f.facts.offered)
    return [
      { label: 'Shown when pairing', value: f.facts.expected, mono: true },
      { label: 'The engine host has', value: f.facts.offered, mono: true },
    ];
  return null;
}

/** The facts that get a line of their own under a finding's fix. */
export function factsOf(f: HealthFinding): Fact[] {
  const out: Fact[] = [];
  const x = f.facts;
  if (f.code === 'ssh_unreachable' && x.host) out.push({ label: 'Tried', value: `${x.user ? `${x.user}@` : ''}${x.host}${x.port != null ? ` port ${x.port}` : ''}`, mono: true });
  if ((f.code === 'http_blocked' || f.code === 'not_allowed' || f.code === 'not_an_engine') && x.address) out.push({ label: 'Tried', value: `${x.address}${x.port != null ? `:${x.port}` : ''}`, mono: true });
  if (f.code === 'rate_limited' && x.retry_after != null) out.push({ label: 'Wait', value: `${x.retry_after} seconds`, mono: false });
  // the other side's own words, when the sentence does not hold them already
  if (f.detail && !f.sentence.includes(f.detail)) out.push({ label: 'It said', value: f.detail, mono: true });
  return out;
}

/** How TandemApp reaches a target, for the fold under its checks. A token shows as its last four
 *  characters at most. */
export function reachOf(t: HealthTarget): Fact[] {
  const out: Fact[] = [];
  if (t.kind === 'controller') {
    if (t.chat) out.push({ label: 'Chat', value: CHAT_WORDS[t.chat], mono: false });
    if (t.sandbox_backend) out.push({ label: 'Sandboxes', value: t.sandbox_backend === 'kubernetes' ? 'pods in the cluster' : 'containers on a runtime socket', mono: false });
    if (t.sandbox_socket) out.push({ label: 'Sandbox socket', value: t.sandbox_socket, mono: true });
    return out;
  }
  if (t.ssh?.host) out.push({ label: 'SSH login', value: `${t.ssh.user ? `${t.ssh.user}@` : ''}${t.ssh.host} port ${t.ssh.port}`, mono: true });
  if (t.host_key) out.push({ label: 'Host key pinned', value: t.host_key, mono: true });
  if (t.http?.port != null) out.push({ label: 'Engine port', value: String(t.http.port), mono: true });
  if (t.http) out.push({ label: 'Token', value: t.http.token.set ? `stored${t.http.token.last4 ? `, ends in ${t.http.token.last4}` : ''}` : 'none stored', mono: false });
  if (t.engine_api) out.push({ label: 'Engine API', value: t.engine_api, mono: true });
  if (t.mode) out.push({ label: 'Mode', value: t.mode, mono: false });
  return out;
}

// ---- time ----------------------------------------------------------------------------------------

/** "just now", "12 s ago", "3 min ago", "2 h ago" */
export function ago(seconds: number): string {
  const s = Math.max(0, seconds);
  if (s < 5) return 'just now';
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

// ---- fixes ---------------------------------------------------------------------------------------

/** what the page says while a fix's call is under way */
export function busyWords(a: ActionId): string {
  switch (a) {
    case 'retry':
      return 'Checking again…';
    case 'start':
      return 'Starting the engine over SSH, then checking again…';
    case 'rotate_token':
      return 'Fetching a new token over SSH and storing it sealed…';
    case 'restart_chatd':
      return 'Restarting chatd and waiting for it to answer, up to 45 seconds…';
    case 'upgrade':
      return 'Asking the engine host…';
    default:
      return 'Asking for the steps…';
  }
}

const LIVE = ['waiting', 'acting', 'running'];
/** a job that has not ended */
export const jobLive = (j: FixJob | null | undefined): boolean => !!j && LIVE.includes(j.state ?? '');

/** A fix's job in a sentence; null when there is none to tell of. */
export function jobWords(j: FixJob | null | undefined): string | null {
  if (!j || !j.state) return null;
  const kind = j.kind ?? j.action ?? 'fix';
  const state =
    j.state === 'waiting'
      ? `waits${j.waiting_for ? ` for ${j.waiting_for}` : j.when?.mode === 'idle' ? ' until the engine is idle' : ''}`
      : j.state === 'acting' || j.state === 'running'
        ? `runs${j.phase ? ` (${j.phase})` : ''}`
        : j.state === 'done'
          ? 'is done'
          : j.state === 'cancelled'
            ? 'was cancelled'
            : j.state === 'lost'
              ? 'was lost'
              : j.state;
  return `The ${kind} job ${state}${j.message ? `: ${j.message}` : ''}.`;
}

export type NoteKind = 'cleared' | 'working' | 'still' | 'refused' | 'wait' | 'sign_in';

/** What a fix left on its finding's row, or, once the finding is gone, at the top of its card. */
export interface Note {
  kind: NoteKind;
  /** the target the fix was sent to */
  target: string;
  action: ActionId;
  label: string;
  text: string;
  /** the job's own line, or a refusal's fix */
  more: string | null;
  /** the sentence the finding had before, shown once it is gone */
  was: string;
  /** the codes the action was for: the note follows them */
  codes: string[];
  /** the page's clock, ms */
  at: number;
  /** the job a working fix started: the page reads it again until it ends */
  job?: string;
}

/** a token's hint in words: never the token */
function hintWords(h: unknown): string {
  const last4 = h && typeof h === 'object' ? (h as { last4?: unknown }).last4 : null;
  return typeof last4 === 'string' && last4 ? ` It ends in ${last4}.` : '';
}

/** What to say after a fix answered: it cleared the finding, it is still under way, or the
 *  finding is still there (its row then shows the new reason). */
export function noteOf(fix: HealthFix, action: HealthAction, was: HealthFinding, now: number): Note {
  const base = { target: fix.id, action: action.id, label: action.label, was: was.sentence, codes: [was.code], at: now };
  const job = jobWords(fix.job);
  if (fix.cleared) {
    const loading = fix.target.findings.some((f) => f.code === 'engine_loading');
    const text =
      action.id === 'retry'
        ? 'Checked again: this passes now.'
        : action.id === 'start'
          ? loading
            ? 'Started. The engine loads its model now; this page checks again by itself.'
            : 'Started. The engine runs.'
          : action.id === 'rotate_token'
            ? `A new token is in place.${hintWords(fix.result?.hint)}`
            : action.id === 'restart_chatd'
              ? 'Restarted. chatd answers.'
              : action.id === 'upgrade'
                ? 'Upgraded. The engine fits this TandemApp now.'
                : `${action.label}: done.`;
    return { ...base, kind: 'cleared', text, more: null };
  }
  if (jobLive(fix.job)) {
    const text =
      action.id === 'upgrade'
        ? fix.job?.state === 'waiting'
          ? 'The upgrade waits until the engine is idle, then runs.'
          : 'The upgrade runs.'
        : action.id === 'start'
          ? 'The engine is starting.'
          : action.id === 'restart_chatd'
            ? 'chatd is still restarting.'
            : `${action.label} is under way.`;
    return { ...base, kind: 'working', text: `${text} This page checks again by itself.`, more: job, ...(fix.job?.id ? { job: fix.job.id } : {}) };
  }
  return { ...base, kind: 'still', text: action.id === 'retry' ? 'Checked again: still the same.' : `${action.label} did not fix it. What it says now is above.`, more: job };
}

/** A working note after its job was read again: still under way (the job's line), or ended badly
 *  (the fix failed, in the host's words); null once it ended well, when the target is read again. */
export function jobNote(n: Note, j: FixJob, now: number): Note | null {
  if (jobLive(j)) return { ...n, more: jobWords(j) };
  if (j.state === 'done') return null;
  return { ...n, kind: 'still', text: `${n.label} did not finish.`, more: jobWords(j), job: undefined, at: now };
}

/** A refusal of a fix as a note: the controller's sentence, its fix below. */
export function refusalNote(e: EngineFinding, target: string, action: HealthAction, was: HealthFinding, now: number, kind: NoteKind = 'refused'): Note {
  return { kind, target, action: action.id, label: action.label, text: e.message, more: e.fix, was: was.sentence, codes: [was.code], at: now };
}

/** Whose rights a fix needs, as the sign-in card says it ("… need TandemApp's admin password"). */
export function signInWhat(a: ActionId | 'read'): string {
  switch (a) {
    case 'read':
      return 'The health checks';
    case 'start':
      return 'Fixes that start an engine';
    case 'rotate_token':
    case 'upgrade':
      return 'Fixes that rotate a token or upgrade an engine';
    case 'restart_chatd':
      return 'Fixes that restart chatd';
    default:
      return 'These fixes';
  }
}

/** Where a guide's button goes: Add engine, or its model step for one engine host. */
/** SRV-875: the page a finding about the sign-in or the API is settled on, named as its fix
 *  sentence names it. The contract gives these findings no action: the way on is a page. */
export const FINDING_PAGE: Record<string, { href: string; label: string }> = {
  auth_owner_no_password: { href: '#/account', label: 'Set a password' },
  local_mode_proxy_seen: { href: '#/account', label: 'Set a password' },
  tokens_need_review: { href: '#/access/tokens', label: 'Open All tokens' },
  legacy_admin_token: { href: '#/access/tokens', label: 'Open All tokens' },
  exposure_no_tokens: { href: '#/account/tokens', label: 'Open My tokens' },
  exposure_unreachable: { href: '#/access/exposure', label: 'Open API exposure' },
  exposure_plain_http: { href: '#/access/exposure', label: 'Open API exposure' },
  token_expiring: { href: '#/account/tokens', label: 'Open My tokens' },
  token_limit_near: { href: '#/account/tokens', label: 'Open My tokens' },
  token_limit_reached: { href: '#/account/tokens', label: 'Open My tokens' },
};

export function guideLink(g: HealthGuide): { href: string; label: string } {
  if (g.open === 'model' && g.engine) return { href: `#/add-engine/${encodeURIComponent(g.engine)}`, label: 'Open the model step' };
  return { href: '#/add-engine', label: 'Open Add engine' };
}

// ---- the answer, kept ---------------------------------------------------------------------------

const worst = (states: HealthState[]): HealthState => (states.includes('error') ? 'error' : states.includes('warning') ? 'warning' : 'ok');

/** A host key that is not the one expected is shown as what it is, something that does not work:
 *  TandemApp does not connect there, whatever else still answers at that address. The controller
 *  lowers the finding to a warning when the engine's port answers; the page does not follow it
 *  there, and reads the states again with it. */
export function normalized(v: HealthView): HealthView {
  const fix = (f: HealthFinding): HealthFinding => (isHostKeyStop(f.code) && f.severity !== 'error' ? { ...f, severity: 'error' } : f);
  const target = (t: HealthTarget): HealthTarget => {
    if (!t.findings.some((f) => fix(f) !== f)) return t;
    const findings = t.findings.map(fix);
    return { ...t, findings, state: stateOf(findings) };
  };
  const controller = target(v.controller);
  const engines = v.engines.map(target);
  const pairings = v.pairings.map((p) => (fix(p.finding) === p.finding ? p : { ...p, finding: fix(p.finding) }));
  return { ...v, controller, engines, pairings, state: worst([controller.state, ...engines.map((e) => e.state), stateOf(pairings.map((p) => p.finding))]) };
}

/** The view with one target replaced by a newer answer of it, and the worst state read again. An
 *  answer older than the one held is left out: a read that began before a fix may land after it. */
export function withTarget(v: HealthView, t: HealthTarget): HealthView {
  const newer = (had: HealthTarget): HealthTarget => (t.checked_at >= had.checked_at ? t : had);
  const controller = t.id === CONTROLLER ? newer(v.controller) : v.controller;
  const engines = v.engines.map((e) => (e.id === t.id ? newer(e) : e));
  return normalized({ ...v, controller, engines });
}

/** A whole answer taken over the one held: a target the page holds a newer check of keeps it. */
export function merged(had: HealthView | null, next: HealthView): HealthView {
  let out = normalized(next);
  if (!had) return out;
  for (const t of [had.controller, ...had.engines]) {
    const now = t.id === CONTROLLER ? next.controller : next.engines.find((e) => e.id === t.id);
    if (now && t.checked_at > now.checked_at) out = withTarget(out, t);
  }
  return out;
}
