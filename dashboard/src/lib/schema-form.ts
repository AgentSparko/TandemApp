// A JSON schema as a form (SRV-857, contracts/plugin-v1/form.schema.json): the same generator as
// proxy/schemaform.py `describe`, for a page that holds the schema itself (a plugin's settings, a
// hook's settings, the conductor's documents). Both are held to the same fixtures
// (contracts/plugin-v1/forms/*.json: a schema and the form it gives), so a field reads the same
// whichever side drew it. Kept free of the DOM: a page draws `fields` with the control each names.
//
// Controls: switch (boolean), number (integer, number), text, textarea (a string over 200
// characters), choice (an enum of 2 to 4), select (more, or one), const (one fixed value, shown),
// secret (writeOnly: set or unset, never shown), group (an object: `fields`), list (an array:
// `item`), json (the raw editor; `why` says why). Nesting deeper than 4 levels, more than 200
// fields or an enum of more than 200 values fall back to json; each fallback adds a note.

export const FORM_VERSION = '1.0';
export const MAX_DEPTH = 4;
export const MAX_FIELDS = 200;
export const MAX_OPTIONS = 200;
const CHOICE_MAX = 4;
const TEXTAREA_OVER = 200;

export type FieldType = 'string' | 'integer' | 'number' | 'boolean' | 'object' | 'array' | 'null' | 'any';
export type FormControl = 'switch' | 'number' | 'text' | 'textarea' | 'select' | 'choice' | 'const' | 'secret' | 'group' | 'list' | 'json';

export interface FormOption {
  value: unknown;
  label: string;
}

export interface FormField {
  /** the dot path; a list's item is `<list>[]` */
  key: string;
  path: string[];
  label: string;
  description: string | null;
  type: FieldType;
  control: FormControl;
  required: boolean;
  nullable: boolean;
  read_only: boolean;
  /** write-only: the page shows set or unset, never a value */
  secret: boolean;
  has_default: boolean;
  default: unknown;
  options?: FormOption[];
  min?: number;
  max?: number;
  exclusive_min?: number;
  exclusive_max?: number;
  step?: number;
  min_length?: number;
  max_length?: number;
  pattern?: string;
  format?: string;
  min_items?: number;
  max_items?: number;
  unique?: boolean;
  fields?: FormField[];
  item?: FormField;
  /** control json: why the raw editor */
  why?: string;
}

export interface Form {
  form: string;
  title: string | null;
  description: string | null;
  fields: FormField[];
  notes: string[];
}

type Node = Record<string, unknown>;

class SchemaError extends Error {}

interface State {
  root: Node;
  count: number;
  notes: string[];
  maxDepth: number;
  maxFields: number;
}

const isObj = (v: unknown): v is Node => typeof v === 'object' && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isInt = (v: unknown): v is number => isNum(v) && Number.isInteger(v);

function words(name: string): string {
  let s = String(name).replace(/[_-]+/g, ' ').trim();
  s = s.replace(/(?<=[a-z0-9])(?=[A-Z])/g, ' ');
  return s ? s.slice(0, 1).toUpperCase() + s.slice(1).toLowerCase() : String(name);
}

function jsonType(v: unknown): string {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean') return 'boolean';
  if (typeof v === 'number') return Number.isInteger(v) ? 'integer' : 'number';
  if (typeof v === 'string') return 'string';
  if (Array.isArray(v)) return 'array';
  return 'object';
}

function resolve(node: unknown, root: Node, seen: string[] = []): [Node, string[]] {
  let n: unknown = node;
  while (isObj(n) && '$ref' in n) {
    const ref = n.$ref;
    if (typeof ref !== 'string' || !ref.startsWith('#/')) throw new SchemaError(`$ref ${repr(ref)}: only a local reference (#/$defs/...) is followed`);
    if (seen.includes(ref)) throw new SchemaError(`$ref ${repr(ref)} refers to itself`);
    seen = [...seen, ref];
    let target: unknown = root;
    for (const raw of ref.slice(2).split('/')) {
      const part = raw.replace(/~1/g, '/').replace(/~0/g, '~');
      if (!isObj(target) || !(part in target)) throw new SchemaError(`$ref ${repr(ref)} does not resolve`);
      target = target[part];
    }
    const rest: Node = {};
    for (const [k, v] of Object.entries(n)) if (k !== '$ref') rest[k] = v;
    n = Object.keys(rest).length && isObj(target) ? { ...target, ...rest } : target;
  }
  if (n === true || n === null || n === undefined) return [{}, seen];
  if (n === false) return [{ not: {} }, seen];
  if (!isObj(n)) throw new SchemaError('a schema is an object');
  return [n, seen];
}

/** Python's repr of a string, as proxy/schemaform.py prints a $ref in a note */
function repr(v: unknown): string {
  if (typeof v === 'string') return v.includes("'") && !v.includes('"') ? `"${v}"` : `'${v.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`;
  return String(v);
}

function typesOf(node: Node): string[] {
  const t = node.type;
  if (typeof t === 'string') return [t];
  if (Array.isArray(t)) return t.filter((x): x is string => typeof x === 'string');
  if ('const' in node) return [jsonType(node.const)];
  if (Array.isArray(node.enum) && node.enum.length) {
    const got: string[] = [];
    for (const v of node.enum) {
      const jt = jsonType(v);
      if (!got.includes(jt)) got.push(jt);
    }
    return got;
  }
  if ('properties' in node) return ['object'];
  if ('items' in node) return ['array'];
  return [];
}

function option(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v);
}

function keyOf(path: string[]): string {
  return path.join('.').split('.[]').join('[]');
}

function raw(base: FormField, why: string, st: State): FormField {
  base.control = 'json';
  base.why = why;
  st.notes.push(`${base.key}: ${why}`);
  return base;
}

function field(node0: unknown, path: string[], required: boolean, st: State, depth: number, refs: string[]): FormField {
  st.count += 1;
  const last = path[path.length - 1];
  const name = last !== '[]' ? last : path.length > 1 ? path[path.length - 2] : 'item';
  const base: FormField = {
    key: keyOf(path), path: [...path], label: words(name), description: null, type: 'any', control: 'json',
    required: !!required, nullable: false, read_only: false, secret: false, has_default: false, default: null,
  };
  let node: Node = {};
  try {
    [node, refs] = resolve(node0, st.root, refs);
  } catch (e) {
    return raw(base, (e as Error).message, st);
  }
  if (typeof node.title === 'string' && node.title.trim()) base.label = node.title.trim();
  if (typeof node.description === 'string') base.description = node.description;
  base.read_only = node.readOnly === true;
  base.secret = node.writeOnly === true;
  if ('default' in node) {
    base.has_default = true;
    base.default = node.default;
  }
  if (st.count > st.maxFields) return raw(base, `more than ${st.maxFields} fields: edited as JSON`, st);
  for (const key of ['anyOf', 'oneOf'] as const) {
    const alts = node[key];
    if (!Array.isArray(alts) || !alts.length) continue;
    const plain = alts.filter((a) => !(isObj(a) && a.type === 'null' && Object.keys(a).length === 1));
    if (plain.length === 1 && alts.length === 2) {
      const outer: Node = {};
      for (const [k, v] of Object.entries(node)) if (k !== key) outer[k] = v;
      let inner: Node = {};
      let refs2: string[] = refs;
      try {
        [inner, refs2] = resolve(plain[0], st.root, refs);
      } catch (e) {
        return raw(base, (e as Error).message, st);
      }
      st.count -= 1;
      const f = field({ ...inner, ...outer }, path, required, st, depth, refs2);
      f.nullable = true;
      return f;
    }
    const consts = alts.filter((a): a is Node => isObj(a) && 'const' in a);
    if (consts.length === alts.length) {
      const merged: Node = {};
      for (const [k, v] of Object.entries(node)) if (k !== key) merged[k] = v;
      merged.enum = consts.map((a) => a.const);
      const labels = consts.map((a) => (typeof a.title === 'string' ? a.title : null));
      return plainField(merged, base, st, depth, refs, path, labels);
    }
    return raw(base, `${key} of ${alts.length} forms: edited as JSON`, st);
  }
  return plainField(node, base, st, depth, refs, path, null);
}

function plainField(node: Node, base: FormField, st: State, depth: number, refs: string[], path: string[], labels: (string | null)[] | null): FormField {
  let ts = typesOf(node);
  if (ts.includes('null') && ts.length > 1) {
    base.nullable = true;
    ts = ts.filter((t) => t !== 'null');
  }
  if (ts.length === 2 && ts.includes('integer') && ts.includes('number')) ts = ['number'];
  if (ts.length !== 1) {
    base.type = 'any';
    return raw(base, ts.length ? 'no single type: edited as JSON' : 'no type: edited as JSON', st);
  }
  const t = ts[0];
  base.type = t as FieldType;
  if ('const' in node) {
    base.control = 'const';
    base.default = node.const;
    base.has_default = true;
    return base;
  }
  if (base.secret) {
    base.control = 'secret';
    base.has_default = false;
    base.default = null;
    return base;
  }
  if (Array.isArray(node.enum) && node.enum.length && t !== 'boolean') {
    const vals = node.enum.filter((v) => v !== null);
    if (node.enum.includes(null)) base.nullable = true;
    if (vals.length > MAX_OPTIONS) return raw(base, `an enum of ${vals.length} values: edited as JSON`, st);
    base.control = vals.length >= 2 && vals.length <= CHOICE_MAX ? 'choice' : 'select';
    base.options = vals.map((v, i) => ({ value: v, label: labels && i < labels.length && labels[i] ? (labels[i] as string) : option(v) }));
    return base;
  }
  if (t === 'boolean') {
    base.control = 'switch';
    return base;
  }
  if (t === 'integer' || t === 'number') {
    base.control = 'number';
    if (isNum(node.minimum)) base.min = node.minimum;
    if (isNum(node.maximum)) base.max = node.maximum;
    if (isNum(node.exclusiveMinimum)) base.exclusive_min = node.exclusiveMinimum;
    if (isNum(node.exclusiveMaximum)) base.exclusive_max = node.exclusiveMaximum;
    if (t === 'integer') base.step = 1;
    if (isNum(node.multipleOf)) base.step = node.multipleOf;
    return base;
  }
  if (t === 'string') {
    const ml = node.maxLength;
    base.control = isInt(ml) && ml > TEXTAREA_OVER ? 'textarea' : 'text';
    if (node.minLength !== undefined && node.minLength !== null) base.min_length = node.minLength as number;
    if (node.maxLength !== undefined && node.maxLength !== null) base.max_length = node.maxLength as number;
    if (node.pattern !== undefined && node.pattern !== null) base.pattern = node.pattern as string;
    if (node.format !== undefined && node.format !== null) base.format = node.format as string;
    return base;
  }
  if (t === 'object') {
    if (depth >= st.maxDepth) return raw(base, `nested deeper than ${st.maxDepth} levels: edited as JSON`, st);
    const props = node.properties;
    if (!isObj(props) || !Object.keys(props).length) return raw(base, 'an object without named properties: edited as JSON', st);
    const req = new Set(Array.isArray(node.required) ? node.required : []);
    base.control = 'group';
    base.fields = Object.entries(props).map(([k, sub]) => field(sub, [...path, k], req.has(k), st, depth + 1, refs));
    return base;
  }
  if (t === 'array') {
    const items = node.items;
    if (!isObj(items)) return raw(base, 'an array without one item schema: edited as JSON', st);
    if (depth >= st.maxDepth) return raw(base, `nested deeper than ${st.maxDepth} levels: edited as JSON`, st);
    base.control = 'list';
    if (isInt(node.minItems)) base.min_items = node.minItems;
    if (isInt(node.maxItems)) base.max_items = node.maxItems;
    if (node.uniqueItems === true) base.unique = true;
    base.item = field(items, [...path, '[]'], true, st, depth + 1, refs);
    return base;
  }
  if (t === 'null') {
    base.control = 'const';
    base.default = null;
    base.has_default = true;
    return base;
  }
  return raw(base, `type ${repr(t)}: edited as JSON`, st);
}

/** The form of `schema`. An object schema gives its properties as the fields; any other schema one
 *  field with the key "value". */
export function schemaForm(schema: unknown, opts: { maxDepth?: number; maxFields?: number } = {}): Form {
  const st: State = { root: isObj(schema) ? schema : {}, count: 0, notes: [], maxDepth: opts.maxDepth ?? MAX_DEPTH, maxFields: opts.maxFields ?? MAX_FIELDS };
  let node: Node = {};
  let refs: string[] = [];
  try {
    [node, refs] = resolve(schema, st.root);
  } catch (e) {
    st.notes.push(`$: ${(e as Error).message}`);
  }
  const title = typeof node.title === 'string' ? node.title : null;
  const description = typeof node.description === 'string' ? node.description : null;
  const ts = typesOf(node);
  let fields: FormField[];
  if ((ts.length === 1 && ts[0] === 'object') || (!ts.length && 'properties' in node)) {
    const req = new Set(Array.isArray(node.required) ? node.required : []);
    const props = isObj(node.properties) ? node.properties : {};
    fields = Object.entries(props).map(([k, sub]) => field(sub, [k], req.has(k), st, 1, refs));
  } else {
    fields = [field(node, ['value'], true, st, 1, refs)];
  }
  return { form: FORM_VERSION, title, description, fields, notes: st.notes };
}

/** Every field of a form, groups and list items included, in drawing order. */
export function flatFields(form: Form | FormField[]): FormField[] {
  const out: FormField[] = [];
  const walk = (fs: FormField[]) => {
    for (const f of fs) {
      out.push(f);
      if (f.fields) walk(f.fields);
      if (f.item) walk([f.item]);
    }
  };
  walk(Array.isArray(form) ? form : form.fields);
  return out;
}

/** The value a form starts from: each field's default, a group's fields' defaults nested; a field
 *  without a default (and a secret) is left out. */
export function defaultsOf(form: Form | FormField[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of Array.isArray(form) ? form : form.fields) {
    const name = f.path[f.path.length - 1];
    if (f.secret) continue;
    if (f.has_default) out[name] = structuredClone(f.default);
    else if (f.control === 'group' && f.fields) {
      const inner = defaultsOf(f.fields);
      if (Object.keys(inner).length) out[name] = inner;
    }
  }
  return out;
}
