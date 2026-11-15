// Add engine, after pairing (SRV-833): the bare engine is the preselected mode, and while the
// engine serves no model the user picks one and sees its install: the plan first (how much it
// downloads, or that the weights are on its disk already), then the job's step, the download's GB
// so far and the steps done. Everything goes through the engine's own tools on its host; nothing
// there is edited by hand. The engine restarts inside the job, so a poll that fails then is not
// the job's end. Kept free of the DOM: the page (views/fleet/engine/after.ts) draws `state`.

import type { EngineFinding, EngineModel, EngineView, EnginesApi, JobProgress, ModeView, PullPlan } from '../api/engines';
import { findingOf } from './engine-words';

/** how often the page reads a running job */
export const PROGRESS_EVERY_MS = 2000;

export type AfterPhase =
  | 'load'
  /** the engine host's status cannot be read: its finding says why */
  | 'unreadable'
  | 'pick'
  | 'installing'
  | 'failed'
  /** it serves a model already: only the mode is left to accept */
  | 'serving'
  | 'ready';

export interface AfterState {
  id: string;
  engine: EngineView | null;
  phase: AfterPhase;
  /** what stops the phase: the load, the list, the start */
  error: EngineFinding | null;
  models: EngineModel[] | null;
  picked: string | null;
  plan: PullPlan | null;
  planning: boolean;
  planError: EngineFinding | null;
  /** the mode call's answer, once it ran */
  mode: ModeView | null;
  busy: 'start' | 'accept' | null;
  job: string | null;
  /** the id of the model the job installs */
  jobModel: string | null;
  progress: JobProgress | null;
  /** a poll that failed while the job runs (the engine restarts inside it) */
  progressError: EngineFinding | null;
}

/** the model the list starts on: the recommended one, else the first */
export const defaultModel = (models: EngineModel[]): string | null => (models.find((m) => m.recommended) ?? models[0])?.id ?? null;

export const modelName = (models: EngineModel[] | null, id: string | null): string => models?.find((m) => m.id === id)?.name ?? id ?? 'the model';

const gb = (n: number): string => `${Number.isInteger(n) ? n : n.toFixed(1)} GB`;

/** A plan in a sentence: what starting it costs. */
export function planWords(p: PullPlan): string {
  const warn = p.warnings.length ? ` ${p.warnings.map(String).join(' ')}` : '';
  if (p.on_disk) return `Its weights are on the engine host’s disk already: nothing to download.${warn}`;
  if (p.download_gb != null && p.download_gb > 0) return `${gb(p.download_gb)} to download to the engine host.${warn}`;
  return `The engine did not say how much it downloads.${warn}`;
}

export interface Bar {
  /** 0..1 */
  value: number;
  /** what the bar measures now */
  text: string;
  kind: 'download' | 'steps';
}

/** The bar: the download's share while it downloads (the long part, and the only one with a
 *  figure), else the steps done of all. */
export function barOf(p: JobProgress): Bar {
  const m = p.downloading ? /([\d.]+)\s+of\s+([\d.]+)\s*GB/i.exec(p.detail ?? '') : null;
  if (m && Number(m[2]) > 0) return { value: Math.min(1, Number(m[1]) / Number(m[2])), text: `${m[1]} of ${m[2]} GB`, kind: 'download' };
  const done = p.state === 'done' ? p.steps : p.steps_done;
  return { value: p.steps ? Math.min(1, done / p.steps) : 0, text: `${done} of ${p.steps} steps done`, kind: 'steps' };
}

/** what the job does now, in the engine's own words for the step */
export function stepWords(p: JobProgress): string {
  if (p.state === 'done') return 'Done.';
  if (p.state === 'queued') return 'Waiting to start';
  if (p.finished) return p.message ?? 'The install stopped.';
  return p.label ?? p.step ?? 'Working';
}

/** how a job ended: null while it runs */
export const endOf = (p: JobProgress | null): 'done' | 'failed' | null => (!p || !p.finished ? null : p.state === 'done' ? 'done' : 'failed');

/** How the engine host was paired, in words. */
export function viaWords(e: Pick<EngineView, 'via'>, method?: string | null): string {
  if (method === 'password') return 'with a password, once';
  if (method === 'key') return 'with a pasted key';
  return e.via === 'code' ? 'by code' : 'over SSH';
}

/** the install ended well and the engine does not serve yet */
export const STARTING: EngineFinding = { code: 'engine_loading', message: 'The install is done. The engine is starting with its model.', fix: 'Wait: a large model takes some minutes to load. This page goes on by itself once it serves.' };

const START = (id: string): AfterState => ({
  id, engine: null, phase: 'load', error: null, models: null, picked: null, plan: null, planning: false, planError: null, mode: null, busy: null,
  job: null, jobModel: null, progress: null, progressError: null,
});

export class AfterFlow {
  state: AfterState;

  constructor(
    private api: EnginesApi,
    id: string,
    private changed: () => void = () => undefined,
  ) {
    this.state = START(id);
  }

  private set(p: Partial<AfterState>): void {
    this.state = { ...this.state, ...p };
    this.changed();
  }

  /** The step opens (or "Check again"): the engine's view says what is next; a job that runs
   *  already (the page was left and opened again) is followed. `engine`: a view the pairing just
   *  answered, so the first read is saved. */
  async open(engine?: EngineView | null): Promise<void> {
    const { id } = this.state;
    this.set({ ...START(id), engine: engine ?? this.state.engine });
    let e: EngineView;
    try {
      e = engine?.wizard ? engine : await this.api.engine(id);
    } catch (err) {
      this.set({ phase: 'unreadable', error: findingOf(err) });
      return;
    }
    if (!e.wizard?.next) {
      this.set({ engine: e, phase: 'unreadable', error: e.finding ?? { code: 'error', message: 'TandemApp could not read the engine’s status.', fix: 'Check again in a moment.' } });
      return;
    }
    this.set({ engine: e });
    const running = await this.api.progress(id).catch(() => null);
    if (running?.progress && !running.progress.finished && running.job) {
      this.set({ phase: 'installing', job: running.job.id, jobModel: running.job.model ?? null, progress: running.progress });
      void this.api.models(id).then((m) => this.set({ models: m.models })).catch(() => undefined);
      return;
    }
    if (e.wizard.next === 'chat') {
      this.set({ phase: 'serving' });
      return;
    }
    this.set({ phase: 'pick' });
    await this.loadModels();
  }

  async loadModels(): Promise<void> {
    this.set({ error: null });
    try {
      const m = await this.api.models(this.state.id);
      this.set({ models: m.models });
      const first = defaultModel(m.models);
      if (first) await this.pick(first);
    } catch (err) {
      this.set({ error: findingOf(err) });
    }
  }

  /** A model is chosen: its plan is read (nothing starts). */
  async pick(model: string): Promise<void> {
    if (this.state.busy) return;
    this.set({ picked: model, plan: null, planning: true, planError: null, error: null });
    try {
      const r = await this.api.pull(this.state.id, { model, dry_run: true });
      if (this.state.picked === model) this.set({ plan: r.plan, planning: false });
    } catch (err) {
      if (this.state.picked === model) this.set({ planning: false, planError: findingOf(err) });
    }
  }

  /** The bare engine is accepted and the picked model's install starts. */
  async start(): Promise<void> {
    const { id, picked, busy, plan } = this.state;
    if (!picked || busy || !plan) return;
    this.set({ busy: 'start', error: null });
    try {
      const mode = this.state.mode ?? (await this.api.setMode(id));
      this.set({ mode });
      const r = await this.api.pull(id, { model: picked });
      this.set({ busy: null, phase: 'installing', job: r.job?.id ?? null, jobModel: picked, progress: r.progress, progressError: null });
    } catch (err) {
      this.set({ busy: null, error: findingOf(err) });
    }
  }

  /** One read of the running job. Its end: the engine's view again, which now names the model. */
  async tick(): Promise<void> {
    const { id, job, phase } = this.state;
    if (phase !== 'installing') return;
    let p: JobProgress | null;
    try {
      const r = await this.api.progress(id, job ?? undefined);
      p = r.progress;
    } catch (err) {
      if (this.state.phase === 'installing') this.set({ progressError: findingOf(err) });
      return;
    }
    if (this.state.phase !== 'installing') return;
    if (!p) {
      // no job is known any more: the engine's view says how it ended
      await this.finish();
      return;
    }
    this.set({ progress: p, progressError: null });
    const end = endOf(p);
    if (end === 'failed') this.set({ phase: 'failed' });
    else if (end === 'done') await this.finish();
  }

  private async finish(): Promise<void> {
    try {
      const e = await this.api.engine(this.state.id);
      if (e.wizard?.next === 'chat') this.set({ engine: e, phase: 'ready', progressError: null });
      // done, and the engine does not name its model yet: it still loads it; the next read asks again
      else this.set({ engine: e, progressError: e.finding ?? STARTING });
    } catch (err) {
      // the engine starts with its model: the next read finds it
      this.set({ progressError: findingOf(err) });
    }
  }

  /** A failed install: back to the list, the plan read again. */
  async again(): Promise<void> {
    const model = this.state.jobModel ?? this.state.picked;
    this.set({ phase: 'pick', job: null, progress: null, progressError: null, error: null });
    if (!this.state.models) await this.loadModels();
    else if (model) await this.pick(model);
  }

  /** It serves a model already: the bare engine is accepted, then the first message. */
  async accept(): Promise<void> {
    if (this.state.busy) return;
    this.set({ busy: 'accept', error: null });
    try {
      const mode = await this.api.setMode(this.state.id);
      this.set({ busy: null, mode, phase: 'ready' });
    } catch (err) {
      this.set({ busy: null, error: findingOf(err) });
    }
  }
}
