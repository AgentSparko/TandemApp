// Tests for VIS-280: the settings pages' own logic, without a browser: the draft (what is staged,
// one part a scope and target), the form a schema makes (the control a type asks for, the lock, the
// cost chips, the reset), what a typed text means, a version against now, and the words: a plan's
// head and cost, when an engine restarts, the countdown, each restart state, every refusal of the
// contract, the audit row. The menu and the route of the area are here too.

import { describe, expect, it } from 'vitest';
import { RESTART_ENDED, SettingsError, type KeySpec, type KeyValue, type Plan, type Restart, type SchemaView } from '../src/api/settings.ts';
import { SETTINGS_TABS, crumb, sectionOf, settingsHref, subPages } from '../src/lib/menu.ts';
import { parseHash } from '../src/lib/router.ts';
import { Draft, refusedField, same } from '../src/lib/settings-draft.ts';
import { EXT_KEY, MODE_KEY, asText, controlOf, fieldsOf, groupsOf, humanize, labelOf, parseInput, sentence, shown, textOf, versionDiff } from '../src/lib/settings-form.ts';
import { DEADLINES, WHEN_DEFAULT, auditWords, changeWords, clock, costOf, dur, errorWords, fingerprintWords, leftNow, partStateWords, placeOf, planSummary, restartModel, resultHead, whenError, whenParam, whenWords } from '../src/lib/settings-words.ts';
import { ERRORS } from './settings-rig.ts';

const key = (k: string, type: KeySpec['type'], o: Partial<KeySpec> = {}): KeySpec => ({ key: k, type, restart: 'none', fingerprint: false, secret: false, write_only: false, writable: true, nullable: false, default: null, ...o });
const val = (k: string, value: unknown, o: Partial<KeyValue> = {}): KeyValue => ({ key: k, value, set: true, source: 'database', ...o });
const schemaOf = (scope: SchemaView['scope'], keys: KeySpec[], o: Partial<SchemaView> = {}): SchemaView => ({ contract_version: '1.0', scope, target: scope === 'engine' ? 'eh-desk' : 'controller', writable: true, why: null, keys, ...o });
const time = () => '22:00';

describe('the draft', () => {
  it('stages a key, forgets one set back to what holds, and gives the parts in the order they apply', () => {
    const d = new Draft();
    let heard = 0;
    const off = d.on(() => heard++);
    d.set('engine', 'eh-desk', 'MAX_QUEUE', '32', { value: '16', set: true });
    d.set('app', 'controller', 'conductor', 'on', { value: 'off', set: false });
    d.unset('backends', 'controller', 'backends.cloud.max_inflight', { set: true });
    expect([d.count(), d.count('engine'), d.count('engine', 'eh-desk'), d.count('hooks'), d.empty]).toEqual([3, 1, 1, 0, false]);
    expect(d.changes()).toEqual([
      { scope: 'app', target: 'controller', set: { conductor: 'on' } },
      { scope: 'backends', target: 'controller', unset: ['backends.cloud.max_inflight'] },
      { scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '32' } },
    ]);
    expect(d.staged('engine', 'eh-desk', 'MAX_QUEUE')).toEqual({ kind: 'set', value: '32' });
    expect(d.staged('backends', 'controller', 'backends.cloud.max_inflight')).toEqual({ kind: 'unset' });
    // back to what holds: no change is left, and the part goes with its last key
    d.set('engine', 'eh-desk', 'MAX_QUEUE', '16', { value: '16', set: true });
    expect([d.count('engine'), d.part('engine', 'eh-desk')]).toEqual([0, undefined]);
    // nothing to unset is no change
    d.unset('engine', 'eh-desk', 'QSE_ALLOW_FROM', { set: false });
    expect(d.count('engine')).toBe(0);
    expect(heard).toBeGreaterThan(4);
    off();
    d.clear();
    expect([d.empty, d.changes()]).toEqual([true, []]);
  });

  it('a secret is always a change; a restore stands for its part alone and an edit takes its place', () => {
    const d = new Draft();
    d.set('backends', 'controller', 'backends.cloud.key', 'sk-1', { value: null, set: true }, true);
    expect(d.count()).toBe(1);
    d.set('engine', 'eh-desk', 'MAX_QUEUE', '32');
    d.restore('engine', 'eh-desk', '1');
    expect(d.changes()[1]).toEqual({ scope: 'engine', target: 'eh-desk', restore: '1' });
    expect([d.count('engine'), d.staged('engine', 'eh-desk', 'MAX_QUEUE')]).toEqual([1, null]);
    d.set('engine', 'eh-desk', 'MAX_QUEUE', '8');
    expect(d.changes()[1]).toEqual({ scope: 'engine', target: 'eh-desk', set: { MAX_QUEUE: '8' } });
    d.drop('engine', 'eh-desk', 'MAX_QUEUE');
    d.dropPart('backends', 'controller');
    expect(d.empty).toBe(true);
    expect([same({ a: 1, b: [1, 2] }, { b: [1, 2], a: 1 }), same([1, 2], [2, 1]), same(null, undefined)]).toEqual([true, false, true]);
  });
});

describe('a refusal’s field', () => {
  it('the part the refusal names, not another part with the same key', () => {
    const d = new Draft();
    d.set('engine', 'eh-a', 'MAX_QUEUE', '32');
    d.set('engine', 'eh-b', 'MAX_QUEUE', 'many');
    d.set('backends', 'controller', 'backends.cloud.label', 'Cloud');
    expect(refusedField(d.parts, { scope: 'engine', target: 'eh-b', key: 'MAX_QUEUE' })).toEqual({ scope: 'engine', target: 'eh-b', key: 'MAX_QUEUE' });
    expect(refusedField(d.parts, { scope: 'engine', target: 'eh-a', key: 'MAX_QUEUE' })).toEqual({ scope: 'engine', target: 'eh-a', key: 'MAX_QUEUE' });
    // no key, or a key the named part does not hold: no field is marked
    expect(refusedField(d.parts, { scope: 'engine', target: 'eh-b' })).toBeNull();
    expect(refusedField(d.parts, { scope: 'backends', target: 'controller', key: 'MAX_QUEUE' })).toBeNull();
    // a refusal that names no part (an older controller): only a key one part holds
    expect(refusedField(d.parts, { key: 'MAX_QUEUE' })).toBeNull();
    expect(refusedField(d.parts, { key: 'backends.cloud.label' })).toEqual({ scope: 'backends', target: 'controller', key: 'backends.cloud.label' });
    d.unset('engine', 'eh-b', 'QSE_ALLOW_FROM', { set: true });
    expect(refusedField(d.parts, { scope: 'engine', target: 'eh-b', key: 'QSE_ALLOW_FROM' })).toEqual({ scope: 'engine', target: 'eh-b', key: 'QSE_ALLOW_FROM' });
  });
});

describe('a form from a schema', () => {
  it('each type gets the control it asks for', () => {
    expect(controlOf(key('chat', 'enum', { values: ['on', 'off'] }))).toBe('switch');
    expect(controlOf(key('users.1.enabled', 'boolean'))).toBe('switch');
    expect(controlOf(key('QSE_MODE', 'enum', { values: ['engine', 'chat'] }))).toBe('choice');
    expect(controlOf(key('host.mode', 'enum', { values: ['auto', 'same', 'collector', 'exporter', 'off'] }))).toBe('select');
    expect([controlOf(key('MAX_QUEUE', 'integer')), controlOf(key('t', 'number'))]).toEqual(['number', 'number']);
    expect([controlOf(key('u', 'string')), controlOf(key('p', 'path')), controlOf(key('m', 'module')), controlOf(key('c', 'cidrs'))]).toEqual(['text', 'text', 'text', 'text']);
    expect(controlOf(key('models', 'list', { default: [] }))).toBe('lines');
    expect(controlOf(key('routes', 'list', { default: [] }))).toBe('routes');
    expect(controlOf(key('x', 'list'), { value: [{ a: 1 }] })).toBe('json');
    expect([controlOf(key('router', 'document')), controlOf(key('o', 'object'))]).toEqual(['json', 'json']);
    expect(controlOf(key('backends.cloud.key', 'secret', { secret: true, write_only: true }))).toBe('secret');
  });

  it('a field: what holds or what is staged, the lock with its variable, the cost, the reset', () => {
    const s = schemaOf('engine', [
      key('MAX_QUEUE', 'integer', { restart: 'engine', default: '16' }),
      key('QSE_ALLOW_FROM', 'cidrs', { restart: 'hot', default: '' }),
      key('QWEN38_PREFIX_CHUNK', 'integer', { restart: 'engine', fingerprint: true, default: '2048' }),
      key(MODE_KEY, 'enum', { restart: 'engine', values: ['engine', 'chat'], default: 'engine' }),
      key('HF_TOKEN', 'secret', { secret: true, write_only: true, writable: false, why: 'may hold a secret' }),
      key('PORT', 'integer', { restart: 'engine', writable: false, why: 'the install’s', default: '8000' }),
    ]);
    const values = { values: [val('MAX_QUEUE', '16', { source: 'managed' }), val('QSE_ALLOW_FROM', '', { set: false, source: 'default' }), val('QWEN38_PREFIX_CHUNK', '2048', { set: false, source: 'default' }), val(MODE_KEY, 'engine', { set: false, source: 'default' }), val('HF_TOKEN', null, { source: 'local' }), val('PORT', '8000', { source: 'install' })] };
    const d = new Draft();
    d.set('engine', 'eh-desk', 'MAX_QUEUE', '32');
    const f = fieldsOf(s, values, d.part('engine', 'eh-desk'));
    const by = (k: string) => f.find((x) => x.key === k)!;
    expect([by('MAX_QUEUE').value, by('MAX_QUEUE').staged, by('MAX_QUEUE').mono, by('MAX_QUEUE').label]).toEqual(['32', 'set', true, 'MAX_QUEUE']);
    expect(by('MAX_QUEUE').chips.map((c) => [c.tone, c.text])).toEqual([['live', 'restarts the engine']]);
    expect(by('MAX_QUEUE').hints).toEqual(['Default 16', 'Set from TandemApp']);
    expect(by('MAX_QUEUE').reset).toBe('Use the engine’s own value');
    expect(by('QSE_ALLOW_FROM').chips.map((c) => c.text)).toEqual(['no restart']);
    expect([by('QSE_ALLOW_FROM').reset, by('QSE_ALLOW_FROM').hints]).toEqual([null, ['The engine’s default']]);
    expect(by('QWEN38_PREFIX_CHUNK').chips.map((c) => [c.tone, c.text])).toEqual([['live', 'restarts the engine'], ['warn', 'saved caches start cold']]);
    expect([by(MODE_KEY).label, by(MODE_KEY).control]).toEqual(['Mode', 'choice']);
    expect(by(MODE_KEY).text).toContain('the chat extension and its data stay on the engine host’s disk');
    expect([by('HF_TOKEN').locked, by('HF_TOKEN').why, by('HF_TOKEN').control]).toEqual([true, 'It may hold a secret: it is set on the engine host, never from here.', 'secret']);
    expect([by('PORT').locked, by('PORT').why]).toEqual([true, 'Not changed from here: the install’s.']);
    // by what a change costs: the mode, what applies at once, what restarts, what is the host's
    expect(groupsOf('engine', f).map((g) => [g.id, g.fields.map((x) => x.key)])).toEqual([
      ['mode', [MODE_KEY]],
      ['hot', ['QSE_ALLOW_FROM']],
      ['restart', ['MAX_QUEUE', 'QWEN38_PREFIX_CHUNK']],
      ['host', ['HF_TOKEN', 'PORT']],
    ]);
    // the extension chat mode runs stands beside the mode while chat is chosen, and only then
    const ext = schemaOf('engine', [key(MODE_KEY, 'enum', { restart: 'engine', values: ['engine', 'chat'], default: 'engine' }), key(EXT_KEY, 'module', { restart: 'engine', nullable: true, default: 'tandemapp.engine_ext' }), key('MAX_QUEUE', 'integer', { restart: 'engine', default: '16' })]);
    const extVals = (mode: string) => ({ values: [val(MODE_KEY, mode, { source: 'managed' }), val(EXT_KEY, mode === 'chat' ? 'tandemapp.engine_ext' : null, { set: mode === 'chat', source: 'managed' }), val('MAX_QUEUE', '16', { source: 'managed' })] });
    expect(groupsOf('engine', fieldsOf(ext, extVals('engine'), undefined)).map((x) => [x.id, x.fields.map((y) => y.key)])).toEqual([['mode', [MODE_KEY]], ['restart', ['MAX_QUEUE']]]);
    const chat = fieldsOf(ext, extVals('chat'), undefined);
    expect(groupsOf('engine', chat).map((x) => [x.id, x.fields.map((y) => y.key)])).toEqual([['mode', [MODE_KEY, EXT_KEY]], ['restart', ['MAX_QUEUE']]]);
    const e = chat.find((x) => x.key === EXT_KEY)!;
    expect([e.label, e.mono, e.control, e.text]).toEqual(['Chat extension', false, 'text', 'The module chat mode runs. TandemApp installs its own on the engine host when it is missing there']);
    const staged = new Draft();
    staged.set('engine', 'eh-desk', MODE_KEY, 'chat');
    expect(groupsOf('engine', fieldsOf(ext, extVals('engine'), staged.part('engine', 'eh-desk')))[0].fields.map((y) => y.key)).toEqual([MODE_KEY, EXT_KEY]);
    // a staged unset shows what the key goes back to
    d.unset('engine', 'eh-desk', 'MAX_QUEUE', { set: true });
    const g = fieldsOf(s, values, d.part('engine', 'eh-desk')).find((x) => x.key === 'MAX_QUEUE')!;
    expect([g.staged, g.value]).toEqual(['unset', '16']);
  });

  it('the backends: a group each with its name, the environment’s lock, the rules and the users’ keys', () => {
    const s = schemaOf('backends', [
      key('backends.cloud.enabled', 'boolean', { default: true }),
      key('backends.cloud.key', 'secret', { secret: true, write_only: true, nullable: true }),
      key('backends.cloud.label', 'string', { nullable: true }),
      key('backends.cloud.timeouts.first_byte', 'number'),
      key('backends.cloud.url', 'string'),
      key('backends.engine.label', 'string', { nullable: true }),
      key('backends.engine.url', 'string', { writable: false, why: 'set by TANDEM_BACKENDS', variable: 'TANDEM_BACKENDS' }),
      key('routes', 'list', { default: [], text: 'the routing rules in order' }),
      key('users.1.enabled', 'boolean', { default: true, text: 'the key of laptop' }),
    ]);
    const values = { values: [val('backends.cloud.label', 'Cloud'), val('backends.cloud.url', 'https://api.example.com/v1'), val('backends.cloud.key', null, { hint: 'a1b2c3' }), val('backends.cloud.enabled', true), val('backends.cloud.timeouts.first_byte', 120), val('backends.engine.label', null), val('backends.engine.url', 'http://127.0.0.1:8000/v1', { source: 'env', variable: 'TANDEM_BACKENDS' }), val('routes', []), val('users.1.enabled', true, { label: 'laptop' })] };
    const f = fieldsOf(s, values, undefined);
    const groups = groupsOf('backends', f);
    expect(groups.map((g) => [g.id, g.title])).toEqual([['backend:cloud', 'Cloud'], ['backend:engine', 'engine'], ['routes', 'Routing rules'], ['users', 'Users’ keys']]);
    expect(groups[0].fields.map((x) => x.label)).toEqual(['Label', 'URL', 'Enabled', 'API key', 'Timeouts: first byte']);
    expect(groups[2].sub).toBe('The routing rules in order.');
    const url = f.find((x) => x.key === 'backends.engine.url')!;
    expect([url.locked, url.why]).toEqual([true, 'Set by TANDEM_BACKENDS in the environment. Change it there.']);
    const secret = f.find((x) => x.key === 'backends.cloud.key')!;
    expect([secret.control, secret.value, secret.reset]).toEqual(['secret', null, 'Remove the key']);
    expect(f.find((x) => x.key === 'users.1.enabled')!.label).toBe('Key of laptop');
    expect(f.find((x) => x.key === 'backends.cloud.label')!.reset).toBe('Clear');
    // a scope that is read-only says why once; its rows are locked without a reason of their own
    const ro = fieldsOf({ ...s, writable: false, why: 'read-only' }, values, undefined);
    expect(ro.every((x) => x.locked && x.why === null)).toBe(true);
    expect([humanize('max_inflight'), humanize('host.dcgm_url'), labelOf('conductor', 'profiles.default').label, labelOf('app', 'chat').label, labelOf('hooks', 'hooks.audit.mode').label]).toEqual(['Max inflight', 'Host: DCGM URL', 'Profile default', 'Chat', 'Audit: mode']);
    expect(sentence('the conductor in front of the backends')).toBe('The conductor in front of the backends.');
  });

  it('what a typed text means, or why it means nothing yet', () => {
    const int = key('MAX_QUEUE', 'integer');
    expect(parseInput(int, 'number', ' 32 ')).toEqual({ ok: true, value: 32 });
    expect(parseInput(int, 'number', '32', true)).toEqual({ ok: true, value: '32' });
    expect(parseInput(int, 'number', '3.5')).toEqual({ ok: false, error: 'A whole number, like 16.' });
    expect(parseInput(int, 'number', '')).toEqual({ ok: false, error: 'A number is needed here.' });
    expect(parseInput(key('m', 'integer', { nullable: true }), 'number', ' ')).toEqual({ ok: true, unset: true });
    expect(parseInput(key('t', 'number'), 'number', '2.5')).toEqual({ ok: true, value: 2.5 });
    expect(parseInput(key('t', 'number'), 'number', 'soon')).toEqual({ ok: false, error: 'A number, like 2.5.' });
    expect(parseInput(key('models', 'list'), 'lines', ' a \n\n b\n')).toEqual({ ok: true, value: ['a', 'b'] });
    expect(parseInput(key('router', 'document'), 'json', '{"rules": []}')).toEqual({ ok: true, value: { rules: [] } });
    expect(parseInput(key('router', 'document'), 'json', '[1]')).toEqual({ ok: false, error: 'A whole document: { … }.' });
    expect(parseInput(key('x', 'list'), 'json', '{}')).toEqual({ ok: false, error: 'A list: [ … ].' });
    const bad = parseInput(key('router', 'document'), 'json', '{"rules": ');
    expect(bad.ok === false && bad.error.startsWith('Not valid JSON yet:')).toBe(true);
    expect(parseInput(key('c', 'cidrs'), 'text', ' 192.0.2.0/24 ')).toEqual({ ok: true, value: '192.0.2.0/24' });
    expect(parseInput(key('c', 'cidrs'), 'text', '')).toEqual({ ok: true, value: '' });
    expect(parseInput(key('l', 'string', { nullable: true }), 'text', '')).toEqual({ ok: true, unset: true });
    expect([textOf('lines', ['a', 'b']), textOf('json', { a: 1 }), textOf('number', 16), textOf('text', null)]).toEqual(['a\nb', '{\n  "a": 1\n}', '16', '']);
    expect([asText({ scope: 'engine', held: null }), asText({ scope: 'backends', held: val('x', 3) })]).toEqual([true, false]);
    expect([shown(null), shown(''), shown(true), shown(['a', 'b']), shown([]), shown({ a: 1 }), shown('x'.repeat(100)).length]).toEqual(['not set', 'empty', 'on', 'a, b', 'none', '{"a":1}', 72]);
  });

  it('a version against now: what differs, the engine’s overlay alone, no secret', () => {
    const now = [val('MAX_QUEUE', '32', { source: 'managed' }), val('QSE_ALLOW_FROM', '', { set: false, source: 'default' }), val('PORT', '8000', { source: 'install' })];
    expect(versionDiff('engine', { values: { MAX_QUEUE: '16' } }, now)).toEqual([{ key: 'MAX_QUEUE', then: '16', now: '32' }]);
    expect(versionDiff('engine', { values: {} }, now)).toEqual([{ key: 'MAX_QUEUE', then: undefined, now: '32' }]);
    expect(versionDiff('app', { values: { chat: 'off', conductor: 'on' } }, [val('chat', 'off'), val('conductor', 'off')])).toEqual([{ key: 'conductor', then: 'on', now: 'off' }]);
    expect(versionDiff('backends', { values: { a: 1, k: 'x' } }, [val('a', 1), val('k', null)], new Set(['k']))).toEqual([]);
    // a conductor document's version carries no values: its plan shows the diff
    expect(versionDiff('conductor', {}, [])).toBeNull();
  });
});

const PLAN: Plan = {
  contract_version: '1.0', change_set: 'cs-0123456789ab', changed: true, restart: 'engine', restart_needed: true, fingerprint_keys: [], needs_confirm: true, confirm: 'c'.repeat(32), confirm_fingerprint: null,
  parts: [
    { scope: 'app', target: 'controller', kind: 'set', version: null, changed: true, restart: 'none', restart_needed: false, fingerprint_keys: [], warnings: [], changes: [{ key: 'conductor', from: 'off', to: 'on', was_set: false, is_set: true, secret: false, restart: 'none', fingerprint: false }] },
    { scope: 'engine', target: 'eh-desk', kind: 'set', version: null, changed: true, restart: 'engine', restart_needed: true, fingerprint_keys: [], warnings: [], changes: [{ key: 'MAX_QUEUE', from: '16', to: '32', was_set: true, is_set: true, secret: false, restart: 'engine', fingerprint: false }] },
  ],
};
const OVERVIEW = { contract_version: '1.0', order: [], operations: [], scopes: [{ scope: 'engine' as const, order: 3, why: null, targets: [{ id: 'eh-desk', label: 'spark-desk', writable: true, why: null }] }] };

describe('a plan and a result in words', () => {
  it('the head says how much changes and what restarts; each stop says what it costs', () => {
    expect(planSummary(PLAN, OVERVIEW)).toEqual({ title: 'Review 2 changes', text: 'One engine restart: spark-desk. The rest applies at once.', restarts: ['spark-desk'] });
    expect(planSummary({ ...PLAN, parts: [PLAN.parts[0]] }, OVERVIEW).text).toBe('No restart. Everything applies at once.');
    expect(planSummary({ ...PLAN, changed: false }, OVERVIEW).title).toBe('Nothing to change');
    expect([placeOf(PLAN.parts[0], OVERVIEW), placeOf(PLAN.parts[1], OVERVIEW), placeOf(PLAN.parts[1])]).toEqual(['App', 'Engine: spark-desk', 'Engine: eh-desk']);
    expect(costOf(PLAN.parts[0], 'this controller')).toEqual({ tone: 'ok', text: 'No restart: it applies at once.' });
    expect(costOf(PLAN.parts[1], 'spark-desk')).toEqual({ tone: 'live', text: 'Restarts the engine on spark-desk, once.' });
    // the second confirm: the verb agrees with the number of keys, each key named once
    expect(fingerprintWords(['engine/eh-desk:QWEN38_PREFIX_CHUNK'], OVERVIEW)).toEqual({ keys: ['QWEN38_PREFIX_CHUNK'], hosts: ['spark-desk'], verb: 'feeds' });
    expect(fingerprintWords(['engine/eh-desk:QWEN38_PREFIX_CHUNK', 'engine/eh-desk:QWEN38_SPILL'], OVERVIEW)).toEqual({ keys: ['QWEN38_PREFIX_CHUNK', 'QWEN38_SPILL'], hosts: ['spark-desk'], verb: 'feed' });
    expect(fingerprintWords(['engine/eh-desk:QWEN38_PREFIX_CHUNK', 'engine/eh-two:QWEN38_PREFIX_CHUNK'], OVERVIEW)).toEqual({ keys: ['QWEN38_PREFIX_CHUNK'], hosts: ['spark-desk', 'eh-two'], verb: 'feeds' });
    // how long the restart itself takes, and where the figure comes from
    expect(costOf({ ...PLAN.parts[1], restart_estimate: { seconds: 180, source: 'default' } }, 'spark-desk').text).toBe('Restarts the engine on spark-desk, once. The restart takes about 3 min, a usual time for an engine.');
    expect(costOf({ ...PLAN.parts[1], restart_estimate: { seconds: 144, source: 'engine' } }, 'spark-desk').text).toBe('Restarts the engine on spark-desk, once. The restart takes about 2 min, as its last one did.');
    expect(costOf({ ...PLAN.parts[1], restart_estimate: { seconds: 7, source: 'measured' } }, 'spark-desk').text).toBe('Restarts the engine on spark-desk, once. The restart takes about 1 min, as its last one did.');
    expect(costOf({ ...PLAN.parts[1], restart: 'hot' }, 'x').text).toBe('No restart: the running engine takes it at once.');
    expect(costOf({ ...PLAN.parts[0], scope: 'conductor', restart: 'hot' }, 'x').text).toBe('No restart: the conductor reads it at the next request.');
    expect(costOf({ ...PLAN.parts[0], changed: false }, 'x').text).toBe('Nothing to change: it holds already.');
  });

  it('a change: from and to; a secret never shows a value; a document says how many fields', () => {
    expect(changeWords(PLAN.parts[1].changes[0])).toEqual({ from: '16', to: '32', note: null });
    expect(changeWords({ ...PLAN.parts[1].changes[0], to: '16', is_set: false }).note).toBe('back to what stands below it');
    expect(changeWords({ key: 'backends.cloud.key', from: null, to: null, was_set: true, is_set: true, secret: true, restart: 'none', fingerprint: false })).toEqual({ from: 'a key is stored', to: 'a new key', note: null });
    expect(changeWords({ key: 'backends.cloud.key', from: null, to: null, was_set: true, is_set: false, secret: true, restart: 'none', fingerprint: false }).to).toBe('no key');
    expect(changeWords({ key: 'router', from: { version: 1 }, to: { version: 'new' }, was_set: true, is_set: true, secret: false, restart: 'hot', fingerprint: false, diff: [{ path: 'default', before: 'main', after: 'side' }], diff_count: 1 })).toEqual({ from: 'version 1', to: 'a new version', note: '1 field changes' });
  });

  it('when an engine restarts: idle with its wait, a set time, now', () => {
    expect(whenParam(WHEN_DEFAULT)).toEqual({ when: 'idle', deadline_s: 600 });
    expect(whenParam({ mode: 'now', at: '', deadline_s: 600 })).toEqual({ when: 'now' });
    expect(whenParam({ mode: 'at', at: '2026-11-16T22:00:00Z', deadline_s: 1800 })).toEqual({ when: 'at=2026-11-16T22:00:00Z', deadline_s: 1800 });
    const now = Date.parse('2026-11-16T09:00:00Z');
    expect([whenError(WHEN_DEFAULT, now), whenError({ mode: 'at', at: '', deadline_s: 600 }, now), whenError({ mode: 'at', at: '2026-11-16T08:00:00Z', deadline_s: 600 }, now), whenError({ mode: 'at', at: '2026-11-16T22:00:00Z', deadline_s: 600 }, now)]).toEqual([null, 'Pick the day and the time.', 'That time has passed. Pick a later one.', null]);
    expect(whenWords(WHEN_DEFAULT)).toBe('It waits for the running requests to end, at most 10 minutes. Still busy then: nothing changes.');
    expect(whenWords({ mode: 'now', at: '', deadline_s: 600 })).toBe('It restarts at once. Requests that run are cut off.');
    expect(whenWords({ mode: 'at', at: '2026-11-16T22:00:00Z', deadline_s: 300 }, time)).toBe('Nothing happens before 22:00. Then: it waits for the running requests to end, at most 5 minutes. Still busy then: nothing changes.');
    expect([dur(45), dur(120), dur(600), dur(3600), dur(5400), dur(14400)]).toEqual(['45 seconds', '2 minutes', '10 minutes', '1 hour', '1 h 30 min', '4 hours']);
    expect([clock(582), clock(0), clock(3723), clock(-4)]).toEqual(['09:42', '00:00', '1:02:03', '00:00']);
    expect(DEADLINES.every((d) => d >= 10 && d <= 86400)).toBe(true);
  });

  it('how a set ended, and each part', () => {
    const part = { scope: 'engine' as const, target: 'eh-desk', kind: 'set' as const, keys: ['MAX_QUEUE'], version_before: '1', version_after: null, restore: [], restart: null, error: null };
    const r = { contract_version: '1.0', change_set: 'cs-0123456789ab', replayed: false, applied: [], failed: null, not_run: [] };
    expect(resultHead({ ...r, state: 'applied', parts: [{ ...part, state: 'scheduled' }] })).toMatchObject({ title: 'Applied. One engine restart is on its way.', tone: 'live' });
    expect(resultHead({ ...r, state: 'applied', parts: [{ ...part, state: 'applied' }] })).toMatchObject({ title: 'Applied', tone: 'ok' });
    expect(resultHead({ ...r, state: 'partial', failed: 'engine/eh-desk', parts: [] }).title).toBe('Stopped at engine/eh-desk');
    expect(resultHead({ ...r, state: 'failed', failed: 'app/controller', parts: [] }).text).toContain('app/controller refused the change');
    expect(resultHead({ ...r, state: 'unchanged', replayed: true, parts: [] }).text).toBe('Everything held already. This is the answer of the apply before: nothing was applied twice.');
    expect((['applied', 'scheduled', 'unchanged', 'failed', 'not_run'] as const).map((state) => partStateWords({ ...part, state }).text)).toEqual(['Applied', 'Applies with the engine’s restart', 'Held already', 'Failed. Nothing changed here.', 'Not run: the set stopped before it']);
    expect(partStateWords({ ...part, state: 'applied', kind: 'restore' }).text).toBe('Restored');
  });
});

const RESTART: Restart = {
  id: 'rs-0123456789ab', host: 'eh-desk', alias: 'spark-desk', change_set: 'cs-0123456789ab', kind: 'settings', who: 'admin', state: 'draining', message: null, when: 'idle', deadline_s: 600, deadline_at: '2026-11-16T09:10:00Z',
  remaining_s: 582, created_at: '2026-11-16T09:00:00Z', ended_at: null, job: { id: '20261116T090000-abc123', kind: 'settings', state: 'waiting', phase: 'waiting for idle' }, backends: ['engine'], draining: true, warm: null, restored: null, can_cancel: true,
};

describe('a restart in words', () => {
  it('while it waits: the countdown, the track, who is told to try again', () => {
    const m = restartModel(RESTART);
    expect([m.tone, m.title, m.count, m.ended]).toEqual(['live', 'spark-desk restarts when it is idle', '09:42', false]);
    expect(m.text).toBe('It waits for the running requests to end. New requests to engine are answered “try again in 30 s” until the engine is back.');
    expect(m.steps).toEqual([{ label: 'Wait for idle', state: 'now' }, { label: 'Restart', state: 'todo' }, { label: 'Warm up', state: 'todo' }]);
    expect(m.used).toBeCloseTo(0.03, 2);
    expect(m.job).toBe('Engine job 20261116T090000-abc123: waiting, waiting for idle');
    // between two reads the page counts on
    expect([leftNow(RESTART, 0), leftNow(RESTART, 2400), leftNow(RESTART, 700_000), leftNow({ ...RESTART, remaining_s: null }, 5000)]).toEqual([582, 580, 0, null]);
    expect(restartModel(RESTART, 60).count).toBe('01:00');
  });

  it('every state has its sentence, and an end stops the track where it stood', () => {
    const at = (state: Restart['state'], o: Partial<Restart> = {}) => restartModel({ ...RESTART, state, remaining_s: null, can_cancel: false, ...o }, null, time);
    const sched = at('scheduled', { when: 'at=2026-11-16T22:00:00Z', deadline_at: null });
    expect([sched.title, sched.steps.map((s) => s.state), sched.steps[0].label, sched.count]).toEqual(['spark-desk restarts at 22:00', ['now', 'todo', 'todo', 'todo'], 'Wait until 22:00', null]);
    expect(at('restarting')).toMatchObject({ title: 'spark-desk is restarting', text: 'The engine stops, takes the new settings and starts. It can no longer be cancelled.' });
    expect(at('restarting').steps.map((s) => s.state)).toEqual(['done', 'now', 'todo']);
    expect(at('warming').steps.map((s) => s.state)).toEqual(['done', 'done', 'now']);
    const done = at('done', { warm: { ok: true, backends: { engine: { ok: true, why: null } } } });
    expect([done.tone, done.title, done.text, done.steps.map((s) => s.state), done.ended]).toEqual(['ok', 'spark-desk runs the new settings', 'Its backends serve again.', ['done', 'done', 'done'], true]);
    expect(at('done', { warm: { ok: false, backends: {} } }).text).toContain('gave no warm-up token');
    const failed = at('failed', { message: 'the engine did not start', restored: { version: 1, healthy: true } });
    expect([failed.tone, failed.title, failed.text]).toEqual(['err', 'spark-desk did not start with the new settings', 'The engine did not start. It runs version 1 again and answers.']);
    expect(failed.steps.map((s) => s.state)).toEqual(['done', 'stopped', 'todo']);
    expect(at('failed', { message: 'exit 1.', restored: { version: 1, healthy: false } }).text).toBe('Exit 1. It runs version 1 again, but does not answer yet.');
    const timeout = at('timeout');
    expect([timeout.tone, timeout.title, timeout.steps.map((s) => s.state)]).toEqual(['warn', 'spark-desk stayed busy: nothing changed', ['stopped', 'todo', 'todo']]);
    const cancelled = at('cancelled', { message: 'cancelled by admin: the engine keeps the settings it runs' });
    expect([cancelled.tone, cancelled.text, cancelled.steps[0].state]).toEqual(['muted', 'The engine keeps the settings it runs, and its backends serve again.', 'stopped']);
    expect(at('cancelled', { message: 'replaced by another call on the engine host' }).text).toBe('Replaced by another call on the engine host. The engine keeps the settings it runs.');
    expect(RESTART_ENDED).toEqual(['done', 'failed', 'timeout', 'cancelled']);
  });
});

describe('refusals and the audit', () => {
  it('every error type of the contract has a sentence of its own; none is a status number', () => {
    for (const type of Object.keys(ERRORS)) {
      const w = errorWords(new SettingsError(type, `the controller said ${type}`, ERRORS[type], '/v1/settings', { key: 'MAX_QUEUE', keys: ['MAX_QUEUE'] }));
      expect(w.title.length, type).toBeGreaterThan(10);
      expect(w.title, type).not.toMatch(/\b[45]\d\d\b/);
    }
    expect(errorWords(new SettingsError('locked', 'x', 422, '', { key: 'backends.engine.url', variable: 'TANDEM_BACKENDS' }))).toEqual({ title: 'backends.engine.url cannot be changed here.', fix: 'TANDEM_BACKENDS sets it in the environment: change it there.', detail: null });
    expect(errorWords(new SettingsError('stale_plan', 'x', 409, '', { plan: PLAN })).fix).toBe('This is the plan as it is now. Check it, then apply.');
    expect(errorWords(new SettingsError('too_late', 'x', 409, '')).title).toBe('The restart has begun: it can no longer be cancelled.');
    expect(errorWords(new SettingsError('engine_unreachable', 'ssh: timed out', 502, ''))).toEqual({ title: 'TandemApp could not reach the engine host.', fix: 'Check that the machine is on and answers over SSH, then try again. Nothing was changed there.', detail: 'ssh: timed out' });
    expect(errorWords(new SettingsError('busy', 'x', 409, '')).title).toBe('Another change set is being applied right now.');
    expect(errorWords(new SettingsError('unauthorized', 'x', 401, '')).fix).toContain('Sign in');
    expect(errorWords(new SettingsError('weird', 'something else', 500, '')).title).toBe('something else');
    // the chat extension: what to do is the controller's own sentence
    const ext = errorWords(new SettingsError('extension_missing', 'the engine host has no tandemapp.engine_ext that loads, and this controller has no wheel to send: set TANDEM_NODE_WHEEL …', 409, '', { key: 'mode.ext' }));
    expect([ext.title, ext.detail]).toEqual(['The engine host lacks the chat extension, and TandemApp cannot install it from here.', 'the engine host has no tandemapp.engine_ext that loads, and this controller has no wheel to send: set TANDEM_NODE_WHEEL …']);
  });

  it('an audit row: what, where, how it ended', () => {
    const row = { at: '2026-11-16T09:00:00Z', ts_ms: 0, who: 'admin', object: 'settings', keys: ['MAX_QUEUE'], change_set: 'cs-0123456789ab' };
    expect(auditWords({ ...row, action: 'settings.apply', object_id: 'engine/eh-desk', scope: 'engine', target: 'eh-desk', result: 'scheduled' }, OVERVIEW)).toEqual({ action: 'Applied', where: 'Engine: spark-desk', tone: 'live' });
    expect(auditWords({ ...row, action: 'settings.change_set', object_id: null, scope: null, target: null, result: 'partial' })).toEqual({ action: 'Change set ended', where: '', tone: 'warn' });
    expect(auditWords({ ...row, action: 'settings.cancel', object_id: 'engine/eh-desk', scope: 'engine', target: 'eh-desk', result: 'cancelled' }).action).toBe('Restart cancelled');
    expect(auditWords({ ...row, action: 'backend.update', object: 'backend', object_id: 'cloud', scope: null, target: null, result: null })).toEqual({ action: 'backend.update', where: 'backend cloud', tone: 'muted' });
  });
});

describe('where the settings area lives', () => {
  it('six pages under Settings, App; the panel lists them and marks the one you are on', () => {
    expect(SETTINGS_TABS.map((t) => [t.id, t.scope])).toEqual([['app', 'app'], ['backends', 'backends'], ['conductor', 'conductor'], ['engines', 'engine'], ['hooks', 'hooks'], ['history', null]]);
    expect([settingsHref('app'), settingsHref('engines'), settingsHref('engines', 'eh desk'), settingsHref('history')]).toEqual(['#/app', '#/app/engines', '#/app/engines/eh%20desk', '#/app/history']);
    const r = parseHash('#/app/engines/eh-desk');
    expect([r.view, r.path, sectionOf(r.view), crumb(r.view)]).toEqual(['app', ['engines', 'eh-desk'], 'app', { group: 'Settings', label: 'App' }]);
    expect(subPages(r).map((s) => `${s.on ? '*' : ''}${s.label} ${s.href}`)).toEqual(['App #/app', 'Backends #/app/backends', 'Conductor #/app/conductor', '*Engines #/app/engines', 'Hooks #/app/hooks', 'History #/app/history']);
    expect(subPages(parseHash('#/app')).find((s) => s.on)!.label).toBe('App');
    expect(parseHash('#/app/history?change_set=cs-0123456789ab').params.get('change_set')).toBe('cs-0123456789ab');
    // the Backends page of the API section keeps its own route
    expect(parseHash('#/api/backends').view).toBe('backends');
  });
});
