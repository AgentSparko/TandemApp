// SRV-889: the smart token dialog's logic: what the key is for, which backends and models it may
// run, and the body POST /v1/auth/tokens takes. Pure functions, tested in token-plan.test.ts.

import type { NewToken } from '../api/auth';
import type { BackendConfig } from '../api/config';

/** what a key is for: each fills in the scopes, a name and the snippets the made key shows */
export type Purpose = 'agent' | 'chat-app' | 'script' | 'anthropic' | 'monitoring' | 'custom';

export interface PurposeDef {
  id: Purpose;
  label: string;
  text: string;
  scopes: string[];
  name: string;
  /** whether the key runs models (the backend and model step shows) */
  runs: boolean;
}

export const PURPOSES: PurposeDef[] = [
  { id: 'agent', label: 'A coding agent', text: 'opencode, Hermes, any agent that speaks the OpenAI API', scopes: ['inference:openai'], name: 'coding agent', runs: true },
  { id: 'anthropic', label: 'A Messages API client', text: 'Claude Code and other clients of the Messages API', scopes: ['inference:anthropic'], name: 'messages client', runs: true },
  { id: 'chat-app', label: 'A chat app', text: 'Open WebUI, LibreChat, a phone app', scopes: ['inference:openai'], name: 'chat app', runs: true },
  { id: 'script', label: 'A script or SDK', text: 'the OpenAI or Anthropic SDK, curl, a batch job', scopes: ['inference:openai', 'inference:anthropic'], name: 'script', runs: true },
  { id: 'monitoring', label: 'Monitoring', text: 'Prometheus and the usage views, no models', scopes: ['metrics:read', 'usage:read'], name: 'monitoring', runs: false },
  { id: 'custom', label: 'Something else', text: 'pick every right yourself', scopes: [], name: '', runs: true },
];
export const purposeDef = (id: Purpose): PurposeDef => PURPOSES.find((p) => p.id === id) ?? PURPOSES[PURPOSES.length - 1];

/** one backend and the models it serves, as the picker shows them */
export interface CatalogBackend {
  id: string;
  label: string;
  kind: string;
  enabled: boolean;
  /** the model ids a client sends (the public id where one is set) */
  models: string[];
}

/** the picker's catalog from GET /v1/config: every backend, its present and enabled models */
export function catalogOf(backends: BackendConfig[]): CatalogBackend[] {
  return backends
    .map((b) => {
      const rows = (b.model_rows ?? []).filter((m) => m.present && m.enabled);
      const models = rows.length ? rows.map((m) => m.public_id || m.model) : b.models;
      return { id: b.id, label: b.label || b.id, kind: b.kind, enabled: b.enabled, models: [...new Set(models)].sort() };
    })
    .sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.label.localeCompare(b.label));
}

/** what the key may run: everything, or these backends with all or some of their models */
export interface RunPick {
  every: boolean;
  /** backend id -> 'all' or the chosen models */
  backends: Record<string, 'all' | string[]>;
  /** advanced: name patterns over every backend ("small-*") */
  patterns: string;
}
export const everything = (): RunPick => ({ every: true, backends: {}, patterns: '' });

export const patternList = (s: string): string[] => [...new Set(s.split(/[\s,]+/).filter(Boolean))];

/** the backends and models fields of the body (none: every backend, every model) */
export function runFields(p: RunPick): { backends?: string[]; models?: string[] } {
  const patterns = patternList(p.patterns);
  if (p.every) return patterns.length ? { models: patterns } : {};
  const ids = Object.keys(p.backends).filter((b) => p.backends[b] === 'all' || (p.backends[b] as string[]).length);
  const models: string[] = [];
  for (const b of ids) {
    const v = p.backends[b];
    if (v === 'all') models.push(`${b}/*`);
    else for (const m of v) models.push(`${b}/${m}`);
  }
  // the patterns narrow further only when no backend is picked whole... keep them as given:
  // a pattern over the public id lets that model through on the picked backends
  return { backends: ids, models: [...new Set([...models, ...patterns])] };
}

/** the sentence or the error of the pick: empty picks stop the dialog */
export function runError(p: RunPick): string | null {
  if (p.every) return null;
  const f = runFields(p);
  if (!f.backends?.length) return 'Pick at least one backend, or let the key run everything.';
  if ((f.models?.length ?? 0) > 64) return 'A key names at most 64 models: pick whole backends instead.';
  return null;
}

/** counts for the summary: backends, and models (null: all of a backend's) */
export function runCounts(p: RunPick, cat: CatalogBackend[]): { backends: number; models: number | null } {
  if (p.every) return { backends: cat.length, models: null };
  const ids = runFields(p).backends ?? [];
  let n = 0;
  for (const b of ids) {
    const v = p.backends[b];
    if (v === 'all') n += cat.find((c) => c.id === b)?.models.length ?? 0;
    else n += v.length;
  }
  return { backends: ids.length, models: n };
}

export interface Plan {
  purpose: Purpose;
  label: string;
  scopes: string[];
  run: RunPick;
  days: string;
  limits: Partial<Record<string, string>>;
}

/** the body of POST /v1/auth/tokens, or what is wrong */
export function planBody(p: Plan, allowedScopes: string[], admin: boolean): NewToken | string {
  const label = p.label.trim();
  if (!label || label.length > 64) return 'Give the key a name of 1 to 64 characters.';
  const scopes = p.scopes.filter((s) => allowedScopes.includes(s));
  if (!scopes.length) return 'Pick at least one thing the key may do.';
  const runs = scopes.some((s) => s.startsWith('inference:'));
  if (runs) {
    const e = runError(p.run);
    if (e) return e;
  }
  let days: number | 'never';
  if (p.days === 'never') {
    if (!admin) return 'Only an admin makes a key that never expires.';
    days = 'never';
  } else {
    days = Number(p.days);
    if (!Number.isInteger(days) || days < 1) return 'The days are a whole number of 1 or more.';
  }
  const limits: Record<string, number> = {};
  for (const [k, raw] of Object.entries(p.limits)) {
    const t = (raw ?? '').trim();
    if (!t) continue;
    const n = Number(t);
    if (!Number.isFinite(n) || n < 0) return 'A limit is a number of 0 or more.';
    limits[k] = n;
  }
  const run = runs ? runFields(p.run) : {};
  return { label, scopes, days, ...run, ...(Object.keys(limits).length ? { limits } : {}) } as NewToken;
}

/** one sentence that says what the key will do */
export function planSummary(p: Plan, cat: CatalogBackend[], scopeLabel: (s: string) => string): string {
  const parts: string[] = [];
  const api = p.scopes.filter((s) => s.startsWith('inference:'));
  const other = p.scopes.filter((s) => !s.startsWith('inference:'));
  if (api.length) {
    const how = api.map(scopeLabel).join(' and ');
    if (p.run.every && !patternList(p.run.patterns).length) parts.push(`calls the ${how} with every model of every backend`);
    else if (p.run.every) parts.push(`calls the ${how} with the models that match ${patternList(p.run.patterns).join(', ')}`);
    else {
      const c = runCounts(p.run, cat);
      const names = (runFields(p.run).backends ?? []).map((b) => cat.find((x) => x.id === b)?.label ?? b);
      parts.push(`calls the ${how} with ${c.models} model${c.models === 1 ? '' : 's'} on ${names.join(', ')}`);
    }
  }
  if (other.length) parts.push(`may ${other.map((s) => scopeLabel(s).toLowerCase()).join(', ')}`);
  const end = p.days === 'never' ? 'never expires' : `expires in ${p.days} days`;
  return `This key ${parts.join('; it ')}, and ${end}.`;
}

/** the first model the key may run, for the test command */
export function firstModel(p: Plan, cat: CatalogBackend[]): string {
  if (!p.run.every) {
    for (const b of runFields(p.run).backends ?? []) {
      const v = p.run.backends[b];
      const m = v === 'all' ? cat.find((c) => c.id === b)?.models[0] : v[0];
      if (m) return m;
    }
  }
  const pat = patternList(p.run.patterns).find((x) => !/[*?[]/.test(x));
  return pat ?? cat.find((c) => c.enabled && c.models.length)?.models[0] ?? 'MODEL';
}

/** what the made key shows: the address and a ready setup for what it is for */
export function setupFor(purpose: Purpose, origin: string, token: string, model: string): { label: string; value: string }[] {
  const v1 = `${origin}/v1`;
  const out = [
    { label: 'Base URL, OpenAI-compatible', value: v1 },
    { label: 'Base URL, Messages API', value: origin },
  ];
  if (purpose === 'monitoring')
    return [{ label: 'Prometheus scrape: bearer token', value: `authorization: {type: Bearer, credentials: ${token}}` }, { label: 'Metrics URL', value: `${origin}/metrics` }];
  if (purpose === 'anthropic') return [out[1], { label: 'Environment', value: `export ANTHROPIC_BASE_URL=${origin}\nexport ANTHROPIC_API_KEY=${token}` }, { label: 'Test it', value: `curl -s ${origin}/v1/messages -H 'x-api-key: ${token}' -H 'content-type: application/json' -d '{"model":"${model}","max_tokens":32,"messages":[{"role":"user","content":"Say hi"}]}'` }];
  return [...out, { label: 'Environment', value: `export OPENAI_BASE_URL=${v1}\nexport OPENAI_API_KEY=${token}` }, { label: 'Test it', value: `curl -s ${v1}/chat/completions -H 'Authorization: Bearer ${token}' -H 'Content-Type: application/json' -d '{"model":"${model}","messages":[{"role":"user","content":"Say hi"}]}'` }];
}
