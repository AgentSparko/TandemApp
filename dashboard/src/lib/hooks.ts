// The hooks pages in logic and words (VIS-282): the request's path as the stage table gives it,
// which hooks sit at each stop and in what order they run, what a move costs in priorities, how
// much of a stage's time budget its hooks use, a new hook's form and its checks, what a test call
// answered, a request's trace out of the hooks' journals, why a replay cannot run, and every
// refusal of the hooks' API as a sentence. Kept free of the DOM so each piece is tested by itself;
// views/hooks/ draws them.

import { SCOPE_KEYS, type Breaker, type CallDiff, type DiffEntry, type Hook, type HookCall, type HookError, type HookMode, type HookScope, type HookTest, type NewHook, type Outcome, type Payload, type Policy, type Role, type StageSpec, type StageTable } from '../api/hooks';
import { same, type Draft, type DraftPart } from './settings-draft';
import type { Tone } from './settings-words';

export type ScopeKey = (typeof SCOPE_KEYS)[number];

/** the hooks scope of the settings interface, where every switch of a hook is a key */
export const HOOKS_SCOPE = 'hooks' as const;
export const HERE = 'controller';
/** a hook's switches: the keys `hooks.<id>.<field>` */
export const HOOK_SWITCHES = ['enabled', 'mode', 'priority', 'scope', 'policy', 'timeout_ms', 'payload'] as const;
export type HookSwitch = (typeof HOOK_SWITCHES)[number];
export const hookKey = (id: string, field: HookSwitch): string => `hooks.${id}.${field}`;

export const PRIORITY_MIN = -100000;
export const PRIORITY_MAX = 100000;

// ---- the stages in words --------------------------------------------------------------------------

const STAGE_TITLE: Record<string, string> = {
  'request.received': 'A request arrives',
  'route.select': 'The backend is chosen',
  'request.before_engine': 'Just before the engine',
  'decision.ask': 'The conductor asks a question',
  'stream.chunk': 'Each streamed event',
  'tool_call.detected': 'The answer holds a tool call',
  'tool.before_exec': 'Before a tool runs',
  'tool.after_exec': 'After a tool ran',
  'response.after_engine': 'The whole answer',
  'request.error': 'An upstream error',
  'request.finished': 'The request is finished',
  'memory.write': 'A note is about to be stored',
  'session.start': 'A session starts',
  'session.compact': 'A session is compacted',
  'engine.lifecycle': 'An engine starts or stops',
};
/** the work that calls a stage this controller does not call itself yet */
const STAGE_OWNER: Record<string, string> = {
  'decision.ask': 'the decision providers',
  'tool.before_exec': 'Chat’s tool stages',
  'tool.after_exec': 'Chat’s tool stages',
  'memory.write': 'the memory writer',
  'session.start': 'Chat’s session stages',
  'session.compact': 'Chat’s session stages',
  'engine.lifecycle': 'engine control',
};

/** a stage's name in plain words; a stage this page does not know keeps its id */
export const stageTitle = (stage: string): string => STAGE_TITLE[stage] ?? stage;
export const stageOwner = (stage: string): string => STAGE_OWNER[stage] ?? 'the work that owns it';

const MAY: Record<string, string> = { modify: 'change it', deny: 'refuse it', decide: 'decide', observe: 'watch', retry: 'ask once more', 'choose fallback': 'choose a fallback' };
/** what a hook may do at a stage: `change it, refuse it` */
export const mayWords = (spec: Pick<StageSpec, 'may'>): string[] => spec.may.map((m) => MAY[m] ?? m);

export const ROLE_ORDER: Role[] = ['transform', 'guard', 'decider', 'observer'];
export const ROLE_WORDS: Record<Role, { label: string; does: string; runs: string }> = {
  transform: { label: 'Transform', does: 'Changes the value with a patch.', runs: 'Transforms run first, in order: each sees what the one before it made.' },
  guard: { label: 'Guard', does: 'May refuse the request.', runs: 'Guards run after the transforms. The first one that refuses wins.' },
  decider: { label: 'Decider', does: 'Chooses: a backend, a fallback, one retry or an answer.', runs: 'Deciders run after the guards. The first decision wins.' },
  observer: { label: 'Observer', does: 'Only watches.', runs: 'Observers see the final value, off the request’s path.' },
};

export const MODE_WORDS: Record<HookMode, { label: string; text: string }> = {
  off: { label: 'Off', text: 'Never called.' },
  shadow: { label: 'Shadow', text: 'Called on a copy of real requests. Its answer is kept in the journal, nothing is applied.' },
  on: { label: 'On', text: 'Called on real requests. What it answers is applied.' },
};

/** the payload levels, as what leaves TandemApp */
export const PAYLOAD_WORDS: Record<Payload, { label: string; sees: string; never: string }> = {
  meta: {
    label: 'Facts only',
    sees: 'Ids, the model, sizes, counts, timings, the scalar parameters and tool names.',
    never: 'Never a word of a prompt or an answer.',
  },
  redacted: {
    label: 'Text with secrets removed',
    sees: 'The facts, and the prompts and answers with keys, tokens and passwords replaced by [redacted:kind].',
    never: 'If the redaction fails, the hook is not called.',
  },
  full: {
    label: 'Everything as written',
    sees: 'The facts, and every prompt and answer exactly as it is.',
    never: 'Granting it needs pairing’s rights. A destination outside the LAN asks a second time, and TandemApp keeps who granted it and when.',
  },
};

export const POLICY_WORDS: Record<Policy, { label: string; text: string }> = {
  open: { label: 'Skip the hook', text: 'The request goes on as if the hook were not there.' },
  closed: { label: 'Refuse the request', text: 'The client gets 503 hook_unavailable.' },
};

/** the policy a hook has at a stage when it sets none: guards refuse, the rest follow the stage */
export function defaultPolicy(spec: Pick<StageSpec, 'on_failure' | 'sync'>, role: Role): string {
  if (!spec.sync) return 'drop';
  return role === 'guard' ? 'closed' : spec.on_failure;
}

export const policyWords = (p: string): string => (p === 'closed' ? 'fails closed' : p === 'drop' ? 'failures dropped' : 'fails open');

export const KIND_WORDS: Record<string, { label: string; text: string }> = {
  http: { label: 'Webhook', text: 'TandemApp POSTs a signed JSON document to your URL and reads the answer.' },
  builtin: { label: 'Built-in', text: 'Code that ships in TandemApp: set a field, refuse, route, fall back, or only journal.' },
  plugin: { label: 'Plugin', text: 'Arrives with the plugin host.' },
  mcp: { label: 'MCP', text: 'Arrives with the plugin host’s MCP client.' },
  inproc: { label: 'In-process', text: 'Arrives with the in-process chain.' },
};

export function kindWords(h: Pick<Hook, 'kind' | 'target' | 'destination'>): string {
  if (h.kind === 'http') return `webhook${h.destination.host ? ` to ${h.destination.host}` : ''}`;
  if (h.kind === 'builtin') return `built-in ${h.target ?? ''}`.trim();
  return h.kind;
}

// ---- a hook's breaker and a call's end -----------------------------------------------------------

export function breakerWords(b: Breaker): { tone: Tone; label: string; text: string } {
  if (b.state === 'open') return { tone: 'err', label: 'breaker open', text: `It failed too often and is skipped for ${Math.max(1, Math.ceil(b.open_for_s))} s more. Then one probe call decides.` };
  if (b.state === 'half_open') return { tone: 'live', label: 'breaker probing', text: 'The next call is a probe: a success closes the breaker, a failure opens it again for 30 s.' };
  return { tone: 'ok', label: 'breaker closed', text: b.consecutive_failures ? `Calls go through. ${b.consecutive_failures} in a row failed; five open the breaker.` : 'Calls go through.' };
}

const OUTCOME: Record<Outcome, { tone: Tone; text: string }> = {
  ok: { tone: 'ok', text: 'answered' },
  timeout: { tone: 'err', text: 'timed out' },
  error: { tone: 'err', text: 'failed' },
  invalid: { tone: 'err', text: 'invalid answer' },
  cap: { tone: 'warn', text: 'no time left' },
  breaker_open: { tone: 'warn', text: 'skipped, breaker open' },
  redaction_failed: { tone: 'err', text: 'redaction failed' },
  refused: { tone: 'warn', text: 'not applied' },
  dropped: { tone: 'warn', text: 'dropped' },
};
const ACTION: Record<string, string> = { continue: 'no change', modify: 'changed', deny: 'refused', decide: 'decided' };
const WOULD: Record<string, string> = { continue: 'would change nothing', modify: 'would change', deny: 'would refuse', decide: 'would decide' };

export interface DiffRow {
  field: string;
  text: string;
}

const bytes = (n: number | undefined): string => (n == null ? '' : n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);
const DIFF_OP: Record<string, string> = { add: 'added', replace: 'replaced', remove: 'removed' };

/** A diff as rows: the fields a patch touches with what happens to each and its size. Never a value. */
export function diffRows(diff: CallDiff | Record<string, DiffEntry> | undefined): DiffRow[] {
  if (!diff) return [];
  return Object.keys(diff)
    .sort()
    .filter((k) => k !== 'deny' && k !== 'decision')
    .map((k) => {
      const d = diff[k] as DiffEntry;
      const size = bytes(d.bytes);
      return { field: k, text: `${DIFF_OP[d.op] ?? d.op}${size ? `, ${size}` : ''}` };
    });
}

/** a decision in a few words: `backend cloud, model big-1` */
export function decisionWords(d: Record<string, unknown> | undefined | null): string {
  if (!d) return '';
  const flat = (v: unknown): string => (v && typeof v === 'object' ? Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k} ${flat(x)}`).join(', ') : String(v));
  return Object.entries(d)
    .map(([k, v]) => (v === true ? k : `${k} ${flat(v)}`))
    .join(', ');
}

/** One journal row in a line: how the call ended and what it did, or would have done in shadow. */
export function callWords(c: Pick<HookCall, 'outcome' | 'action' | 'mode' | 'fields' | 'decision' | 'reason' | 'error' | 'diff'>): { tone: Tone; outcome: string; did: string } {
  const o = OUTCOME[c.outcome] ?? { tone: 'muted' as Tone, text: c.outcome };
  if (c.outcome !== 'ok' && c.outcome !== 'refused') return { tone: o.tone, outcome: o.text, did: c.error ?? '' };
  const a = c.action ?? 'continue';
  const live = c.mode === 'on' && c.outcome === 'ok';
  let did = live ? (ACTION[a] ?? a) : (WOULD[a] ?? a);
  if (a === 'modify') {
    const f = c.fields?.length ? c.fields : diffRows(c.diff).map((d) => d.field);
    if (f.length) did += ` ${f.join(', ')}`;
  } else if (a === 'decide') {
    const w = decisionWords(c.decision ?? (c.diff?.decision as Record<string, unknown> | undefined));
    if (w) did += `: ${w}`;
  } else if (a === 'deny' && c.reason) did += `: ${c.reason}`;
  if (c.outcome === 'refused') return { tone: o.tone, outcome: o.text, did: `${did} (${c.error ?? 'the answer went out already'})` };
  return { tone: a === 'deny' && live ? 'warn' : o.tone, outcome: o.text, did };
}

export const ms = (n: number | null | undefined): string => (n == null ? 'not measured' : `${n < 10 ? n.toFixed(1) : Math.round(n)} ms`);

// ---- what is staged -------------------------------------------------------------------------------

export interface ShownHook extends Hook {
  /** the switches with a staged change: what shows is what would hold after the apply */
  staged: Set<HookSwitch>;
}

const emptyScope = (v: unknown): boolean => !v || typeof v !== 'object' || Object.values(v as HookScope).every((x) => !x || !x.length);

/** The hooks as the page shows them: a staged switch stands in for what holds now. */
export function withStaged(hooks: Hook[], part: DraftPart | undefined): ShownHook[] {
  return hooks.map((h) => {
    const out: ShownHook = { ...h, staged: new Set() };
    if (!part) return out;
    for (const f of HOOK_SWITCHES) {
      const k = hookKey(h.id, f);
      if (k in part.set) {
        (out as unknown as Record<string, unknown>)[f] = part.set[k];
        out.staged.add(f);
      } else if (part.unset.includes(k)) {
        (out as unknown as Record<string, unknown>)[f] = f === 'scope' ? {} : null;
        out.staged.add(f);
      }
    }
    return out;
  });
}

/** Stage one switch of a hook as a key of the settings interface. A value equal to what holds is
 *  no change; nothing (null, an empty scope) hands a key back to its default. */
export function stageSwitch(d: Draft, h: Hook, field: HookSwitch, value: unknown): void {
  const key = hookKey(h.id, field);
  const now = h[field] as unknown;
  const held = { value: field === 'scope' && emptyScope(now) ? null : now, set: field === 'scope' ? !emptyScope(now) : now !== null };
  if (value === null || (field === 'scope' && emptyScope(value))) {
    if (held.set) d.unset(HOOKS_SCOPE, HERE, key, held);
    else d.drop(HOOKS_SCOPE, HERE, key);
  } else if (held.set && same(held.value, value)) d.drop(HOOKS_SCOPE, HERE, key);
  else d.set(HOOKS_SCOPE, HERE, key, value, held);
}

/** Take back whatever is staged for a hook or a provider that is gone. */
export function dropStaged(d: Draft, prefix: string): void {
  const p = d.part(HOOKS_SCOPE, HERE);
  if (!p) return;
  for (const k of [...Object.keys(p.set), ...p.unset]) if (k.startsWith(prefix)) d.drop(HOOKS_SCOPE, HERE, k);
}

// ---- the path -------------------------------------------------------------------------------------

/** The hooks of one role at a stage, in the order they run: priority, low first; ties by id. */
export function runOrder<T extends Pick<Hook, 'id' | 'priority'>>(hooks: T[]): T[] {
  return [...hooks].sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

export interface Use {
  budget_ms: number;
  /** the p95 of each hook that runs on the request's path here, added up */
  used_ms: number;
  parts: { id: string; name: string; ms: number }[];
  /** hooks on the path with no call measured yet */
  unmeasured: number;
  over: boolean;
}

/** on the request's path: switched on, applied, and not an observer (those run beside it) */
const onPath = (h: Pick<Hook, 'enabled' | 'mode' | 'role'>): boolean => h.enabled && h.mode === 'on' && h.role !== 'observer';

function useOf(budget: number, hooks: Hook[]): Use {
  const on = hooks.filter(onPath);
  const parts = on.filter((h) => h.metrics.p95_ms != null).map((h) => ({ id: h.id, name: h.name, ms: h.metrics.p95_ms! }));
  const used = parts.reduce((n, p) => n + p.ms, 0);
  return { budget_ms: budget, used_ms: Math.round(used * 10) / 10, parts, unmeasured: on.length - parts.length, over: used > budget };
}

export interface StopGroup {
  role: Role;
  hooks: ShownHook[];
}

export interface Stop {
  /** its place on the path, from 1 */
  n: number;
  stage: string;
  title: string;
  spec: StageSpec;
  /** the hooks here by role, in the order the roles run */
  groups: StopGroup[];
  count: number;
  /** how much of the stage's budget its hooks use; null off the path or with no hook on it */
  use: Use | null;
}

/** The request's path: every stage in the table's order with the hooks that sit there. */
export function pipeline(table: StageTable, hooks: ShownHook[]): Stop[] {
  return Object.entries(table.stages).map(([stage, spec], i) => {
    const here = hooks.filter((h) => h.stages.includes(stage));
    const groups = ROLE_ORDER.map((role) => ({ role, hooks: runOrder(here.filter((h) => h.role === role)) })).filter((g) => g.hooks.length);
    const use = spec.sync && spec.budget_ms != null && here.some(onPath) ? useOf(spec.budget_ms, here) : null;
    return { n: i + 1, stage, title: stageTitle(stage), spec, groups, count: here.length, use };
  });
}

/** The cap before the first token: what the hooks on the path use of it, over the stages that count. */
export function capUse(table: StageTable, hooks: Hook[]): Use {
  const all: Use = { budget_ms: table.cap_ms, used_ms: 0, parts: [], unmeasured: 0, over: false };
  for (const [stage, spec] of Object.entries(table.stages)) {
    if (!spec.sync || !spec.first_token) continue;
    const u = useOf(spec.budget_ms ?? table.cap_ms, hooks.filter((h) => h.stages.includes(stage)));
    all.used_ms += u.used_ms;
    all.unmeasured += u.unmeasured;
    all.parts.push(...u.parts);
  }
  all.used_ms = Math.round(all.used_ms * 10) / 10;
  all.over = all.used_ms > all.budget_ms;
  return all;
}

/** the time limit a hook has at a stage: its own, at most the stage's budget */
export function timeoutAt(h: Pick<Hook, 'timeout_ms'>, spec: Pick<StageSpec, 'budget_ms'>, table: Pick<StageTable, 'async_timeout_ms'>): number {
  if (spec.budget_ms == null) return h.timeout_ms ?? table.async_timeout_ms;
  return Math.min(h.timeout_ms ?? spec.budget_ms, spec.budget_ms);
}

const before = (a: { priority: number; id: string }, b: { priority: number; id: string }): boolean => a.priority < b.priority || (a.priority === b.priority && a.id < b.id);

/** Move the hook at `from` to `to` within one role's run order: the priorities to set so the new
 *  order holds, as few as possible. One new priority for the moved hook when a number fits between
 *  its new neighbours; otherwise the whole group is numbered again in steps of ten. A priority is
 *  the hook's own, so it moves at every stage the hook runs at. */
export function movePriorities(order: Pick<Hook, 'id' | 'priority'>[], from: number, to: number): Record<string, number> {
  if (from === to || from < 0 || to < 0 || from >= order.length || to >= order.length) return {};
  const next = order.map((h) => ({ id: h.id, priority: h.priority }));
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  const prev = next[to - 1];
  const after = next[to + 1];
  const tries: number[] = [];
  if (prev && after) tries.push(Math.floor((prev.priority + after.priority) / 2), prev.priority, after.priority);
  else if (after) tries.push(after.priority - 10, after.priority - 1, after.priority);
  else if (prev) tries.push(prev.priority + 10, prev.priority + 1, prev.priority);
  for (const p of tries) {
    if (p < PRIORITY_MIN || p > PRIORITY_MAX) continue;
    const me = { id: moved.id, priority: p };
    if ((!prev || before(prev, me)) && (!after || before(me, after))) return p === moved.priority ? {} : { [moved.id]: p };
  }
  const base = Math.max(PRIORITY_MIN, Math.min(next[0].priority, PRIORITY_MAX - 10 * next.length));
  const out: Record<string, number> = {};
  next.forEach((h, i) => {
    const p = base + i * 10;
    if (p !== order.find((x) => x.id === h.id)!.priority) out[h.id] = p;
  });
  return out;
}

// ---- which requests -------------------------------------------------------------------------------

export const SCOPE_LABEL: Record<ScopeKey, { label: string; hint: string }> = {
  models: { label: 'Models', hint: 'qwen*, big-*' },
  backends: { label: 'Backends', hint: 'local, cloud' },
  profiles: { label: 'Profiles', hint: 'default' },
  users: { label: 'Users', hint: 'guest*' },
  sessions: { label: 'Sessions', hint: 's-*' },
  client_kinds: { label: 'Client kinds', hint: 'chat, api' },
};

/** `every request`, or `models qwen*, big-*; users guest*` */
export function scopeWords(s: HookScope | null | undefined): string {
  const parts = SCOPE_KEYS.filter((k) => s?.[k]?.length).map((k) => `${SCOPE_LABEL[k].label.toLowerCase()} ${s![k]!.join(', ')}`);
  return parts.length ? parts.join('; ') : 'every request';
}

/** patterns typed in one field: commas or lines between them */
export const parsePatterns = (text: string): string[] => [...new Set(text.split(/[,\n]/).map((x) => x.trim()).filter(Boolean))];

export function scopeOf(texts: Partial<Record<ScopeKey, string>>): HookScope {
  const out: HookScope = {};
  for (const k of SCOPE_KEYS) {
    const v = parsePatterns(texts[k] ?? '');
    if (v.length) out[k] = v;
  }
  return out;
}

export const scopeTexts = (s: HookScope | null | undefined): Record<ScopeKey, string> => Object.fromEntries(SCOPE_KEYS.map((k) => [k, (s?.[k] ?? []).join(', ')])) as Record<ScopeKey, string>;

/** what the registry refuses in a scope; null when it would take it */
export function scopeError(s: HookScope): string | null {
  for (const k of SCOPE_KEYS) {
    const v = s[k] ?? [];
    if (v.length > 64) return `${SCOPE_LABEL[k].label}: at most 64 patterns.`;
    if (v.some((x) => x.length > 128)) return `${SCOPE_LABEL[k].label}: a pattern is at most 128 characters.`;
  }
  return null;
}

// ---- a new hook -----------------------------------------------------------------------------------

/** what each built-in does, the role it has, the stage it needs and the options it reads */
export const BUILTINS: Record<string, { role: Role; stage?: string; text: string; options: { key: string; label: string; hint: string; json?: boolean; need?: boolean }[] }> = {
  set: { role: 'transform', text: 'Sets fields of the value to what you name.', options: [{ key: 'set', label: 'Fields to set (JSON)', hint: '{"temperature": 0.3}', json: true, need: true }] },
  deny: { role: 'guard', text: 'Refuses every request in its scope.', options: [{ key: 'reason', label: 'What the client is told', hint: 'ask the admin for this model' }] },
  route: {
    role: 'decider',
    stage: 'route.select',
    text: 'Sends the requests in its scope to a backend, a model or a profile.',
    options: [
      { key: 'backend', label: 'Backend', hint: 'cloud' },
      { key: 'model', label: 'Model', hint: 'example-model' },
      { key: 'profile', label: 'Profile', hint: 'default' },
    ],
  },
  fallback: {
    role: 'decider',
    stage: 'request.error',
    text: 'Names where a failed request goes next.',
    options: [
      { key: 'backend', label: 'Backend', hint: 'cloud', need: true },
      { key: 'model', label: 'Model', hint: 'example-model' },
    ],
  },
  noop: { role: 'observer', text: 'Does nothing but write its call into the journal.', options: [] },
};

/** the built-ins that fit a role at a stage; one this page does not know fits everywhere */
export function builtinsFor(names: string[], role: Role | '', stage: string): string[] {
  return names.filter((n) => {
    const b = BUILTINS[n];
    return !b || (b.role === role && (!b.stage || b.stage === stage));
  });
}

export interface HookForm {
  name: string;
  stage: string;
  role: Role | '';
  kind: 'http' | 'builtin';
  endpoint: string;
  target: string;
  /** the built-in's options, as typed */
  options: Record<string, string>;
  scope: Record<ScopeKey, string>;
  /** '': the stage's default */
  policy: Policy | '';
  /** '': the stage's budget */
  timeout: string;
  payload: Payload;
  mode: HookMode;
  priority: string;
}

export const rolesAt = (table: StageTable, stage: string): Role[] => ROLE_ORDER.filter((r) => table.stages[stage]?.roles.includes(r));

/** A new hook's form: it starts in shadow, so it sees real requests before it changes one. */
export function newForm(table: StageTable, stage = ''): HookForm {
  const at = table.stages[stage] ? stage : '';
  const roles = at ? rolesAt(table, at) : [];
  return { name: '', stage: at, role: roles.length === 1 ? roles[0] : '', kind: 'http', endpoint: '', target: '', options: {}, scope: scopeTexts({}), policy: '', timeout: '', payload: 'meta', mode: 'shadow', priority: '100' };
}

const URL_RE = /^https?:\/\/[^\s/@]+(\/\S*)?$/;

/** What is missing or wrong in the form, field by field; {} when it can be sent. */
export function checkForm(f: HookForm, table: StageTable): Record<string, string> {
  const e: Record<string, string> = {};
  const spec = table.stages[f.stage];
  if (!spec) e.stages = 'Pick where it runs.';
  if (!f.role) e.role = 'Pick what it does there.';
  else if (spec && !spec.roles.includes(f.role)) e.role = `A ${f.role} cannot run at ${f.stage}. It takes ${spec.roles.join(', ')}.`;
  const name = f.name.trim();
  if (!name) e.name = 'Give it a name.';
  else if (name.length > 80) e.name = 'At most 80 characters.';
  if (f.kind === 'http') {
    if (!f.endpoint.trim()) e.endpoint = 'The URL TandemApp calls.';
    else if (!URL_RE.test(f.endpoint.trim())) e.endpoint = 'A URL that starts with http:// or https://.';
  } else {
    if (!f.target) e.target = 'Pick the built-in.';
    for (const o of BUILTINS[f.target]?.options ?? []) {
      const v = (f.options[o.key] ?? '').trim();
      if (o.need && !v) e[`options.${o.key}`] = 'Needed.';
      else if (o.json && v) {
        try {
          const p = JSON.parse(v);
          if (!p || typeof p !== 'object' || Array.isArray(p)) e[`options.${o.key}`] = 'An object: { … }.';
        } catch (x) {
          e[`options.${o.key}`] = `Not valid JSON yet: ${(x as Error).message}`;
        }
      }
    }
  }
  if (!/^-?\d+$/.test(f.priority.trim()) || Number(f.priority) < PRIORITY_MIN || Number(f.priority) > PRIORITY_MAX) e.priority = `A whole number, ${PRIORITY_MIN} to ${PRIORITY_MAX}.`;
  if (f.timeout.trim()) {
    const max = spec?.budget_ms ?? table.max_timeout_ms;
    const t = Number(f.timeout);
    if (!/^\d+$/.test(f.timeout.trim()) || t < 1 || t > table.max_timeout_ms) e.timeout_ms = `A whole number of milliseconds, 1 to ${table.max_timeout_ms}.`;
    else if (t > max) e.timeout_ms = `At most the stage’s budget, ${max} ms.`;
  }
  const scope = scopeError(scopeOf(f.scope));
  if (scope) e.scope = scope;
  return e;
}

/** The form as the body of POST /v1/fleet/hooks. */
export function bodyOf(f: HookForm): NewHook {
  const out: NewHook = { name: f.name.trim(), kind: f.kind, stages: [f.stage], role: f.role as Role, priority: Number(f.priority), mode: f.mode, payload: f.payload };
  const scope = scopeOf(f.scope);
  if (Object.keys(scope).length) out.scope = scope;
  if (f.policy) out.policy = f.policy;
  if (f.timeout.trim()) out.timeout_ms = Number(f.timeout);
  if (f.kind === 'http') out.endpoint = f.endpoint.trim();
  else {
    out.target = f.target;
    const options: Record<string, unknown> = {};
    for (const o of BUILTINS[f.target]?.options ?? []) {
      const v = (f.options[o.key] ?? '').trim();
      if (v) options[o.key] = o.json ? JSON.parse(v) : v;
    }
    if (Object.keys(options).length) out.options = options;
  }
  return out;
}

// ---- the playground -------------------------------------------------------------------------------

/** A stage's sample event as a hook with payload `meta` gets it: the controller serves them with the
 *  stage table (GET /v1/fleet/hooks/stages `samples`, contracts/hooks-v1/examples). */
const sampleOf = (stage: string, table: StageTable | null | undefined): Record<string, unknown> => table?.samples?.[stage]?.event ?? {};

export const sampleText = (stage: string, table: StageTable | null | undefined): string => JSON.stringify(sampleOf(stage, table), null, 2);

/** The event typed in the playground: nothing to send when it is the sample itself, the object
 *  when it differs, the error when it is no JSON object. */
export function parseEvent(stage: string, raw: string, table: StageTable | null | undefined): { ok: true; event?: Record<string, unknown> } | { ok: false; error: string } {
  if (!raw.trim()) return { ok: true };
  let v: unknown;
  try {
    v = JSON.parse(raw);
  } catch (e) {
    return { ok: false, error: `Not valid JSON yet: ${(e as Error).message}` };
  }
  if (!v || typeof v !== 'object' || Array.isArray(v)) return { ok: false, error: 'An event is an object: { … }.' };
  return same(v, sampleOf(stage, table)) ? { ok: true } : { ok: true, event: v as Record<string, unknown> };
}

export interface Explained {
  tone: Tone;
  title: string;
  lines: string[];
}

/** A test call's answer, explained: what the hook said and what that would do to a request. */
export function explainTest(t: HookTest, limit_ms: number | null = null): Explained {
  const took = ms(t.duration_ms);
  const r = t.result ?? {};
  const action = typeof r.action === 'string' ? r.action : 'continue';
  switch (t.outcome) {
    case 'ok': {
      if (action === 'modify') {
        const rows = diffRows(t.diff);
        const fields = rows.length ? rows.map((d) => `${d.field}: ${d.text}`) : Object.keys((r.patch as object) ?? {}).map((k) => `${k}: set`);
        return { tone: 'ok', title: `It answered in ${took} and would change ${fields.length === 1 ? 'one field' : `${fields.length} fields`}.`, lines: fields };
      }
      if (action === 'deny') return { tone: 'warn', title: `It answered in ${took} and would refuse the request.`, lines: [typeof r.reason === 'string' && r.reason ? `The client would read: ${r.reason}` : 'It gave no reason.', 'The client would get 403 hook_denied.'] };
      if (action === 'decide') return { tone: 'ok', title: `It answered in ${took} with a decision.`, lines: [decisionWords(r.decision as Record<string, unknown>) || 'An empty decision: the built-in choice stands.'] };
      return { tone: 'ok', title: `It answered in ${took} and would change nothing.`, lines: ['“continue”: the request goes on as it is.'] };
    }
    case 'timeout':
      return { tone: 'err', title: `No answer within ${limit_ms != null ? `its ${limit_ms} ms` : 'its time limit'}.`, lines: ['On a real request this counts as a failure and its failure policy applies.'] };
    case 'invalid':
      return { tone: 'err', title: 'It answered, but not with something this stage allows.', lines: [t.error ?? 'The answer is not a result the contract knows.', 'On a real request this counts as a failure.'] };
    case 'redaction_failed':
      return { tone: 'err', title: 'The redaction failed, so the hook was not called.', lines: ['Its failure policy would apply to a real request.'] };
    case 'error':
      return { tone: 'err', title: 'The call failed.', lines: [t.error ?? 'The hook did not answer 200 or 204.', 'On a real request this counts as a failure and its failure policy applies.'] };
    default:
      return { tone: 'warn', title: `The call ended as “${t.outcome}”.`, lines: t.error ? [t.error] : [] };
  }
}

// ---- a request's trace ----------------------------------------------------------------------------

export interface TraceCall extends HookCall {
  hook: string;
  name: string;
  role: Role | null;
  payload: Payload | null;
}

export interface TraceStage {
  n: number;
  stage: string;
  title: string;
  calls: TraceCall[];
}

export interface Trace {
  request: string;
  stages: TraceStage[];
  calls: number;
  /** the hooks that were called, by id */
  hooks: string[];
  /** the time the hooks on the request's path took, added up */
  path_ms: number;
  started: number | null;
  /** the call that refused the request, when one did */
  refused: TraceCall | null;
}

/** One request across every stage, out of the hooks' journals: the stages in the path's order, the
 *  calls at each in the order they were made (ties: the order the hooks run in now). */
export function traceOf(request: string, journals: Record<string, HookCall[]>, hooks: Hook[], table: StageTable): Trace {
  const by = new Map(hooks.map((h) => [h.id, h]));
  const all: TraceCall[] = [];
  for (const [id, calls] of Object.entries(journals)) {
    const h = by.get(id);
    for (const c of calls) if (c.request === request) all.push({ ...c, hook: id, name: h?.name ?? id, role: h?.role ?? null, payload: h?.payload ?? null });
  }
  const order = Object.keys(table.stages);
  const rank = (c: TraceCall): number => {
    const h = by.get(c.hook);
    return h ? ROLE_ORDER.indexOf(h.role) * 1e7 + (h.priority - PRIORITY_MIN) : 9e9;
  };
  all.sort((a, b) => a.ts - b.ts || rank(a) - rank(b) || (a.hook < b.hook ? -1 : 1));
  const stages = [...new Set(all.map((c) => c.stage))]
    .sort((a, b) => (order.indexOf(a) < 0 ? 99 : order.indexOf(a)) - (order.indexOf(b) < 0 ? 99 : order.indexOf(b)))
    .map((stage) => ({ n: order.indexOf(stage) + 1, stage, title: stageTitle(stage), calls: all.filter((c) => c.stage === stage) }));
  const path = all.filter((c) => c.mode === 'on' && c.role !== 'observer');
  return {
    request,
    stages,
    calls: all.length,
    hooks: [...new Set(all.map((c) => c.hook))],
    path_ms: Math.round(path.reduce((n, c) => n + c.duration_ms, 0) * 10) / 10,
    started: all.length ? Math.min(...all.map((c) => c.ts)) : null,
    refused: all.find((c) => c.mode === 'on' && c.outcome === 'ok' && c.action === 'deny') ?? null,
  };
}

export interface RecentRequest {
  request: string;
  ts: number;
  calls: number;
  hooks: number;
  failed: boolean;
  refused: boolean;
}

/** The requests the journals still hold, newest first. A test call is not a request. */
export function recentRequests(journals: Record<string, HookCall[]>, limit = 20): RecentRequest[] {
  const by = new Map<string, RecentRequest & { ids: Set<string> }>();
  for (const [id, calls] of Object.entries(journals)) {
    for (const c of calls) {
      if (!c.request || c.mode === 'test') continue;
      let r = by.get(c.request);
      if (!r) by.set(c.request, (r = { request: c.request, ts: c.ts, calls: 0, hooks: 0, failed: false, refused: false, ids: new Set() }));
      r.ts = Math.max(r.ts, c.ts);
      r.calls++;
      r.ids.add(id);
      if (c.outcome !== 'ok' && c.outcome !== 'refused') r.failed = true;
      if (c.mode === 'on' && c.outcome === 'ok' && c.action === 'deny') r.refused = true;
    }
  }
  return [...by.values()]
    .sort((a, b) => b.ts - a.ts || (a.request < b.request ? -1 : 1))
    .slice(0, limit)
    .map(({ ids, ...r }) => ({ ...r, hooks: ids.size }));
}

export interface ReplayNote {
  /** this version has no replay: the page says so and runs nothing */
  available: false;
  title: string;
  lines: string[];
  /** what can be tried instead: a hook with a stage's sample event */
  playground: { hook: string; name: string; stage: string }[];
}

/** Why this request cannot be replayed, and what to do instead. Nothing here runs anything. */
export function replayNote(trace: Trace): ReplayNote {
  const calls = trace.stages.flatMap((s) => s.calls);
  const text = calls.filter((c) => c.payload === 'redacted' || c.payload === 'full');
  const kept =
    text.length === 0
      ? 'The body of this request was not kept. Its hooks ran at the payload level meta: they got ids, sizes and counts, never the text.'
      : 'The body of this request was not kept. Some of its hooks got the text, but the journal holds ids, outcomes and sizes, never a prompt or an answer.';
  const seen = new Set<string>();
  const playground = calls.filter((c) => !seen.has(`${c.hook}|${c.stage}`) && seen.add(`${c.hook}|${c.stage}`) && c.role !== null).map((c) => ({ hook: c.hook, name: c.name, stage: c.stage }));
  return {
    available: false,
    title: 'This request cannot be replayed',
    lines: [kept, 'This TandemApp has no replay yet either, so nothing was run and nothing went to the engine.', 'The playground does the nearest thing: it sends one hook the stage’s sample event and shows what it answers. Nothing is applied or counted.'],
    playground,
  };
}

// ---- the secret -----------------------------------------------------------------------------------

/** How a receiver checks a call's signature, with the standard library (docs/hooks.md). */
export const VERIFY: Record<'python' | 'node', string> = {
  python: `import hashlib, hmac, time

def verify(secret: str, headers, body: bytes) -> bool:
    ts = headers.get("X-Tandem-Timestamp") or ""
    sig = headers.get("X-Tandem-Signature") or ""
    if not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, part.strip()[3:])
               for part in sig.split(",") if part.strip().startswith("v1="))`,
  node: `const crypto = require("node:crypto");

function verify(secret, headers, body) {
  const ts = headers["x-tandem-timestamp"] || "";
  const sig = headers["x-tandem-signature"] || "";
  if (!/^\\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const want = crypto.createHmac("sha256", secret).update(ts + ".").update(body).digest();
  return sig.split(",").map((s) => s.trim()).filter((s) => s.startsWith("v1="))
    .some((s) => {
      const got = Buffer.from(s.slice(3), "hex");
      return got.length === want.length && crypto.timingSafeEqual(got, want);
    });
}`,
};

/** the waits on offer while the previous secret still signs beside a new one */
export const GRACES = [
  { s: 0, label: 'At once', text: 'The old secret stops signing now.' },
  { s: 3600, label: 'One hour', text: 'Both secrets sign for an hour: time to change the receiver.' },
  { s: 86400, label: 'One day', text: 'Both secrets sign for a day.' },
];

export function secretWords(s: Hook['secret'], now: number): string {
  if (!s?.set) return 'No secret is stored.';
  const both = s.previous_until && s.previous_until > now ? ` The previous one still signs beside it for ${Math.max(1, Math.round((s.previous_until - now) / 60000))} min.` : '';
  return `A secret is stored${s.last4 ? `, ending in ${s.last4}` : ''}. It is never shown again.${both}`;
}

// ---- refusals -------------------------------------------------------------------------------------

export interface HookRefusal {
  title: string;
  fix: string | null;
  detail: string | null;
}

const FIELD: Record<string, string> = { name: 'The name', stages: 'Where it runs', role: 'What it does', endpoint: 'The URL', target: 'The built-in', options: 'The options', priority: 'The priority', timeout_ms: 'The time limit', payload: 'What it sees', scope: 'Which requests', questions: 'The questions', hook_id: 'The hook', model: 'The model', api_key: 'The API key', grace_s: 'The grace', stage: 'The stage', event: 'The event', question: 'The question', text: 'The text' };

/** the form's field a refusal is about (`scope.models` -> scope) */
export const fieldOf = (e: Pick<HookError, 'facts'>): string | null => (e.facts.field ? e.facts.field.split('.')[0] : null);

/** Every refusal of the hooks' and the providers' API as a sentence and what to do about it. */
export function hookErrorWords(e: Pick<HookError, 'type' | 'message' | 'facts'>): HookRefusal {
  const m = e.message;
  switch (e.type) {
    case 'unauthorized':
      return { title: 'This needs TandemApp’s admin password.', fix: 'Sign in below, then try again. The full payload and a hook’s secret take pairing’s rights: with no admin password set, only the controller’s own machine may do this.', detail: m && !m.startsWith('TandemApp’s') ? m : null };
    case 'trust_required':
      return { title: 'This takes pairing’s rights.', fix: 'The full payload and a hook’s secret take them. Set an admin password in App settings and sign in, or do this on the controller’s own machine.', detail: null };
    case 'offline':
      return { title: 'TandemApp did not answer.', fix: 'Check that it still runs and that this device reaches it, then try again.', detail: null };
    case 'confirm_needed':
      return { title: 'Prompts and answers would leave the LAN.', fix: `Confirm it to grant the full payload to ${e.facts.destination ?? 'that destination'}.`, detail: null };
    case 'conflict':
      return { title: 'It changed meanwhile, on another screen or through the API.', fix: 'This is how it stands now. Make your change again.', detail: null };
    case 'exists':
      return { title: 'One with that id exists already.', fix: 'Give it another name.', detail: m };
    case 'question_taken':
      return { title: 'Another provider answers one of these questions already.', fix: 'One provider answers a question on or in A/B; any number may watch in shadow. Start this one in shadow, or move the question first.', detail: m };
    case 'kind_unavailable':
      return { title: 'That kind of hook does not run in this version.', fix: 'Use a webhook or a built-in.', detail: m };
    case 'invalid':
      return { title: `${FIELD[fieldOf(e) ?? ''] ?? 'A value'} is not accepted.`, fix: 'Correct it, then try again. Nothing was changed.', detail: m };
    case 'no_secret':
      return { title: 'Only a webhook has a secret.', fix: null, detail: null };
    case 'not_found':
      return { title: 'It is no longer there.', fix: 'It was removed on another screen or through the API. Go back to the list.', detail: m };
    case 'unavailable':
      return { title: 'TandemApp’s configuration database is not open.', fix: 'Look at TandemApp’s log on the controller’s machine.', detail: m };
    case 'bad_request':
      return { title: 'TandemApp did not take this request.', fix: null, detail: m };
    default:
      return { title: m || 'TandemApp answered with an error.', fix: 'Try again. If it keeps failing, look at TandemApp’s log.', detail: null };
  }
}

/** `14 Nov, 09:00:07` in the reader's own time, from milliseconds */
export const at = (t: number | null | undefined): string => (t ? new Date(t).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'not kept');
