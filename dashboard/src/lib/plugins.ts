// The plugins pages in words (VIS-284): what a capability lets a plugin do and how much that
// risks, an install's checks in the order they run with each refusal in plain words, a plugin's
// state as the plugin host tells it, where it came from, what its signature says, what it adds to
// TandemApp, and every refusal of the plugins' API as a sentence and what to do about it. Also the
// install form's own checks, and a settings form's values held against the fields the form
// generator made of the plugin's schema (lib/schema-form.ts). Kept free of the DOM so each
// sentence is tested by itself.

import type { CheckError, ContribKind, Inspection, Installed, Manifest, Plugin, PluginError, PluginFull, Run, RunState, Signature, Source, Status, Trust } from '../api/plugins';
import type { FormField } from './schema-form';
import type { Tone } from './settings-words';

export const PLUGINS_SCOPE = 'plugins' as const;
export { NEVER, RISK_WORDS, TRUST_WORDS, capList, capWords, looksLan, networkHost, type CapWords, type Risk } from './plugin-caps';

/** the pages below #/app/plugins: the list, `install`, `p/<id>`, `p/<id>/panel/<name>` */
export function pluginsHref(parts: string[] = [], q?: Record<string, string>): string {
  const s = q ? new URLSearchParams(q).toString() : '';
  return `#/app/plugins${parts.map((p) => `/${encodeURIComponent(p)}`).join('')}${s ? `?${s}` : ''}`;
}

export const nameOf = (p: Pick<Plugin, 'title' | 'name'>): string => p.title || p.name;
const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;
const cap1 = (s: string): string => `${s.charAt(0).toUpperCase()}${s.slice(1)}`;
/** the controller's own words as a sentence */
export const said = (m: string): string => `${cap1(m)}${/[.!?]$/.test(m) ? '' : '.'}`;

/** `1.2 MB`, `340 KB`, `12 bytes` */
export function bytes(n: number): string {
  if (n < 1024) return plural(n, 'byte');
  if (n < 1048576) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/** `16 Nov, 09:00:12` in the reader's own time, from milliseconds */
export const at = (t: number | null | undefined): string => (t ? new Date(t).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'not kept');

/** a sha256 in a row: its first twelve digits */
export const shortSha = (s: string | null | undefined): string => (s ? s.slice(0, 12) : 'none');

// ---- capabilities: lib/plugin-caps.ts (the review sheet words a risky grant with the same) ---------

/** `host, not allowed yet`, `sandboxed` */
export function trustLine(t: Plugin['trust']): { text: string; tone: Tone } {
  if (t.requested === 'sandboxed') return { text: 'sandboxed', tone: 'muted' };
  if (t.requested === 'engine') return { text: 'inside the engine: refused', tone: 'err' };
  return t.granted === 'host' ? { text: 'on the host', tone: 'warn' } : { text: 'asks for the host: not allowed yet', tone: 'warn' };
}

// ---- where it came from, what its files say -------------------------------------------------------

export function sourceWords(s: Source): { kind: string; where: string; pin: string | null } {
  if (s.type === 'git') return { kind: 'Git commit', where: `${s.url}${s.subdir ? ` (${s.subdir})` : ''}`, pin: s.commit };
  if (s.type === 'wheel') return { kind: 'Wheel', where: s.path, pin: s.archive_sha256 ?? null };
  return { kind: 'Folder', where: s.path, pin: null };
}

export function signatureWords(s: Signature | null): { tone: Tone; label: string; text: string } {
  if (!s || !s.present) return { tone: 'muted', label: 'not signed', text: 'It carries no signature. The sha256 of its files is what TandemApp holds it to.' };
  if (!s.verified) return { tone: 'err', label: 'signature does not hold', text: said(s.error ?? 'its signature does not match its files') };
  const key = s.key_id ? `key ${s.key_id}` : 'its key';
  if (s.trusted === true) return { tone: 'ok', label: 'signed, key trusted', text: `Signed with ${key}, a key this TandemApp trusts.` };
  if (s.trusted === false) return { tone: 'warn', label: 'signed, key not trusted', text: `The signature holds for ${key}, which this TandemApp does not trust.` };
  return { tone: 'ok', label: 'signed', text: `The signature holds for ${key}. This version does not decide which keys to trust: check that the key is the author’s.` };
}

export function integrityWords(p: Pick<Plugin, 'integrity' | 'sha256'>): { tone: Tone; label: string; text: string } {
  if (p.integrity === 'ok') return { tone: 'ok', label: 'files unchanged', text: 'Its files are the ones that were installed.' };
  if (p.integrity === 'missing') return { tone: 'err', label: 'files missing', text: 'Its folder is gone from the controller’s disk. It does not load until it is installed again.' };
  return { tone: 'err', label: 'files changed', text: 'Its files differ from the ones that were installed. It does not load until it is installed again.' };
}

// ---- a plugin's state -----------------------------------------------------------------------------

export interface StateWords {
  tone: Tone;
  label: string;
  text: string;
}

const RUNTIME: Record<string, string> = { docker: 'a container', kubernetes: 'a pod', host: 'a process on the host' };

/** A plugin's state as the plugin host tells it: its status, or the run state the list carries
 *  (without where it runs). `now`: the page's clock, for the next start. */
export function stateWords(s: Run & Partial<Pick<Status, 'runtime' | 'where'>>, now: number = Date.now()): StateWords {
  const wait = s.next_start_at && s.next_start_at > now ? Math.ceil((s.next_start_at - now) / 1000) : null;
  switch (s.state) {
    case 'running':
      return { tone: 'ok', label: 'running', text: `Runs as ${RUNTIME[s.runtime ?? ''] ?? 'a process'}${s.where ? ` (${s.where})` : ''}. What it adds is registered.` };
    case 'starting':
      return { tone: 'live', label: 'starting', text: 'The plugin host started it and waits for its first answer, at most 20 seconds.' };
    case 'stopping':
      return { tone: 'live', label: 'stopping', text: 'Its process is ending. What it added is gone already.' };
    case 'backoff':
      return { tone: 'warn', label: 'restarting', text: `It exited or did not answer. The plugin host starts it again${wait ? ` in ${wait} s` : ' shortly'}; after five failures within an hour it gives up.` };
    case 'stopped':
      return s.held ? { tone: 'muted', label: 'stopped', text: 'Stopped by hand. It stays stopped until you start it.' } : { tone: 'muted', label: 'stopped', text: 'It is on, and the plugin host has not started it yet.' };
    case 'unavailable':
      return { tone: 'warn', label: 'unavailable', text: 'This machine has no sandbox runtime (Docker, Podman or Kubernetes), so a sandboxed plugin cannot start. The plugin host looks again every 30 seconds.' };
    case 'refused':
      return { tone: 'err', label: 'refused', text: s.last_error ? said(s.last_error) : 'It may not start as it is: its trust level, its files or its compatibility stand in the way.' };
    case 'failed':
      return { tone: 'err', label: 'failed', text: `It failed ${s.gave_up?.failures ?? 5} times within an hour, so the plugin host gave up and turned it off. Read its log, then turn it on again.` };
    default:
      return { tone: 'muted', label: 'off', text: 'It is turned off: its code does not run and it adds nothing.' };
  }
}

/** A list row's state: the run state the list carries, or what is known without one (no plugin
 *  host runs, or a kind it does not run). */
export function listState(p: Pick<Plugin, 'kind' | 'enabled' | 'integrity'>, s: Run | null | undefined, now: number = Date.now()): StateWords {
  if (p.kind === 'engine-stats') return { tone: 'muted', label: 'recorded', text: 'An engine loads this package itself. TandemApp records it and does not run it.' };
  if (s) return stateWords(s, now);
  return p.enabled ? { tone: 'muted', label: 'on', text: 'It is on. Its state could not be read.' } : { tone: 'muted', label: 'off', text: 'It is turned off.' };
}

/** what each control does, and whether it can go now */
export function controls(p: Pick<Plugin, 'enabled' | 'kind'>, s: Status | null): { start: string | null; stop: string | null; restart: string | null } {
  if (p.kind !== 'plugin') return { start: 'TandemApp does not run this kind.', stop: 'TandemApp does not run this kind.', restart: 'TandemApp does not run this kind.' };
  const st = s?.state ?? (p.enabled ? 'stopped' : 'disabled');
  const live = st === 'running' || st === 'starting' || st === 'backoff' || st === 'unavailable';
  return {
    start: !p.enabled ? 'Turn it on first.' : live ? 'It runs already.' : null,
    stop: live ? null : 'It does not run.',
    restart: st === 'running' ? null : 'It does not run.',
  };
}

// ---- what it adds ---------------------------------------------------------------------------------

export interface Contribution {
  kind: ContribKind;
  name: string;
  /** what it is, in a line */
  text: string;
  /** small marks beside the name: a tool that only reads, a hook's payload level */
  marks: { text: string; tone: Tone }[];
  /** registered by the plugin host right now; null when the status does not say */
  live: boolean | null;
  /** where TandemApp shows it */
  href: string | null;
  link: string | null;
}

export const CONTRIB_WORDS: Record<ContribKind, { one: string; many: string; text: string }> = {
  tools: { one: 'tool', many: 'tools', text: 'Offered to the Chat tab’s models as MCP tools. A model asks before it calls one.' },
  hooks: { one: 'hook', many: 'hooks', text: 'Run on a request’s way through TandemApp, at priority 100 until a hook of kind plugin names one and sets its own.' },
  decision_providers: { one: 'decision provider', many: 'decision providers', text: 'Answer the conductor’s questions. Each starts in shadow: asked beside the request, journaled, never used.' },
  backends: { one: 'backend', many: 'backends', text: 'Servers behind the one API: requests the gateway routes there go to the plugin.' },
  panels: { one: 'panel', many: 'panels', text: 'Pages of the plugin’s own. Each opens in a sandboxed frame: it reads no cookie and no route of TandemApp’s, and asks it only through a bridge that the plugin’s capabilities bound.' },
  skills: { one: 'skill', many: 'skills', text: 'Skill folders the conductor reads beside its own.' },
  engine_hooks: { one: 'engine hook', many: 'engine hooks', text: 'Code inside the engine. Not available yet.' },
};
export const CONTRIB_ORDER: ContribKind[] = ['tools', 'hooks', 'decision_providers', 'backends', 'panels', 'skills', 'engine_hooks'];

const PAYLOAD: Record<string, string> = { meta: 'facts only', redacted: 'redacted content', full: 'full content' };

/** What a manifest contributes, each with where TandemApp shows it. `id`: the installed plugin's
 *  (null for an inspect's preview: nothing is registered, nothing links); `status`: what is
 *  registered right now. */
export function contributions(m: Manifest | null, id: string | null = null, status: Status | null = null): Contribution[] {
  const c = m?.contributes ?? {};
  const out: Contribution[] = [];
  const on = (list: string[] | undefined, name: string): boolean | null => (status ? (status.state === 'running' ? !!list?.includes(name) : false) : null);
  for (const t of c.tools ?? []) {
    const a = t.annotations ?? {};
    const marks: Contribution['marks'] = [a.readOnlyHint ? { text: 'only reads', tone: 'muted' } : a.destructiveHint ? { text: 'can delete or overwrite', tone: 'err' } : { text: 'changes things', tone: 'warn' }];
    if (a.openWorldHint) marks.push({ text: 'reaches outside', tone: 'warn' });
    out.push({ kind: 'tools', name: t.name, text: t.description ?? t.title ?? '', marks, live: on(status?.tools, t.name), href: id ? '#/chat/settings/mcp' : null, link: 'Chat’s MCP servers' });
  }
  for (const h of c.hooks ?? [])
    out.push({
      kind: 'hooks', name: h.name, text: `${cap1(h.role)} at ${h.stages.join(', ')}${h.description ? `: ${h.description}` : ''}`,
      marks: [{ text: PAYLOAD[h.payload ?? 'meta'] ?? h.payload ?? 'facts only', tone: h.payload === 'full' ? 'err' : h.payload === 'redacted' ? 'warn' : 'muted' }],
      live: on(status?.hooks, `${id}/${h.name}`), href: id ? '#/app/hooks' : null, link: 'The pipeline',
    });
  for (const p of c.decision_providers ?? [])
    out.push({ kind: 'decision_providers', name: p.name, text: `Answers ${p.questions.join(', ')}${p.description ? `: ${p.description}` : ''}`, marks: [], live: on(status?.providers, `${id}/${p.name}`), href: id ? '#/app/hooks/providers' : null, link: 'Decision providers' });
  for (const b of c.backends ?? [])
    out.push({ kind: 'backends', name: b.name, text: `${b.description ? `${said(b.description)} ` : ''}${b.models?.length ? `Models: ${b.models.join(', ')}` : 'No model listed'}`, marks: [], live: on(status?.backends, b.name), href: id ? '#/api/backends' : null, link: 'Backends' });
  for (const p of c.panels ?? []) out.push({ kind: 'panels', name: p.name, text: p.title, marks: [], live: on(status?.panels, p.name), href: id ? pluginsHref(['p', id, 'panel', p.name]) : null, link: 'Open the panel' });
  for (const s of c.skills ?? []) out.push({ kind: 'skills', name: s, text: 'A folder of skills', marks: [], live: status ? status.state === 'running' && status.skills.some((x) => x === s || x.startsWith(`${s}/`)) : null, href: null, link: null });
  for (const e of c.engine_hooks ?? []) out.push({ kind: 'engine_hooks', name: e.name, text: `Would hook ${e.hooks.join(', ')} inside the engine`, marks: [{ text: 'not available yet', tone: 'err' }], live: status ? false : null, href: null, link: null });
  return out;
}

/** `2 tools, 1 hook` from a list row's counts; `nothing yet` for none */
export function contribLine(c: Plugin['contributes']): string {
  const parts = CONTRIB_ORDER.filter((k) => (c[k] ?? 0) > 0).map((k) => plural(c[k]!, CONTRIB_WORDS[k].one, CONTRIB_WORDS[k].many));
  return parts.length ? parts.join(', ') : 'adds nothing';
}

// ---- an install's checks --------------------------------------------------------------------------

export type CheckState = 'pass' | 'refused' | 'skipped';

export interface CheckGroup {
  id: string;
  title: string;
  /** what holds when it passes */
  pass: string;
  codes: string[];
  /** it runs only once the manifest fits its schema */
  needsManifest: boolean;
}

/** The checks in the order the controller runs them (proxy/pluginmanifest.py `inspect`): the tree,
 *  its signature, the manifest, then what the manifest says. */
export const CHECK_GROUPS: CheckGroup[] = [
  { id: 'files', title: 'Its files are plain files within the limits', pass: 'No symlink, no device, at most 5000 files and 64 MiB.', codes: ['symlink', 'special_file', 'bad_name', 'too_large'], needsManifest: false },
  { id: 'signature', title: 'Its signature, when it has one, holds', pass: 'Nothing in the tree contradicts its signature.', codes: ['signature_invalid'], needsManifest: false },
  { id: 'manifest', title: 'Its manifest is a valid plugin.json', pass: 'Every field is one plugin-v1 knows, with a value it takes.', codes: ['manifest_missing', 'manifest_too_large', 'manifest_invalid_json', 'missing_field', 'unknown_field', 'bad_value', 'kind_rules'], needsManifest: false },
  { id: 'tools', title: 'Its tools say what they do and take strict input', pass: 'Every tool states its four annotations and refuses arguments outside its schema.', codes: ['annotation_missing', 'extra_properties', 'tool_schema'], needsManifest: true },
  { id: 'contributions', title: 'What it adds is named once, at real places', pass: 'No name twice; hooks at stages that exist with a role the stage takes.', codes: ['duplicate_name', 'stage_unknown', 'role_not_allowed'], needsManifest: true },
  { id: 'paths', title: 'Its paths stay inside the plugin', pass: 'Every file the manifest names is in the plugin.', codes: ['path_outside', 'path_missing'], needsManifest: true },
  { id: 'asks', title: 'It asks for everything it uses', pass: 'Each hook’s payload, each provider and each secret is covered by a capability it requests.', codes: ['capability_missing', 'secret_undeclared', 'trust_mismatch'], needsManifest: true },
  { id: 'settings', title: 'Its settings can be drawn as a form', pass: 'The settings schema uses only what a form reads, and holds no secret.', codes: ['settings_schema'], needsManifest: true },
  { id: 'compat', title: 'It runs on this TandemApp', pass: 'This TandemApp is in its range, and its API versions are not newer than this controller’s.', codes: ['incompatible_tandemapp', 'incompatible_hooks_api', 'incompatible_plugin_api'], needsManifest: true },
];

/** a check's refusal in plain words: what is wrong, and what the author changes */
const CHECK_WORDS: Record<string, (e: CheckError) => { title: string; fix: string }> = {
  manifest_missing: () => ({ title: 'There is no plugin.json at the plugin’s root.', fix: 'Point at the folder that holds plugin.json, or add one.' }),
  manifest_too_large: () => ({ title: 'plugin.json is larger than 256 KiB.', fix: 'Move data out of the manifest into the plugin’s own files.' }),
  manifest_invalid_json: () => ({ title: 'plugin.json is not valid JSON.', fix: 'Fix the syntax error the sentence below names.' }),
  missing_field: (e) => ({ title: `A required field is missing${e.field ? `: ${e.field}` : ''}.`, fix: 'Add the field to plugin.json.' }),
  unknown_field: (e) => ({ title: `plugin.json has a field plugin-v1 does not know${e.field ? `: ${e.field}` : ''}.`, fix: 'Remove it or correct its name. An unknown field is refused, never ignored.' }),
  bad_value: (e) => ({ title: `A field holds a value it does not take${e.field ? `: ${e.field}` : ''}.`, fix: 'Correct the value as the sentence below says.' }),
  kind_rules: (e) => ({ title: `A field does not belong to this kind of plugin${e.field ? `: ${e.field}` : ''}.`, fix: 'Remove the field, or change the plugin’s kind.' }),
  duplicate_name: (e) => ({ title: `Two things share one name${e.name ? `: ${e.name}` : ''}.`, fix: 'Give each tool, hook, provider and secret a name of its own.' }),
  annotation_missing: (e) => ({ title: `The tool ${e.tool ?? 'named below'} does not say ${e.annotation ?? 'one of its four annotations'}.`, fix: 'State readOnlyHint, destructiveHint, idempotentHint and openWorldHint as true or false on every tool, so a user knows what a call can do.' }),
  extra_properties: (e) => ({ title: `The tool ${e.tool ?? 'named below'} would take arguments outside its schema.`, fix: 'Say "additionalProperties": false on every object of its input schema, nested ones included.' }),
  tool_schema: (e) => ({ title: `The input schema of the tool ${e.tool ?? 'named below'} is not an object.`, fix: 'A tool’s inputSchema is of type object.' }),
  stage_unknown: (e) => ({ title: `A hook names a stage TandemApp does not have${e.stage ? `: ${e.stage}` : ''}.`, fix: 'Use a stage of hooks-v1.' }),
  role_not_allowed: (e) => ({ title: `A hook’s role cannot run at its stage${e.stage ? ` (${e.stage})` : ''}.`, fix: 'Pick a role the stage takes, or another stage.' }),
  capability_missing: (e) => ({ title: `It uses more than it asks for${e.capability ? `: ${e.capability} is not requested` : ''}.`, fix: 'Add the capability to the manifest’s capabilities, so the admin sees it and can grant it.' }),
  secret_undeclared: (e) => ({ title: `It asks for a secret it does not declare${e.name ? `: ${e.name}` : ''}.`, fix: 'List the secret under secrets in plugin.json.' }),
  trust_mismatch: () => ({ title: 'It brings code for the engine without asking to run inside the engine.', fix: 'engine_hooks need "trust": "engine".' }),
  path_outside: (e) => ({ title: `A path leaves the plugin’s folder${e.field ? `: ${e.field}` : ''}.`, fix: 'Name only files inside the plugin.' }),
  path_missing: (e) => ({ title: `A file the manifest names is not in the plugin${e.field ? `: ${e.field}` : ''}.`, fix: 'Add the file, or correct its path.' }),
  settings_schema: () => ({ title: 'Its settings schema is not one a form can draw.', fix: 'Use type object with "additionalProperties": false, only the keywords a form reads, defaults that hold, and no secret in it.' }),
  signature_invalid: () => ({ title: 'Its signature does not match its files.', fix: 'The files changed after they were signed, or the signature is another plugin’s. Get the plugin from its author again.' }),
  incompatible_tandemapp: (e) => ({ title: `It is written for another TandemApp${e.needed ? `: it needs ${e.needed}` : ''}${e.have ? `, this is ${e.have}` : ''}.`, fix: 'Install a version of the plugin for this TandemApp, or update TandemApp.' }),
  incompatible_hooks_api: (e) => ({ title: `It speaks a newer hooks API${e.needed ? ` (${e.needed})` : ''} than this controller${e.have ? ` (${e.have})` : ''}.`, fix: 'Update TandemApp, or install an older version of the plugin.' }),
  incompatible_plugin_api: (e) => ({ title: `Its manifest is written for a newer plugin API${e.needed ? ` (${e.needed})` : ''} than this controller’s${e.have ? ` (${e.have})` : ''}.`, fix: 'Update TandemApp, or install an older version of the plugin.' }),
  symlink: () => ({ title: 'There is a symlink among its files.', fix: 'A plugin holds plain files only: replace the link with the file.' }),
  special_file: () => ({ title: 'There is a device, fifo or socket among its files.', fix: 'A plugin holds plain files only.' }),
  bad_name: () => ({ title: 'A file name holds a control character.', fix: 'Rename the file.' }),
  too_large: () => ({ title: 'It is over 5000 files or 64 MiB.', fix: 'Leave build output, caches and data out of the plugin.' }),
};

export interface Problem {
  code: string;
  field: string | null;
  title: string;
  fix: string;
  /** the controller's sentence, as it came */
  message: string;
}

export function problemOf(e: CheckError): Problem {
  const w = CHECK_WORDS[e.code]?.(e) ?? { title: said(e.message), fix: 'Correct the manifest, then check it again.' };
  return { code: e.code, field: e.field ?? null, title: w.title, fix: w.fix, message: e.message };
}

export interface CheckRow {
  group: CheckGroup;
  state: CheckState;
  problems: Problem[];
  /** under a passed check: what held, or what there was to check */
  note: string;
}

/** Every check with how it ended. A refusal of the tree ends the checks there; a manifest that
 *  does not fit its schema leaves what it says unchecked, and each of those reads `skipped`. */
export function checkReport(r: Pick<Inspection, 'errors' | 'manifest' | 'signature' | 'sha256'>): { rows: CheckRow[]; passed: number; refused: number; skipped: number; other: Problem[] } {
  const known = new Set(CHECK_GROUPS.flatMap((g) => g.codes));
  const by = (g: CheckGroup) => r.errors.filter((e) => g.codes.includes(e.code));
  const treeBad = by(CHECK_GROUPS[0]).length > 0;
  const manifestBad = by(CHECK_GROUPS[2]).length > 0 || r.manifest === null;
  const rows = CHECK_GROUPS.map((group): CheckRow => {
    const errs = by(group);
    if (errs.length) return { group, state: 'refused', problems: errs.map(problemOf), note: '' };
    if (group.id !== 'files' && treeBad) return { group, state: 'skipped', problems: [], note: 'Not checked: the files were refused before it.' };
    if (group.needsManifest && manifestBad) return { group, state: 'skipped', problems: [], note: 'Not checked: it needs a valid manifest first.' };
    if (group.id === 'signature') return { group, state: 'pass', problems: [], note: r.signature?.present ? signatureWords(r.signature).text : 'It carries no signature, so there is none to check.' };
    return { group, state: 'pass', problems: [], note: group.pass };
  });
  return { rows, passed: rows.filter((x) => x.state === 'pass').length, refused: rows.filter((x) => x.state === 'refused').length, skipped: rows.filter((x) => x.state === 'skipped').length, other: r.errors.filter((e) => !known.has(e.code)).map(problemOf) };
}

/** What an inspect's answer means for the install button. */
export function inspectHead(r: Inspection): { tone: Tone; title: string; text: string } {
  const n = r.errors.length;
  const name = r.manifest?.name ? `${r.manifest.title || r.manifest.name} ${r.manifest.version ?? ''}`.trim() : 'This source';
  if (!r.ok) return { tone: 'err', title: `${name} cannot be installed`, text: `${n === 1 ? 'One check refused it' : `${n} checks refused it`}. Nothing was installed and nothing in it ran. The reasons are below, each with what to change.` };
  if (r.installed && r.installed.version === r.manifest?.version) return r.installed.sha256 === r.sha256 ? { tone: 'muted', title: `${name} is installed already`, text: 'The same version with the same files. Installing it again changes nothing.' } : { tone: 'err', title: `${name} is installed with other files`, text: 'A changed plugin carries a new version: raise the version in plugin.json, then check it again.' };
  if (r.installed) return { tone: 'ok', title: `${name} passed every check`, text: `It replaces the installed version ${r.installed.version}. Nothing in it has run.` };
  return { tone: 'ok', title: `${name} passed every check`, text: 'Nothing is installed yet and nothing in it has run. Read what it asks for before you install it.' };
}

/** how the version offered stands to the one installed */
export function versionMove(r: Pick<Inspection, 'installed' | 'manifest'>): 'new' | 'same' | 'upgrade' | 'downgrade' {
  const have = r.installed?.version;
  const next = r.manifest?.version;
  if (!have || !next) return 'new';
  if (have === next) return 'same';
  return compareVersions(next, have) > 0 ? 'upgrade' : 'downgrade';
}

/** MAJOR.MINOR.PATCH with an optional -pre; a pre-release sorts before its release */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string): [number[], string | null] => {
    const [core, ...pre] = v.split('-');
    return [core.split('.').map((x) => Number(x) || 0), pre.length ? pre.join('-') : null];
  };
  const [ca, pa] = parse(a);
  const [cb, pb] = parse(b);
  for (let i = 0; i < 3; i++) if ((ca[i] ?? 0) !== (cb[i] ?? 0)) return (ca[i] ?? 0) - (cb[i] ?? 0);
  if (pa === pb) return 0;
  if (pa === null) return 1;
  if (pb === null) return -1;
  return pa < pb ? -1 : 1;
}

/** What an install did, as the page says it. */
export function installedWords(r: Installed): { title: string; text: string; kept: string[]; dropped: string[] } {
  const name = `${nameOf(r.plugin)} ${r.plugin.version}`;
  if (r.action === 'unchanged') return { title: `${name} was installed already`, text: 'The same version with the same files: nothing changed.', kept: [], dropped: [] };
  if (r.action === 'upgraded')
    return {
      title: `${nameOf(r.plugin)} went from ${r.from ?? 'the old version'} to ${r.plugin.version}`,
      text: `${r.kept.length ? `It kept ${plural(r.kept.length, 'setting, secret or grant', 'settings, secrets and grants')}` : 'It had nothing to keep'}${r.dropped.length ? `; ${r.dropped.length} no longer fit the new version and went` : ''}. A capability that is new starts ungranted.`,
      kept: r.kept, dropped: r.dropped,
    };
  return { title: `${name} is installed, and turned off`, text: 'Its code has not run. Nothing it asks for is granted yet: grant what it needs, then turn it on.', kept: [], dropped: [] };
}

// ---- the install form -----------------------------------------------------------------------------

export interface SourceForm {
  type: Source['type'];
  path: string;
  url: string;
  commit: string;
  subdir: string;
}

export const SOURCE_EMPTY: SourceForm = { type: 'folder', path: '', url: '', commit: '', subdir: '' };

export const SOURCE_WORDS: Record<Source['type'], { label: string; text: string }> = {
  folder: { label: 'A folder', text: 'On the controller’s machine. Copied file by file.' },
  wheel: { label: 'A wheel', text: 'A .whl file on the controller’s machine. Read as a zip, never through pip.' },
  git: { label: 'A git commit', text: 'A repository and one full commit id. Read from the commit, with no checkout.' },
};

/** What is wrong with the form, field by field; {} when it can be sent. The controller checks the
 *  same again: these are the refusals a user can be told before the round trip. */
export function sourceErrors(f: SourceForm): Partial<Record<'path' | 'url' | 'commit' | 'subdir', string>> {
  const out: Partial<Record<'path' | 'url' | 'commit' | 'subdir', string>> = {};
  if (f.type === 'git') {
    const url = f.url.trim();
    if (!url) out.url = 'The repository’s URL.';
    else if (!/^(https:\/\/|ssh:\/\/|file:\/\/|\/)/.test(url) && !/^[\w.-]+@[\w.-]+:/.test(url)) out.url = 'An https, ssh or file URL. Plain http and git:// are not taken.';
    const c = f.commit.trim();
    if (!c) out.commit = 'The full commit id: 40 hex digits.';
    else if (!/^[0-9a-fA-F]{40}$/.test(c)) out.commit = /^[0-9a-fA-F]{4,39}$/.test(c) ? `That is a short id (${c.length} of 40 digits). A short id can come to mean another commit: paste the full one (git rev-parse ${c}).` : 'A branch or a tag can move, so neither is taken. Paste the full commit id: 40 hex digits (git rev-parse HEAD).';
    const sub = f.subdir.trim();
    if (sub && (sub.startsWith('/') || sub.split('/').includes('..'))) out.subdir = 'A folder inside the repository, like plugins/hello.';
    return out;
  }
  const p = f.path.trim();
  if (!p) out.path = f.type === 'wheel' ? 'The wheel’s path on the controller’s machine.' : 'The folder’s path on the controller’s machine.';
  else if (!p.startsWith('/')) out.path = 'A full path from the root, like /srv/plugins/hello-tool.';
  else if (f.type === 'wheel' && !p.endsWith('.whl')) out.path = 'A wheel’s name ends in .whl.';
  return out;
}

export function sourceOf(f: SourceForm): Source {
  if (f.type === 'git') return { type: 'git', url: f.url.trim(), commit: f.commit.trim().toLowerCase(), ...(f.subdir.trim() ? { subdir: f.subdir.trim().replace(/^\.\//, '').replace(/\/+$/, '') } : {}) };
  return { type: f.type, path: f.path.trim() };
}

/** The form an upgrade starts from: where the plugin came from, without the commit (a new one is
 *  the point). */
export function formOf(s: Source): SourceForm {
  if (s.type === 'git') return { ...SOURCE_EMPTY, type: 'git', url: s.url, subdir: s.subdir ?? '' };
  return { ...SOURCE_EMPTY, type: s.type, path: s.path };
}

/** the form field a refusal of the source is about */
export function sourceField(field: string | undefined): 'path' | 'url' | 'commit' | 'subdir' | null {
  const f = (field ?? '').replace(/^source\./, '');
  return f === 'path' || f === 'url' || f === 'commit' || f === 'subdir' ? f : null;
}

// ---- refusals -------------------------------------------------------------------------------------

export interface Said {
  title: string;
  /** what to do about it */
  fix: string | null;
  /** the controller's own sentence, when the title is not it */
  detail: string | null;
}

/** Every refusal of the plugins' API as a sentence and its fix. */
export function pluginErrorWords(e: Pick<PluginError, 'type' | 'message' | 'facts'>): Said {
  const m = e.message;
  const f = e.facts ?? {};
  const own = (title: string, fix: string | null): Said => ({ title, fix, detail: m && m !== title ? m : null });
  switch (e.type) {
    case 'unauthorized':
      return { title: 'This needs pairing’s rights.', fix: 'Installing, turning on and removing a plugin are the admin’s: sign in below, or do it on the controller’s own machine. Nothing was changed.', detail: m && !/^a session/.test(m) && !/admin password is needed/.test(m) ? m : null };
    case 'offline':
      return { title: 'TandemApp did not answer.', fix: 'Check that it still runs and that this device reaches it, then try again.', detail: null };
    case 'bad_source':
      return own('The source is not one TandemApp can read.', 'Correct the field named below.');
    case 'commit_required':
      return own('A git source is pinned to one full commit id.', 'A branch, a tag and a short id can each move. Paste the 40 hex digits of the commit (git rev-parse HEAD).');
    case 'commit_not_found':
      return own('The repository has no such commit.', 'Check the id and that it was pushed to this URL.');
    case 'not_found':
      return f.field?.startsWith('source') ? own('Nothing is there on the controller’s machine.', 'The path is read on the machine TandemApp’s controller runs on, not on this device. Check it there.') : own('That plugin is no longer installed.', 'Go back to the list: it was removed on another screen.');
    case 'not_a_folder':
      return own('That path is not a folder.', 'Point at the folder that holds plugin.json. For a .whl file, choose A wheel.');
    case 'manifest_missing':
      return own('There is no plugin.json there.', 'Point at the folder that holds plugin.json. In a repository, name its folder under Folder in the repository.');
    case 'bad_wheel':
      return own('That file is not a wheel TandemApp can read.', 'It must be a zip whose files stay inside it and match the hashes in its RECORD. Build it again.');
    case 'upload_too_large':
      return own('The wheel is too large to upload.', 'The controller takes a wheel of at most 32 MiB. Copy a larger one to the controller’s machine and give its path.');
    case 'unsupported_media_type':
      return own('The controller did not take the file as a wheel.', 'Choose the .whl file again. Nothing was kept.');
    // the trust routes' refusals (SEC-18): no page here asks them yet, a settings write can meet some
    case 'not_requested':
      return own('The plugin does not ask for that capability.', 'A grant stays within what its manifest asks for. Install a version that asks for it.');
    case 'bad_value':
      return own('That value is not one this takes.', 'Correct the field named below. Nothing was changed.');
    case 'typed_confirm_required':
      return own('This takes the plugin’s id typed as the confirmation.', 'Type its id where it is asked for, then send it again. Nothing was changed.');
    case 'typed_confirm_mismatch':
      return own('What was typed is not the plugin’s id.', 'Type it exactly, then send it again. Nothing was changed.');
    case 'trust_confirm_needed':
      return own('This version asks for more trust than the one installed.', 'Confirm the new trust level to upgrade. The installed version runs meanwhile.');
    case 'signature_required':
      return own('This TandemApp takes only signed plugins.', 'Its signature policy asks for a signature, a sandboxed plugin’s too. Install a signed version, or change the policy.');
    case 'untrusted':
      return own('The plugin is not signed by a key this TandemApp trusts.', 'Add the author’s key with its fingerprint, or grant the level by typing the plugin’s id.');
    case 'fingerprint_confirm_required':
      return own('Adding a key takes its fingerprint typed back.', 'Compare it with the one the author published, then type it back. No key was added.');
    case 'fingerprint_mismatch':
      return own('The fingerprint typed is not the key’s.', 'Copy it again from where the author published it. No key was added.');
    case 'key_exists':
      return own('That key is trusted already.', 'Nothing was added. To change its limits, remove it and add it again.');
    case 'symlink':
    case 'special_file':
    case 'bad_name':
    case 'too_large':
      return own(problemOf({ code: e.type, field: null, message: m }).title, problemOf({ code: e.type, field: null, message: m }).fix);
    case 'submodule':
      return own('The commit holds a submodule, whose code is not in the commit.', 'Vendor the code into the repository, or say "git": {"submodules": "ignore"} in plugin.json if the plugin runs without it.');
    case 'lfs':
      return own('The commit holds an LFS file, whose content is not in git.', 'Commit the file itself, or say "git": {"lfs": "ignore"} in plugin.json if the plugin runs without it.');
    case 'unreadable':
      return own('The source could not be read.', 'Check that the controller’s user may read it, then try again.');
    case 'git_failed':
      return own('git could not fetch the commit.', 'Check the URL, and that the controller’s machine reaches it without asking for a password.');
    case 'git_missing':
      return own('git is not installed on the controller’s machine.', 'Install git there, or install the plugin from a folder or a wheel.');
    case 'git_timeout':
      return own('git took too long.', 'Try again. A very large repository installs faster from a folder.');
    case 'plugin_refused':
      return { title: `${!f.errors?.length ? 'The checks' : f.errors.length === 1 ? 'One check' : `${f.errors.length} checks`} refused this plugin. Nothing was installed.`, fix: f.errors?.length ? 'The reasons are listed with what to change.' : 'Check the source again to see the reasons.', detail: f.errors?.length ? null : m || null };
    case 'exists':
      return { title: `A plugin of this name is installed${f.installed ? ` (${f.installed})` : ''}.`, fix: `Install it as an upgrade to replace it${f.version ? ` with ${f.version}` : ''}.`, detail: null };
    case 'version_unchanged':
      return own('This version is installed already, with other files.', 'A changed plugin carries a new version: raise the version in plugin.json, then install it again.');
    case 'downgrade':
      return own('This version is older than the installed one.', 'Going back is possible, and asked for on purpose: settings the old version does not know are dropped.');
    case 'conflict':
      return { title: 'The plugin changed while this was under way.', fix: 'Read it again, then try once more.', detail: null };
    case 'referenced':
      return { title: `${plural(f.hooks?.length ?? 1, 'hook')} still ${f.hooks?.length === 1 ? 'names' : 'name'} this plugin, so it stays.`, fix: 'Remove those hooks, or point them at something else. Then remove the plugin.', detail: null };
    case 'integrity':
      return own('Its files changed since it was installed, so it does not load.', 'Install it again from its source.');
    case 'disabled':
      return { title: 'The plugin is turned off.', fix: 'Turn it on first: that takes a review, and pairing’s rights.', detail: null };
    case 'incompatible_tandemapp':
    case 'incompatible_hooks_api':
    case 'incompatible_plugin_api': {
      const p = problemOf({ code: e.type, field: null, message: m, needed: f.needed ?? undefined, have: f.have ?? undefined });
      return own(p.title, p.fix);
    }
    case 'plugin_down':
      return { title: 'The plugin is not running.', fix: 'Start it, then try again.', detail: null };
    case 'refused':
      return own('The plugin host refused that: the plugin was not granted it.', 'The refusal is in the plugin’s journal below.');
    case 'plugin_error':
      return own('The plugin answered with an error.', 'Read its log.');
    case 'bad_arguments':
      return own('The plugin’s tool refused those arguments.', null);
    case 'unavailable':
      return own('TandemApp’s configuration database is not open.', 'Check the controller’s log, then try again.');
    case 'cross_site':
    case 'cross_origin':
      return own('The controller refused this page’s request.', 'Open TandemApp at its own address and try again.');
    case 'bad_request':
      return { title: m, fix: null, detail: null };
    default:
      return { title: m || 'TandemApp answered with an error.', fix: 'Try again. If it keeps failing, look at TandemApp’s log.', detail: null };
  }
}

// ---- a settings form's values ---------------------------------------------------------------------

export type Step = string | number;
export const pathKey = (path: Step[]): string => path.join('\u0000');

export function getAt(root: unknown, path: Step[]): unknown {
  let v = root;
  for (const s of path) {
    if (v === null || typeof v !== 'object') return undefined;
    v = (v as Record<string, unknown>)[s as string];
  }
  return v;
}

/** `root` with the value at `path` replaced (`undefined` removes an object's key or a list's
 *  item); nothing of `root` is changed in place. */
export function setAt(root: unknown, path: Step[], value: unknown): unknown {
  if (!path.length) return value;
  const [head, ...rest] = path;
  if (typeof head === 'number') {
    const list = Array.isArray(root) ? [...root] : [];
    if (!rest.length && value === undefined) list.splice(head, 1);
    else list[head] = setAt(list[head], rest, value);
    return list;
  }
  const obj: Record<string, unknown> = root && typeof root === 'object' && !Array.isArray(root) ? { ...(root as Record<string, unknown>) } : {};
  if (!rest.length && value === undefined) delete obj[head];
  else obj[head] = setAt(obj[head], rest, value);
  return obj;
}

/** What a text field's content means for its field, or why it means nothing yet. An empty field
 *  of a nullable value is null; of any other, missing. */
export function parseField(f: FormField, raw: string): { ok: true; value: unknown } | { ok: false; error: string } {
  const t = raw.trim();
  if (f.control === 'number') {
    if (!t) return f.nullable || !f.required ? { ok: true, value: f.nullable ? null : undefined } : { ok: false, error: 'A number is needed here.' };
    if (f.type === 'integer' ? !/^-?\d+$/.test(t) : !Number.isFinite(Number(t))) return { ok: false, error: f.type === 'integer' ? 'A whole number, like 16.' : 'A number, like 2.5.' };
    return { ok: true, value: Number(t) };
  }
  if (f.control === 'json') {
    if (!t) return f.nullable || !f.required ? { ok: true, value: f.nullable ? null : undefined } : { ok: false, error: 'A JSON value is needed here.' };
    try {
      return { ok: true, value: JSON.parse(raw) };
    } catch (e) {
      return { ok: false, error: `Not valid JSON yet: ${(e as Error).message}` };
    }
  }
  // an empty text is a value of its own; only a nullable one reads as not set
  return { ok: true, value: !raw && f.nullable ? null : raw };
}

/** The text a field's control starts with. */
export function fieldText(f: FormField, value: unknown): string {
  if (value === null || value === undefined) return '';
  if (f.control === 'json') return JSON.stringify(value, null, 2);
  return String(value);
}

const num = (n: number): string => String(n);

/** What is wrong with a value held against its field: one sentence a place, keyed by the path's
 *  key. The controller holds it against the schema again; this is what a user is told at once. */
export function validateField(f: FormField, value: unknown, path: Step[] = [f.path[f.path.length - 1]], out: Record<string, string> = {}): Record<string, string> {
  const k = pathKey(path);
  if (value === undefined || value === null) {
    if (value === null && f.nullable) return out;
    if (value === undefined && !f.required) return out;
    if (f.control === 'group' && f.fields) {
      for (const sub of f.fields) validateField(sub, undefined, [...path, sub.path[sub.path.length - 1]], out);
      return out;
    }
    if (f.control !== 'const' && f.control !== 'secret') out[k] = 'This is needed.';
    return out;
  }
  switch (f.control) {
    case 'switch':
      if (typeof value !== 'boolean') out[k] = 'On or off.';
      break;
    case 'number': {
      if (typeof value !== 'number' || !Number.isFinite(value)) out[k] = 'A number is needed here.';
      else if (f.type === 'integer' && !Number.isInteger(value)) out[k] = 'A whole number, like 16.';
      else if (f.min !== undefined && value < f.min) out[k] = `At least ${num(f.min)}.`;
      else if (f.max !== undefined && value > f.max) out[k] = `At most ${num(f.max)}.`;
      else if (f.exclusive_min !== undefined && value <= f.exclusive_min) out[k] = `More than ${num(f.exclusive_min)}.`;
      else if (f.exclusive_max !== undefined && value >= f.exclusive_max) out[k] = `Less than ${num(f.exclusive_max)}.`;
      else if (f.step !== undefined && f.type !== 'integer' && Math.abs(value / f.step - Math.round(value / f.step)) > 1e-9) out[k] = `A multiple of ${num(f.step)}.`;
      else if (f.step !== undefined && f.type === 'integer' && f.step !== 1 && value % f.step !== 0) out[k] = `A multiple of ${num(f.step)}.`;
      break;
    }
    case 'text':
    case 'textarea': {
      if (typeof value !== 'string') out[k] = 'Text is needed here.';
      else if (f.min_length !== undefined && value.length < f.min_length) out[k] = f.min_length === 1 ? 'This cannot be empty.' : `At least ${plural(f.min_length, 'character')}.`;
      else if (f.max_length !== undefined && value.length > f.max_length) out[k] = `At most ${plural(f.max_length, 'character')}; this is ${value.length}.`;
      else if (f.pattern !== undefined) {
        let re: RegExp | null = null;
        try {
          re = new RegExp(f.pattern, 'u');
        } catch {
          /* a pattern this browser cannot read: the controller checks it */
        }
        if (re && !re.test(value)) out[k] = /^\^https\?:\/\//.test(f.pattern) ? 'An address that starts with http:// or https://.' : `It must match ${f.pattern}`;
      }
      break;
    }
    case 'choice':
    case 'select':
      if (!f.options?.some((o) => JSON.stringify(o.value) === JSON.stringify(value))) out[k] = `One of ${(f.options ?? []).map((o) => o.label).join(', ')}.`;
      break;
    case 'group':
      if (typeof value !== 'object' || Array.isArray(value)) out[k] = 'A group of values is needed here.';
      else for (const sub of f.fields ?? []) validateField(sub, (value as Record<string, unknown>)[sub.path[sub.path.length - 1]], [...path, sub.path[sub.path.length - 1]], out);
      break;
    case 'list': {
      if (!Array.isArray(value)) {
        out[k] = 'A list is needed here.';
        break;
      }
      if (f.min_items !== undefined && value.length < f.min_items) out[k] = `At least ${plural(f.min_items, 'item')}.`;
      else if (f.max_items !== undefined && value.length > f.max_items) out[k] = `At most ${plural(f.max_items, 'item')}; this has ${value.length}.`;
      else if (f.unique && new Set(value.map((x) => JSON.stringify(x))).size !== value.length) out[k] = 'Each item once.';
      if (f.item) value.forEach((x, i) => validateField({ ...f.item!, required: true }, x, [...path, i], out));
      break;
    }
    default:
      break;
  }
  return out;
}

/** a field's limits in a line under it: `1 to 365`, `at most 40 characters` */
export function limitsOf(f: FormField): string | null {
  if (f.control === 'number') {
    if (f.min !== undefined && f.max !== undefined) return `${num(f.min)} to ${num(f.max)}`;
    if (f.min !== undefined) return `at least ${num(f.min)}`;
    if (f.max !== undefined) return `at most ${num(f.max)}`;
    return null;
  }
  if (f.control === 'text' || f.control === 'textarea') return f.max_length !== undefined ? `at most ${plural(f.max_length, 'character')}` : null;
  if (f.control === 'list') return f.max_items !== undefined ? `at most ${plural(f.max_items, 'item')}` : null;
  return null;
}

/** a list whose items are a few named values, each at most once: drawn as a set of checkboxes */
export const isSet = (f: FormField): boolean => f.control === 'list' && !!f.item && (f.item.control === 'choice' || f.item.control === 'select') && !!f.item.options && f.item.options.length <= 12;

/** the value a new item of a list starts with */
export function blankOf(f: FormField): unknown {
  if (f.has_default) return structuredClone(f.default);
  switch (f.control) {
    case 'switch':
      return false;
    case 'number':
      return f.min ?? 0;
    case 'choice':
    case 'select':
      return f.options?.[0]?.value ?? null;
    case 'group':
      return Object.fromEntries((f.fields ?? []).filter((s) => s.required || s.has_default).map((s) => [s.path[s.path.length - 1], blankOf(s)]));
    case 'list':
      return [];
    default:
      return '';
  }
}

/** What the plugin's page says first when the plugin is not set up: how much it still waits for. */
export function setupOf(p: PluginFull): { needed: boolean; grants: string[]; secrets: string[]; trust: boolean; text: string } {
  const grants = p.capabilities.requested.filter((c) => !p.capabilities.granted.includes(c));
  const secrets = Object.entries(p.secrets).filter(([, h]) => !h.set).map(([n]) => n);
  const trust = p.trust.requested === 'host' && p.trust.granted !== 'host';
  const needed = p.kind === 'plugin' && !p.enabled;
  const parts = [grants.length ? `asks for ${plural(grants.length, 'capability', 'capabilities')} that ${grants.length === 1 ? 'is' : 'are'} not granted` : '', secrets.length ? `has ${plural(secrets.length, 'secret')} without a value` : '', trust ? 'asks to run outside the sandbox' : ''].filter(Boolean);
  return { needed, grants, secrets, trust, text: parts.length ? `It ${parts.join(', ')}.` : p.capabilities.requested.length ? 'What it asks for is granted.' : 'It asks for no capability.' };
}
