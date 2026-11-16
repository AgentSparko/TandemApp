// The settings pages in words (VIS-280): where a part lands, what a plan changes and what it
// restarts, when an engine restart happens and how long it waits, how each part ended, a restart's
// state with its countdown, every refusal of the settings interface as a sentence and what to do
// about it, and an audit row. Kept free of the DOM so each sentence is tested by itself.

import { RESTART_ENDED, type AuditRow, type Change, type Overview, type PartPlan, type PartResult, type Plan, type Restart, type Result, type Scope, type SettingsError } from '../api/settings';
import { shown } from './settings-form';

export const SCOPE_NAME: Record<Scope, string> = { app: 'App', backends: 'Backends', conductor: 'Conductor', engine: 'Engine', hooks: 'Hooks' };

/** a target's name as the overview gives it; the id when it is not listed (any more) */
export function targetLabel(o: Overview | null, scope: Scope, target: string): string {
  return o?.scopes.find((s) => s.scope === scope)?.targets.find((t) => t.id === target)?.label ?? target;
}

/** `App`, `Engine: spark-desk` */
export function placeOf(p: { scope: Scope; target: string }, o: Overview | null = null): string {
  return p.scope === 'engine' ? `Engine: ${targetLabel(o, p.scope, p.target)}` : SCOPE_NAME[p.scope];
}

const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** `45 seconds`, `10 minutes`, `1 hour`, `1 h 30 min` */
export function dur(s: number): string {
  if (s < 90) return plural(Math.round(s), 'second');
  if (s < 5400 && s % 3600 !== 0) return plural(Math.round(s / 60), 'minute');
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : plural(h, 'hour');
}

/** a countdown: `09:42`, `1:02:03` */
export function clock(s: number): string {
  const t = Math.max(0, Math.round(s));
  const p = (n: number) => String(n).padStart(2, '0');
  const h = Math.floor(t / 3600);
  return h ? `${h}:${p(Math.floor((t % 3600) / 60))}:${p(t % 60)}` : `${p(Math.floor(t / 60))}:${p(t % 60)}`;
}

// ---- when an engine restarts ----------------------------------------------------------------------

export interface When {
  /** idle: once no request runs; at: at a time, then once idle; now: at once */
  mode: 'idle' | 'at' | 'now';
  /** a datetime-local field's value */
  at: string;
  /** how long it waits for idle */
  deadline_s: number;
}

export const WHEN_DEFAULT: When = { mode: 'idle', at: '', deadline_s: 600 };
/** the waits on offer; the controller takes 10 s to a day */
export const DEADLINES = [120, 300, 600, 1800, 3600, 14400];

export function whenError(w: When, now: number): string | null {
  if (w.mode !== 'at') return null;
  const t = Date.parse(w.at);
  if (!w.at || Number.isNaN(t)) return 'Pick the day and the time.';
  return t <= now ? 'That time has passed. Pick a later one.' : null;
}

/** what the apply sends for an engine part */
export function whenParam(w: When): { when: string; deadline_s?: number } {
  if (w.mode === 'now') return { when: 'now' };
  if (w.mode === 'at') return { when: `at=${new Date(w.at).toISOString().replace(/\.\d{3}Z$/, 'Z')}`, deadline_s: w.deadline_s };
  return { when: 'idle', deadline_s: w.deadline_s };
}

const hhmm = (t: number): string => new Date(t).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });

export function whenWords(w: When, time: (t: number) => string = hhmm): string {
  if (w.mode === 'now') return 'It restarts at once. Requests that run are cut off.';
  const wait = `It waits for the running requests to end, at most ${dur(w.deadline_s)}. Still busy then: nothing changes.`;
  return w.mode === 'at' && w.at && !Number.isNaN(Date.parse(w.at)) ? `Nothing happens before ${time(Date.parse(w.at))}. Then: ${wait.charAt(0).toLowerCase()}${wait.slice(1)}` : wait;
}

// ---- a plan ---------------------------------------------------------------------------------------

export type Tone = 'ok' | 'live' | 'warn' | 'err' | 'muted';

/** How long an engine restart takes, in words: `about 3 min`. */
export function aboutMin(s: number): string {
  return `about ${Math.max(1, Math.round(s / 60))} min`;
}

/** The restart's own length, in a sentence: where the figure comes from decides the words. */
export function estimateWords(e: PartPlan['restart_estimate']): string | null {
  if (!e) return null;
  if (e.source === 'default') return `The restart takes ${aboutMin(e.seconds)}, a usual time for an engine.`;
  return `The restart takes ${aboutMin(e.seconds)}, as its last one did.`;
}

/** What a part costs, in a line. */
export function costOf(p: PartPlan, label: string): { tone: Tone; text: string } {
  if (!p.changed) return { tone: 'muted', text: 'Nothing to change: it holds already.' };
  if (p.restart === 'engine') {
    const e = estimateWords(p.restart_estimate);
    return { tone: 'live', text: `Restarts the engine on ${label}, once.${e ? ` ${e}` : ''}` };
  }
  if (p.restart === 'app') return { tone: 'live', text: 'Restarts TandemApp.' };
  if (p.restart === 'hot') return { tone: 'ok', text: p.scope === 'engine' ? 'No restart: the running engine takes it at once.' : 'No restart: the conductor reads it at the next request.' };
  return { tone: 'ok', text: 'No restart: it applies at once.' };
}

export const changeCount = (plan: Pick<Plan, 'parts'>): number => plan.parts.reduce((n, p) => n + p.changes.length, 0);

/** The plan's head: how much changes and what restarts. */
export function planSummary(plan: Plan, o: Overview | null = null): { title: string; text: string; restarts: string[] } {
  const n = changeCount(plan);
  const restarts = plan.parts.filter((p) => p.changed && p.restart === 'engine').map((p) => targetLabel(o, p.scope, p.target));
  if (!plan.changed) return { title: 'Nothing to change', text: 'Everything in this change set holds already. Nothing would be written.', restarts };
  const app = plan.parts.some((p) => p.changed && p.restart === 'app');
  const text = restarts.length
    ? `${restarts.length === 1 ? 'One engine restart' : `${restarts.length} engine restarts`}: ${restarts.join(', ')}. ${plan.parts.some((p) => p.changed && p.restart !== 'engine') ? 'The rest applies at once.' : ''}`.trim()
    : app
      ? 'TandemApp restarts. No engine restarts.'
      : 'No restart. Everything applies at once.';
  return { title: `Review ${plural(n, 'change')}`, text, restarts };
}

/** One key's change, from and to in words. A secret shows that it changes, never what to. */
export function changeWords(c: Change): { from: string; to: string; note: string | null } {
  if (c.secret) return { from: c.was_set ? 'a key is stored' : 'no key', to: c.is_set ? 'a new key' : 'no key', note: null };
  if (c.diff) {
    const n = c.diff_count ?? c.diff.length;
    const v = (c.from as { version?: unknown } | null)?.version;
    return { from: c.was_set ? `version ${v}` : 'the built-in one', to: 'a new version', note: `${plural(n, 'field')} ${n === 1 ? 'changes' : 'change'}${n > c.diff.length ? `; the first ${c.diff.length} are shown` : ''}` };
  }
  return { from: shown(c.from), to: shown(c.to), note: c.is_set ? null : 'back to what stands below it' };
}

// ---- a result -------------------------------------------------------------------------------------

export function resultHead(r: Result): { title: string; text: string; tone: Tone } {
  const again = r.replayed ? ' This is the answer of the apply before: nothing was applied twice.' : '';
  const waits = r.parts.filter((p) => p.state === 'scheduled').length;
  switch (r.state) {
    case 'applied':
      return waits
        ? { title: waits === 1 ? 'Applied. One engine restart is on its way.' : `Applied. ${waits} engine restarts are on their way.`, text: `The rest holds now. Each restart shows below until it ends; you can leave this page.${again}`, tone: 'live' }
        : { title: 'Applied', text: `Every part holds now.${again}`, tone: 'ok' };
    case 'partial':
      return { title: `Stopped at ${r.failed}`, text: `The first failure stops a change set. What was applied before it stays applied and can be restored; what comes after it was not run.${again}`, tone: 'warn' };
    case 'failed':
      return { title: 'Nothing was applied', text: `${r.failed} refused the change, so the set stopped before it wrote anything.${again}`, tone: 'err' };
    default:
      return { title: 'Nothing changed', text: `Everything held already.${again}`, tone: 'muted' };
  }
}

export const PART_GLYPH: Record<PartResult['state'], string> = { applied: '✓', scheduled: '◷', unchanged: '–', failed: '✕', not_run: '○' };

export function partStateWords(p: PartResult): { tone: Tone; text: string } {
  switch (p.state) {
    case 'applied':
      return { tone: 'ok', text: p.kind === 'restore' ? 'Restored' : 'Applied' };
    case 'scheduled':
      return { tone: 'live', text: 'Applies with the engine’s restart' };
    case 'unchanged':
      return { tone: 'muted', text: 'Held already' };
    case 'failed':
      return { tone: 'err', text: 'Failed. Nothing changed here.' };
    default:
      return { tone: 'muted', text: 'Not run: the set stopped before it' };
  }
}

// ---- a restart ------------------------------------------------------------------------------------

export interface RestartStep {
  label: string;
  state: 'done' | 'now' | 'todo' | 'stopped';
}

export interface RestartModel {
  tone: Tone;
  title: string;
  text: string;
  steps: RestartStep[];
  /** the countdown's figure while it waits for idle */
  count: string | null;
  /** 0..1 of the wait used up; null when there is no deadline to count against */
  used: number | null;
  job: string | null;
  ended: boolean;
}

const ORDER = ['scheduled', 'draining', 'restarting', 'warming'] as const;
/** the controller's own words as a sentence */
const said = (m: string): string => `${m.charAt(0).toUpperCase()}${m.slice(1)}${/[.!?]$/.test(m) ? '' : '.'}`;

/** A restart as the page shows it. `left`: the seconds left to wait as the page counts them
 *  between two reads (the read's own `remaining_s` when null). */
export function restartModel(r: Restart, left: number | null = null, time: (t: number) => string = hhmm): RestartModel {
  const timed = r.when.startsWith('at=');
  const at = timed ? Date.parse(r.when.slice(3)) : NaN;
  const names = [...(timed ? [`Wait until ${Number.isNaN(at) ? 'the time' : time(at)}`] : []), 'Wait for idle', 'Restart', 'Warm up'];
  const off = timed ? 0 : 1;
  const ended = RESTART_ENDED.includes(r.state);
  // where it stands on the track; an end that is not `done` stops at the step it reached
  let pos: number;
  if (r.state === 'done') pos = names.length;
  else if (r.state === 'failed') pos = ORDER.indexOf('restarting') - off;
  else if (r.state === 'timeout') pos = ORDER.indexOf('draining') - off;
  else if (r.state === 'cancelled') pos = r.deadline_at || !timed ? ORDER.indexOf('draining') - off : 0;
  else pos = ORDER.indexOf(r.state) - off;
  const steps: RestartStep[] = names.map((label, i) => ({ label, state: i < pos ? 'done' : i > pos ? 'todo' : ended ? 'stopped' : 'now' }));
  const secs = left ?? r.remaining_s;
  const waiting = r.state === 'draining' && secs != null;
  const who = r.backends.length ? r.backends.join(', ') : 'its backends';
  const back = r.restored ? ` It runs version ${r.restored.version} again${r.restored.healthy ? ' and answers.' : ', but does not answer yet.'}` : '';
  const say: Record<Restart['state'], [Tone, string, string]> = {
    scheduled: ['live', `${r.alias} restarts ${Number.isNaN(at) ? 'later' : `at ${time(at)}`}`, 'Nothing is held back yet. At that time it waits for the engine to be idle.'],
    draining: ['live', `${r.alias} restarts when it is idle`, `It waits for the running requests to end. New requests to ${who} are answered “try again in 30 s” until the engine is back.`],
    restarting: ['live', `${r.alias} is restarting`, 'The engine stops, takes the new settings and starts. It can no longer be cancelled.'],
    warming: ['live', `${r.alias} is warming up`, `The engine is back. One token goes through ${who}, then requests are served again.`],
    done: ['ok', `${r.alias} runs the new settings`, r.warm && !r.warm.ok ? 'A backend gave no warm-up token: the gateway’s health check takes it from here.' : 'Its backends serve again.'],
    failed: ['err', `${r.alias} did not start with the new settings`, `${said(r.message ?? 'The engine’s job failed')}${back}`],
    timeout: ['warn', `${r.alias} stayed busy: nothing changed`, 'The wait ended while requests were still running. The engine keeps its settings and serves as before. Apply the change again at a quieter time, or let it restart at once.'],
    cancelled: ['muted', `The restart of ${r.alias} was cancelled`, r.message && !r.message.startsWith('cancelled by') ? `${said(r.message)} The engine keeps the settings it runs.` : 'The engine keeps the settings it runs, and its backends serve again.'],
  };
  const [tone, title, text] = say[r.state];
  const j = r.job;
  return {
    tone,
    title,
    text,
    steps,
    count: waiting ? clock(secs!) : null,
    used: waiting && r.deadline_s ? Math.min(1, Math.max(0, 1 - secs! / r.deadline_s)) : null,
    job: j?.id ? `Engine job ${j.id}${j.state ? `: ${j.state}` : ''}${j.phase ? `, ${j.phase}` : ''}` : null,
    ended,
  };
}

/** The seconds left to wait now, counted on from a read `ageMs` ago. */
export function leftNow(r: Restart, ageMs: number): number | null {
  return r.remaining_s == null ? null : Math.max(0, r.remaining_s - Math.floor(ageMs / 1000));
}

// ---- refusals -------------------------------------------------------------------------------------

export interface Refusal {
  title: string;
  /** what to do about it */
  fix: string | null;
  /** the controller's own sentence, when the title is not it */
  detail: string | null;
}

/** Every refusal of the settings interface as a sentence and its fix. */
export function errorWords(e: Pick<SettingsError, 'type' | 'message' | 'facts'>): Refusal {
  const m = e.message;
  const f = e.facts ?? {};
  const own = (title: string, fix: string | null): Refusal => ({ title, fix, detail: m && m !== title ? m : null });
  switch (e.type) {
    case 'unauthorized':
      return { title: 'This needs TandemApp’s admin password.', fix: 'Sign in below, then try again. Nothing was changed.', detail: null };
    case 'offline':
      return { title: 'TandemApp did not answer.', fix: 'Check that it still runs and that this device reaches it, then try again.', detail: null };
    case 'stale_plan':
      return { title: 'The settings changed since this plan was read.', fix: f.plan ? 'This is the plan as it is now. Check it, then apply.' : 'Review the changes again.', detail: null };
    case 'busy':
      return { title: 'Another change set is being applied right now.', fix: 'Wait a moment, then apply again. Nothing of this one was written.', detail: null };
    case 'confirm_required':
    case 'fingerprint_confirm_required':
      return { title: 'This change needs its confirm.', fix: 'Review the changes again.', detail: null };
    case 'locked':
      return { title: `${f.key ?? 'This setting'} cannot be changed here.`, fix: f.variable ? `${f.variable} sets it in the environment: change it there.` : 'Take it out of the change set.', detail: f.variable ? null : m };
    case 'unknown_key':
      return own(`This controller no longer has ${f.keys?.length ? f.keys.join(', ') : 'one of these settings'}.`, 'Reload the page: the settings on offer changed.');
    case 'bad_value':
      return { title: m, fix: 'Correct the value, then review again.', detail: null };
    case 'invalid_document':
      return { title: m, fix: f.errors && f.errors.length > 1 ? `${f.errors.length} things are wrong in it; the first is named above.` : 'Correct the document, then review again.', detail: null };
    case 'restore_invalid':
      return { title: m, fix: 'That version no longer fits. Pick another one, or change the settings by hand.', detail: null };
    case 'no_version':
      return own('That version is no longer kept.', 'Open the versions again and pick one that is listed.');
    case 'no_target':
      return own('That engine host is no longer in the fleet.', 'Take its changes out of the change set.');
    case 'no_scope':
    case 'not_found':
      return own('This controller does not offer these settings.', 'Update TandemApp on the controller’s machine.');
    case 'read_only':
    case 'no_http':
      return { title: m, fix: null, detail: null };
    case 'extension_missing':
      return { title: 'The engine host lacks the chat extension, and TandemApp cannot install it from here.', fix: 'Do what the sentence below says, then review again. Nothing was changed.', detail: m || null };
    case 'engine_unreachable':
      return own('TandemApp could not reach the engine host.', 'Check that the machine is on and answers over SSH, then try again. Nothing was changed there.');
    case 'engine_timeout':
      return own('The engine host took too long to answer.', 'Try again in a moment. Nothing was changed there.');
    case 'engine_too_old':
      return own('This engine is too old to take settings from TandemApp.', 'Update the engine on its host, then try again.');
    case 'engine_busy':
      return own('The engine host is busy with another job.', 'Wait for that job to end, then try again.');
    case 'engine_refused':
      return own('The engine refused the change.', null);
    case 'engine_failed':
      return own('The engine host’s own tool failed.', 'Look at the engine’s log on its host. Nothing was changed there.');
    case 'host_key_changed':
      return { title: 'The engine host shows another SSH host key than the one pinned for it. Nothing ran.', fix: 'If the machine was set up again, pair it again under Add engine. If not, something else answers at its address.', detail: null };
    case 'store_refused':
      return own('The store refused the change.', null);
    case 'too_late':
      return { title: 'The restart has begun: it can no longer be cancelled.', fix: null, detail: null };
    case 'ended':
      return { title: 'That restart has ended already.', fix: null, detail: null };
    case 'no_restart':
      return { title: 'That restart is no longer known here.', fix: 'The controller restarted since. The engine’s own job went on by itself: its state shows under Engines.', detail: null };
    case 'cross_site':
    case 'cross_origin':
    case 'json_only':
      return own('The controller refused this page’s request.', 'Open TandemApp at its own address and try again.');
    default:
      return { title: m || 'TandemApp answered with an error.', fix: 'Try again. If it keeps failing, look at TandemApp’s log.', detail: null };
  }
}

// ---- the audit ------------------------------------------------------------------------------------

const ACTION: Record<string, string> = {
  'settings.apply': 'Applied',
  'settings.restore': 'Restored',
  'settings.change_set': 'Change set ended',
  'settings.restart': 'Restart ended',
  'settings.cancel': 'Restart cancelled',
};
const RESULT_TONE: Record<string, Tone> = { applied: 'ok', done: 'ok', scheduled: 'live', partial: 'warn', timeout: 'warn', failed: 'err', cancelled: 'muted', unchanged: 'muted' };

export function auditWords(row: AuditRow, o: Overview | null = null): { action: string; where: string; tone: Tone } {
  const scope = row.scope as Scope | null;
  const where = scope && SCOPE_NAME[scope] ? placeOf({ scope, target: row.target ?? '' }, o) : row.object === 'settings' ? '' : `${row.object}${row.object_id ? ` ${row.object_id}` : ''}`;
  return { action: ACTION[row.action] ?? row.action, where, tone: RESULT_TONE[row.result ?? ''] ?? 'muted' };
}
