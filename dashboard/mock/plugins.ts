// The plugins in the mock (`/v1/fleet/plugins/*`, contracts/plugin-v1; VIS-284), as the controller
// answers them (proxy/pluginapi.py, pluginstore.py, pluginhost.py): the registry of installed
// plugins, a source checked without installing it, install and upgrade, enable, disable, verify
// and remove, and the plugin host's side: each plugin's state on a clock, its log, start, stop and
// restart, its panels and the panel bridge. A plugin's settings, secrets and grants are the
// settings interface's plugins scope (mock/settings.ts `PluginsScope`), a view of this registry.
// No answer holds a secret.
//
// What a source holds is a catalog here (nothing is read from a disk but the three example
// manifests of the contract):
//   folder /srv/plugins/hello-tool           hello-tool 0.1.0: one read-only tool, two settings
//   wheel  /srv/dist/hello_tool-0.2.0-py3-none-any.whl
//                                            hello-tool 0.2.0: a panel and settings:read on top
//   folder /srv/plugins/hello-tool-0.0.9     an older hello-tool (409 downgrade)
//   folder /srv/plugins/hello-tool-changed   hello-tool 0.1.0 with other files (409 version_unchanged)
//   folder /srv/plugins/audit-hook           an observer hook, prompts:meta, a number and a set
//   folder /srv/plugins/classify-provider    a decision provider: a secret, a LAN host, nested settings
//   folder /srv/plugins/usage-board          signed; a panel, two tools, prompts:full, a host
//                                            outside the LAN, settings:read
//   git    https://git.example.com/acme/tandem-plugins.git at GIT_COMMIT, subdir host-metrics
//                                            asks to run on the host, engine:control, a backend
//   folder /srv/plugins/flaky-export         installs, then exits at every start
//   folder /srv/plugins/vllm                 kind engine-stats: recorded, never run
//   folder /srv/plugins/broken-tool          refused: a tool without destructiveHint, a loose schema
//   folder /srv/plugins/no-manifest          refused: no plugin.json
//   folder /srv/plugins/too-new              refused: needs a newer TandemApp
//   folder /srv/plugins/odd-fields           refused: an unknown field (what it says stays unchecked)
// Any other path is 404 not_found; /srv/dist/broken.whl is 422 bad_wheel; a git URL that is not
// the one above is 422 git_failed, .../with-submodule.git 422 submodule, a full commit id that is
// not GIT_COMMIT 422 commit_not_found.
//
// `seed(true)` installs hello-tool (on, running). `seed('demo')` adds audit-hook (on, running, a
// hook names it), classify-provider (off, nothing granted, its secret unset), flaky-export (the
// plugin host gave up) and the vLLM stats package. A process follows the clock: starting for
// 1.5 s, then running; one that exits is started again after 1, 2, 4, 8 s and given up at the
// fifth failure. `control()` (POST /__mock/plugins) sets what no click produces:
//   {fail: "<route>:<error type>" | null}  a route answers that refusal until cleared (list,
//                                          inspect, install, get, verify, delete, status, logs,
//                                          start, stop, restart, panels, bridge)
//   {trust: true | false}                  the caller has pairing's rights (install, enable, remove)
//   {sandbox: true | false}                this machine has a sandbox runtime
//   {crash: "<id>"}                        that plugin's process exits at once, and at each start
//   {tamper: "<id>"}                       its files changed since the install
//   {unref: "<id>"}                        the hooks that named it are gone
//   {panel: "<id>/<name>" | null}          that panel's file is gone (its address answers 404)
//   {seed: true | false | "demo"}          the registry as seeded

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { CheckError, Grant, HookRef, Manifest, Panel, Plugin, PluginFull, Signature, Source, Status } from '../src/api/plugins.ts';
import { looksLan, networkHost } from '../src/lib/plugin-caps.ts';
import { schemaForm } from '../src/lib/schema-form.ts';

export type Reply = { status: number; body: unknown };

const CONTRACT = '1.0';
export const TANDEMAPP = '0.16.0';
const HOOKS_API = '1.0';
export const GIT_URL = 'https://git.example.com/acme/tandem-plugins.git';
export const GIT_COMMIT = '3f2a9c1e4b7d8a6f5e0c1b2a3d4e5f60718293a4';
const START_MS = 1500;
const BACKOFF_S = [1, 2, 4, 8, 16, 32, 60];
const MAX_CRASHES = 5;
export const PANEL_CSP = "sandbox allow-scripts allow-forms; default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'";

/** every error type with its HTTP status (contracts/plugin-v1/api.json `errors`) */
export const PLUGIN_ERRORS: Record<string, number> = {
  bad_request: 400, unauthorized: 401, cross_site: 403, cross_origin: 403, not_found: 404, bad_source: 422, commit_required: 422, commit_not_found: 422, plugin_refused: 422,
  manifest_missing: 422, bad_wheel: 422, symlink: 422, special_file: 422, bad_name: 422, too_large: 422, submodule: 422, lfs: 422, not_a_folder: 422, exists: 409,
  version_unchanged: 409, downgrade: 409, conflict: 409, referenced: 409, integrity: 409, disabled: 409, incompatible_tandemapp: 409, incompatible_hooks_api: 409,
  incompatible_plugin_api: 409, git_failed: 422, git_missing: 503, git_timeout: 504, unavailable: 503, unreadable: 422, plugin_down: 503, refused: 403, plugin_error: 502, bad_arguments: 400,
};
const SENTENCE: Record<string, string> = {
  unavailable: 'the config database is not open',
  git_missing: 'git is not installed on the controller’s host',
  git_timeout: 'git took longer than 120 s',
  unreadable: 'the source could not be read or copied: permission denied',
  conflict: 'the plugin changed meanwhile; try again',
  plugin_error: 'the plugin answered -32603: internal error',
};

class Refuse extends Error {
  constructor(
    public type: string,
    message: string,
    public facts: Record<string, unknown> = {},
    public status = PLUGIN_ERRORS[type] ?? 400,
  ) {
    super(message);
  }
}
function no(type: string, message: string, facts: Record<string, unknown> = {}, status?: number): never {
  throw new Refuse(type, message, facts, status);
}

const copy = <T>(v: T): T => JSON.parse(JSON.stringify(v ?? null)) as T;
const sha = (text: string): string => createHash('sha256').update(text).digest('hex');
const example = (name: string): Manifest => JSON.parse(readFileSync(new URL(`../../contracts/plugin-v1/examples/${name}/plugin.json`, import.meta.url), 'utf8')) as Manifest;

/** a capability's grant takes the second confirm (proxy/pluginmanifest.py `needs_confirm`) */
export function needsConfirm(capability: string): boolean {
  if (capability === 'prompts:full' || capability === 'engine:control') return true;
  return capability.startsWith('network:') ? !looksLan(networkHost(capability)) : false;
}

/** What a source holds: a tree with its manifest, or why it is refused. */
interface Pack {
  manifest: Manifest | null;
  errors: CheckError[];
  warnings: string[];
  files: number;
  bytes: number;
  /** what the tree's sha256 is made of: another salt, other files */
  salt: string;
  signed: boolean;
  /** its process exits at every start */
  crashes?: boolean;
  panels?: Record<string, string>;
}

const BOARD_PANEL = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Usage board</title>
<style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:20px;color:#17202b;background:#f6f8fb}h1{font-size:19px;margin:0 0 4px}p{margin:0 0 12px;color:#4d5b6c}
button{font:inherit;padding:8px 12px;margin:0 6px 6px 0;border:1px solid #b9c4d2;border-radius:6px;background:#fff;cursor:pointer}button:focus-visible{outline:2px solid #2f6fdd;outline-offset:2px}
pre{padding:12px;border-radius:6px;background:#17202b;color:#e8eef6;overflow:auto;min-height:72px;white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>
<h1>Usage board</h1><p>This page is the plugin’s own. It runs in a sandboxed frame and asks TandemApp through the bridge.</p>
<div><button data-call="plugin.info">Who am I</button><button data-call="plugin.status">My state</button><button data-call="tools.call" data-tool="usage_today">Today’s usage</button><button data-call="settings.read">My settings</button><button data-call="tools.call" data-tool="reset_counters">Reset the counters</button><button data-call="engine.control">Restart the engine</button></div>
<pre id="out" aria-live="polite">Nothing asked yet.</pre>
<script>
let n = 0; const out = document.getElementById('out');
addEventListener('message', (e) => { const m = e.data; if (!m || m.type !== 'tandem-bridge-result') return; out.textContent = (m.error ? 'refused: ' + m.error.message : JSON.stringify(m.result, null, 2)); });
for (const b of document.querySelectorAll('button')) b.addEventListener('click', () => { const tool = b.dataset.tool; parent.postMessage({ type: 'tandem-bridge', id: ++n, method: b.dataset.call, params: tool ? { tool, arguments: {} } : undefined }, '*'); out.textContent = 'asking ' + b.dataset.call + '…'; });
</script></body></html>`;

const GREETER_PANEL = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Greeter</title><style>body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:20px}</style></head><body><h1>Greeter</h1><p id="g">…</p>
<script>addEventListener('message',(e)=>{const m=e.data;if(m&&m.type==='tandem-bridge-result')document.getElementById('g').textContent=m.error?m.error.message:JSON.stringify(m.result)});parent.postMessage({type:'tandem-bridge',id:1,method:'tools.call',params:{tool:'greet',arguments:{name:'you'}}},'*');</script></body></html>`;

function catalog(): Record<string, Pack> {
  const hello = example('hello-tool');
  const audit = example('audit-hook');
  const classify = example('classify-provider');
  const pack = (manifest: Manifest | null, o: Partial<Pack> = {}): Pack => ({ manifest, errors: [], warnings: [], files: 2, bytes: 3120, salt: `${manifest?.name}@${manifest?.version}`, signed: false, ...o });
  const hello2: Manifest = copy(hello);
  hello2.version = '0.2.0';
  hello2.capabilities = ['settings:read'];
  hello2.contributes = { ...hello2.contributes, panels: [{ name: 'greeter', title: 'Greeter', path: 'panel/index.html' }] };
  (hello2.settings as { properties: Record<string, unknown> }).properties = { greeting: (hello.settings as { properties: Record<string, unknown> }).properties.greeting, language: { enum: ['en', 'de', 'ar', 'fr', 'es'], default: 'en', description: 'the language of the greeting' } };
  const board: Manifest = {
    plugin_api: '1.0', name: 'usage-board', version: '1.4.0', title: 'Usage board', description: 'A panel that shows today’s tokens by user and model, with a tool the Chat tab can call for the same figures.', author: 'Example Labs', license: 'MIT', homepage: 'https://example.com/usage-board',
    tandemapp: '>=0.15', hooks_api: '1.0', entry: { command: ['python3', 'board.py'] },
    contributes: {
      panels: [{ name: 'board', title: 'Usage board', path: 'panel/index.html' }],
      tools: [
        { name: 'usage_today', description: 'Today’s tokens by user and model.', annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false } },
        { name: 'reset_counters', description: 'Set today’s counters back to zero.', annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false } },
      ],
      hooks: [{ name: 'count', stages: ['request.finished'], role: 'observer', payload: 'full', description: 'counts each request’s tokens' }],
    },
    capabilities: ['settings:read', 'prompts:full', 'network:api.example.com:443', 'network:192.168.1.40:9100'],
    settings: {
      type: 'object', additionalProperties: false, required: ['refresh_s'],
      properties: {
        refresh_s: { type: 'integer', title: 'Refresh every (s)', minimum: 5, maximum: 600, default: 30, description: 'how often the panel reads the figures again' },
        currency: { enum: ['USD', 'EUR'], default: 'USD', description: 'the currency of the cost column' },
        export: {
          type: 'object', title: 'Export', additionalProperties: false,
          properties: { enabled: { type: 'boolean', default: false, description: 'send the day’s totals to the address below' }, url: { anyOf: [{ type: 'string', pattern: '^https?://', maxLength: 200 }, { type: 'null' }], default: null, description: 'where the totals go' } },
        },
        hide_users: { type: 'array', items: { type: 'string', minLength: 1, maxLength: 60 }, maxItems: 20, uniqueItems: true, default: [], description: 'users left out of the board' },
      },
    },
  };
  // the tools' strict schemas, as an install requires them
  for (const t of board.contributes!.tools!) (t as unknown as Record<string, unknown>).inputSchema = { type: 'object', properties: {}, additionalProperties: false };
  const metrics: Manifest = {
    plugin_api: '1.0', name: 'host-metrics', version: '1.2.0', title: 'Host metrics', description: 'Reads the machine’s own sensors and serves them as a backend of one model.', author: 'Acme', license: 'Apache-2.0', tandemapp: '>=0.15, <0.18', hooks_api: '1.0',
    entry: { command: ['python3', '-m', 'host_metrics'] }, trust: 'host',
    contributes: { backends: [{ name: 'echo', description: 'answers with the sensors’ figures', models: ['sensors-1'] }], skills: ['skills'] },
    capabilities: ['prompts:meta', 'engine:control'],
  };
  const flaky: Manifest = {
    plugin_api: '1.0', name: 'flaky-export', version: '0.3.1', title: 'Flaky export', description: 'Sends each finished request’s figures to a collector.', license: 'MIT', tandemapp: '>=0.15', hooks_api: '1.0', entry: { command: ['node', 'export.js'] },
    contributes: { hooks: [{ name: 'export', stages: ['request.finished'], role: 'observer', payload: 'meta' }] }, capabilities: ['prompts:meta', 'network:192.168.1.52:4318'],
  };
  const vllm: Manifest = {
    plugin_api: '1.0', name: 'tandemllm-vllm-plugin', version: '0.1.0', kind: 'engine-stats', title: 'vLLM stats', description: 'Per-request queue, prefill and decode times, cached tokens and preemptions from inside vLLM, pushed to the controller.', license: 'AGPL-3.0-only', tandemapp: '>=0.15',
    engine_stats: { engine: 'vllm', package: 'tandemllm-vllm-plugin', install: 'pip install plugins/vllm' },
  };
  const broken: Manifest = copy(hello);
  broken.name = 'broken-tool';
  broken.title = 'Broken tool';
  const odd: Manifest = { ...copy(hello), name: 'odd-fields', title: 'Odd fields' };
  (odd as unknown as Record<string, unknown>).autostart = true;
  const tooNew: Manifest = { ...copy(hello), name: 'too-new', title: 'Too new', tandemapp: '>=99.0' };
  return {
    'folder:/srv/plugins/hello-tool': pack(hello),
    'wheel:/srv/dist/hello_tool-0.2.0-py3-none-any.whl': pack(hello2, { files: 4, bytes: 5890, panels: { greeter: GREETER_PANEL } }),
    'folder:/srv/plugins/hello-tool-0.0.9': pack({ ...copy(hello), version: '0.0.9' }),
    'folder:/srv/plugins/hello-tool-changed': pack(hello, { salt: 'hello-tool@0.1.0+edited', bytes: 3188 }),
    'folder:/srv/plugins/audit-hook': pack(audit, { bytes: 2764 }),
    'folder:/srv/plugins/classify-provider': pack(classify, { bytes: 4410 }),
    'folder:/srv/plugins/usage-board': pack(board, { files: 9, bytes: 48_212, signed: true, panels: { board: BOARD_PANEL } }),
    [`git:${GIT_URL}#${GIT_COMMIT}:host-metrics`]: pack(metrics, { files: 14, bytes: 96_300, warnings: ['left as they are: docs/diagram.psd (an LFS pointer, as the manifest allows)'] }),
    'folder:/srv/plugins/flaky-export': pack(flaky, { files: 3, bytes: 7020, crashes: true }),
    'folder:/srv/plugins/vllm': pack(vllm, { files: 6, bytes: 22_150 }),
    'folder:/srv/plugins/broken-tool': pack(broken, {
      errors: [
        { code: 'annotation_missing', field: 'contributes.tools[0].annotations.destructiveHint', message: "tool 'greet': annotations.destructiveHint is missing: state readOnlyHint, destructiveHint, idempotentHint and openWorldHint as booleans", tool: 'greet', annotation: 'destructiveHint' },
        { code: 'extra_properties', field: 'contributes.tools[0].inputSchema', message: "tool 'greet': inputSchema does not say additionalProperties false", tool: 'greet' },
      ],
    }),
    'folder:/srv/plugins/no-manifest': pack(null, { files: 1, bytes: 12, salt: 'no-manifest', errors: [{ code: 'manifest_missing', field: null, message: 'no plugin.json at the plugin’s root' }] }),
    'folder:/srv/plugins/too-new': pack(tooNew, { errors: [{ code: 'incompatible_tandemapp', field: 'tandemapp', message: `tandemapp: needs >=99.0, this TandemApp is ${TANDEMAPP}`, needed: '>=99.0', have: TANDEMAPP }] }),
    'folder:/srv/plugins/odd-fields': pack(odd, { errors: [{ code: 'unknown_field', field: 'autostart', message: 'autostart: not a field of plugin-v1' }] }),
  };
}

interface Run {
  state: Status['state'];
  held: boolean;
  /** when the state began */
  since: number;
  started_at: number | null;
  restarts: number;
  crashes: number[];
  next_at: number | null;
  last_error: string | null;
  refusals: Status['refusals'];
  gave_up: { at: number; error: string | null; failures: number } | null;
  log: string[];
}

export interface MockPlugin {
  id: string;
  manifest: Manifest;
  source: Source;
  sha256: string;
  signature: Signature;
  enabled: boolean;
  trust_granted: 'sandboxed' | 'host';
  integrity: 'ok' | 'mismatch' | 'missing';
  checked_at: number | null;
  granted: Record<string, Grant>;
  /** the values set; the rest are the schema's defaults */
  settings: Record<string, unknown>;
  secrets: Record<string, { value: string; set_at: number }>;
  installed_at: number;
  installed_by: string | null;
  updated_at: number;
  updated_by: string | null;
  row_version: number;
  crashes: boolean;
  panels: Record<string, string>;
  run: Run;
}

const fresh = (now: number): Run => ({ state: 'disabled', held: false, since: now, started_at: null, restarts: 0, crashes: [], next_at: null, last_error: null, refusals: [], gave_up: null, log: [] });
const stamp = (ms: number): string => new Date(ms).toISOString().slice(11, 19);

export class MockPlugins {
  rows: Record<string, MockPlugin> = {};
  /** the hooks that name a plugin: a removal is refused while there is one */
  refs: Record<string, HookRef[]> = {};
  /** the caller has pairing's rights (this machine, or the admin) */
  trust = true;
  sandbox = true;
  now: number;
  private packs = catalog();
  private fails = new Map<string, string>();
  private gone = new Set<string>();

  constructor(t0 = Date.now()) {
    this.now = t0;
  }

  /** The registry as seeded: nothing, hello-tool alone, or the demo set. */
  seed(level: boolean | 'demo'): void {
    this.rows = {};
    this.refs = {};
    if (!level) return;
    const t = this.now;
    const put = (key: string, at: number, on: boolean, grant: string[] = []): MockPlugin => {
      const [type, path] = [key.slice(0, key.indexOf(':')), key.slice(key.indexOf(':') + 1)];
      const p = this.build(this.packs[key], { type, path } as Source, at, 'admin');
      for (const c of grant) p.granted[c] = { by: 'admin', at, confirmed_by: needsConfirm(c) ? 'admin' : null, confirmed_at: needsConfirm(c) ? at : null };
      p.enabled = on;
      p.run = fresh(at);
      this.rows[p.id] = p;
      return p;
    };
    const hello = put('folder:/srv/plugins/hello-tool', t - 6 * 86_400_000, true);
    this.boot(hello, t - 3_600_000);
    if (level !== 'demo') return;
    const audit = put('folder:/srv/plugins/audit-hook', t - 5 * 86_400_000, true, ['prompts:meta']);
    audit.settings = { keep_days: 14 };
    this.boot(audit, t - 3_590_000);
    this.refs['audit-hook'] = [{ id: 'audit-to-disk', name: 'Audit to disk', enabled: true }];
    put('folder:/srv/plugins/classify-provider', t - 2 * 3_600_000, false);
    const flaky = put('folder:/srv/plugins/flaky-export', t - 86_400_000, false, ['prompts:meta', 'network:192.168.1.52:4318']);
    const err = 'export.js exited with 1: Error: connect ECONNREFUSED 192.168.1.52:4318';
    flaky.run = { ...fresh(t - 1_500_000), state: 'failed', restarts: 4, crashes: [], last_error: err, gave_up: { at: t - 1_500_000, error: err, failures: MAX_CRASHES }, log: this.crashLog(flaky, t - 1_500_000) };
    flaky.updated_by = 'plugin-host';
    flaky.updated_at = t - 1_500_000;
    put('folder:/srv/plugins/vllm', t - 9 * 86_400_000, false);
  }

  /** What no click produces (POST /__mock/plugins); see the head of this file. */
  control(b: { fail?: string | null; trust?: boolean; sandbox?: boolean; crash?: string; tamper?: string; unref?: string; panel?: string | null; seed?: boolean | 'demo' }): Record<string, unknown> {
    if (b.fail === null) this.fails.clear();
    else if (typeof b.fail === 'string') {
      const [route, type] = b.fail.split(':');
      if (!(type in PLUGIN_ERRORS)) throw new Error(`no plugins error ${type}`);
      this.fails.set(route, type);
    }
    if (typeof b.trust === 'boolean') this.trust = b.trust;
    if (typeof b.sandbox === 'boolean') this.sandbox = b.sandbox;
    if (b.seed !== undefined) this.seed(b.seed);
    if (b.crash) {
      const p = this.need(b.crash);
      p.crashes = true;
      if (p.run.state === 'running' || p.run.state === 'starting') this.fail(p, this.now);
    }
    if (b.tamper) this.need(b.tamper).integrity = 'mismatch';
    if (b.unref) delete this.refs[b.unref];
    if (b.panel === null) this.gone.clear();
    else if (b.panel) this.gone.add(b.panel);
    return { fails: Object.fromEntries(this.fails), trust: this.trust, sandbox: this.sandbox, plugins: Object.keys(this.rows) };
  }

  // ---- the registry -------------------------------------------------------------------------------

  need(id: string): MockPlugin {
    const p = this.rows[id];
    if (!p) no('not_found', `no plugin ${id}`);
    return p;
  }

  list(): MockPlugin[] {
    return Object.values(this.rows).sort((a, b) => (a.id < b.id ? -1 : 1));
  }

  requested(p: Pick<MockPlugin, 'manifest'>): string[] {
    return [...(p.manifest.capabilities ?? [])];
  }

  schema(p: Pick<MockPlugin, 'manifest'>): Record<string, unknown> | null {
    const s = p.manifest.settings;
    return s && typeof s === 'object' ? (s as Record<string, unknown>) : null;
  }

  /** the settings as they hold: the set values over the schema's defaults */
  effective(p: MockPlugin): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const props = (this.schema(p)?.properties ?? {}) as Record<string, { default?: unknown; properties?: Record<string, { default?: unknown }> }>;
    for (const [k, node] of Object.entries(props)) {
      if (k in p.settings) out[k] = copy(p.settings[k]);
      else if ('default' in node) out[k] = copy(node.default);
      else if (node.properties) {
        // an object without a default of its own: its properties' defaults
        const inner = Object.fromEntries(Object.entries(node.properties).filter(([, s]) => 'default' in s).map(([n, s]) => [n, copy(s.default)]));
        if (Object.keys(inner).length) out[k] = inner;
      }
    }
    return out;
  }

  hints(p: MockPlugin): PluginFull['secrets'] {
    return Object.fromEntries((p.manifest.secrets ?? []).map((s) => {
      const got = p.secrets[s.name];
      return [s.name, got ? { set: true, last4: got.value.slice(-4), sha256_6: sha(got.value).slice(0, 6), set_at: got.set_at } : { set: false, last4: null, sha256_6: null, set_at: null }];
    }));
  }

  private view(p: MockPlugin): Plugin {
    const m = p.manifest;
    const c = (m.contributes ?? {}) as unknown as Record<string, unknown>;
    return {
      id: p.id, name: p.id, version: m.version!, kind: m.kind ?? 'plugin', title: m.title ?? null, description: m.description ?? null, source: copy(p.source), sha256: p.sha256, signature: copy(p.signature), enabled: p.enabled,
      trust: { requested: m.trust ?? 'sandboxed', granted: p.trust_granted }, integrity: p.integrity, checked_at: p.checked_at,
      capabilities: { requested: this.requested(p), granted: Object.keys(p.granted).sort(), grants: copy(p.granted) },
      contributes: Object.fromEntries(Object.entries(c).filter(([, v]) => Array.isArray(v)).map(([k, v]) => [k, (v as unknown[]).length])) as Plugin['contributes'],
      installed_at: p.installed_at, installed_by: p.installed_by, updated_at: p.updated_at, updated_by: p.updated_by, row_version: p.row_version,
    };
  }

  private full(p: MockPlugin): PluginFull {
    const sch = this.schema(p);
    return { ...this.view(p), manifest: copy(p.manifest), settings_form: sch ? schemaForm(sch) : null, settings: this.effective(p), secrets: this.hints(p), references: copy(this.refs[p.id] ?? []), path: `/var/lib/tandemapp/plugins/${p.id}/${p.manifest.version}`, manifest_dir: '' };
  }

  private build(pack: Pack, source: Source, now: number, who: string): MockPlugin {
    const m = copy(pack.manifest!);
    const digest = sha(`tree:${pack.salt}`);
    const record: Source = source.type === 'wheel' ? { type: 'wheel', path: source.path, archive_sha256: sha(`whl:${source.path}`) } : copy(source);
    return {
      id: m.name!, manifest: m, source: record, sha256: digest, signature: this.signature(pack, digest), enabled: false, trust_granted: 'sandboxed', integrity: 'ok', checked_at: now, granted: {}, settings: {}, secrets: {},
      installed_at: now, installed_by: who, updated_at: now, updated_by: who, row_version: 1, crashes: !!pack.crashes, panels: { ...(pack.panels ?? {}) }, run: fresh(now),
    };
  }

  private signature(pack: Pack, digest: string): Signature {
    return pack.signed ? { present: true, verified: true, algorithm: 'ed25519', key_id: sha(`key:${digest}`).slice(0, 16), trusted: null, error: null } : { present: false, verified: false, algorithm: null, key_id: null, trusted: null, error: null };
  }

  /** The tree a source names, or the refusal of the source itself. */
  private stage(source: unknown): { pack: Pack; source: Source } {
    const s = source as Record<string, unknown> | null;
    if (!s || typeof s !== 'object' || Array.isArray(s)) no('bad_source', 'source: an object {type, ...}', { field: 'source' });
    const t = s!.type;
    if (t !== 'folder' && t !== 'wheel' && t !== 'git') no('bad_source', 'source.type: one of folder, wheel, git', { field: 'source.type' });
    const allowed = t === 'git' ? ['type', 'url', 'commit', 'subdir'] : ['type', 'path'];
    const extra = Object.keys(s!).filter((k) => !allowed.includes(k)).sort();
    if (extra.length) no('bad_source', `source.${extra[0]}: not a field of a ${t} source`, { field: `source.${extra[0]}` });
    if (t === 'git') {
      const { url, commit, subdir } = s as { url?: unknown; commit?: unknown; subdir?: unknown };
      if (typeof url !== 'string' || !url) no('bad_source', 'source.url: a git URL (https, ssh, file or an absolute path)', { field: 'source.url' });
      if (!/^(https:\/\/|ssh:\/\/|file:\/\/|\/)/.test(url as string) && !/^[\w.-]+@[\w.-]+:/.test(url as string)) no('bad_source', 'source.url: only the https, ssh and file transports', { field: 'source.url' });
      if (typeof commit !== 'string' || !/^[0-9a-f]{40}$/.test(commit)) no('commit_required', 'source.commit: the full commit id (40 hex digits; a branch, a tag or a short id can move)', { field: 'source.commit' });
      if (subdir !== undefined && (typeof subdir !== 'string' || subdir.startsWith('/') || subdir.split('/').includes('..'))) no('bad_source', 'source.subdir: a relative folder in the repository', { field: 'source.subdir' });
      if ((url as string).endsWith('/with-submodule.git')) no('submodule', 'vendor/lib is a submodule: its code is not in the commit. Say "git": {"submodules": "ignore"} in the manifest to install without it', { field: 'source.url' });
      if (url !== GIT_URL) no('git_failed', `git fetch failed: fatal: unable to access '${url}': Could not resolve host`, { field: 'source.url' });
      if (commit !== GIT_COMMIT) no('commit_not_found', `source.commit: ${commit} is not a commit of ${url}`, { field: 'source.commit' });
      const pack = this.packs[`git:${url}#${commit}:${subdir ?? ''}`];
      if (!pack) no(subdir ? 'not_found' : 'manifest_missing', subdir ? `source.subdir: no ${subdir} at ${(commit as string).slice(0, 12)}` : 'no plugin.json at the repository’s root: name its folder in source.subdir', { field: subdir ? 'source.subdir' : 'source.url' }, subdir ? 404 : 422);
      return { pack, source: { type: 'git', url: url as string, commit: commit as string, ...(subdir ? { subdir: subdir as string } : {}) } };
    }
    const path = s!.path;
    if (typeof path !== 'string' || !path.startsWith('/')) no('bad_source', 'source.path: an absolute path on the controller’s machine', { field: 'source.path' });
    if (t === 'wheel') {
      if (!(path as string).endsWith('.whl')) no('bad_source', 'source.path: a .whl file', { field: 'source.path' });
      if (path === '/srv/dist/broken.whl') no('bad_wheel', 'not a wheel (zip): File is not a zip file', { field: 'source.path' });
    }
    const pack = this.packs[`${t}:${path}`];
    if (!pack) {
      if (t === 'folder' && this.packs[`wheel:${path}`]) no('not_a_folder', `source.path: ${path} is a file, not a folder`, { field: 'source.path' });
      no('not_found', `source.path: no ${t === 'wheel' ? 'file' : 'folder'} ${path}`, { field: 'source.path' }, 404);
    }
    return { pack, source: { type: t, path: path as string } as Source };
  }

  private report(pack: Pack, source: Source): Record<string, unknown> {
    const m = pack.manifest;
    const ok = pack.errors.length === 0;
    const digest = sha(`tree:${pack.salt}`);
    const installed = m?.name ? this.rows[m.name] : undefined;
    const requested = m?.capabilities ?? [];
    const sch = m?.settings;
    return {
      ok, source: source.type === 'wheel' ? { ...source, archive_sha256: sha(`whl:${source.path}`) } : source, manifest: m ? copy(m) : null, errors: copy(pack.errors), warnings: [...pack.warnings], sha256: digest, signature: this.signature(pack, digest),
      files: pack.files, bytes: pack.bytes, manifest_dir: '', capabilities: { requested: [...requested], confirm: requested.filter(needsConfirm) }, compat: { tandemapp: TANDEMAPP, hooks_api: HOOKS_API, plugin_api: CONTRACT },
      installed: installed ? { version: installed.manifest.version!, sha256: installed.sha256 } : null, settings_form: ok && sch ? schemaForm(sch) : null,
    };
  }

  private install(body: Record<string, unknown>, who: string, now: number): Reply {
    const { pack, source } = this.stage(body.source);
    if (pack.errors.length) no('plugin_refused', pack.errors[0].message, { ...(pack.errors[0].field ? { field: pack.errors[0].field } : {}), errors: copy(pack.errors), warnings: [...pack.warnings], sha256: sha(`tree:${pack.salt}`) });
    const m = pack.manifest!;
    const old = this.rows[m.name!];
    const note: { action: string; from?: string; kept: string[]; dropped: string[]; warnings: string[] } = { action: 'installed', kept: [], dropped: [], warnings: [...pack.warnings] };
    const next = this.build(pack, source, now, who);
    if (old) {
      if (old.manifest.version === m.version) {
        if (old.sha256 === next.sha256) return { status: 200, body: { contract_version: CONTRACT, plugin: this.full(old), ...note, action: 'unchanged' } };
        no('version_unchanged', `${old.id} ${m.version} is installed with another tree (sha256 ${old.sha256.slice(0, 12)}, this one ${next.sha256.slice(0, 12)}): a changed plugin carries a new version`, { field: 'version' });
      }
      if (body.upgrade !== true) no('exists', `a plugin named ${old.id} is installed (${old.manifest.version}): install with upgrade true to replace it with ${m.version}`, { field: 'name', installed: old.manifest.version, version: m.version });
      if (versionOrder(m.version!, old.manifest.version!) < 0 && body.allow_downgrade !== true) no('downgrade', `${m.version} is older than the installed ${old.manifest.version}: install with allow_downgrade true to go back`, { field: 'version' });
      note.action = 'upgraded';
      note.from = old.manifest.version;
      // what still fits is kept: a setting whose property is still there, a secret still declared, a grant still asked for
      const props = ((this.schema(next)?.properties ?? {}) as Record<string, unknown>) ?? {};
      for (const [k, v] of Object.entries(old.settings)) {
        if (k in props) {
          next.settings[k] = v;
          note.kept.push(`settings.${k}`);
        } else note.dropped.push(`settings.${k}`);
      }
      for (const [c, g] of Object.entries(old.granted)) {
        if (this.requested(next).includes(c)) {
          next.granted[c] = g;
          note.kept.push(`capabilities.${c}`);
        } else note.dropped.push(`capabilities.${c}`);
      }
      if (old.trust_granted === 'host') {
        if (m.trust === 'host') {
          next.trust_granted = 'host';
          note.kept.push('trust');
        } else note.dropped.push('trust');
      }
      const names = new Set((m.secrets ?? []).map((s) => s.name));
      for (const [n, s] of Object.entries(old.secrets)) {
        if (names.has(n)) {
          next.secrets[n] = s;
          note.kept.push(`secrets.${n}`);
        } else note.dropped.push(`secrets.${n}`);
      }
      next.enabled = old.enabled;
      next.installed_at = old.installed_at;
      next.installed_by = old.installed_by;
      next.row_version = old.row_version + 1;
      next.run = { ...old.run };
      this.rows[next.id] = next;
      if (old.enabled) this.boot(next, now, `[host] ${next.id} ${old.manifest.version} replaced by ${m.version}`);
    } else this.rows[next.id] = next;
    return { status: old ? 200 : 201, body: { contract_version: CONTRACT, plugin: this.full(next), ...note } };
  }

  /** Why the plugin may not be enabled or started now. */
  enableCheck(p: MockPlugin): void {
    if (p.integrity !== 'ok') no('integrity', `${p.id}: its tree is ${p.integrity} since the install (sha256 ${p.sha256.slice(0, 12)} recorded); install it again`, { field: 'enabled' });
  }

  /** What the plugins scope writes (mock/settings.ts): the registry row, then the process follows. */
  write(id: string, w: { enabled?: boolean | null; settings_set?: Record<string, unknown>; settings_unset?: string[]; secrets_set?: Record<string, string>; secrets_unset?: string[]; grant?: string[]; revoke?: string[]; trust?: 'sandboxed' | 'host'; confirmed?: boolean }, who: string, now: number): void {
    const p = this.need(id);
    Object.assign(p.settings, copy(w.settings_set ?? {}));
    for (const k of w.settings_unset ?? []) delete p.settings[k];
    for (const [n, v] of Object.entries(w.secrets_set ?? {})) p.secrets[n] = { value: v, set_at: now };
    for (const n of w.secrets_unset ?? []) delete p.secrets[n];
    for (const c of w.grant ?? []) if (!p.granted[c]) p.granted[c] = { by: who, at: now, confirmed_by: needsConfirm(c) && w.confirmed ? who : null, confirmed_at: needsConfirm(c) && w.confirmed ? now : null };
    for (const c of w.revoke ?? []) delete p.granted[c];
    if (w.trust) p.trust_granted = w.trust;
    const was = p.enabled;
    if (typeof w.enabled === 'boolean') {
      if (w.enabled) this.enableCheck(p);
      p.enabled = w.enabled;
    }
    p.updated_at = now;
    p.updated_by = who;
    p.row_version++;
    // the plugin host reads the registry again: off stops it, a change starts it again with it
    if (!p.enabled) {
      if (was) this.line(p, now, '[host] disabled: its process ended');
      p.run = { ...fresh(now), log: p.run.log, refusals: p.run.refusals };
    } else {
      p.run.gave_up = null;
      this.boot(p, now, was ? '[host] its settings or grants changed: started again with them' : '[host] enabled');
    }
  }

  // ---- the plugin host ----------------------------------------------------------------------------

  private line(p: MockPlugin, at: number, text: string): void {
    let t = text;
    for (const [n, s] of Object.entries(p.secrets)) t = t.split(s.value).join(`[secret ${n}]`);
    p.run.log.push(`${stamp(at)} ${t}`);
    if (p.run.log.length > 1000) p.run.log.splice(0, p.run.log.length - 1000);
  }

  private crashLog(p: MockPlugin, at: number): string[] {
    const out: string[] = [];
    for (let i = 0; i < MAX_CRASHES; i++) {
      const t = at - (MAX_CRASHES - i) * 9000;
      out.push(`${stamp(t)} [host] starting ${p.id} ${p.manifest.version} (docker)`, `${stamp(t + 400)} Error: connect ECONNREFUSED 192.168.1.52:4318`, `${stamp(t + 400)}     at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1611:16)`, `${stamp(t + 420)} [host] exited with 1 after 0.4 s`);
    }
    out.push(`${stamp(at)} [host] 5 failures within an hour: gave up and disabled ${p.id}`);
    return out;
  }

  /** Start the process (again): it is starting until START_MS passed. */
  private boot(p: MockPlugin, now: number, why: string | null = null): void {
    const r = p.run;
    if (why) this.line(p, now, why);
    r.held = false;
    r.next_at = null;
    const asked = p.manifest.trust ?? 'sandboxed';
    if ((p.manifest.kind ?? 'plugin') !== 'plugin') {
      r.state = 'disabled';
      return;
    }
    if (asked === 'engine' || (asked === 'host' && p.trust_granted !== 'host')) {
      r.state = 'refused';
      r.since = now;
      r.started_at = null;
      r.last_error = asked === 'engine' ? 'trust engine is not available yet' : 'it asks to run on the host, which was not granted: set its trust key to host, or it does not start';
      this.line(p, now, `[host] refused: ${r.last_error}`);
      return;
    }
    if (asked === 'sandboxed' && !this.sandbox) {
      r.state = 'unavailable';
      r.since = now;
      r.started_at = null;
      r.next_at = now + 30_000;
      r.last_error = 'no sandbox runtime on this machine (docker, podman or kubernetes)';
      return;
    }
    r.state = 'starting';
    r.since = now;
    r.started_at = null;
    this.line(p, now, `[host] starting ${p.id} ${p.manifest.version} (${asked === 'host' ? 'host' : 'docker'})`);
  }

  private fail(p: MockPlugin, now: number): void {
    const r = p.run;
    r.last_error = `${p.manifest.entry?.command?.[1] ?? 'the process'} exited with 1: Error: connect ECONNREFUSED 192.168.1.52:4318`;
    r.crashes = [...r.crashes.filter((t) => now - t < 3_600_000), now];
    this.line(p, now, 'Error: connect ECONNREFUSED 192.168.1.52:4318');
    this.line(p, now, '[host] exited with 1 after 0.4 s');
    r.started_at = null;
    if (r.crashes.length >= MAX_CRASHES) {
      r.state = 'failed';
      r.since = now;
      r.next_at = null;
      r.gave_up = { at: now, error: r.last_error, failures: r.crashes.length };
      p.enabled = false;
      p.updated_by = 'plugin-host';
      p.updated_at = now;
      p.row_version++;
      this.line(p, now, `[host] ${r.crashes.length} failures within an hour: gave up and disabled ${p.id}`);
      return;
    }
    r.state = 'backoff';
    r.since = now;
    r.next_at = now + BACKOFF_S[Math.min(r.crashes.length - 1, BACKOFF_S.length - 1)] * 1000;
  }

  /** The clock moves every process on. */
  private advance(p: MockPlugin, now: number): void {
    const r = p.run;
    for (let i = 0; i < 12; i++) {
      if (r.state === 'starting' && now - r.since >= (p.crashes ? 400 : START_MS)) {
        const at = r.since + (p.crashes ? 400 : START_MS);
        if (p.crashes) this.fail(p, at);
        else {
          r.state = 'running';
          r.since = at;
          r.started_at = at;
          this.line(p, at, '[host] initialize ok in 212 ms');
          const c = p.manifest.contributes ?? {};
          const n = [c.tools?.length ? `${c.tools.length} tool(s)` : '', c.hooks?.length ? `${this.liveHooks(p).length} hook(s)` : '', c.decision_providers?.length ? `${this.liveProviders(p).length} provider(s)` : '', c.backends?.length ? `${c.backends.length} backend(s)` : '', c.panels?.length ? `${c.panels.length} panel(s)` : ''].filter(Boolean);
          this.line(p, at, `[host] registered ${n.join(', ') || 'nothing'}`);
          for (const x of this.notes(p)) this.line(p, at, `[host] not registered: ${x}`);
        }
      } else if (r.state === 'backoff' && r.next_at !== null && now >= r.next_at) {
        const at = r.next_at;
        r.restarts++;
        this.boot(p, at);
      } else if (r.state === 'unavailable' && this.sandbox) this.boot(p, now);
      else break;
    }
  }

  private level(p: MockPlugin): number {
    return Math.max(-1, ...['meta', 'redacted', 'full'].map((l, i) => (p.granted[`prompts:${l}`] ? i : -1)));
  }
  private liveHooks(p: MockPlugin): string[] {
    return (p.manifest.contributes?.hooks ?? []).filter((h) => ['meta', 'redacted', 'full'].indexOf(h.payload ?? 'meta') <= this.level(p)).map((h) => `${p.id}/${h.name}`);
  }
  private liveProviders(p: MockPlugin): string[] {
    return this.level(p) < 0 ? [] : (p.manifest.contributes?.decision_providers ?? []).map((d) => `${p.id}/${d.name}`);
  }
  private notes(p: MockPlugin): string[] {
    const out: string[] = [];
    for (const h of p.manifest.contributes?.hooks ?? []) if (!this.liveHooks(p).includes(`${p.id}/${h.name}`)) out.push(`the hook ${h.name} needs prompts:${h.payload ?? 'meta'}, which is not granted`);
    if (this.level(p) < 0) for (const d of p.manifest.contributes?.decision_providers ?? []) out.push(`the decision provider ${d.name} needs a prompts: capability, and none is granted`);
    return out;
  }

  status(id: string, now: number): Status {
    const p = this.need(id);
    this.advance(p, now);
    const r = p.run;
    const run = r.state === 'running';
    const c = p.manifest.contributes ?? {};
    const asked = p.manifest.trust ?? 'sandboxed';
    const state = r.state === 'disabled' && p.enabled && !r.gave_up ? 'stopped' : r.state;
    return {
      id, state, held: r.held, runtime: run || r.state === 'starting' ? (asked === 'host' ? 'host' : 'docker') : null, where: run ? (asked === 'host' ? 'pid 48213' : `tandem-plugin-${id}`) : null, version: p.manifest.version ?? null,
      started_at: run ? r.started_at : null, restarts: r.restarts, failures_last_hour: r.crashes.filter((t) => now - t < 3_600_000).length, next_start_at: r.state === 'backoff' || r.state === 'unavailable' ? r.next_at : null, last_error: r.last_error,
      tools: run ? (c.tools ?? []).map((t) => t.name) : [], hooks: run ? this.liveHooks(p) : [], providers: run ? this.liveProviders(p) : [], backends: run ? (c.backends ?? []).map((b) => b.name) : [], skills: run ? [...(c.skills ?? [])] : [],
      panels: run ? (c.panels ?? []).map((x) => x.name) : [], notes: run ? this.notes(p) : [], refusals: r.refusals.slice(-20), trust: { requested: asked, granted: p.trust_granted }, gave_up: r.gave_up ? { ...r.gave_up } : null,
    };
  }

  private refusal(p: MockPlugin, capability: string, what: string, now: number): void {
    p.run.refusals.push({ ts: now, capability, what });
    this.line(p, now, `[host] refused: ${what} (needs ${capability})`);
  }

  panelsOf(id: string, now: number): Panel[] {
    const p = this.need(id);
    this.advance(p, now);
    if (p.run.state !== 'running') return [];
    const methods: Panel['bridge']['methods'] = ['plugin.info', 'plugin.status', ...(p.granted['settings:read'] ? (['settings.read'] as const) : []), 'tools.call'];
    return (p.manifest.contributes?.panels ?? []).map((x) => ({
      name: x.name, title: x.title, url: `/plugin-panels/${id}/${x.name}/`, sandbox: 'allow-scripts allow-forms', csp: PANEL_CSP,
      bridge: { message: "postMessage({type: 'tandem-bridge', id, method, params}) to the parent; the answer is {type: 'tandem-bridge-result', id, result | error}", methods },
    }));
  }

  /** A panel's page, as /plugin-panels/<id>/<panel>/ serves it; null: 404. */
  panelFile(id: string, name: string, now = this.now): string | null {
    const p = this.rows[id];
    if (!p) return null;
    this.advance(p, now);
    if (p.run.state !== 'running' || this.gone.has(`${id}/${name}`)) return null;
    return p.panels[name] ?? null;
  }

  private bridge(p: MockPlugin, body: Record<string, unknown>, now: number): unknown {
    if (p.run.state !== 'running') no('plugin_down', `the plugin ${p.id} is not running`);
    const method = body.method as string;
    const known = ['plugin.info', 'plugin.status', 'settings.read', 'tools.call', 'engine.control'];
    if (!known.includes(method)) no('refused', `the bridge has no method '${method}'`);
    if (method === 'engine.control') {
      this.refusal(p, 'engine:control', 'engine control from its panel (not available yet)', now);
      no('refused', 'engine control is not available to plugins yet');
    }
    if (method === 'settings.read' && !p.granted['settings:read']) {
      this.refusal(p, 'settings:read', 'the bridge’s settings.read', now);
      no('refused', 'settings.read needs settings:read, which was not granted');
    }
    if (method === 'plugin.info') return { id: p.id, version: p.manifest.version, title: p.manifest.title ?? null, capabilities: Object.keys(p.granted).sort() };
    if (method === 'plugin.status') {
      const s = this.status(p.id, now);
      return { state: s.state, restarts: s.restarts, tools: s.tools, hooks: s.hooks, providers: s.providers };
    }
    if (method === 'settings.read') return { settings: this.effective(p) };
    const params = (body.params ?? {}) as { tool?: unknown; arguments?: Record<string, unknown> };
    const tool = (p.manifest.contributes?.tools ?? []).find((t) => t.name === params.tool);
    if (!tool) no('not_found', `no tool '${String(params.tool)}' of ${p.id}`);
    if (tool!.annotations?.readOnlyHint !== true) {
      this.refusal(p, 'tools:write', `the tool ${tool!.name} (not read-only) from its panel`, now);
      no('refused', `a panel may call only read-only tools; ${tool!.name} is not`);
    }
    if (tool!.name === 'greet') {
      const who = params.arguments?.name;
      if (typeof who !== 'string' || !who) no('bad_arguments', 'greet: name is required');
      const s = this.effective(p);
      return { content: [{ type: 'text', text: `${s.greeting ?? 'Hello'}, ${who}${s.excited ? '!' : '.'}` }] };
    }
    return { content: [{ type: 'text', text: JSON.stringify({ day: new Date(now).toISOString().slice(0, 10), users: { ada: 182_400, lin: 96_120 }, models: { 'TandemLLM': 278_520 } }) }] };
  }

  // ---- the routes ---------------------------------------------------------------------------------

  private forced(route: string): void {
    const type = this.fails.get(route);
    if (type) no(type, SENTENCE[type] ?? `${type} (the mock was told to refuse ${route})`);
  }
  private rights(): void {
    if (!this.trust) no('unauthorized', 'installing, enabling and removing a plugin take pairing’s rights: set an admin password (App settings) to do it from another machine, or do it from this one');
  }
  private fields(body: Record<string, unknown>, allowed: string[]): void {
    const extra = Object.keys(body).filter((k) => !allowed.includes(k)).sort();
    if (extra.length) no('bad_request', `${extra[0]}: not a field of this request`, { field: extra[0] });
  }

  /** One request. `parts`: the path after /v1/fleet (`plugins`, then the rest). */
  handle(method: string, parts: string[], q: URLSearchParams, body: unknown, nowMs = Date.now(), who = 'admin'): Reply {
    this.now = nowMs;
    try {
      return this.route(method, parts.slice(1).map((p) => decodeURIComponent(p)), q, body, nowMs, who);
    } catch (e) {
      if (e instanceof Refuse) return { status: e.status, body: { error: { type: e.type, message: e.message, ...e.facts } } };
      throw e;
    }
  }

  private route(method: string, sub: string[], q: URLSearchParams, raw: unknown, now: number, who: string): Reply {
    if (raw !== null && raw !== undefined && (typeof raw !== 'object' || Array.isArray(raw))) no('bad_request', 'the body must be a JSON object');
    const body = (raw ?? {}) as Record<string, unknown>;
    const v = { contract_version: CONTRACT };
    const ok = (out: Record<string, unknown>, status = 200): Reply => ({ status, body: { ...v, ...out } });
    if (!sub.length) {
      if (method === 'GET') {
        this.forced('list');
        return ok({ plugin_api: CONTRACT, hooks_api: HOOKS_API, tandemapp: TANDEMAPP, plugins: this.list().map((p) => this.view(p)) });
      }
      if (method === 'POST') {
        this.rights();
        this.forced('install');
        this.fields(body, ['source', 'upgrade', 'allow_downgrade']);
        for (const f of ['upgrade', 'allow_downgrade']) if (f in body && typeof body[f] !== 'boolean') no('bad_request', `${f}: true or false`, { field: f });
        return this.install(body, who, now);
      }
    } else if (sub.length === 1 && sub[0] === 'inspect' && method === 'POST') {
      this.forced('inspect');
      this.fields(body, ['source']);
      const { pack, source } = this.stage(body.source);
      return ok(this.report(pack, source));
    } else if (sub.length === 1) {
      const p = this.need(sub[0]);
      if (method === 'GET') {
        this.forced('get');
        this.advance(p, now);
        return ok({ plugin: this.full(p) });
      }
      if (method === 'DELETE') {
        this.rights();
        this.forced('delete');
        const refs = this.refs[p.id] ?? [];
        if (refs.length) no('referenced', `${p.id} is named by ${refs.length} hook(s): ${refs.map((r) => r.id).join(', ')}. Remove those hooks first`, { hooks: copy(refs) });
        delete this.rows[p.id];
        return ok({ removed: p.id, version: p.manifest.version });
      }
    } else if (sub.length === 2) {
      const p = this.need(sub[0]);
      const act = sub[1];
      this.advance(p, now);
      if (method === 'GET' && act === 'status') {
        this.forced('status');
        return ok({ status: this.status(p.id, now) });
      }
      if (method === 'GET' && act === 'logs') {
        this.forced('logs');
        const n = Number(q.get('tail') ?? 200);
        if (!Number.isInteger(n)) no('bad_request', 'tail: a number 1..1000', { field: 'tail' });
        return ok({ id: p.id, lines: p.run.log.slice(-Math.max(1, Math.min(1000, n))) });
      }
      if (method === 'GET' && act === 'panels') {
        this.forced('panels');
        return ok({ id: p.id, panels: this.panelsOf(p.id, now) });
      }
      if (method === 'POST') {
        if (act === 'enable' || act === 'disable') {
          if (act === 'enable') this.rights();
          this.fields(body, []);
          if (p.enabled !== (act === 'enable')) this.write(p.id, { enabled: act === 'enable' }, who, now);
          return ok({ plugin: this.full(p) });
        }
        if (act === 'verify') {
          this.forced('verify');
          this.fields(body, []);
          p.checked_at = now;
          return ok({ id: p.id, integrity: p.integrity, sha256: p.integrity === 'ok' ? p.sha256 : p.integrity === 'missing' ? null : sha(`tree:${p.id}+changed`), expected: p.sha256 });
        }
        if (act === 'start' || act === 'stop' || act === 'restart') {
          this.forced(act);
          this.fields(body, []);
          if (act === 'stop') {
            if (p.run.state !== 'disabled' && p.run.state !== 'failed') {
              this.line(p, now, '[host] stopped by hand: held until a start by hand');
              p.run = { ...p.run, state: 'stopped', held: true, since: now, started_at: null, next_at: null };
            }
          } else {
            if (!p.enabled) no('disabled', `${p.id} is disabled: enable it first`);
            this.enableCheck(p);
            if (act === 'restart') p.run.restarts++;
            this.boot(p, now, act === 'restart' ? '[host] restarted by hand' : '[host] started by hand');
          }
          return ok({ status: this.status(p.id, now) });
        }
        if (act === 'bridge') {
          this.forced('bridge');
          this.fields(body, ['method', 'params', 'panel']);
          if (typeof body.method !== 'string') no('bad_request', 'method: the bridge method', { field: 'method' });
          return ok({ result: this.bridge(p, body, now) });
        }
      }
    }
    return no('not_found', `no route ${method} /v1/fleet/plugins${sub.length ? '/' : ''}${sub.join('/')}`);
  }
}

/** MAJOR.MINOR.PATCH with an optional -pre; a pre-release sorts before its release */
function versionOrder(a: string, b: string): number {
  const parse = (v: string): [number[], string | null] => {
    const [core, ...pre] = v.split('-');
    return [core.split('.').map((x) => Number(x) || 0), pre.length ? pre.join('-') : null];
  };
  const [ca, pa] = parse(a);
  const [cb, pb] = parse(b);
  for (let i = 0; i < 3; i++) if ((ca[i] ?? 0) !== (cb[i] ?? 0)) return (ca[i] ?? 0) - (cb[i] ?? 0);
  if (pa === pb) return 0;
  return pa === null ? 1 : pb === null ? -1 : pa < pb ? -1 : 1;
}
