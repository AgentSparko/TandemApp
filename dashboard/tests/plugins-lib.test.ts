// Tests for VIS-284: the plugins pages in words, each sentence by itself (lib/plugins.ts,
// lib/plugin-caps.ts), held to the contract's own tables (contracts/plugin-v1/api.json,
// capabilities.json): every capability pattern has words and a risk, every check code a place in
// the report and a plain sentence, every error type a sentence and a fix. And where the plugins
// scope shows in the settings area: its tab, its place in a plan, its keys as groups, a risky
// grant's words in the review.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import type { CheckError, Inspection, Plugin, Status } from '../src/api/plugins.ts';
import type { KeySpec, SchemaView } from '../src/api/settings.ts';
import { SCOPES } from '../src/api/settings.ts';
import { SETTINGS_TABS, settingsHref, subPages } from '../src/lib/menu.ts';
import { RISK_WORDS, TRUST_WORDS, capList, capWords, grantLine, looksLan, networkHost } from '../src/lib/plugin-caps.ts';
import {
  CHECK_GROUPS, blankOf, bytes, checkReport, compareVersions, contribLine, contributions, controls, fieldText, formOf, getAt, inspectHead, isSet, limitsOf, listState, parseField, pathKey, pluginErrorWords, pluginsHref,
  problemOf, setAt, signatureWords, sourceErrors, sourceField, sourceOf, sourceWords, stateWords, trustLine, validateField, versionMove, type SourceForm,
} from '../src/lib/plugins.ts';
import { parseHash } from '../src/lib/router.ts';
import { schemaForm, type FormField } from '../src/lib/schema-form.ts';
import { fieldsOf, groupsOf } from '../src/lib/settings-form.ts';
import { costOf, errorWords, grantWords, placeOf, pluginGrantWords } from '../src/lib/settings-words.ts';
import { CONTRACTS } from './engine-rig.ts';
import { API } from './plugins-rig.ts';

const CAPS: { capabilities: { pattern: string; confirm: string }[]; trust: Record<string, string> } = JSON.parse(readFileSync(join(CONTRACTS, 'plugin-v1', 'capabilities.json'), 'utf8'));
const status = (o: Partial<Status>): Status => ({ id: 'x', state: 'running', held: false, runtime: 'docker', where: 'tandem-plugin-x', version: '1.0.0', started_at: 1, restarts: 0, failures_last_hour: 0, next_start_at: null, last_error: null, tools: [], hooks: [], providers: [], backends: [], skills: [], panels: [], notes: [], refusals: [], trust: { requested: 'sandboxed', granted: 'sandboxed' }, gave_up: null, ...o });
const field = (schema: Record<string, unknown>, required = false): FormField => schemaForm({ type: 'object', properties: { x: schema }, required: required ? ['x'] : [] }).fields[0];

describe('a capability in plain words, with its risk', () => {
  it('every capability of the contract has words; what the contract asks twice is high risk', () => {
    const sample: Record<string, string> = { 'network:<host>[:<port>]': 'network:api.example.com:443', 'secrets:<NAME>': 'secrets:API_KEY' };
    for (const c of CAPS.capabilities) {
      const w = capWords(sample[c.pattern] ?? c.pattern);
      expect(w.kind, c.pattern).not.toBe('other');
      expect(w.title.length, c.pattern).toBeGreaterThan(8);
      expect(w.text.length, c.pattern).toBeGreaterThan(30);
      if (c.confirm === 'always') expect([w.risk, w.confirm], c.pattern).toEqual(['high', true]);
      if (c.confirm === 'never') expect([w.risk === 'high', w.confirm], c.pattern).toEqual([false, false]);
    }
    expect(Object.keys(TRUST_WORDS).sort()).toEqual(Object.keys(CAPS.trust).sort());
    expect(Object.keys(RISK_WORDS)).toEqual(['low', 'elevated', 'high']);
  });

  it('a network host: on the LAN it is some risk, outside it high and asked twice; the controller’s answer wins over the page’s reading', () => {
    expect([networkHost('network:api.example.com:443'), networkHost('network:192.168.1.30'), networkHost('network:nas.local:9000')]).toEqual(['api.example.com', '192.168.1.30', 'nas.local']);
    for (const h of ['192.168.1.30', '10.0.0.4', '172.16.9.9', '127.0.0.1', '169.254.3.3', 'nas', 'nas.local', 'box.lan', 'x.home.arpa', 'svc.internal', 'localhost']) expect(looksLan(h), h).toBe(true);
    for (const h of ['api.example.com', '8.8.8.8', '172.32.0.1', '192.169.1.1', 'example.local.com', '']) expect(looksLan(h), h).toBe(false);
    expect([capWords('network:192.168.1.30:8720').risk, capWords('network:192.168.1.30:8720').confirm]).toEqual(['elevated', false]);
    expect([capWords('network:api.example.com').risk, capWords('network:api.example.com').confirm]).toEqual(['high', true]);
    // a name the controller resolved to the LAN: not asked twice, whatever it looks like
    expect(capWords('network:gateway.example.com', []).risk).toBe('elevated');
    expect(capWords('network:nas', ['network:nas']).confirm).toBe(true);
    expect(capWords('network:api.example.com:443').subject).toBe('api.example.com:443');
    expect(capWords('secrets:API_KEY').title).toBe('Get its secret API_KEY');
  });

  it('the riskiest first; a capability this page has no words for is told as one to read up on', () => {
    expect(capList(['settings:read', 'prompts:meta', 'network:10.0.0.2', 'prompts:full', 'engine:control']).map((c) => c.capability)).toEqual(['prompts:full', 'engine:control', 'network:10.0.0.2', 'settings:read', 'prompts:meta']);
    const odd = capWords('files:write');
    expect([odd.kind, odd.risk, odd.title]).toEqual(['other', 'elevated', 'files:write']);
    expect(grantLine('trust').title).toBe('Run on the host, outside any sandbox');
    expect(grantLine('capabilities.prompts:full').title).toBe('Read every prompt and answer as it is');
    expect(trustLine({ requested: 'sandboxed', granted: 'sandboxed' }).text).toBe('sandboxed');
    expect(trustLine({ requested: 'host', granted: 'host' }).text).toBe('on the host');
    expect(trustLine({ requested: 'engine', granted: 'sandboxed' }).tone).toBe('err');
  });
});

describe('an install’s checks, each with how it ended', () => {
  it('every check code of the contract has its place in the report and a sentence of its own', () => {
    const placed = CHECK_GROUPS.flatMap((g) => g.codes).sort();
    expect(placed).toEqual(Object.keys(API.checks).sort());
    expect(new Set(placed).size).toBe(placed.length);
    for (const code of Object.keys(API.checks)) {
      const p = problemOf({ code, field: 'contributes.tools[0]', message: 'the controller’s sentence', tool: 'greet', annotation: 'readOnlyHint', stage: 'request.none', capability: 'prompts:full', name: 'dup', needed: '>=0.99', have: '0.16.0' });
      expect(p.title, code).not.toBe('The controller’s sentence.');
      expect(p.fix.length, code).toBeGreaterThan(10);
      expect(p.message).toBe('the controller’s sentence');
    }
    // a code from a newer controller keeps its own sentence
    expect(problemOf({ code: 'later_check', field: null, message: 'something new' }).title).toBe('Something new.');
  });

  it('a refusal of the files ends the checks; a manifest that does not fit leaves what it says unchecked; the rest is told as passed', () => {
    const rep = (errors: CheckError[], manifest: object | null = {}): string[] => checkReport({ errors, manifest: manifest as Inspection['manifest'], signature: null, sha256: 'ab' }).rows.map((r) => `${r.group.id}:${r.state}`);
    expect(rep([])).toEqual(['files:pass', 'signature:pass', 'manifest:pass', 'tools:pass', 'contributions:pass', 'paths:pass', 'asks:pass', 'settings:pass', 'compat:pass']);
    expect(rep([{ code: 'symlink', field: null, message: 'a symlink' }], null)).toEqual(['files:refused', 'signature:skipped', 'manifest:skipped', 'tools:skipped', 'contributions:skipped', 'paths:skipped', 'asks:skipped', 'settings:skipped', 'compat:skipped']);
    expect(rep([{ code: 'missing_field', field: 'version', message: 'version is required' }])).toEqual(['files:pass', 'signature:pass', 'manifest:refused', 'tools:skipped', 'contributions:skipped', 'paths:skipped', 'asks:skipped', 'settings:skipped', 'compat:skipped']);
    expect(rep([{ code: 'signature_invalid', field: 'plugin.sig.json', message: 'bad' }, { code: 'stage_unknown', field: 'contributes.hooks[0].stages[0]', message: 'no stage', stage: 'request.none' }])).toEqual(['files:pass', 'signature:refused', 'manifest:pass', 'tools:pass', 'contributions:refused', 'paths:pass', 'asks:pass', 'settings:pass', 'compat:pass']);
    const r = checkReport({ errors: [{ code: 'later_check', field: null, message: 'something new' }], manifest: {}, signature: null, sha256: 'ab' });
    expect([r.passed, r.refused, r.other.map((o) => o.code)]).toEqual([9, 0, ['later_check']]);
    expect(checkReport({ errors: [], manifest: {}, signature: null, sha256: 'ab' }).rows[1].note).toBe('It carries no signature, so there is none to check.');
  });

  it('the head of a report, how a version stands to the one installed, and a signature’s three readings', () => {
    const base: Inspection = { contract_version: '1.0', ok: true, source: { type: 'folder', path: '/p' }, manifest: { name: 'p', title: 'P', version: '1.1.0' }, errors: [], warnings: [], sha256: 'aa', signature: null, files: 1, bytes: 1, manifest_dir: '', capabilities: { requested: [], confirm: [] }, compat: { tandemapp: '0.16.0', hooks_api: '1.0', plugin_api: '1.0' }, installed: null, settings_form: null };
    expect([versionMove(base), inspectHead(base).title]).toEqual(['new', 'P 1.1.0 passed every check']);
    expect(versionMove({ ...base, installed: { version: '1.0.0', sha256: 'bb' } })).toBe('upgrade');
    expect(versionMove({ ...base, installed: { version: '1.2.0', sha256: 'bb' } })).toBe('downgrade');
    expect(versionMove({ ...base, installed: { version: '1.1.0', sha256: 'aa' } })).toBe('same');
    expect(inspectHead({ ...base, installed: { version: '1.1.0', sha256: 'aa' } }).title).toBe('P 1.1.0 is installed already');
    expect(inspectHead({ ...base, ok: false, errors: [{ code: 'bad_value', field: 'x', message: 'm' }] }).text).toContain('One check refused it. Nothing was installed and nothing in it ran.');
    expect([compareVersions('1.0.0', '1.0.0'), Math.sign(compareVersions('1.10.0', '1.9.9')), Math.sign(compareVersions('1.0.0-rc1', '1.0.0')), Math.sign(compareVersions('2.0.0', '10.0.0')), Math.sign(compareVersions('1.0.0-b', '1.0.0-a'))]).toEqual([0, 1, -1, -1, 1]);
    expect(signatureWords(null).label).toBe('not signed');
    expect(signatureWords({ present: true, verified: true, algorithm: 'ed25519', key_id: 'ab12', trusted: null, error: null }).label).toBe('signed');
    expect(signatureWords({ present: true, verified: true, algorithm: 'ed25519', key_id: 'ab12', trusted: null, error: null }).text).toContain('does not decide which keys to trust');
    expect([signatureWords({ present: true, verified: false, algorithm: 'ed25519', key_id: 'ab12', trusted: null, error: 'the signature does not match' }).tone, signatureWords({ present: true, verified: true, algorithm: 'ed25519', key_id: 'k', trusted: true, error: null }).label]).toEqual(['err', 'signed, key trusted']);
  });
});

describe('a plugin’s state, where it came from, what it adds', () => {
  it('every state the plugin host tells has a word, a tone and a sentence', () => {
    const states: Status['state'][] = ['starting', 'running', 'stopping', 'backoff', 'stopped', 'unavailable', 'refused', 'failed', 'disabled'];
    expect(states.map((s) => stateWords(status({ state: s })).label)).toEqual(['starting', 'running', 'stopping', 'restarting', 'stopped', 'unavailable', 'refused', 'failed', 'off']);
    expect(states.map((s) => stateWords(status({ state: s })).tone)).toEqual(['live', 'ok', 'live', 'warn', 'muted', 'warn', 'err', 'err', 'muted']);
    expect(stateWords(status({ state: 'backoff', next_start_at: 9000 }), 1500).text).toContain('again in 8 s');
    expect(stateWords(status({ state: 'stopped', held: true })).text).toBe('Stopped by hand. It stays stopped until you start it.');
    expect(stateWords(status({ state: 'running', runtime: 'host', where: 'pid 7' })).text).toContain('a process on the host (pid 7)');
    expect(stateWords(status({ state: 'failed', gave_up: { failures: 6 } })).text).toContain('It failed 6 times within an hour');
    const p: Pick<Plugin, 'kind' | 'enabled' | 'integrity'> = { kind: 'plugin', enabled: true, integrity: 'ok' };
    expect([listState(p, null).label, listState({ ...p, enabled: false }, undefined).label, listState({ ...p, kind: 'engine-stats' }, null).label]).toEqual(['on', 'off', 'recorded']);
    // start, stop and restart: each offered only when it can go
    expect(controls(p, status({ state: 'running' }))).toEqual({ start: 'It runs already.', stop: null, restart: null });
    expect(controls(p, status({ state: 'stopped', held: true }))).toEqual({ start: null, stop: 'It does not run.', restart: 'It does not run.' });
    expect(controls({ ...p, enabled: false }, status({ state: 'disabled' })).start).toBe('Turn it on first.');
    expect(controls(p, status({ state: 'backoff' }))).toEqual({ start: 'It runs already.', stop: null, restart: 'It does not run.' });
  });

  it('a source in words, sizes, and what a manifest contributes with where TandemApp shows each', () => {
    expect(sourceWords({ type: 'git', url: 'https://e.com/r.git', commit: 'c'.repeat(40), subdir: 'a/b' })).toEqual({ kind: 'Git commit', where: 'https://e.com/r.git (a/b)', pin: 'c'.repeat(40) });
    expect(sourceWords({ type: 'wheel', path: '/d/x.whl', archive_sha256: 'ff' })).toEqual({ kind: 'Wheel', where: '/d/x.whl', pin: 'ff' });
    expect([bytes(1), bytes(900), bytes(2048), bytes(48_212), bytes(5 * 1048576)]).toEqual(['1 byte', '900 bytes', '2.0 KB', '47 KB', '5.0 MB']);
    expect([contribLine({ tools: 2, hooks: 1, panels: 0 }), contribLine({})]).toEqual(['2 tools, 1 hook', 'adds nothing']);
    const m = {
      contributes: {
        tools: [{ name: 'read', description: 'reads', annotations: { readOnlyHint: true } }, { name: 'write', description: 'writes', annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true } }],
        hooks: [{ name: 'h', stages: ['request.finished'], role: 'observer', payload: 'full' }],
        decision_providers: [{ name: 'd', questions: ['classify'] }],
        backends: [{ name: 'b', models: ['m-1'] }],
        panels: [{ name: 'p', title: 'Panel', path: 'p/index.html' }],
        skills: ['skills'],
      },
    };
    // a preview: nothing is registered and nothing links
    const preview = contributions(m);
    expect(preview.map((c) => `${c.kind}:${c.name}:${c.live}:${c.href}`)).toEqual(['tools:read:null:null', 'tools:write:null:null', 'hooks:h:null:null', 'decision_providers:d:null:null', 'backends:b:null:null', 'panels:p:null:null', 'skills:skills:null:null']);
    expect(preview[0].marks.map((x) => x.text)).toEqual(['only reads']);
    expect(preview[1].marks.map((x) => x.text)).toEqual(['changes things', 'reaches outside']);
    expect(preview[2].marks[0]).toEqual({ text: 'full content', tone: 'err' });
    // installed and running: what is registered, and a link each
    const live = contributions(m, 'x', status({ tools: ['read'], hooks: ['x/h'], providers: [], backends: ['b'], panels: ['p'], skills: ['skills'] }));
    expect(live.map((c) => `${c.name}:${c.live}`)).toEqual(['read:true', 'write:false', 'h:true', 'd:false', 'b:true', 'p:true', 'skills:true']);
    expect(live.map((c) => c.href)).toEqual(['#/chat/settings/mcp', '#/chat/settings/mcp', '#/app/hooks', '#/app/hooks/providers', '#/api/backends', '#/app/plugins/p/x/panel/p', null]);
    expect(contributions(m, 'x', status({ state: 'stopped', tools: ['read'] })).every((c) => c.live === false)).toBe(true);
  });
});

describe('the install form, and every refusal of the API', () => {
  it('a source form: what is wrong before it is sent, what is sent, and the form an upgrade starts from', () => {
    const f = (o: Partial<SourceForm>): SourceForm => ({ type: 'folder', path: '', url: '', commit: '', subdir: '', ...o });
    expect(sourceErrors(f({}))).toEqual({ path: 'The folder’s path on the controller’s machine.' });
    expect(sourceErrors(f({ path: '/srv/plugins/x' }))).toEqual({});
    expect(sourceErrors(f({ type: 'git' }))).toEqual({ url: 'The repository’s URL.', commit: 'The full commit id: 40 hex digits.' });
    expect(sourceErrors(f({ type: 'git', url: 'git@example.com:you/p.git', commit: 'A'.repeat(40), subdir: '../x' }))).toEqual({ subdir: 'A folder inside the repository, like plugins/hello.' });
    expect(sourceErrors(f({ type: 'git', url: 'git://example.com/p.git', commit: 'v1.2.0' })).commit).toContain('A branch or a tag can move');
    expect(sourceOf(f({ type: 'git', url: ' https://e.com/r.git ', commit: 'ABCDEF'.padEnd(40, '0'), subdir: './plugins/x/' }))).toEqual({ type: 'git', url: 'https://e.com/r.git', commit: 'abcdef'.padEnd(40, '0'), subdir: 'plugins/x' });
    expect(sourceOf(f({ type: 'wheel', path: ' /d/x.whl ' }))).toEqual({ type: 'wheel', path: '/d/x.whl' });
    // an upgrade keeps where it came from and asks for the new commit
    expect(formOf({ type: 'git', url: 'https://e.com/r.git', commit: 'c'.repeat(40), subdir: 'a' })).toEqual({ type: 'git', path: '', url: 'https://e.com/r.git', commit: '', subdir: 'a' });
    expect(formOf({ type: 'wheel', path: '/d/x.whl', archive_sha256: 'ff' }).path).toBe('/d/x.whl');
    expect([sourceField('source.path'), sourceField('source.commit'), sourceField('version'), sourceField(undefined)]).toEqual(['path', 'commit', null, null]);
  });

  it('every error type of the contract is a sentence of the page’s own, with what to do', () => {
    for (const type of Object.keys(API.errors)) {
      const w = pluginErrorWords({ type, message: 'the controller’s own words', facts: { hooks: [{ id: 'h', name: 'H', enabled: true }], errors: [{ code: 'bad_value', field: 'x', message: 'm' }] } });
      if (type === 'bad_request') expect(w.title).toBe('the controller’s own words');
      else expect(w.title, type).not.toBe('the controller’s own words');
      expect(w.title.length, type).toBeGreaterThan(12);
    }
    expect(pluginErrorWords({ type: 'referenced', message: 'm', facts: { hooks: [{ id: 'a', name: 'A', enabled: true }, { id: 'b', name: 'B', enabled: false }] } }).title).toBe('2 hooks still name this plugin, so it stays.');
    expect(pluginErrorWords({ type: 'exists', message: 'm', facts: { installed: '1.0.0', version: '1.1.0' } })).toEqual({ title: 'A plugin of this name is installed (1.0.0).', fix: 'Install it as an upgrade to replace it with 1.1.0.', detail: null });
    expect(pluginErrorWords({ type: 'not_found', message: 'no plugin x', facts: {} }).title).toBe('That plugin is no longer installed.');
    expect(pluginErrorWords({ type: 'incompatible_tandemapp', message: 'm', facts: { needed: '>=0.20', have: '0.16.0' } }).title).toBe('It is written for another TandemApp: it needs >=0.20, this is 0.16.0.');
    expect(pluginErrorWords({ type: 'plugin_refused', message: 'first reason', facts: {} })).toEqual({ title: 'The checks refused this plugin. Nothing was installed.', fix: 'Check the source again to see the reasons.', detail: 'first reason' });
    // a type from a newer controller keeps its sentence
    expect(pluginErrorWords({ type: 'later', message: 'new words', facts: {} }).title).toBe('new words');
    expect(pluginsHref(['p', 'a b', 'panel', 'x'], { load: '2' })).toBe('#/app/plugins/p/a%20b/panel/x?load=2');
  });
});

describe('a settings form’s values against its fields', () => {
  it('a value held against its field: one sentence a place', () => {
    const v = (schema: Record<string, unknown>, value: unknown, required = false): string[] => Object.values(validateField(field(schema, required), value, ['x']));
    expect(v({ type: 'integer', minimum: 1, maximum: 9 }, 0)).toEqual(['At least 1.']);
    expect(v({ type: 'integer', minimum: 1, maximum: 9 }, 10)).toEqual(['At most 9.']);
    expect(v({ type: 'integer' }, 1.5)).toEqual(['A whole number, like 16.']);
    expect(v({ type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 }, 0)).toEqual(['More than 0.']);
    expect(v({ type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 }, 1)).toEqual(['Less than 1.']);
    expect(v({ type: 'number', multipleOf: 0.5 }, 0.75)).toEqual(['A multiple of 0.5.']);
    expect(v({ type: 'number', multipleOf: 0.5 }, 1.5)).toEqual([]);
    expect(v({ type: 'string', minLength: 1 }, '')).toEqual(['This cannot be empty.']);
    expect(v({ type: 'string', minLength: 3 }, 'ab')).toEqual(['At least 3 characters.']);
    expect(v({ type: 'string', maxLength: 3 }, 'abcd')).toEqual(['At most 3 characters; this is 4.']);
    expect(v({ type: 'string', pattern: '^[a-z]+$' }, 'A1')).toEqual(['It must match ^[a-z]+$']);
    expect(v({ enum: ['a', 'b'] }, 'c')).toEqual(['One of a, b.']);
    expect(v({ type: 'boolean' }, 'yes')).toEqual(['On or off.']);
    expect(v({ type: 'array', items: { type: 'string' }, minItems: 1 }, [])).toEqual(['At least 1 item.']);
    expect(v({ type: 'array', items: { type: 'string' }, maxItems: 1 }, ['a', 'b'])).toEqual(['At most 1 item; this has 2.']);
    expect(v({ type: 'array', items: { type: 'integer', minimum: 0 }, uniqueItems: true }, [1, 1, -1])).toEqual(['Each item once.', 'At least 0.']);
    // not set: fine for an optional one, a null for a nullable one, a sentence for a required one
    expect(v({ type: 'string' }, undefined)).toEqual([]);
    expect(v({ type: ['string', 'null'] }, null, true)).toEqual([]);
    expect(v({ type: 'string' }, undefined, true)).toEqual(['This is needed.']);
    // a group: each field at its own place
    const g = field({ type: 'object', additionalProperties: false, required: ['a'], properties: { a: { type: 'integer', minimum: 1 }, b: { type: 'object', additionalProperties: false, properties: { c: { type: 'string', maxLength: 1 } } } } });
    expect(validateField(g, { a: 0, b: { c: 'xx' } }, ['x'])).toEqual({ [pathKey(['x', 'a'])]: 'At least 1.', [pathKey(['x', 'b', 'c'])]: 'At most 1 character; this is 2.' });
    expect(Object.values(validateField({ ...g, required: true }, undefined, ['x']))).toEqual(['This is needed.']);
  });

  it('what a field’s text means, what a value shows as, and a value at a place', () => {
    const int = field({ type: 'integer' }, true);
    expect([parseField(int, ' 12 '), parseField(int, '1.5'), parseField(int, '')]).toEqual([{ ok: true, value: 12 }, { ok: false, error: 'A whole number, like 16.' }, { ok: false, error: 'A number is needed here.' }]);
    expect(parseField(field({ type: ['number', 'null'] }), '')).toEqual({ ok: true, value: null });
    expect(parseField(field({ type: 'number' }), '')).toEqual({ ok: true, value: undefined });
    expect(parseField(field({ type: 'number' }), '2.50')).toEqual({ ok: true, value: 2.5 });
    expect([parseField(field({ type: 'string' }), ''), parseField(field({ type: ['string', 'null'] }), ''), parseField(field({ type: 'string' }), ' a ')]).toEqual([{ ok: true, value: '' }, { ok: true, value: null }, { ok: true, value: ' a ' }]);
    const json = field({ type: 'object', additionalProperties: false });
    expect(json.control).toBe('json');
    expect(parseField(json, '{"a": 1}')).toEqual({ ok: true, value: { a: 1 } });
    expect((parseField(json, '{a') as { error: string }).error).toContain('Not valid JSON yet');
    expect([fieldText(int, 7), fieldText(int, null), fieldText(json, { a: 1 })]).toEqual(['7', '', '{\n  "a": 1\n}']);
    // a value at a place: nothing is changed in place, a list's item is taken out
    const root = { a: { b: [1, 2, 3] } };
    expect(setAt(root, ['a', 'b', 1], 9)).toEqual({ a: { b: [1, 9, 3] } });
    expect(setAt(root, ['a', 'b', 1], undefined)).toEqual({ a: { b: [1, 3] } });
    expect(setAt(root, ['a', 'c'], 'x')).toEqual({ a: { b: [1, 2, 3], c: 'x' } });
    expect(setAt(undefined, ['a', 0, 'b'], true)).toEqual({ a: [{ b: true }] });
    expect(setAt({ a: 1, b: 2 }, ['a'], undefined)).toEqual({ b: 2 });
    expect(root).toEqual({ a: { b: [1, 2, 3] } });
    expect([getAt(root, ['a', 'b', 2]), getAt(root, ['a', 'x', 'y']), getAt(root, [])]).toEqual([3, undefined, root]);
    // limits in a line, a set of checkboxes, the value a new item starts with
    expect([limitsOf(field({ type: 'integer', minimum: 1, maximum: 9 })), limitsOf(field({ type: 'integer', minimum: 1 })), limitsOf(field({ type: 'string', maxLength: 40 })), limitsOf(field({ type: 'array', items: { type: 'string' }, maxItems: 1 })), limitsOf(field({ type: 'boolean' }))]).toEqual(['1 to 9', 'at least 1', 'at most 40 characters', 'at most 1 item', null]);
    expect([isSet(field({ type: 'array', items: { enum: ['a', 'b', 'c', 'd', 'e'] } })), isSet(field({ type: 'array', items: { type: 'string' } }))]).toEqual([true, false]);
    expect([blankOf(field({ type: 'string' })), blankOf(field({ type: 'integer', minimum: 5 })), blankOf(field({ type: 'boolean' })), blankOf(field({ enum: ['a', 'b'] })), blankOf(field({ type: 'string', default: 'x' }))]).toEqual(['', 5, false, 'a', 'x']);
  });
});

describe('where the plugins show in the settings area', () => {
  it('a page of its own under Settings, a scope of its own that applies last', () => {
    expect(SETTINGS_TABS.map((t) => t.id)).toEqual(['app', 'backends', 'conductor', 'engines', 'hooks', 'plugins', 'history']);
    expect(SETTINGS_TABS.find((t) => t.id === 'plugins')!.scope).toBe('plugins');
    // SRV-875: `auth` and `api` stand after `app` on a controller with accounts (settings-v1 1.2)
    expect(SCOPES).toEqual(['app', 'auth', 'api', 'backends', 'conductor', 'engine', 'hooks', 'plugins']);
    expect(settingsHref('plugins')).toBe('#/app/plugins');
    const r = parseHash('#/app/plugins/p/hello-tool/panel/board');
    expect([r.view, r.path]).toEqual(['app', ['plugins', 'p', 'hello-tool', 'panel', 'board']]);
    expect(subPages(r).find((s) => s.on)!.label).toBe('Plugins');
    expect(parseHash('#/app/plugins/install?upgrade=hello-tool').params.get('upgrade')).toBe('hello-tool');
  });

  it('a plan’s part is named after its plugin; a risky grant has its words in the review, apart from a hook’s', () => {
    const o = { contract_version: '1.1', order: SCOPES, operations: [], scopes: [{ scope: 'plugins' as const, order: 5, why: null, targets: [{ id: 'usage-board', label: 'Usage board', writable: true, why: null }] }] };
    expect([placeOf({ scope: 'plugins', target: 'usage-board' }, o), placeOf({ scope: 'plugins', target: 'gone' }, o)]).toEqual(['Plugin: Usage board', 'Plugin: gone']);
    const part = { scope: 'plugins' as const, target: 'usage-board', kind: 'set' as const, version: null, changed: true, changes: [], restart: 'none' as const, restart_needed: false, fingerprint_keys: [], grant_keys: [], warnings: [] };
    expect(costOf(part, 'Usage board').text).toBe('No restart of TandemApp: the plugin host starts the plugin again with what changed.');
    const plan = { grant_keys: ['hooks/controller:hooks.export.payload', 'plugins/host-metrics:trust', 'plugins/usage-board:capabilities.network:api.example.com:443', 'plugins/usage-board:capabilities.prompts:full'], parts: [] };
    expect(pluginGrantWords(plan, o).map((p) => `${p.plugin}: ${p.grants.map((g) => g.key).join(' + ')}`)).toEqual(['host-metrics: trust', 'Usage board: capabilities.network:api.example.com:443 + capabilities.prompts:full']);
    expect(pluginGrantWords(plan, o)[1].grants[0].title).toBe('Connect to api.example.com:443');
    // the hooks' own second question keeps to the hooks' keys
    expect(grantWords(plan).hooks).toEqual(['export']);
    expect(pluginGrantWords({ grant_keys: ['hooks/controller:hooks.export.payload'] })).toEqual([]);
    expect(errorWords({ type: 'trust_required', message: 'm', facts: { scope: 'plugins', target: 'x', key: 'enabled' } }).title).toBe('Turning a plugin on, and granting it anything, needs pairing’s rights.');
    expect(errorWords({ type: 'trust_required', message: 'm', facts: { scope: 'hooks' } }).title).toBe('Granting a hook the full payload needs pairing’s rights.');
    expect(errorWords({ type: 'no_target', message: 'm', facts: { scope: 'plugins' } }).title).toBe('That plugin is no longer installed.');
  });

  it('a plugin’s keys as the plain form of the settings interface: how it runs, its settings, its secrets, its capabilities', () => {
    const key = (k: string, type: KeySpec['type'], o: Partial<KeySpec> = {}): KeySpec => ({ key: k, type, restart: 'none', fingerprint: false, secret: false, write_only: false, writable: true, nullable: false, default: null, ...o });
    const schema: SchemaView = {
      contract_version: '1.1', scope: 'plugins', target: 'classify-provider', writable: true, why: null,
      keys: [key('capabilities.prompts:redacted', 'boolean', { default: false }), key('enabled', 'boolean', { default: false }), key('secrets.API_KEY', 'secret', { secret: true, write_only: true, nullable: true }), key('settings.fallback', 'enum', { values: ['code', 'chat', 'unclear'], default: 'unclear' }), key('settings.keep_days', 'integer', { nullable: true, default: 30 }), key('trust', 'enum', { values: ['sandboxed', 'host'], default: 'sandboxed' })],
    };
    const fields = fieldsOf(schema, { values: [{ key: 'settings.keep_days', value: 14, set: true, source: 'plugin' }] }, undefined);
    const groups = groupsOf('plugins', fields);
    expect(groups.map((g) => `${g.title}: ${g.fields.map((f) => f.label).join(', ')}`)).toEqual(['How it runs: On, Where it runs', 'Settings: Fallback, Keep days', 'Secrets: API_KEY', 'Capabilities: prompts:redacted']);
    expect(fields.map((f) => `${f.key} ${f.control}`)).toEqual(['capabilities.prompts:redacted switch', 'enabled switch', 'secrets.API_KEY secret', 'settings.fallback choice', 'settings.keep_days number', 'trust choice']);
    expect(fields.find((f) => f.key === 'settings.keep_days')!.reset).toBe('Use the default');
  });
});
