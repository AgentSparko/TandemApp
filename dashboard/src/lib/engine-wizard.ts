// The setup wizard (VIS-278): the first page of a TandemApp with no engine, and Settings → Add
// engine afterwards. Four steps: Add engine (what the LAN advertises, or an address), Pair (the
// code the engine host printed; the other ways stand in for it), Model (the bare engine
// preselected, a model with its download) and First message (handed to the Chat tab). Where the
// page is comes from its route: #/add-engine is the first step, #/add-engine/<engine host> the
// steps after pairing, so a reload or another device opens them again. Kept pure so it can be
// tested; the page is views/fleet/engine-wizard.ts.

export type WizardStep = 'find' | 'pair' | 'model' | 'chat';
export const WIZARD_STEPS: WizardStep[] = ['find', 'pair', 'model', 'chat'];
export const STEP_LABEL: Record<WizardStep, string> = { find: 'Add engine', pair: 'Pair', model: 'Model', chat: 'First message' };

/** what the page shows inside a step */
export type Screen = 'find' | 'pair' | 'connect' | 'after' | 'chat';

export const stepOf = (screen: Screen): WizardStep => (screen === 'connect' ? 'pair' : screen === 'after' ? 'model' : screen);

export interface StepView {
  id: WizardStep;
  label: string;
  n: number;
  state: 'done' | 'on' | 'todo';
}

/** The stepper: the steps before the one shown are done. */
export function stepper(screen: Screen): StepView[] {
  const at = WIZARD_STEPS.indexOf(stepOf(screen));
  return WIZARD_STEPS.map((id, i) => ({ id, label: STEP_LABEL[id], n: i + 1, state: i < at ? 'done' : i === at ? 'on' : 'todo' }));
}

/** A TandemApp with no member yet: the wizard is its setup, not one page among others. */
export const firstRun = (nodes: readonly unknown[] | null | undefined): boolean => !nodes || nodes.length === 0;

/** The page's head, by whether anything is in the fleet. */
export function headOf(first: boolean): { title: string; sub: string } {
  return first
    ? { title: 'Set up TandemApp', sub: 'Four steps to a first answer: find the machine that runs the TandemLLM engine, pair it with the code it printed, give it a model, and send a message.' }
    : { title: 'Add engine', sub: 'Pair another machine that runs the TandemLLM engine: find it, type the code it printed, give it a model.' };
}

/** what the last step's field starts with; the route that carries it to Chat is lib/router.ts
 *  (`chatHash`, `firstMessage`) */
export const FIRST_DEFAULT = 'Hello! Tell me in two sentences what you can do.';

/** The route of the steps after pairing for one engine host. */
export const engineHash = (id: string): string => `#/add-engine/${encodeURIComponent(id)}`;
