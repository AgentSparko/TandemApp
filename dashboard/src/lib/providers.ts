// The decision providers' pages in logic and words (VIS-282): who answers which of the conductor's
// questions, a provider's mode with its A/B share, its health with each endpoint's probe, the
// question mapping and its keys as settings keys, a new provider's form, a test call's answer, the
// agreement report with its arms side by side, and why the built-in answer was used instead. Kept
// free of the DOM; views/hooks/providers.ts draws them.

import type { Arm, Bucket, Health, Hook, NewProvider, Probe, Provider, ProviderCall, ProviderKind, ProviderList, ProviderMode, ProviderTest, QuestionMap, Report, Sticky } from '../api/hooks';
import { HERE, HOOKS_SCOPE, ms } from './hooks';
import { same, type Draft, type DraftPart } from './settings-draft';
import type { Tone } from './settings-words';

export const KIND_WORDS: Record<ProviderKind, { label: string; text: string }> = {
  'systemone-http': { label: 'Your own service', text: 'Any service that speaks the systemone wire: POST /v1/systemone for a question, GET /readyz and GET /v1/models beside it.' },
  hook: { label: 'A hook as the adapter', text: 'A decision.ask hook you wrote, for a model whose API has another shape.' },
  builtin: { label: 'The built-in decider', text: 'The conductor’s own decider, at a model you name for each question.' },
};

export const MODE_WORDS: Record<ProviderMode, { label: string; text: string }> = {
  shadow: { label: 'Shadow', text: 'The conductor decides as before. The provider is asked beside it and the journal keeps whether the two agree. Nothing it says is used. Start here.' },
  ab: { label: 'A/B', text: 'A share of sessions gets the provider’s answers, the rest the built-in. The split sticks, so a session stays on its side.' },
  on: { label: 'On', text: 'The provider answers its questions. On any failure the built-in answer is used.' },
};

export const STICKY_WORDS: Record<Sticky, string> = { session: 'the session', user: 'the user', request: 'each request' };

/** a provider's mode in a few words: `answers`, `watches in shadow`, `answers 30 % by session` */
export function modeWords(p: Pick<Provider, 'mode' | 'ab_percent' | 'sticky' | 'enabled'>): string {
  if (!p.enabled) return 'switched off';
  if (p.mode === 'shadow') return 'watches in shadow';
  return p.mode === 'ab' ? `answers ${p.ab_percent} % by ${p.sticky}` : 'answers';
}

// ---- health ---------------------------------------------------------------------------------------

const HEALTH: Record<Health['status'], { tone: Tone; label: string; text: string }> = {
  ok: { tone: 'ok', label: 'ok', text: 'Every endpoint is ready and its calls go through.' },
  failing: { tone: 'warn', label: 'failing', text: 'A breaker is not closed, or its last call within a minute failed. The built-in answers where it fails.' },
  down: { tone: 'err', label: 'down', text: 'A health probe failed. The built-in answers instead until a probe goes through.' },
  off: { tone: 'muted', label: 'off', text: 'Switched off: it is not asked.' },
};

export function healthWords(h: Pick<Health, 'status'> | null | undefined): { tone: Tone; label: string; text: string } {
  return h ? HEALTH[h.status] : { tone: 'muted', label: 'not probed', text: 'Its health was not read yet.' };
}

const ago = (s: number): string => (s < 1.5 ? 'just now' : s < 90 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`);

/** one endpoint's probe: `ready in 21 ms, checked 4 s ago` */
export function probeWords(p: Probe): { tone: Tone; text: string } {
  if (p.up) return { tone: 'ok', text: `ready${p.ms != null ? ` in ${ms(p.ms)}` : ''}, checked ${ago(p.age_s)}` };
  return { tone: 'err', text: `not ready${p.error ? `: ${fallbackWords(p.error).title.toLowerCase()}` : ''}, checked ${ago(p.age_s)}` };
}

// ---- why the built-in answered --------------------------------------------------------------------

const FALLBACK: Record<string, { title: string; text: string }> = {
  timeout: { title: 'No answer in time', text: 'It did not answer within its timeout (at most the stage’s: 400 ms for the router’s questions).' },
  unreachable: { title: 'Not reachable', text: 'The endpoint did not answer at all.' },
  unknown_label: { title: 'An answer that is no option', text: 'It answered something that is not one of the question’s options.' },
  breaker_open: { title: 'Breaker open', text: 'Five failures in a row, or a p95 at its timeout over the last minute: skipped for 30 s, then one probe call.' },
  provider_down: { title: 'Health probe failed', text: 'Its last probe of /readyz failed, so it was not asked.' },
  hook_off: { title: 'The hook is off', text: 'The hook behind it is off or in shadow. Keep the hook on: the provider’s mode decides shadow and A/B.' },
  no_answer: { title: 'No decision', text: 'The hook answered without a decision.' },
  out_of_scope: { title: 'Out of the hook’s scope', text: 'The hook’s scope does not take that request.' },
  replay_miss: { title: 'Not recorded', text: 'Recorded answers are being replayed, and none was recorded for that question.' },
};

export function fallbackWords(reason: string): { title: string; text: string } {
  const http = /^http_(\d+)$/.exec(reason);
  if (http) return { title: `HTTP ${http[1]}`, text: http[1] === '401' || http[1] === '403' ? 'The endpoint refused the key. Set the right API key.' : `The endpoint answered the error ${http[1]}.` };
  return FALLBACK[reason] ?? { title: reason, text: 'The built-in answer was used.' };
}

export interface FallbackRow {
  reason: string;
  n: number;
  title: string;
  text: string;
  /** the questions it happened for */
  questions: string[];
  last: number;
}

/** The fallbacks among a provider's recent calls, by reason, the most frequent first. */
export function fallbackReasons(calls: ProviderCall[]): FallbackRow[] {
  const by = new Map<string, FallbackRow>();
  for (const c of calls) {
    if (!c.fallback) continue;
    let r = by.get(c.fallback);
    if (!r) by.set(c.fallback, (r = { reason: c.fallback, n: 0, ...fallbackWords(c.fallback), questions: [], last: 0 }));
    r.n++;
    r.last = Math.max(r.last, c.ts_ms);
    if (!r.questions.includes(c.question)) r.questions.push(c.question);
  }
  return [...by.values()].sort((a, b) => b.n - a.n || (a.reason < b.reason ? -1 : 1));
}

// ---- what is staged -------------------------------------------------------------------------------

export const PROVIDER_SWITCHES = ['enabled', 'mode', 'ab_percent', 'sticky', 'questions'] as const;
export type ProviderSwitch = (typeof PROVIDER_SWITCHES)[number];
export const providerKey = (id: string, field: ProviderSwitch): string => `providers.${id}.${field}`;
/** the key of a provider's own API key, or of one question's */
export const apiKeyKey = (id: string, question: string | null): string => (question ? `providers.${id}.questions.${question}.api_key` : `providers.${id}.api_key`);

/** a question's mapping as the settings key holds it: only what is set, never a key */
export type Mapping = Record<string, { model?: string; endpoint?: string; timeout_ms?: number }>;

export function mappingOf(qs: Record<string, QuestionMap>): Mapping {
  const out: Mapping = {};
  for (const [q, m] of Object.entries(qs)) {
    out[q] = {};
    if (m.model != null) out[q].model = m.model;
    if (m.endpoint != null) out[q].endpoint = m.endpoint;
    if (m.timeout_ms != null) out[q].timeout_ms = m.timeout_ms;
  }
  return out;
}

export interface ShownProvider extends Provider {
  staged: Set<ProviderSwitch>;
  /** '*' (its own key) or a question id -> what is staged for that key */
  keys: Record<string, 'set' | 'unset'>;
  /** the questions that are staged and not answered yet: their own key comes after the apply */
  fresh: string[];
}

/** The providers as the page shows them: a staged switch stands in for what holds now. */
export function withStagedProviders(list: Provider[], part: DraftPart | undefined): ShownProvider[] {
  return list.map((p) => {
    const out: ShownProvider = { ...p, staged: new Set(), keys: {}, fresh: [] };
    if (!part) return out;
    for (const f of PROVIDER_SWITCHES) {
      const k = providerKey(p.id, f);
      if (!(k in part.set)) continue;
      out.staged.add(f);
      if (f === 'questions') {
        const m = part.set[k] as Mapping;
        out.questions = Object.fromEntries(Object.entries(m).map(([q, v]) => [q, { model: v.model ?? null, endpoint: v.endpoint ?? null, timeout_ms: v.timeout_ms ?? null, ...(p.questions[q]?.key !== undefined ? { key: p.questions[q].key } : {}) }]));
        out.fresh = Object.keys(m).filter((q) => !(q in p.questions));
      } else (out as unknown as Record<string, unknown>)[f] = part.set[k];
    }
    const own = apiKeyKey(p.id, null);
    if (own in part.set) out.keys['*'] = 'set';
    else if (part.unset.includes(own)) out.keys['*'] = 'unset';
    for (const q of Object.keys(p.questions)) {
      const k = apiKeyKey(p.id, q);
      if (k in part.set) out.keys[q] = 'set';
      else if (part.unset.includes(k)) out.keys[q] = 'unset';
    }
    return out;
  });
}

/** Stage one switch of a provider as a key of the settings interface. */
export function stageProvider(d: Draft, p: Provider, field: ProviderSwitch, value: unknown): void {
  const key = providerKey(p.id, field);
  const held = { value: field === 'questions' ? mappingOf(p.questions) : (p[field] as unknown), set: true };
  if (same(held.value, value)) d.drop(HOOKS_SCOPE, HERE, key);
  else d.set(HOOKS_SCOPE, HERE, key, value, held);
}

/** Stage an API key: a string sets it, null removes the stored one. A key is write-only, so a
 *  typed one is always a change. */
export function stageKey(d: Draft, p: Provider, question: string | null, value: string | null): void {
  const key = apiKeyKey(p.id, question);
  const stored = !!(question ? p.questions[question]?.key : p.key)?.key_set;
  if (value) d.set(HOOKS_SCOPE, HERE, key, value, null, true);
  else if (stored) d.unset(HOOKS_SCOPE, HERE, key, { set: true });
  else d.drop(HOOKS_SCOPE, HERE, key);
}

export function keyWords(k: Provider['key'] | undefined, staged: 'set' | 'unset' | undefined): string {
  if (staged === 'set') return 'A new key is staged. It is sent when the change set is applied.';
  if (staged === 'unset') return 'The stored key is removed when the change set is applied.';
  return k?.key_set ? `A key is stored${k.last4 ? `, ending in ${k.last4}` : ''}. It is never shown.` : 'No key is stored.';
}

// ---- who answers what -----------------------------------------------------------------------------

export interface QuestionRow {
  id: string;
  what: string;
  /** the one enabled provider that answers it on or in A/B; null: the built-in decider */
  answers: { id: string; name: string; words: string } | null;
  /** the providers that watch it in shadow */
  watching: { id: string; name: string }[];
}

/** Each question of the conductor with who answers it and who only watches. */
export function whoAnswers(questions: ProviderList['questions'], list: Pick<Provider, 'id' | 'name' | 'enabled' | 'mode' | 'ab_percent' | 'sticky' | 'questions'>[]): QuestionRow[] {
  return Object.entries(questions).map(([id, q]) => {
    const here = list.filter((p) => p.enabled && id in p.questions);
    const a = here.find((p) => p.mode !== 'shadow');
    return { id, what: q.what, answers: a ? { id: a.id, name: a.name, words: a.mode === 'ab' ? `for ${a.ab_percent} % of ${a.sticky}s` : 'always' } : null, watching: here.filter((p) => p.mode === 'shadow').map((p) => ({ id: p.id, name: p.name })) };
  });
}

/** the provider that already answers a question on or in A/B, other than `self` */
export function takenBy(question: string, list: Pick<Provider, 'id' | 'name' | 'enabled' | 'mode' | 'questions'>[], self: string | null): string | null {
  return list.find((p) => p.id !== self && p.enabled && p.mode !== 'shadow' && question in p.questions)?.name ?? null;
}

// ---- a new provider -------------------------------------------------------------------------------

export interface ProviderForm {
  name: string;
  kind: ProviderKind;
  endpoint: string;
  api_key: string;
  model: string;
  timeout: string;
  hook_id: string;
  questions: string[];
  mode: ProviderMode;
  ab_percent: string;
  sticky: Sticky;
}

export const newProviderForm = (): ProviderForm => ({ name: '', kind: 'systemone-http', endpoint: '', api_key: '', model: 'auto', timeout: '400', hook_id: '', questions: [], mode: 'shadow', ab_percent: '10', sticky: 'session' });

/** the hooks a `hook` provider may name: deciders at decision.ask */
export const adapterHooks = <T extends Pick<Hook, 'role' | 'stages'>>(hooks: T[]): T[] => hooks.filter((h) => h.role === 'decider' && h.stages.includes('decision.ask'));

const URL_RE = /^https?:\/\/[^\s/@]+(\/\S*)?$/;
export const TIMEOUT_MAX_MS = 5000;

export function checkProvider(f: ProviderForm, list: Provider[]): Record<string, string> {
  const e: Record<string, string> = {};
  const name = f.name.trim();
  if (!name) e.name = 'Give it a name.';
  else if (name.length > 80) e.name = 'At most 80 characters.';
  if (f.kind === 'systemone-http') {
    if (!f.endpoint.trim()) e.endpoint = 'Where the service answers, for example http://decider.example:8720.';
    else if (!URL_RE.test(f.endpoint.trim())) e.endpoint = 'A URL that starts with http:// or https://.';
    if (/\s/.test(f.api_key)) e.api_key = 'A key has no spaces.';
  }
  if (f.kind === 'hook' && !f.hook_id) e.hook_id = 'Pick the hook that answers.';
  if (!f.model.trim() || /\s/.test(f.model.trim())) e.model = 'A model id, or auto.';
  if (!/^\d+$/.test(f.timeout.trim()) || Number(f.timeout) < 1 || Number(f.timeout) > TIMEOUT_MAX_MS) e.timeout_ms = `A whole number of milliseconds, 1 to ${TIMEOUT_MAX_MS}.`;
  if (!f.questions.length) e.questions = 'Pick at least one question it answers.';
  else if (f.mode !== 'shadow') {
    const taken = f.questions.map((q) => [q, takenBy(q, list, null)] as const).filter(([, who]) => who);
    if (taken.length) e.questions = `${taken.map(([q, who]) => `${q} is answered by ${who}`).join('; ')}. One provider answers a question; start this one in shadow, or move the question first.`;
  }
  if (f.mode === 'ab' && (!/^\d+$/.test(f.ab_percent.trim()) || Number(f.ab_percent) > 100)) e.ab_percent = 'A share from 0 to 100.';
  return e;
}

export function providerBody(f: ProviderForm): NewProvider {
  const out: NewProvider = { name: f.name.trim(), kind: f.kind, model: f.model.trim(), timeout_ms: Number(f.timeout), questions: Object.fromEntries(f.questions.map((q) => [q, {}])), mode: f.mode };
  if (f.kind === 'systemone-http') {
    out.endpoint = f.endpoint.trim();
    if (f.api_key) out.api_key = f.api_key;
  }
  if (f.kind === 'hook') out.hook_id = f.hook_id;
  if (f.mode === 'ab') {
    out.ab_percent = Number(f.ab_percent);
    out.sticky = f.sticky;
  }
  return out;
}

// ---- a test call ----------------------------------------------------------------------------------

export function explainProviderTest(t: ProviderTest): { tone: Tone; title: string; lines: string[]; probs: { label: string; p: number }[] } {
  if (!t.ok || !t.answer) {
    const w = fallbackWords(t.error ?? 'unreachable');
    return { tone: 'err', title: `It did not answer ${t.question}: ${w.title.toLowerCase()}.`, lines: [w.text, 'On a real question the built-in answer would be used.'], probs: [] };
  }
  const a = t.answer;
  const probs = Object.entries(a.probs).map(([label, p]) => ({ label, p })).sort((x, y) => y.p - x.p);
  return {
    tone: 'ok',
    title: `It answered “${a.label}” in ${ms(t.ms)}, ${Math.round(a.confidence * 100)} % sure.`,
    lines: [...(a.model ? [`Model ${a.model}`] : []), ...(t.endpoint ? [`At ${t.endpoint}`] : []), a.confidence >= 0.8 ? 'At the decider’s default threshold of 80 % this answer would be taken.' : 'Below the decider’s default threshold of 80 %: the next stage would be asked.'],
    probs,
  };
}

// ---- the report -----------------------------------------------------------------------------------

export const ARM_WORDS: Record<Arm, { label: string; text: string }> = {
  on: { label: 'On', text: 'The provider answered.' },
  shadow: { label: 'Shadow', text: 'Asked beside the conductor. Agreement is with the conductor’s own decision.' },
  ab_b: { label: 'A/B, the provider', text: 'The share of sessions the provider answered.' },
  ab_a: { label: 'A/B, the built-in', text: 'The rest, answered by the built-in decider. Its times are the decider’s.' },
};

export interface ReportCell {
  arm: Arm;
  bucket: Bucket | null;
}

export interface ReportRow {
  question: string;
  calls: number;
  cells: ReportCell[];
}

/** One provider's report: a row a question, the arms that have calls side by side. `arms`: the
 *  columns to draw, in the same order for every row. */
export function reportRows(report: Report, pid: string): { arms: Arm[]; rows: ReportRow[] } {
  const qs = report.providers[pid] ?? {};
  const all: Arm[] = ['on', 'shadow', 'ab_b', 'ab_a'];
  const arms = all.filter((a) => Object.values(qs).some((b) => b[a]?.calls));
  const rows = Object.keys(qs)
    .sort()
    .map((question) => ({ question, calls: arms.reduce((n, a) => n + (qs[question][a]?.calls ?? 0), 0), cells: arms.map((arm) => ({ arm, bucket: qs[question][arm] ?? null })) }));
  return { arms, rows };
}

const pct = (n: number, of: number): string => (of ? `${Math.round((100 * n) / of)} %` : '0 %');

/** One arm of one question in words. Agreement is measured in shadow only: elsewhere the answer
 *  was used, so there is nothing to compare it with. */
export function cellWords(b: Bucket, arm: Arm): { calls: string; answered: string; agreement: string | null; time: string; trouble: string | null; top: string | null } {
  const top = Object.entries(b.labels).sort((x, y) => y[1] - x[1])[0];
  const trouble = [b.failures ? `${b.failures} failed` : '', b.fallbacks ? `${b.fallbacks} fell back to the built-in` : ''].filter(Boolean).join(', ');
  return {
    calls: `${b.calls} ${b.calls === 1 ? 'call' : 'calls'}`,
    answered: arm === 'ab_a' ? 'answered by the built-in' : `${pct(b.answered, b.calls)} answered`,
    agreement: b.agreement_pct == null ? null : `${b.agreement_pct} % agree (${b.agreed} of ${b.compared})`,
    time: b.p50_ms == null ? 'no time measured' : `p50 ${ms(b.p50_ms)}, p95 ${ms(b.p95_ms)}`,
    trouble: trouble || null,
    top: top ? `mostly “${top[0]}” (${pct(top[1], Object.values(b.labels).reduce((n, x) => n + x, 0))})` : null,
  };
}

/** the report's reach in words */
export function reportWords(r: Pick<Report, 'hours' | 'source'>): string {
  const span = r.hours >= 48 ? `${Math.round(r.hours / 24)} days` : r.hours === 1 ? 'hour' : `${r.hours} hours`;
  return r.source === 'memory' ? `The last ${span}, from this controller’s memory: it runs no conductor journal, so a restart empties it.` : `The last ${span}, from the conductor’s journal.`;
}

/** The questions two enabled providers would answer at once (on or A/B): the controller refuses
 *  that, so the page says it before the review does. */
export function conflicts(list: Pick<Provider, 'id' | 'name' | 'enabled' | 'mode' | 'questions'>[]): { question: string; names: string[] }[] {
  const by = new Map<string, string[]>();
  for (const p of list) {
    if (!p.enabled || p.mode === 'shadow') continue;
    for (const q of Object.keys(p.questions)) by.set(q, [...(by.get(q) ?? []), p.name]);
  }
  return [...by.entries()].filter(([, names]) => names.length > 1).map(([question, names]) => ({ question, names }));
}

/** a provider's calls in a line each: who answered, what, and whether the two agreed */
export function providerCallWords(c: ProviderCall): { tone: Tone; text: string } {
  if (c.arm === 'a') return { tone: 'muted', text: `built-in answered “${c.label}” (its A/B side)` };
  if (c.fallback) return { tone: 'err', text: `fell back to the built-in: ${fallbackWords(c.fallback).title.toLowerCase()}` };
  if (c.mode === 'shadow') {
    if (c.answer?.error) return { tone: 'warn', text: `no answer in shadow: ${fallbackWords(c.answer.error).title.toLowerCase()}` };
    return c.agree ? { tone: 'ok', text: `agreed with the conductor on “${c.label}”` } : { tone: 'warn', text: `said “${c.answer?.label}”, the conductor decided “${c.label}”` };
  }
  return { tone: 'ok', text: `answered “${c.label}”` };
}
