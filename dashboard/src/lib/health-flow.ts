// The health page's calls (SRV-835): read everything, check again (fresh, at most once every three
// seconds, as the controller allows), run a finding's fix and keep what it left: the target as it
// was checked again, a note on the finding's row (cleared, still under way, still the same, or the
// controller's refusal), the steps of an action that changes nothing. An action that asks first
// opens the dialog: Upgrade is two calls, the plan and then the apply with the plan's confirm. A
// 401 names what needs the admin password; a 429 holds a target's buttons for a minute. After a fix
// that changed something the target is read again every few seconds until it settles. Kept free of
// the DOM: the page (views/health/area.ts) draws this and calls `tick` once a second.

import { EngineError, SignInNeeded, type EngineFinding } from '../api/engines';
import { CONTROLLER, type ActionId, type FixBody, type HealthAction, type HealthApi, type HealthFix, type HealthGuide, type HealthTarget, type HealthView, type UpgradePlan } from '../api/health';
import { SIGN_IN, findingOf } from './engine-words';
import { actionOf, cardsOf, isGuide, merged, noteOf, refusalNote, withTarget, type Card, type Note, type Row } from './health';

/** `fresh=1` is taken once every 3 s a target */
export const REFRESH_GAP_MS = 3000;
/** a target takes 6 fixes a minute: after a 429 its buttons wait this long */
export const FIX_WAIT_MS = 60_000;
/** the page reads again by itself this often while it shows */
export const POLL_MS = 20_000;
/** after a fix that changed something: the target read again this often, for this long at most */
export const FOLLOW_MS = 5000;
export const FOLLOW_FOR_MS = 180_000;
/** how long a "fixed" line stays at the top of its card */
export const NOTE_KEEP_MS = 120_000;

export interface Busy {
  /** the row whose button runs */
  key: string;
  action: ActionId;
}

/** The dialog of an action that asks first. `ask`: what will happen (Upgrade: which release, and
 *  when); `plan`: the engine host's plan, to confirm; `done`: what the apply answered. */
export interface DialogState {
  row: Row;
  action: HealthAction;
  /** the card's name: the engine host */
  title: string;
  phase: 'ask' | 'planning' | 'plan' | 'applying' | 'done';
  ref: string;
  when: 'idle' | 'now';
  plan: UpgradePlan | null;
  /** the release the plan is for */
  planRef: string | null;
  error: EngineFinding | null;
  note: Note | null;
}

const is429 = (e: unknown): boolean => e instanceof EngineError && e.status === 429;

export class HealthFlow {
  view: HealthView | null = null;
  /** the first read has answered, one way or the other */
  loaded = false;
  /** a read the user asked for is under way */
  refreshing = false;
  /** the last read failed: the page keeps what it had and says so */
  error: EngineFinding | null = null;
  /** a 401: what asked (`read`: the page itself) */
  signIn: ActionId | 'read' | null = null;
  /** the last fresh read came too soon after another: the controller answered what it had */
  limited = false;
  /** per target: the fix that runs */
  busy: Record<string, Busy> = {};
  /** the targets a Check again reads */
  checking = new Set<string>();
  /** per row: what its last fix left */
  notes: Record<string, Note> = {};
  /** per row: the steps of its guide, and whether they show */
  guides: Record<string, HealthGuide> = {};
  shown = new Set<string>();
  dialog: DialogState | null = null;

  /** when `fresh=1` may be asked again, on the page's clock (ms) */
  private refreshUntil = 0;
  /** per target: when its fix buttons may be pressed again after a 429 */
  private waits: Record<string, number> = {};
  /** per target: when its answer arrived and how old it was then */
  private seen: Record<string, { at: number; age: number; checked: number }> = {};
  private follows: Record<string, { until: number; next: number }> = {};
  /** when the whole answer arrived, and the controller's clock in it */
  private readAt = { at: 0, checked: 0 };
  private reading = false;

  constructor(
    private api: HealthApi,
    private changed: () => void = () => undefined,
    private now: () => number = () => Date.now(),
  ) {}

  // ---- reading ---------------------------------------------------------------------------------

  /** Reads everything. `fresh`: checked again now (not within three seconds of the last fresh
   *  read); `quiet`: the page's own read, no busy state. */
  async load(o: { fresh?: boolean; quiet?: boolean } = {}): Promise<void> {
    const t = this.now();
    if (o.fresh && t < this.refreshUntil) return;
    if (this.reading && !o.fresh) return;
    this.reading = true;
    if (o.fresh) this.refreshUntil = t + REFRESH_GAP_MS;
    if (!o.quiet) {
      this.refreshing = true;
      this.changed();
    }
    try {
      const v = await this.api.all(o.fresh);
      this.take(v);
      this.error = null;
      if (this.signIn === 'read') this.signIn = null;
      if (o.fresh) this.limited = [v.controller, ...v.engines].some((x) => x.limited);
    } catch (e) {
      if (e instanceof SignInNeeded) this.signIn = 'read';
      else {
        this.error = findingOf(e);
        if (is429(e)) this.refreshUntil = this.now() + REFRESH_GAP_MS;
      }
    } finally {
      this.reading = false;
      this.refreshing = false;
      this.loaded = true;
      this.changed();
    }
  }

  /** Check again: everything, fresh. */
  refresh(): Promise<void> {
    return this.load({ fresh: true });
  }

  /** One target checked again (a card's own Check again; the follow after a fix). */
  async recheck(target: string | null, quiet = false): Promise<void> {
    if (target === null) return this.refresh();
    if (this.checking.has(target)) return;
    this.checking.add(target);
    if (!quiet) this.changed();
    try {
      this.takeTarget(await this.api.target(target, true));
      if (!quiet) this.error = null;
    } catch (e) {
      if (e instanceof SignInNeeded) this.signIn = 'read';
      else if (e instanceof EngineError && e.status === 404) void this.load({ quiet: true });
      else if (!quiet) this.error = findingOf(e);
    } finally {
      this.checking.delete(target);
      this.changed();
    }
  }

  private saw(t: HealthTarget): void {
    const had = this.seen[t.id];
    if (!had || t.checked_at >= had.checked) this.seen[t.id] = { at: this.now(), age: t.age_s, checked: t.checked_at };
  }

  private take(v: HealthView): void {
    this.view = merged(this.view, v);
    this.readAt = { at: this.now(), checked: v.checked_at };
    for (const t of [v.controller, ...v.engines]) this.saw(t);
    this.settle();
  }

  private takeTarget(t: HealthTarget): void {
    if (!this.view) return;
    this.view = withTarget(this.view, t);
    this.saw(t);
    this.settle();
  }

  /** A note whose finding is gone says so; a guide whose finding is gone goes with it. */
  private settle(): void {
    if (!this.view) return;
    const rows = new Set(cardsOf(this.view).flatMap((c) => c.rows.map((r) => r.key)));
    for (const [k, n] of Object.entries(this.notes)) {
      if (rows.has(k) || n.kind === 'cleared') continue;
      if (n.kind === 'working' || n.kind === 'still') this.notes[k] = { ...n, kind: 'cleared', text: 'This passes now.', more: null, at: this.now() };
      else delete this.notes[k];
    }
    for (const k of Object.keys(this.guides)) {
      if (rows.has(k)) continue;
      delete this.guides[k];
      this.shown.delete(k);
    }
  }

  // ---- what the page asks ------------------------------------------------------------------------

  /** how old a target's answer is now, seconds */
  age(t: Pick<HealthTarget, 'id' | 'age_s'>): number {
    const s = this.seen[t.id];
    return s ? s.age + (this.now() - s.at) / 1000 : t.age_s;
  }

  /** how long ago a moment of the controller's clock (Unix seconds) was, seconds */
  since(at: number): number {
    return Math.max(0, this.readAt.checked - at) + (this.now() - this.readAt.at) / 1000;
  }

  /** seconds until Check again may ask the controller anew; 0 when it may */
  refreshLeft(): number {
    return Math.max(0, Math.ceil((this.refreshUntil - this.now()) / 1000));
  }

  /** seconds a target's fix buttons still wait after a 429; 0 when they do not */
  waitLeft(target: string | null): number {
    const until = target === null ? 0 : (this.waits[target] ?? 0);
    return Math.max(0, Math.ceil((until - this.now()) / 1000));
  }

  /** the "fixed" lines of a card: notes whose finding is gone */
  fixed(c: Card): Note[] {
    const keys = new Set(c.rows.map((r) => r.key));
    return Object.entries(this.notes)
      .filter(([k, n]) => n.kind === 'cleared' && k.startsWith(`${c.id}|`) && !keys.has(k))
      .map(([, n]) => n);
  }

  // ---- fixes -------------------------------------------------------------------------------------

  /** A finding's button: the steps of a guide, the dialog of an action that asks first, or the fix. */
  async press(row: Row, title = ''): Promise<void> {
    const a = actionOf(row.finding);
    const id = row.target;
    if (!a || id === null || this.busy[id] || this.waitLeft(id) > 0) return;
    if (isGuide(a)) return this.guide(row, a);
    if (a.confirm) {
      this.dialog = { row, action: a, title, phase: 'ask', ref: '', when: 'idle', plan: null, planRef: null, error: null, note: null };
      this.changed();
      return;
    }
    await this.run(row, a, { action: a.id }, (fix) => this.left(row, a, fix));
  }

  /** The steps of an action that changes nothing: asked once, then shown and hidden in place. */
  private async guide(row: Row, a: HealthAction): Promise<void> {
    if (this.shown.has(row.key)) {
      this.shown.delete(row.key);
      this.changed();
      return;
    }
    if (!this.guides[row.key]) {
      await this.run(row, a, { action: a.id }, (fix) => {
        if (fix.guide) this.guides[row.key] = fix.guide;
      });
      if (!this.guides[row.key]) return;
    }
    this.shown.add(row.key);
    this.changed();
  }

  /** what a fix left: its note, and the target followed until it settles */
  private left(row: Row, a: HealthAction, fix: HealthFix): Note {
    const note = noteOf(fix, a, row.finding, this.now());
    this.notes[row.key] = note;
    if (a.changes) this.follows[fix.id] = { until: this.now() + FOLLOW_FOR_MS, next: this.now() + FOLLOW_MS };
    return note;
  }

  private async run(row: Row, a: HealthAction, body: FixBody, done: (fix: HealthFix) => void): Promise<HealthFix | null> {
    const id = row.target as string;
    this.busy[id] = { key: row.key, action: a.id };
    delete this.notes[row.key];
    this.changed();
    try {
      const fix = await this.api.fix(id, body);
      done(fix);
      this.takeTarget(fix.target);
      return fix;
    } catch (e) {
      this.refused(row, a, e);
      return null;
    } finally {
      delete this.busy[id];
      this.changed();
    }
  }

  /** A fix the controller refused: sign in, wait, or its own sentence. Returns it as a finding. */
  private refused(row: Row, a: HealthAction, e: unknown): EngineFinding {
    const id = row.target as string;
    const f = findingOf(e);
    if (e instanceof SignInNeeded) {
      this.signIn = a.id;
      this.notes[row.key] = refusalNote(f, id, a, row.finding, this.now(), 'sign_in');
    } else if (is429(e)) {
      this.waits[id] = this.now() + FIX_WAIT_MS;
      this.notes[row.key] = refusalNote(f, id, a, row.finding, this.now(), 'wait');
    } else {
      this.notes[row.key] = refusalNote(f, id, a, row.finding, this.now());
      // the finding it was for is gone or changed: the card shows what is there now
      if (e instanceof EngineError && e.finding.code === 'not_offered') void this.recheck(id, true);
    }
    return f;
  }

  dismiss(key: string): void {
    delete this.notes[key];
    this.changed();
  }

  // ---- the dialog ----------------------------------------------------------------------------------

  dialogSet(p: Partial<Pick<DialogState, 'ref' | 'when'>>): void {
    if (!this.dialog) return;
    this.dialog = { ...this.dialog, ...p };
    this.changed();
  }

  /** The first call. An answer that is a plan (`needs_confirm`) shows it; any other is the result. */
  async dialogGo(): Promise<void> {
    const d = this.dialog;
    if (!d || d.phase !== 'ask') return;
    const ref = d.ref.trim();
    this.dialog = { ...d, phase: 'planning', error: null };
    const fix = await this.dialogCall(d, { action: d.action.id, ...(ref ? { ref } : {}) }, 'ask');
    if (!fix || !this.dialog) return;
    if (fix.needs_confirm) this.dialog = { ...this.dialog, phase: 'plan', plan: fix.result?.plan ?? null, planRef: fix.result?.ref ?? (ref || null) };
    else this.dialog = { ...this.dialog, phase: 'done', note: this.left(d.row, d.action, fix) };
    this.changed();
  }

  /** The second call: the same action with the plan's confirm. */
  async dialogApply(): Promise<void> {
    const d = this.dialog;
    if (!d || d.phase !== 'plan' || !d.plan?.confirm) return;
    this.dialog = { ...d, phase: 'applying', error: null };
    const fix = await this.dialogCall(d, { action: d.action.id, confirm: d.plan.confirm, when: d.when, ...(d.planRef ? { ref: d.planRef } : {}) }, 'plan');
    if (!fix || !this.dialog) return;
    this.dialog = { ...this.dialog, phase: 'done', note: this.left(d.row, d.action, fix) };
    this.changed();
  }

  private async dialogCall(d: DialogState, body: FixBody, back: 'ask' | 'plan'): Promise<HealthFix | null> {
    const id = d.row.target as string;
    this.busy[id] = { key: d.row.key, action: d.action.id };
    this.changed();
    try {
      const fix = await this.api.fix(id, body);
      this.takeTarget(fix.target);
      return fix;
    } catch (e) {
      const f = findingOf(e);
      if (e instanceof SignInNeeded) this.signIn = d.action.id;
      else if (is429(e)) this.waits[id] = this.now() + FIX_WAIT_MS;
      if (this.dialog) this.dialog = { ...this.dialog, phase: back, error: f };
      return null;
    } finally {
      delete this.busy[id];
      this.changed();
    }
  }

  /** From the plan back to the question (another release, another time). */
  dialogBack(): void {
    if (this.dialog?.phase !== 'plan') return;
    this.dialog = { ...this.dialog, phase: 'ask', plan: null, planRef: null, error: null };
    this.changed();
  }

  /** Closes the dialog; not while one of its calls runs. */
  dialogClose(): void {
    if (!this.dialog || this.dialog.phase === 'planning' || this.dialog.phase === 'applying') return;
    this.dialog = null;
    this.changed();
  }

  // ---- time and the session ------------------------------------------------------------------------

  /** Once a second: old "fixed" lines go, a wait ends, a followed target is read again. */
  tick(): void {
    const now = this.now();
    for (const [k, n] of Object.entries(this.notes)) if (n.kind === 'cleared' && now - n.at > NOTE_KEEP_MS) delete this.notes[k];
    for (const [id, until] of Object.entries(this.waits)) {
      if (now < until) continue;
      delete this.waits[id];
      for (const [k, n] of Object.entries(this.notes)) if (n.kind === 'wait' && n.target === id) delete this.notes[k];
    }
    for (const [id, f] of Object.entries(this.follows)) {
      if (now >= f.until || !this.unsettled(id)) delete this.follows[id];
      else if (now >= f.next && !this.busy[id] && !this.checking.has(id)) {
        f.next = now + FOLLOW_MS;
        void this.recheck(id, true);
      }
    }
  }

  /** a target that still changes: an engine that loads, a fix whose job has not ended */
  private unsettled(id: string): boolean {
    const t = id === CONTROLLER ? this.view?.controller : this.view?.engines.find((e) => e.id === id);
    if (!t) return false;
    return t.findings.some((f) => f.code === 'engine_loading') || Object.values(this.notes).some((n) => n.kind === 'working' && n.target === id);
  }

  /** the targets read again every few seconds (a test's view of the follow) */
  get following(): string[] {
    return Object.keys(this.follows);
  }

  /** Signed in: the refusals for want of the password go, and the page reads again. */
  async signedIn(): Promise<void> {
    this.signIn = null;
    for (const [k, n] of Object.entries(this.notes)) if (n.kind === 'sign_in') delete this.notes[k];
    if (this.dialog?.error?.code === SIGN_IN) this.dialog = { ...this.dialog, error: null };
    await this.load({ quiet: this.view !== null });
  }
}
