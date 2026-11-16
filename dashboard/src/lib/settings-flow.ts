// A change set from review to result (VIS-280), and the engine restarts it schedules.
//
// ChangeSetFlow: the staged changes are planned first (nothing is written), the plan is shown, and
// Apply sends it back with the plan's confirm. A plan with a key that feeds an engine's disk-cache
// fingerprint asks a second time before it sends the second confirm. When the settings moved since
// the plan (409 stale_plan), the controller's new plan takes its place and is shown to be checked
// again: nothing is applied on a plan the user did not see. The result lists each part.
//
// RestartWatch: the restarts the controller follows, read every two seconds while one is under
// way, with the countdown counted on between two reads, and Cancel while a restart still waits.
//
// Both are free of the DOM: the page (views/settings/) draws their state.

import { RESTART_ENDED, SettingsError, type PartChange, type PartResult, type Plan, type Restart, type Result, type SettingsApi } from '../api/settings';
import { WHEN_DEFAULT, leftNow, whenError, whenParam, type When } from './settings-words';

export type Phase = 'closed' | 'planning' | 'plan' | 'applying' | 'result';

export interface FlowState {
  phase: Phase;
  changes: PartChange[];
  plan: Plan | null;
  /** what stopped the plan or the apply */
  error: SettingsError | null;
  /** the plan shown replaced the one read first: the settings moved under it */
  moved: boolean;
  /** the second confirm shows (the plan has fingerprint keys) */
  second: boolean;
  when: When;
  note: string;
  result: Result | null;
}

const START: FlowState = { phase: 'closed', changes: [], plan: null, error: null, moved: false, second: false, when: WHEN_DEFAULT, note: '', result: null };

export const asError = (e: unknown): SettingsError => (e instanceof SettingsError ? e : new SettingsError('error', (e as Error)?.message ?? String(e), 0, ''));

/** the plan restarts an engine: the apply says when */
export const restartsEngine = (plan: Plan | null): boolean => !!plan?.parts.some((p) => p.changed && p.restart === 'engine');

/** The changes that bring an applied part back: one part a version (a conductor part restores
 *  each of its documents by itself, so each is a change set of its own). */
export function restoreOf(p: Pick<PartResult, 'restore'>): PartChange[][] {
  return p.restore.map((r) => [{ scope: r.scope, target: r.target, restore: r.version }]);
}

export class ChangeSetFlow {
  state: FlowState = START;

  constructor(
    private api: SettingsApi,
    private changed: () => void = () => undefined,
    private clock: () => number = () => Date.now(),
  ) {}

  private set(p: Partial<FlowState>): void {
    this.state = { ...this.state, ...p };
    this.changed();
  }

  /** The plan of these changes: what would change, what restarts. Nothing is written. */
  async review(changes: PartChange[]): Promise<void> {
    this.set({ ...START, phase: 'planning', changes, when: this.state.when, note: this.state.phase === 'closed' ? '' : this.state.note });
    try {
      const plan = await this.api.plan(changes);
      if (this.state.phase === 'planning') this.set({ phase: 'plan', plan });
    } catch (e) {
      if (this.state.phase === 'planning') this.set({ phase: 'plan', plan: null, error: asError(e) });
    }
  }

  setWhen(p: Partial<When>): void {
    this.set({ when: { ...this.state.when, ...p } });
  }

  setNote(note: string): void {
    this.set({ note: note.slice(0, 200) });
  }

  /** Why Apply cannot go now; null when it can. */
  blocked(): string | null {
    const { plan, phase, when } = this.state;
    if (phase !== 'plan' || !plan) return 'There is no plan yet.';
    if (!plan.changed || !plan.confirm) return 'Nothing would change.';
    return restartsEngine(plan) ? whenError(when, this.clock()) : null;
  }

  /** Apply the plan shown. With fingerprint keys the first call opens the second confirm and the
   *  next one applies. */
  async apply(): Promise<void> {
    const { plan, changes, second, when, note } = this.state;
    if (this.blocked() || !plan?.confirm) return;
    if (plan.confirm_fingerprint && !second) {
      this.set({ second: true, error: null });
      return;
    }
    this.set({ phase: 'applying', error: null });
    try {
      const result = await this.api.apply(changes, {
        confirm: plan.confirm,
        ...(plan.confirm_fingerprint ? { confirm_fingerprint: plan.confirm_fingerprint } : {}),
        ...(restartsEngine(plan) ? whenParam(when) : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      this.set({ phase: 'result', result, second: false });
    } catch (e) {
      const err = asError(e);
      // the settings moved since the plan: the new plan stands in, to be checked again
      if (err.facts.plan && (err.type === 'stale_plan' || err.type === 'confirm_required' || err.type === 'fingerprint_confirm_required')) this.set({ phase: 'plan', plan: err.facts.plan, moved: true, second: false, error: err.type === 'stale_plan' ? err : null });
      else this.set({ phase: 'plan', second: false, error: err });
    }
  }

  /** Out of the second confirm, back to the plan. */
  back(): void {
    this.set({ second: false });
  }

  /** The same changes planned again: after a failure only what is still different is planned. */
  again(): Promise<void> {
    return this.review(this.state.changes);
  }

  close(): void {
    this.set(START);
  }
}

export class RestartWatch {
  list: Restart[] = [];
  /** when the list was read, on the page's clock */
  readAt = 0;
  /** the restart a cancel runs for */
  cancelling: string | null = null;
  /** a cancel the controller refused */
  refused: { id: string; error: SettingsError } | null = null;
  /** the controller has no restarts to tell of (no engine scope): the watch stays quiet */
  none = false;

  constructor(
    private api: SettingsApi,
    private changed: () => void = () => undefined,
    private clock: () => number = () => Date.now(),
    /** a restart ended since the last read */
    private ended: (r: Restart) => void = () => undefined,
  ) {}

  /** the restarts still under way */
  get active(): Restart[] {
    return this.list.filter((r) => !RESTART_ENDED.includes(r.state));
  }

  get(id: string): Restart | undefined {
    return this.list.find((r) => r.id === id);
  }

  /** the seconds a restart still waits for idle, counted on from the last read */
  left(r: Restart): number | null {
    return leftNow(r, this.clock() - this.readAt);
  }

  private take(next: Restart[]): void {
    const was = new Map(this.list.map((r) => [r.id, r.state]));
    this.list = next;
    this.readAt = this.clock();
    this.changed();
    for (const r of next) if (RESTART_ENDED.includes(r.state) && was.has(r.id) && !RESTART_ENDED.includes(was.get(r.id)!)) this.ended(r);
  }

  /** A restart the page learned of before the list did: an apply's answer. */
  add(r: Restart): void {
    if (!this.get(r.id)) this.take([r, ...this.list]);
  }

  async read(): Promise<void> {
    try {
      this.take((await this.api.restarts()).restarts);
      this.none = false;
    } catch (e) {
      // a controller without an engine scope has no such route; a read that fails is tried again
      if (asError(e).status === 404) this.none = true;
    }
  }

  /** Cancel a restart that still waits. Too late, or ended already: the refusal is kept and the
   *  list read again, so the card shows where it stands. */
  async cancel(id: string): Promise<void> {
    if (this.cancelling) return;
    this.cancelling = id;
    this.refused = null;
    this.changed();
    try {
      const r = await this.api.cancel(id);
      this.cancelling = null;
      this.take(this.list.map((x) => (x.id === id ? r : x)));
    } catch (e) {
      this.cancelling = null;
      this.refused = { id, error: asError(e) };
      this.changed();
      await this.read();
    }
  }
}
