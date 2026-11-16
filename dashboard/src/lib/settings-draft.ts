// The settings pages' draft (VIS-280): the changes staged so far, one part a scope and target as a
// change set takes them. A field stages a value or an unset; a version stages a restore, which
// stands for its part alone (a restore sets nothing else). A value equal to what holds now is no
// change, so a switch flipped back leaves no trace. The draft lives in this page's memory only: a
// secret typed into it is never stored anywhere. Kept free of the DOM.

import { SCOPES, type ErrorFacts, type PartChange, type Scope } from '../api/settings';

export interface DraftPart {
  scope: Scope;
  target: string;
  set: Record<string, unknown>;
  unset: string[];
  restore: string | null;
}

/** what a key holds now, as the scope answered it */
export interface Held {
  value: unknown;
  set: boolean;
}

export type Staged = { kind: 'set'; value: unknown } | { kind: 'unset' } | null;

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`;
  if (v && typeof v === 'object')
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stable((v as Record<string, unknown>)[k])}`)
      .join(',')}}`;
  return JSON.stringify(v) ?? 'null';
}

/** the same value, whatever the order of an object's keys */
export const same = (a: unknown, b: unknown): boolean => stable(a) === stable(b);

/** The staged field a plan's refusal is about: the part its scope and target name, the key in it.
 *  A refusal that names no part (an older controller) marks a key only when one part holds it. */
export function refusedField(parts: DraftPart[], facts: ErrorFacts): { scope: Scope; target: string; key: string } | null {
  const key = facts.key;
  if (!key) return null;
  const has = (p: DraftPart) => key in p.set || p.unset.includes(key);
  const named = facts.scope ? parts.filter((p) => p.scope === facts.scope && (facts.target == null || p.target === facts.target)) : parts;
  const hit = named.filter(has);
  return hit.length === 1 ? { scope: hit[0].scope, target: hit[0].target, key } : null;
}

export class Draft {
  parts: DraftPart[] = [];
  private subs = new Set<() => void>();

  /** Hears every change; returns the unsubscribe. */
  on(fn: () => void): () => void {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  }

  private changed(): void {
    this.parts = this.parts.filter((p) => p.restore !== null || p.unset.length || Object.keys(p.set).length);
    this.parts.sort((a, b) => SCOPES.indexOf(a.scope) - SCOPES.indexOf(b.scope) || (a.target < b.target ? -1 : a.target > b.target ? 1 : 0));
    for (const f of this.subs) f();
  }

  part(scope: Scope, target: string): DraftPart | undefined {
    return this.parts.find((p) => p.scope === scope && p.target === target);
  }

  private open(scope: Scope, target: string): DraftPart {
    let p = this.part(scope, target);
    if (!p) this.parts.push((p = { scope, target, set: {}, unset: [], restore: null }));
    // an edit after a staged restore: the restore goes, the edit stands
    p.restore = null;
    return p;
  }

  /** Stage a value. `held`: what the key holds now; a secret is never compared (it is never read
   *  back), so a typed one is always a change. */
  set(scope: Scope, target: string, key: string, value: unknown, held: Held | null = null, secret = false): void {
    const p = this.open(scope, target);
    p.unset = p.unset.filter((k) => k !== key);
    if (!secret && held && held.set && same(held.value, value)) delete p.set[key];
    else p.set[key] = value;
    this.changed();
  }

  /** Stage an unset: the key goes back to what stands below it. Nothing to unset is no change. */
  unset(scope: Scope, target: string, key: string, held: Pick<Held, 'set'> | null = null): void {
    const p = this.open(scope, target);
    delete p.set[key];
    p.unset = p.unset.filter((k) => k !== key);
    if (!held || held.set) p.unset.push(key);
    this.changed();
  }

  /** Take one key's staged change back. */
  drop(scope: Scope, target: string, key: string): void {
    const p = this.part(scope, target);
    if (!p) return;
    delete p.set[key];
    p.unset = p.unset.filter((k) => k !== key);
    this.changed();
  }

  /** Stage a version's restore: it replaces what was staged for that scope and target. */
  restore(scope: Scope, target: string, version: string): void {
    const p = this.open(scope, target);
    p.set = {};
    p.unset = [];
    p.restore = version;
    this.changed();
  }

  dropPart(scope: Scope, target: string): void {
    this.parts = this.parts.filter((p) => !(p.scope === scope && p.target === target));
    this.changed();
  }

  clear(): void {
    this.parts = [];
    this.changed();
  }

  staged(scope: Scope, target: string, key: string): Staged {
    const p = this.part(scope, target);
    if (!p) return null;
    if (key in p.set) return { kind: 'set', value: p.set[key] };
    return p.unset.includes(key) ? { kind: 'unset' } : null;
  }

  /** How many changes are staged: a key each, a restore one. */
  count(scope?: Scope, target?: string): number {
    return this.parts
      .filter((p) => (!scope || p.scope === scope) && (target === undefined || p.target === target))
      .reduce((n, p) => n + (p.restore !== null ? 1 : Object.keys(p.set).length + p.unset.length), 0);
  }

  get empty(): boolean {
    return this.parts.length === 0;
  }

  /** The draft as a change set's parts, in the order they apply. */
  changes(): PartChange[] {
    return this.parts.map((p) => {
      if (p.restore !== null) return { scope: p.scope, target: p.target, restore: p.restore };
      const out: { scope: Scope; target: string; set?: Record<string, unknown>; unset?: string[] } = { scope: p.scope, target: p.target };
      if (Object.keys(p.set).length) out.set = { ...p.set };
      if (p.unset.length) out.unset = [...p.unset];
      return out;
    });
  }
}

/** the one draft of the settings pages */
export const draft = new Draft();
