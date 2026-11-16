// A plugin's capabilities in words (VIS-284, contracts/plugin-v1/capabilities.json): what each one
// lets a plugin do, what that means and never means, and how much it risks; the same for where a
// plugin asks to run. The plugins pages draw them, and so does the settings area's review sheet
// when a plan holds a grant that is asked a second time. Kept free of the DOM.

import type { Trust } from '../api/plugins';
import type { Tone } from './settings-words';

/** low: nothing of a prompt leaves; elevated: content or a way out, within bounds; high: prompts
 *  as they are, the engine, or a host outside the LAN: the grant is asked a second time */
export type Risk = 'low' | 'elevated' | 'high';

export interface CapWords {
  capability: string;
  kind: 'prompts' | 'network' | 'settings' | 'engine' | 'secrets' | 'other';
  /** what it lets the plugin do, as a line */
  title: string;
  /** what that means, and what it never means */
  text: string;
  risk: Risk;
  /** granting it asks a second time */
  confirm: boolean;
  /** a network capability's host, a secret's name */
  subject: string | null;
}

export const RISK_WORDS: Record<Risk, { label: string; tone: Tone; text: string }> = {
  low: { label: 'Low risk', tone: 'muted', text: 'No prompt and no answer leaves TandemApp with it.' },
  elevated: { label: 'Some risk', tone: 'warn', text: 'It reads content or reaches another machine, within the limits named.' },
  high: { label: 'High risk', tone: 'err', text: 'Granting it is asked a second time.' },
};
const RISK_RANK: Record<Risk, number> = { high: 0, elevated: 1, low: 2 };

const LAN_SUFFIX = ['.local', '.lan', '.home.arpa', '.internal'];

/** `network:host[:port]` -> the host */
export function networkHost(capability: string): string {
  const rest = capability.split(':').slice(1).join(':');
  return /:\d+$/.test(rest) ? rest.replace(/:\d+$/, '') : rest;
}

/** Whether a host reads as one on the LAN: a private or link-local IPv4 address, a name without a
 *  dot, or one of the LAN's suffixes. The controller decides (it resolves the name): an inspect's
 *  answer and a plan say which grants ask twice; this is what the page says before either. */
export function looksLan(host: string): boolean {
  const h = host.toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
  if (!h) return false;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (m) {
    const [a, b] = [Number(m[1]), Number(m[2])];
    return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
  }
  return h === 'localhost' || !h.includes('.') || LAN_SUFFIX.some((s) => h.endsWith(s));
}

/** A capability in plain words. `confirm`: the capabilities the controller said ask twice (an
 *  inspect's answer); without it the page's own reading of the host decides. */
export function capWords(capability: string, confirm: string[] | null = null): CapWords {
  const asks = (own: boolean): boolean => (confirm ? confirm.includes(capability) : own);
  const [head] = capability.split(':');
  const rest = capability.slice(head.length + 1);
  const base = { capability, subject: null as string | null };
  if (capability === 'prompts:meta')
    return { ...base, kind: 'prompts', title: 'See the facts of each request', text: 'Ids, the model, sizes, counts, timings, the class and tool names. Never a word of a prompt or an answer.', risk: 'low', confirm: asks(false) };
  if (capability === 'prompts:redacted')
    return { ...base, kind: 'prompts', title: 'Read prompts and answers after redaction', text: 'The facts, plus the content once the conductor’s redaction took out what it masks. What the redaction misses, the plugin reads.', risk: 'elevated', confirm: asks(false) };
  if (capability === 'prompts:full')
    return { ...base, kind: 'prompts', title: 'Read every prompt and answer as it is', text: 'Nothing is masked: whatever a user types and whatever a model answers reaches the plugin’s code.', risk: 'high', confirm: asks(true) };
  if (head === 'network') {
    const host = networkHost(capability);
    const lan = confirm ? !confirm.includes(capability) : looksLan(host);
    return {
      ...base, kind: 'network', subject: rest, title: `Connect to ${rest}`,
      text: lan ? 'A machine on the LAN. Its sandbox reaches the hosts you grant and nothing else.' : 'A machine outside the LAN: what the plugin holds can leave your network to this host. Its sandbox reaches the hosts you grant and nothing else.',
      risk: lan ? 'elevated' : 'high', confirm: asks(!lan),
    };
  }
  if (capability === 'settings:read')
    return { ...base, kind: 'settings', title: 'Read TandemApp’s settings', text: 'The values of the settings interface. Never a secret, a key or a password.', risk: 'low', confirm: asks(false) };
  if (capability === 'engine:control')
    return { ...base, kind: 'engine', title: 'Restart the engine or change its mode', text: 'Through the controller. Not available to plugins yet: the plugin host refuses it even when it is granted.', risk: 'high', confirm: asks(true) };
  if (head === 'secrets')
    return { ...base, kind: 'secrets', subject: rest, title: `Get its secret ${rest}`, text: `The value you store for ${rest} is passed to the plugin’s process. It never gets another plugin’s secret, a pairing key or a backend’s key.`, risk: 'low', confirm: asks(false) };
  return { ...base, kind: 'other', title: capability, text: 'This page has no words for this capability. Read the plugin’s documentation before you grant it.', risk: 'elevated', confirm: asks(true) };
}

/** The capabilities a plugin asks for, the riskiest first; ties keep the manifest's order. */
export function capList(requested: string[], confirm: string[] | null = null): CapWords[] {
  return requested.map((c, i) => ({ w: capWords(c, confirm), i })).sort((a, b) => RISK_RANK[a.w.risk] - RISK_RANK[b.w.risk] || a.i - b.i).map((x) => x.w);
}

export const NEVER = 'Whatever it asks for, a plugin never reads the config database, another plugin’s secrets, pairing keys or backend keys.';

export const TRUST_WORDS: Record<Trust, { label: string; text: string; risk: Risk }> = {
  sandboxed: { label: 'Sandboxed', text: 'Runs in a container with a read-only root, no devices and no network but the hosts you grant.', risk: 'low' },
  host: { label: 'On the host', text: 'Runs as a process of the app’s user, outside any sandbox: it can read and write what that user can. It does not start until you allow it.', risk: 'high' },
  engine: { label: 'Inside the engine', text: 'A wheel loaded into the engine. Not available yet: TandemApp refuses to run it.', risk: 'high' },
};

/** A grant's key of the plugins scope (`capabilities.<capability>` or `trust`) in words. */
export function grantLine(key: string): { title: string; text: string } {
  if (key === 'trust') return { title: 'Run on the host, outside any sandbox', text: TRUST_WORDS.host.text.replace(' It does not start until you allow it.', '') };
  const w = capWords(key.replace(/^capabilities\./, ''), null);
  return { title: w.title, text: w.text };
}
