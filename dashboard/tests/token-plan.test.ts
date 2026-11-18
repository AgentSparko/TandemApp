import { describe, expect, it } from 'vitest';
import type { BackendConfig } from '../src/api/config';
import { catalogOf, everything, firstModel, planBody, planSummary, runCounts, runError, runFields, setupFor, type Plan } from '../src/lib/token-plan';

const b = (id: string, models: string[], o: Partial<BackendConfig> = {}): BackendConfig =>
  ({ id, label: id.toUpperCase(), kind: 'generic', enabled: true, models: [], model_rows: models.map((m) => ({ backend: id, model: m, public_id: null, enabled: true, present: true })), ...o }) as unknown as BackendConfig;

const cat = catalogOf([b('vllm1', ['qwen-4b', 'qwen-9b']), b('openrouter', ['anthropic/claude', 'meta/llama']), b('off', ['x'], { enabled: false })]);
const plan = (o: Partial<Plan> = {}): Plan => ({ purpose: 'agent', label: 'laptop', scopes: ['inference:openai'], run: everything(), days: '90', limits: {}, ...o });
const ALL = ['inference:openai', 'inference:anthropic', 'metrics:read', 'usage:read'];

describe('the catalog', () => {
  it('lists enabled backends first, each with its present models sorted', () => {
    expect(cat.map((c) => c.id)).toEqual(['openrouter', 'vllm1', 'off']);
    expect(cat[1].models).toEqual(['qwen-4b', 'qwen-9b']);
  });
  it('takes the public id where one is set, and skips absent or disabled models', () => {
    const c = catalogOf([{ ...b('v', []), model_rows: [{ backend: 'v', model: 'a', public_id: 'fast', enabled: true, present: true }, { backend: 'v', model: 'b', public_id: null, enabled: false, present: true }, { backend: 'v', model: 'c', public_id: null, enabled: true, present: false }] } as unknown as BackendConfig]);
    expect(c[0].models).toEqual(['fast']);
  });
});

describe('the run fields', () => {
  it('every backend and model: no fields', () => {
    expect(runFields(everything())).toEqual({});
    expect(runFields({ ...everything(), patterns: 'small-*, big' })).toEqual({ models: ['small-*', 'big'] });
  });
  it('a whole backend is backend/*, picked models are backend/model', () => {
    const r = { every: false, backends: { vllm1: 'all' as const, openrouter: ['anthropic/claude'] }, patterns: '' };
    expect(runFields(r)).toEqual({ backends: ['vllm1', 'openrouter'], models: ['vllm1/*', 'openrouter/anthropic/claude'] });
    expect(runCounts(r, cat)).toEqual({ backends: 2, models: 3 });
  });
  it('an empty pick is an error', () => {
    expect(runError({ every: false, backends: {}, patterns: '' })).toMatch(/at least one backend/);
    expect(runError({ every: false, backends: { vllm1: [] }, patterns: '' })).toMatch(/at least one backend/);
    expect(runError(everything())).toBeNull();
  });
});

describe('the body', () => {
  it('carries backends and models for a pick', () => {
    const body = planBody(plan({ run: { every: false, backends: { vllm1: ['qwen-4b'] }, patterns: '' } }), ALL, false);
    expect(body).toEqual({ label: 'laptop', scopes: ['inference:openai'], days: 90, backends: ['vllm1'], models: ['vllm1/qwen-4b'] });
  });
  it('drops scopes the person may not give, and refuses none left', () => {
    expect(planBody(plan({ scopes: ['trust'] }), ALL, false)).toMatch(/at least one thing/);
  });
  it('a monitoring key runs no models: no backends, no models', () => {
    const body = planBody(plan({ purpose: 'monitoring', scopes: ['metrics:read'], run: { every: false, backends: {}, patterns: '' } }), ALL, false);
    expect(body).toEqual({ label: 'laptop', scopes: ['metrics:read'], days: 90 });
  });
  it('never expires only for an admin; limits are numbers', () => {
    expect(planBody(plan({ days: 'never' }), ALL, false)).toMatch(/Only an admin/);
    expect(planBody(plan({ days: 'never' }), ALL, true)).toMatchObject({ days: 'never' });
    expect(planBody(plan({ limits: { requests_per_minute: 'x' } }), ALL, false)).toMatch(/number/);
    expect(planBody(plan({ limits: { requests_per_minute: '60' } }), ALL, false)).toMatchObject({ limits: { requests_per_minute: 60 } });
  });
});

describe('the summary and the setup', () => {
  it('says what the key runs', () => {
    const p = plan({ run: { every: false, backends: { vllm1: 'all' }, patterns: '' } });
    expect(planSummary(p, cat, (s) => s)).toBe('This key calls the inference:openai with 2 models on VLLM1, and expires in 90 days.');
    expect(planSummary(plan(), cat, (s) => s)).toContain('every model of every backend');
  });
  it('the test command uses a model the key may run', () => {
    expect(firstModel(plan({ run: { every: false, backends: { openrouter: ['meta/llama'] }, patterns: '' } }), cat)).toBe('meta/llama');
    expect(firstModel(plan(), cat)).toBe('anthropic/claude');
  });
  it('a Messages API key gets its base URL and x-api-key', () => {
    const s = setupFor('anthropic', 'https://t.example', 'tdm-1', 'm');
    expect(s.map((x) => x.label)).toEqual(['Base URL, Messages API', 'Environment', 'Test it']);
    expect(s[2].value).toContain("x-api-key: tdm-1");
    expect(setupFor('agent', 'https://t.example', 'tdm-1', 'm')[0].value).toBe('https://t.example/v1');
  });
});
