// What runs here (OPS-288, VIS-280): the App page's switches in words. A new TandemApp is bare --
// the gateway in front of the engines it pairs, Chat and the conductor off, no MCP server -- and
// each part is switched on here: the switch is the app scope's key of the settings interface, so a
// flip is staged and applies with the change set's plan and confirm. Kept pure so it is tested
// without a browser; the panel is views/fleet/app-parts.ts.

import type { AppParts, PartId, PartPlan } from '../api/fleet';
import type { McpServer } from '../chat/types';

export type Tone = 'ok' | 'warn' | 'muted';

export interface PartRow {
  id: PartId;
  title: string;
  on: boolean;
  badge: string;
  tone: Tone;
  /** what the part is, one line */
  why: string;
  /** what it does now */
  detail: string;
  /** the button that opens its plan */
  action: string;
  /** where the state comes from: the page's switch, or the install */
  source: string;
}

export const partTitle = (id: PartId): string => (id === 'chat' ? 'Chat' : 'Conductor');

export const isParts = (x: AppParts | PartPlan): x is AppParts => 'chat' in x && typeof (x as AppParts).chat === 'object';

function chatDetail(c: AppParts['chat']): { detail: string; tone: Tone } {
  if (!c.on) return { detail: 'Off. No chatd runs here; the Chat tab says Chat is off.', tone: 'muted' };
  if (c.runner === 'external') return { detail: `On. ${c.note ?? 'Another host runs chatd.'}`, tone: 'ok' };
  if (c.running === false) return { detail: 'On, but chatd is not running. The health page says why.', tone: 'warn' };
  if (c.sandbox === 'none') return { detail: `On. ${c.note ?? 'No sandbox runtime on this host.'}`, tone: 'warn' };
  if (c.sandbox === 'kubernetes') return { detail: 'On. Each chat’s sandbox is a pod in this namespace.', tone: 'ok' };
  return { detail: 'On. Each chat’s sandbox runs in this host’s container runtime.', tone: 'ok' };
}

/** The two switches, Chat first. */
export function partRows(p: AppParts): PartRow[] {
  const c = chatDetail(p.chat);
  const k = p.conductor;
  const src = (switched: boolean, install: string) => (switched ? 'Set on this page' : `As installed (${install})`);
  return [
    {
      id: 'chat',
      title: 'Chat',
      on: p.chat.on,
      badge: p.chat.on ? '● On' : '○ Off',
      tone: c.tone,
      why: 'The Chat tab and chatd, its service: chats, their tools and MCP servers, a sandbox per chat.',
      detail: c.detail,
      action: p.chat.on ? 'Switch Chat off…' : 'Switch Chat on…',
      source: src(p.chat.switched, `TANDEM_CHAT=${p.chat.install}`),
    },
    {
      id: 'conductor',
      title: 'Conductor',
      on: k.on,
      badge: k.on ? '● On' : '○ Off',
      tone: k.on ? 'ok' : 'muted',
      why: 'This controller’s harness in front of the backends: profiles, the router, packages, notes.',
      detail: k.on ? `On. Its documents are in ${k.dir ?? 'its folder'}.` : 'Off. Every backend is relayed as it is.',
      action: k.on ? 'Switch the conductor off…' : 'Switch the conductor on…',
      source: src(k.switched, k.install === 'off' ? 'TANDEM_CONDUCTOR=off' : 'TANDEM_CONDUCTOR set'),
    },
  ];
}

/** A staged flip in words, beside its switch (VIS-280: the switches are staged in the settings
 *  draft and apply with the change set). */
export const stagedWords = (staged: 'set' | 'unset', on: boolean): string => (staged === 'unset' ? `goes back to the install’s setting (${on ? 'on' : 'off'}) when applied` : `switches ${on ? 'on' : 'off'} when applied`);

/** The plan dialog's title. */
export const planTitle = (plan: PartPlan): string => `Switch ${plan.part === 'chat' ? 'Chat' : 'the conductor'} ${plan.on ? 'on' : 'off'}`;

/** A sentence for the page after a switch applied. */
export function doneWords(p: AppParts): string | null {
  if (!p.done) return null;
  return `${partTitle(p.done.part)} is ${p.done.on ? 'on' : 'off'}: ${p.done.said}.`;
}

// ---- connected things: backends, MCP servers, the engines' own mode ----------------------------

export type BackendAct = 'disable' | 'enable' | 'remove';

export interface BackendRow {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  /** declared in the env file (TANDEM_BACKENDS): changed there, not here */
  env: boolean;
  acts: BackendAct[];
}

export function backendRows(p: AppParts): BackendRow[] {
  return p.backends.map((b) => ({
    id: b.id,
    name: b.label || b.id,
    kind: b.kind,
    enabled: b.enabled,
    env: b.env,
    acts: b.env ? [] : [b.enabled ? 'disable' : 'enable', 'remove'],
  }));
}

/** What a backend's action changes, for its confirm. */
export function backendChanges(b: { name: string }, act: BackendAct): string[] {
  if (act === 'disable') return [`Requests for ${b.name}’s models stop going to it; a model no other backend lists answers 404.`, 'Its settings and key stay: Enable brings it back.'];
  if (act === 'enable') return [`${b.name} takes requests for its models again.`];
  return [`${b.name}, its models, its routes and its stored key are deleted from this controller.`, 'To use it again, add it on the API page.'];
}

export type McpAct = 'disable' | 'remove';

/** What an MCP server's action changes, for its confirm. */
export function mcpChanges(m: Pick<McpServer, 'name'>, act: McpAct): string[] {
  if (act === 'disable') return [`${m.name} is no longer on in every chat; a chat that turned it on itself keeps it.`, 'Its settings stay.'];
  return [`${m.name} and its settings are removed from Chat; the chats that used it lose its tools.`];
}

export const ENGINE_MODE_NOTE = 'An engine’s own chat mode (its conductor and chat extension inside the engine) is the engine’s setting, not this controller’s.';

/** What an engine host's mode switch changes. */
export function engineModeChanges(alias: string, to: 'engine' | 'chat'): string[] {
  return [
    to === 'engine' ? `${alias} serves its models bare: no conductor inside the engine.` : `${alias} runs its own conductor and chat extension in front of its model.`,
    `${alias} restarts once, when it is idle.`,
  ];
}
