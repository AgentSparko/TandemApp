// SRV-857 (QA SRV-858): the form generator the pages call (src/lib/schema-form.ts). It gives the
// form proxy/schemaform.py gives for every fixture in contracts/plugin-v1/forms (each a schema and
// its form, written by the Python side), every form validates against form.schema.json, and the
// generator draws every schema the contracts publish (the conductor's documents, hooks-v1,
// settings-v1, plugin-v1) without throwing, falling back to the raw editor where it must.
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import Ajv2020 from 'ajv/dist/2020.js';
import { defaultsOf, flatFields, MAX_FIELDS, schemaForm, type Form } from '../src/lib/schema-form';

const CON = join(__dirname, '..', '..', 'contracts');
const FORMS = join(CON, 'plugin-v1', 'forms');
const ajv = new Ajv2020({ allErrors: true, strict: false, allowUnionTypes: true });
const formSchema = JSON.parse(readFileSync(join(CON, 'plugin-v1', 'form.schema.json'), 'utf8'));
delete formSchema.$id;
const validForm = ajv.compile(formSchema);

const fixtures = readdirSync(FORMS).filter((f) => f.endsWith('.json')).map((f) => JSON.parse(readFileSync(join(FORMS, f), 'utf8')) as { name: string; schema: unknown; form: Form });

describe('schema-form: the same form as the controller', () => {
  it('has the fixtures', () => {
    expect(fixtures.length).toBeGreaterThanOrEqual(12);
  });
  for (const fx of fixtures) {
    it(`${fx.name}: equal to proxy/schemaform.py`, () => {
      const got = schemaForm(fx.schema);
      expect(got).toEqual(fx.form);
      expect(validForm(got), JSON.stringify(validForm.errors)).toBe(true);
    });
  }
});

describe('schema-form: the controls', () => {
  it('picks a control by type, enum size and length', () => {
    const f = schemaForm({
      type: 'object', required: ['a'], additionalProperties: false,
      properties: {
        a: { type: 'boolean' }, b: { enum: ['x', 'y'] }, c: { enum: ['1', '2', '3', '4', '5'] },
        d: { type: 'string', maxLength: 500 }, e: { type: 'string', writeOnly: true, default: 'nope' },
        g: { type: 'integer', minimum: 0, maximum: 9 },
      },
    });
    const by = Object.fromEntries(f.fields.map((x) => [x.key, x]));
    expect(by.a.control).toBe('switch');
    expect(by.a.required).toBe(true);
    expect(by.b.control).toBe('choice');
    expect(by.c.control).toBe('select');
    expect(by.d.control).toBe('textarea');
    expect(by.e.control).toBe('secret');
    expect(by.e.default).toBeNull();
    expect(by.e.has_default).toBe(false);
    expect([by.g.min, by.g.max, by.g.step]).toEqual([0, 9, 1]);
  });

  it('falls back to the raw editor past the limits, with a note each', () => {
    const many = { type: 'object', properties: Object.fromEntries(Array.from({ length: MAX_FIELDS + 3 }, (_, i) => [`k${i}`, { type: 'integer' }])) };
    const f = schemaForm(many);
    expect(f.fields.filter((x) => x.control === 'json')).toHaveLength(3);
    expect(f.notes).toHaveLength(3);
    const shallow = schemaForm({ type: 'object', properties: { a: { type: 'object', properties: { b: { type: 'string' } } } } }, { maxDepth: 1 });
    expect(shallow.fields[0].control).toBe('json');
    expect(shallow.fields[0].why).toMatch(/deeper than 1/);
  });

  it('follows local refs, refuses remote ones and cycles without throwing', () => {
    const f = schemaForm({
      type: 'object', $defs: { loop: { $ref: '#/$defs/loop' }, n: { type: 'number', title: 'N' } },
      properties: { n: { $ref: '#/$defs/n' }, loop: { $ref: '#/$defs/loop' }, far: { $ref: 'x.json#/a' } },
    });
    const by = Object.fromEntries(f.fields.map((x) => [x.key, x]));
    expect(by.n.label).toBe('N');
    expect(by.loop.why).toMatch(/refers to itself/);
    expect(by.far.why).toMatch(/only a local reference/);
  });

  it('flattens and gives defaults', () => {
    const fx = fixtures.find((x) => x.name === 'classify-provider')!;
    const f = schemaForm(fx.schema);
    expect(flatFields(f).map((x) => x.key)).toEqual(['classes', 'classes.code', 'classes.code[]', 'classes.chat', 'classes.chat[]', 'fallback', 'endpoint']);
    expect(defaultsOf(f)).toEqual({ classes: { code: ['traceback', 'compile', 'function'], chat: ['hello', 'thanks'] }, fallback: 'unclear', endpoint: null });
  });
});

describe('schema-form: every schema the contracts publish', () => {
  const dirs = ['conductor-v1', 'hooks-v1', 'hooks-v1/events', 'settings-v1', 'plugin-v1'];
  const files = dirs.flatMap((d) => readdirSync(join(CON, d)).filter((f) => f.endsWith('.schema.json')).map((f) => join(CON, d, f)));
  it(`draws ${files.length} schemas`, () => {
    expect(files.length).toBeGreaterThan(80);
    let fields = 0;
    for (const p of files) {
      const f = schemaForm(JSON.parse(readFileSync(p, 'utf8')));
      expect(validForm(f), `${p}: ${JSON.stringify(validForm.errors)}`).toBe(true);
      fields += flatFields(f).length;
    }
    expect(fields).toBeGreaterThan(500);
  });
});
