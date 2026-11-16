// The plugins' admin API (contracts/plugin-v1, `/v1/fleet/plugins/*`; VIS-284, SRV-857, SRV-859):
// the installed plugins, a source checked without installing it, install and upgrade, one plugin
// with its manifest and its settings' form, enable, disable, verify and remove; and the plugin
// host's routes: a plugin's status, its log, start, stop, restart, its panels and one bridge call.
// A plugin's settings, secrets and capability grants are not here: they are the settings
// interface's plugins scope (api/settings.ts), one target a plugin.
//
// A refusal throws PluginError with the contract's `type`, its sentence and its facts (`field`,
// `errors`, `hooks`, `installed`, `version`, `needed`, `have`). The calls go through `send`, so a
// test (or the mock) can answer them without HTTP.

import type { Form } from '../lib/schema-form';
import { apiBase } from './client';
import type { Sent } from './engines';

export type PluginKind = 'plugin' | 'engine-stats';
export type Trust = 'sandboxed' | 'host' | 'engine';
export type Integrity = 'ok' | 'mismatch' | 'missing';

export type Source = { type: 'folder'; path: string } | { type: 'wheel'; path: string; archive_sha256?: string } | { type: 'git'; url: string; commit: string; subdir?: string };

export interface Signature {
  present: boolean;
  verified: boolean;
  algorithm: string | null;
  key_id: string | null;
  /** null: which keys are trusted is not decided in this version */
  trusted: boolean | null;
  error: string | null;
}

/** One refusal of a check: its code, the manifest field it is about, a sentence and its facts. */
export interface CheckError {
  code: string;
  field: string | null;
  message: string;
  tool?: string;
  annotation?: string;
  stage?: string;
  capability?: string;
  name?: string;
  needed?: string;
  have?: string;
}

export interface Grant {
  by: string | null;
  at: number;
  confirmed_by: string | null;
  confirmed_at: number | null;
}

export type ContribKind = 'hooks' | 'tools' | 'decision_providers' | 'backends' | 'panels' | 'skills' | 'engine_hooks';

/** A plugin as the list holds it. */
export interface Plugin {
  id: string;
  name: string;
  version: string;
  kind: PluginKind;
  title: string | null;
  description: string | null;
  source: Source;
  sha256: string;
  signature: Signature | null;
  enabled: boolean;
  trust: { requested: Trust; granted: Trust };
  integrity: Integrity;
  checked_at: number | null;
  capabilities: { requested: string[]; granted: string[]; grants: Record<string, Grant> };
  contributes: Partial<Record<ContribKind, number>>;
  installed_at: number;
  installed_by: string | null;
  updated_at: number;
  updated_by: string | null;
  row_version: number;
}

export interface ToolDecl {
  name: string;
  title?: string;
  description?: string;
  annotations?: { readOnlyHint?: boolean; destructiveHint?: boolean; idempotentHint?: boolean; openWorldHint?: boolean };
}

export interface Manifest {
  plugin_api?: string;
  name?: string;
  version?: string;
  kind?: PluginKind;
  title?: string;
  description?: string;
  author?: string;
  license?: string;
  homepage?: string;
  tandemapp?: string;
  hooks_api?: string;
  entry?: { command?: string[] };
  trust?: Trust;
  contributes?: {
    tools?: ToolDecl[];
    hooks?: { name: string; stages: string[]; role: string; payload?: string; description?: string }[];
    decision_providers?: { name: string; questions: string[]; description?: string }[];
    backends?: { name: string; description?: string; models?: string[] }[];
    panels?: { name: string; title: string; path: string }[];
    skills?: string[];
    engine_hooks?: { name: string; wheel: string; hooks: string[] }[];
  };
  settings?: unknown;
  secrets?: { name: string; description?: string }[];
  capabilities?: string[];
  engine_stats?: { engine?: string; package?: string; install?: string };
}

export interface SecretHint {
  set: boolean;
  last4: string | null;
  sha256_6: string | null;
  set_at: number | null;
}

export interface HookRef {
  id: string;
  name: string;
  enabled: boolean;
}

/** One plugin, whole. */
export interface PluginFull extends Plugin {
  manifest: Manifest;
  settings_form: Form | null;
  /** the settings as they hold: the set values over the schema's defaults */
  settings: Record<string, unknown>;
  secrets: Record<string, SecretHint>;
  /** the hooks that name it: a removal is refused while there is one */
  references: HookRef[];
  path: string;
  manifest_dir: string;
}

export interface PluginList {
  contract_version: string;
  plugin_api: string;
  hooks_api: string;
  tandemapp: string;
  plugins: Plugin[];
}

/** Every check on a source: nothing installed, nothing run. */
export interface Inspection {
  contract_version: string;
  ok: boolean;
  source: Source;
  manifest: Manifest | null;
  errors: CheckError[];
  warnings: string[];
  sha256: string | null;
  signature: Signature | null;
  files: number;
  bytes: number;
  manifest_dir: string;
  /** what it asks for, and which of those take the second confirm to grant */
  capabilities: { requested: string[]; confirm: string[] };
  compat: { tandemapp: string; hooks_api: string; plugin_api: string };
  installed: { version: string; sha256: string } | null;
  settings_form: Form | null;
}

export interface Installed {
  contract_version: string;
  plugin: PluginFull;
  action: 'installed' | 'upgraded' | 'unchanged';
  from?: string;
  /** an upgrade: the settings, secrets and grants it kept, and those it dropped */
  kept: string[];
  dropped: string[];
  warnings: string[];
}

export type RunState = 'starting' | 'running' | 'stopping' | 'backoff' | 'stopped' | 'unavailable' | 'refused' | 'failed' | 'disabled';

export interface Refusal {
  ts: number;
  capability: string;
  what: string;
}

/** The plugin host's view of one plugin. */
export interface Status {
  id: string;
  state: RunState;
  /** stopped by hand: the host does not start it until a hand start */
  held: boolean;
  runtime: 'host' | 'docker' | 'kubernetes' | null;
  /** its pid, container or pod */
  where: string | null;
  version: string | null;
  started_at: number | null;
  restarts: number;
  failures_last_hour: number;
  next_start_at: number | null;
  last_error: string | null;
  tools: string[];
  hooks: string[];
  providers: string[];
  backends: string[];
  skills: string[];
  panels: string[];
  /** contributions not registered, and why */
  notes: string[];
  refusals: Refusal[];
  trust: { requested: Trust; granted: 'sandboxed' | 'host' };
  gave_up: { at?: number; error?: string | null; failures?: number } | null;
}

export type BridgeMethod = 'plugin.info' | 'plugin.status' | 'settings.read' | 'tools.call' | 'engine.control';

export interface Panel {
  name: string;
  title: string;
  url: string;
  /** the frame's sandbox attribute */
  sandbox: string;
  csp: string;
  bridge: { message: string; methods: BridgeMethod[] };
}

export interface Verified {
  id: string;
  integrity: Integrity;
  sha256: string | null;
  expected: string;
}

export interface PluginFacts {
  field?: string;
  errors?: CheckError[];
  warnings?: string[];
  sha256?: string | null;
  /** 409 exists: the version installed, and the one offered */
  installed?: string;
  version?: string;
  /** 409 referenced: the hooks that name the plugin */
  hooks?: HookRef[];
  needed?: string | null;
  have?: string | null;
}

/** A refusal of a plugins route. */
export class PluginError extends Error {
  constructor(
    public type: string,
    message: string,
    public status: number,
    public endpoint: string,
    public facts: PluginFacts = {},
  ) {
    super(message);
  }
}

export const asPluginError = (e: unknown): PluginError => (e instanceof PluginError ? e : new PluginError('error', (e as Error)?.message ?? String(e), 0, ''));

export type Method = 'GET' | 'POST' | 'DELETE';
export type PluginSend = (method: Method, path: string, body?: unknown) => Promise<Sent>;

export interface InstallOptions {
  upgrade?: boolean;
  allow_downgrade?: boolean;
}

export interface PluginsApi {
  list(): Promise<PluginList>;
  /** every check on a source; nothing is installed */
  inspect(source: Source): Promise<Inspection>;
  install(source: Source, o?: InstallOptions): Promise<Installed>;
  get(id: string): Promise<PluginFull>;
  verify(id: string): Promise<Verified>;
  remove(id: string): Promise<{ removed: string; version: string }>;
  status(id: string): Promise<Status>;
  logs(id: string, tail?: number): Promise<string[]>;
  start(id: string): Promise<Status>;
  stop(id: string): Promise<Status>;
  restart(id: string): Promise<Status>;
  panels(id: string): Promise<Panel[]>;
  /** one call a panel made, as the page relays it */
  bridge(id: string, method: string, params?: Record<string, unknown>, panel?: string): Promise<unknown>;
}

export const PLUGINS = '/v1/fleet/plugins';

function fail(endpoint: string, status: number, body: unknown): never {
  const e = (body as { error?: PluginFacts & { type?: string; message?: string } } | null)?.error;
  if (e && typeof e.type === 'string') {
    const { type, message, ...facts } = e;
    throw new PluginError(type, message ?? type, status, endpoint, facts);
  }
  if (status === 401) throw new PluginError('unauthorized', 'TandemApp’s admin password is needed for this.', 401, endpoint);
  if (status === 0) throw new PluginError('offline', 'TandemApp did not answer.', 0, endpoint);
  throw new PluginError('error', `TandemApp answered with an error (${status}).`, status, endpoint);
}

export function pluginsApi(send: PluginSend): PluginsApi {
  const call = async <T>(method: Method, path: string, body?: unknown): Promise<T> => {
    const r = await send(method, PLUGINS + path, body);
    if (r.status < 200 || r.status >= 300) fail(PLUGINS + path, r.status, r.body);
    return r.body as T;
  };
  const at = (id: string, sub = ''): string => `/${encodeURIComponent(id)}${sub}`;
  const status = async (method: Method, path: string) => (await call<{ status: Status }>(method, path, method === 'POST' ? {} : undefined)).status;
  return {
    list: () => call('GET', ''),
    inspect: (source) => call('POST', '/inspect', { source }),
    install: (source, o = {}) => call('POST', '', { source, ...(o.upgrade ? { upgrade: true } : {}), ...(o.allow_downgrade ? { allow_downgrade: true } : {}) }),
    get: async (id) => (await call<{ plugin: PluginFull }>('GET', at(id))).plugin,
    verify: (id) => call('POST', at(id, '/verify'), {}),
    remove: (id) => call('DELETE', at(id)),
    status: (id) => status('GET', at(id, '/status')),
    logs: async (id, tail = 200) => (await call<{ lines: string[] }>('GET', at(id, `/logs?tail=${tail}`))).lines,
    start: (id) => status('POST', at(id, '/start')),
    stop: (id) => status('POST', at(id, '/stop')),
    restart: (id) => status('POST', at(id, '/restart')),
    panels: async (id) => (await call<{ panels: Panel[] }>('GET', at(id, '/panels'))).panels,
    bridge: async (id, method, params, panel) => (await call<{ result: unknown }>('POST', at(id, '/bridge'), { method, ...(params ? { params } : {}), ...(panel ? { panel } : {}) })).result,
  };
}

/** Same-origin fetch, a JSON body on every write. A 401 is read here: the page shows its own
 *  sign-in for it. */
const http: PluginSend = async (method, path, body) => {
  let res: Response;
  try {
    res = await fetch(apiBase() + path, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
    });
  } catch {
    return { status: 0, body: null };
  }
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    /* not JSON: the status says the rest */
  }
  return { status: res.status, body: parsed };
};

export const plugins: PluginsApi = pluginsApi(http);
