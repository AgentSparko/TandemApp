// App settings in the mock (`/v1/fleet/app*`, contracts/fleet-v1, 6.3b), as the controller answers
// them (proxy/appsettings.py): the permissions of the controller's machine (a Mac in gold3, a Spark
// otherwise), the service it runs as, the configuration file and its import (plan, then apply with
// the plan's confirm: 428 without, 409 spent or the fleet changed). The admin password
// (/v1/fleet/app/password) is the mock server's (mock/server.ts): it holds the sessions. Mock-only:
// a deployment record the file has and this fleet not is a conflict (the mock makes no records
// from a file), and the export's member engines are null. What runs here (/v1/fleet/app/parts,
// OPS-288) starts bare as a new install does: Chat and the conductor off, no backend, no engine host
// over SSH until one is paired; a switch takes its plan's confirm (409 stale_plan for one that no
// longer matches). The same switches are the app scope of the settings interface (mock/settings.ts).

import type { AppParts, AppPermission, AppService, AppSetup, AppView, FleetConfigFile, ImportItem, ImportMode, ImportPlan, ImportResult, Layout, PartId, PartPlan } from '../src/api/fleet.ts';
import { FLEET_CONTRACT } from '../src/api/fleet.ts';
import type { MockFleet } from './fleet.ts';

type Reply = { status: number; body: unknown };
const bad = (status: number, type: string, message: string): Reply => ({ status, body: { error: { type, message } } });
const CONFIRM_S = 300;
const EXCLUDED = [
  "this controller's certificate key and every node's trust (only each pin's SHA-256 is kept)",
  'SSH routes, key files and tunnels',
  'the admin password and the admin token',
  'the backends and their API keys',
  'the metric history and the event log',
];
const KIND = 'tandemapp-fleet-config';

/** `alias Gold → Golden`, `enabled on → off` */
function was(k: string, a: unknown, b: unknown): string {
  const w = (v: unknown) => (typeof v === 'boolean' ? (v ? 'on' : 'off') : Array.isArray(v) ? v.join(', ') || 'none' : v == null ? 'none' : String(v));
  return `${k} ${w(a)} → ${w(b)}`;
}

function stable(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`;
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${stable((v as Record<string, unknown>)[k])}`).join(',')}}`;
  return JSON.stringify(v);
}

export class MockApp {
  private tokens = new Map<string, { file: string; mode: ImportMode; state: string; until: number }>();
  private seq = 0;
  settings: Record<string, unknown> = {};
  /** OPS-288: what runs here, bare as a new install */
  on: Record<PartId, boolean> = { chat: false, conductor: false };
  switched: Record<PartId, boolean> = { chat: false, conductor: false };

  constructor(private fleet: MockFleet) {}

  private get mac(): boolean {
    return this.fleet.hostInfo().may_sleep;
  }

  private service(): AppService {
    return this.mac
      ? { manager: 'launchd', unit: 'org.tandemllm.app', starts_at_boot: true, restarts: true, linger: null, text: 'A launchd agent (org.tandemllm.app): it starts at login and while the Mac is awake.' }
      : { manager: 'systemd', unit: 'tandemapp.service', starts_at_boot: true, restarts: true, linger: true, text: 'A systemd user unit (tandemapp.service), enabled, restarted when it fails.' };
  }

  permissions(): AppPermission[] {
    const mac = this.mac;
    const lan = this.fleet.discovery.lanState;
    const state = !mac ? 'not_applicable' : lan === 'granted' ? 'granted' : lan === 'denied' ? 'lan_denied' : 'not_asked';
    const files = [{ what: 'the controller’s certificate key', path: '~/.tandemllm/controller/controller.key', mode: '0600', state: 'ok' as const }];
    const system = this.fleet.handle('GET', '/v1/fleet/rules', new URLSearchParams(), null).body as { rules: { enabled: boolean; notify: string[] }[] };
    const n = system.rules.filter((r) => r.enabled && r.notify.includes('system')).length;
    return [
      mac
        ? {
            id: 'local_network', title: 'Local Network', applies: true, state, why: 'Find lists the Sparks on your network, and the controller reaches them there.',
            detail: state === 'granted' ? 'Allowed: this controller reaches the local network.' : state === 'lan_denied' ? 'macOS keeps this controller off the local network: every LAN address fails (EHOSTUNREACH), not only discovery. A controller started by launchd (the login service) gets no prompt for it.' : 'Not checked yet: Check asks macOS (its prompt shows the first time).',
            fallback: state !== 'lan_denied' ? null : 'Run the controller on a Spark (Move, above), or start it from a Terminal window. Adding a Spark by its address needs this permission too.',
            settings: 'System Settings → Privacy & Security → Local Network', action: { kind: 'check', label: 'Check now' },
          }
        : { id: 'local_network', title: 'Local Network', applies: false, state: 'not_applicable', why: 'Find lists the Sparks on your network, and the controller reaches them there.', detail: 'Linux asks no app for the local network: nothing to allow.', fallback: null, action: null },
      {
        id: 'keys', title: 'Keychain and key files', applies: mac, state: mac ? 'file' : 'not_applicable', why: 'The controller’s certificate key pins it to every Spark; the SSH key reaches a rented one.',
        detail: `${mac ? 'The Keychain is not used for the certificate or SSH keys (not built): ' : 'Linux has no Keychain here: '}1 key file only your user can read (0600). The API keys' master key is ${mac ? 'in the Keychain' : 'in a file beside the config'}.`,
        fallback: null, files, action: null,
      },
      { id: 'notifications', title: 'Notifications', applies: true, state: 'in_browser', why: 'An alert shows as a notification while the page is closed or hidden.', detail: `The browser asks, not the controller (it sends none itself). ${n} alert rule${n === 1 ? '' : 's'} notif${n === 1 ? 'ies' : 'y'} this way.`, fallback: 'Alerts stay in Events & Alerts and on the fleet readout.', action: { kind: 'browser', label: 'Allow notifications' } },
      { id: 'node_sudo', title: 'Administrator rights on a Spark', applies: true, state: 'per_action', why: 'Setting up a fabric link changes the Spark’s network (link addresses, MTU, modules).', detail: 'Asked for each such action on that Spark, used once, never stored. Nothing else on a node needs them.', fallback: null, action: null },
    ];
  }

  view(): Omit<AppView, 'password'> {
    return { contract_version: FLEET_CONTRACT, platform: this.mac ? 'mac' : 'linux', release: '0.15.1', permissions: this.permissions(), service: this.service(), export: { kind: KIND, version: 1, secrets: 'excluded', excluded: EXCLUDED }, setup: this.setup() };
  }

  /** OPS-282: as proxy/fleethealth.py setup_state: engine hosts (SSH), other nodes, backends on no node */
  private setup(): AppSetup {
    const here = this.fleet.hereId();
    const doc = this.fleet.handle('GET', '/v1/fleet/nodes', new URLSearchParams(), null).body as { nodes: { id: string; transport: string }[]; backends: unknown[] };
    const engines = doc.nodes.filter((n) => n.transport === 'ssh').length;
    const nodes = doc.nodes.filter((n) => n.transport !== 'ssh' && n.id !== here).length;
    const backends = doc.backends.length;
    const needs = engines === 0 && nodes === 0 && backends === 0;
    return { needs_setup: needs, engines, nodes, backends, first_page: needs ? 'add_engine' : null };
  }

  // ---- the file --------------------------------------------------------------------------------
  export(now: number): FleetConfigFile {
    const nodes = this.fleet.nodes(now);
    const here = this.fleet.hereId();
    const rules = (this.fleet.handle('GET', '/v1/fleet/rules', new URLSearchParams(), null).body as { rules: { id: string; enabled: boolean; notify: ('app' | 'system')[]; threshold: number | null; for_s: number; scope: string }[] }).rules;
    return {
      kind: KIND, version: 1, release: '0.15.1', exported_at: now, from: { name: 'TandemApp on mock', fingerprint: 'c0'.repeat(32), node: here }, secrets: 'excluded', excluded: EXCLUDED,
      nodes: nodes.map((n) => ({
        id: n.id, alias: n.alias, colour: n.colour, icon: n.icon, tags: [...n.tags], class: n.class, hw: n.hw, address: n.address.replace(/:\d+$/, ''), transport: n.transport,
        lease_end_ms: n.lease_end == null ? null : n.lease_end * 1000, cost_per_hour: n.cost_per_hour, controller_host: n.id === here, paired: n.pairing.state === 'paired', pin_sha256: n.pairing.state === 'paired' ? n.pairing.fingerprint : null,
      })),
      deployments: this.fleet.deployments(now).filter((d) => d.record !== false).map((d) => ({ id: d.id, state: d.state ?? 'serving', name: d.name, model: d.model, pack: d.pack, type: d.type, members: d.members.map((m) => ({ node: m, engine: null, backend: null, model: d.api_id })), stages: d.stages ?? [], options: { api_id: d.api_id, ctx: d.ctx } })),
      rules: rules.map((r) => ({ id: r.id, enabled: r.enabled, notify: [...r.notify], threshold: r.threshold, for_s: r.for_s, scope: r.scope })),
      layouts: this.fleet.savedLayouts().map((l) => ({ page: l.page, device: l.device, doc: { ...l } as Record<string, unknown> })),
      settings: { ...this.settings },
    };
  }

  private state(now: number): string {
    const e = this.export(now);
    return stable({ nodes: e.nodes.map(({ id, alias, colour, icon, tags }) => ({ id, alias, colour, icon, tags })), deps: e.deployments, rules: e.rules, layouts: e.layouts, settings: e.settings });
  }

  plan(body: unknown, now: number, mint = true): Reply & { apply?: (() => void)[] } {
    const b = body as { config?: unknown; mode?: unknown; confirm?: unknown } | null;
    if (!b || typeof b !== 'object' || !('config' in b) || Object.keys(b).some((k) => !['config', 'mode', 'confirm'].includes(k))) return bad(400, 'bad_request', 'the body is {config, mode?, confirm?}: config is the exported file');
    const mode = (b.mode ?? 'merge') as ImportMode;
    if (mode !== 'merge' && mode !== 'replace') return bad(400, 'bad_request', 'mode is merge or replace');
    const doc = b.config as FleetConfigFile | null;
    if (!doc || typeof doc !== 'object') return bad(400, 'bad_request', 'the file is a JSON object');
    if ((doc as { kind?: string }).kind === 'tandemapp-export') return bad(400, 'bad_request', 'this is a controller move’s export (with its keys): take the fleet with Take the fleet instead');
    if (doc.kind !== KIND) return bad(400, 'bad_request', `not a TandemApp fleet configuration (kind is ${KIND})`);
    if (doc.version !== 1) return bad(400, 'bad_request', `version ${JSON.stringify(doc.version)}: this controller reads version 1`);
    for (const k of ['nodes', 'deployments', 'rules', 'layouts'] as const) if (doc[k] != null && !Array.isArray(doc[k])) return bad(400, 'bad_request', `${k} is a list`);
    const items: ImportItem[] = [];
    const apply: (() => void)[] = [];
    const it = (kind: ImportItem['kind'], id: string, label: string, op: ImportItem['op'], detail: string | null = null, fields: string[] = [], fn?: () => void) => {
      items.push({ kind, id, label, op, detail, fields });
      if (fn) apply.push(fn);
    };
    const nodes = this.fleet.nodes(now);
    const here = this.fleet.hereId();
    const aliases = new Map(nodes.map((n) => [n.alias.toLowerCase(), n.id]));
    const colours = new Map(nodes.map((n) => [n.colour, n.id]));
    const fileIds = new Set<string>();
    for (const n of doc.nodes ?? []) {
      fileIds.add(n.id);
      const r = nodes.find((x) => x.id === n.id);
      const a = typeof n.alias === 'string' ? n.alias.trim() : '';
      if (!a || a.length > 40) {
        it('node', n.id, n.id, 'conflict', 'its alias is not 1 to 40 characters');
        continue;
      }
      const holder = aliases.get(a.toLowerCase());
      if (holder && holder !== n.id) {
        it('node', n.id, a, 'conflict', `the alias ${a} is ${nodes.find((x) => x.id === holder)?.alias}'s here`);
        continue;
      }
      const note: string[] = [];
      let colour: number | undefined = n.colour;
      const cHolder = colour != null ? colours.get(colour) : undefined;
      if (colour != null && cHolder && cHolder !== n.id) {
        note.push(`colour ${colour} is ${nodes.find((x) => x.id === cHolder)?.alias}'s here: it keeps its own`);
        colour = undefined;
      }
      if (r) {
        const want: Record<string, unknown> = { alias: a, ...(colour != null ? { colour } : {}), icon: n.icon, tags: n.tags };
        const changed = Object.keys(want).filter((k) => stable(want[k]) !== stable((r as unknown as Record<string, unknown>)[k]));
        if (r.id === here) note.unshift('this controller’s own machine: name and looks only');
        if (n.pin_sha256 && r.pairing.fingerprint && n.pin_sha256 !== r.pairing.fingerprint) note.push('paired here with another certificate: the pairing here stays');
        if (changed.length) {
          const patch = Object.fromEntries(changed.map((k) => [k, want[k]]));
          it('node', n.id, r.alias, 'change', [...changed.map((k) => was(k, (r as unknown as Record<string, unknown>)[k], want[k])), ...note].join('; '), changed, () => void this.fleet.handle('PATCH', `/v1/fleet/nodes/${encodeURIComponent(n.id)}`, new URLSearchParams(), patch, now * 1000));
          aliases.set(a.toLowerCase(), n.id);
        } else it('node', n.id, r.alias, 'keep', note.join('; ') || null);
        continue;
      }
      if (n.transport === 'tunnel') {
        it('node', n.id, a, 'conflict', 'reached through an SSH tunnel: add it again with its key (Add Spark), the file holds no route');
        continue;
      }
      const addr = typeof n.address === 'string' && /^[0-9.]+$/.test(n.address) ? n.address : null;
      if (!addr) {
        it('node', n.id, a, 'conflict', 'no address to pair it at');
        continue;
      }
      note.unshift(`added unpaired at ${addr}: pair it with Add Spark, it keeps this name`);
      aliases.set(a.toLowerCase(), n.id);
      if (colour != null) colours.set(colour, n.id);
      it('node', n.id, a, 'add', note.join('; '), ['alias', 'colour', 'icon', 'tags'], () => this.fleet.importNode({ id: n.id, alias: a, colour, icon: n.icon, tags: n.tags, address: addr }, now));
    }
    if (mode === 'replace') for (const r of nodes) if (!fileIds.has(r.id)) it('node', r.id, r.alias, 'keep', r.id === here ? 'this controller’s own machine' : 'paired: release it on its page to remove it');
    // deployments: the mock keeps what it has
    const deps = this.export(now).deployments;
    for (const d of doc.deployments ?? []) {
      const r = deps.find((x) => x.id === d.id);
      if (!r) it('deployment', d.id, d.name ?? d.id, 'conflict', 'its engine is not known here: serve it from Models once its Spark answers');
      else if (stable({ ...d, state: null }) === stable({ ...r, state: null })) it('deployment', d.id, r.name, 'keep');
      else it('deployment', d.id, r.name, 'conflict', r.state === 'serving' ? 'serves here and differs: stop it on Models first' : 'differs: the mock keeps its own record');
    }
    // rules
    const rules = this.export(now).rules;
    for (const x of doc.rules ?? []) {
      const r = rules.find((y) => y.id === x.id);
      if (!r) {
        it('rule', x.id, x.id, 'conflict', 'no such rule in this release');
        continue;
      }
      const want: Record<string, unknown> = { enabled: x.enabled, notify: x.notify, for_s: x.for_s, ...(r.threshold != null ? { threshold: x.threshold } : {}), ...(r.scope !== 'host' ? { scope: x.scope } : {}) };
      const diff = Object.keys(want).filter((k) => want[k] !== undefined && stable(want[k]) !== stable((r as Record<string, unknown>)[k]));
      if (diff.length) it('rule', x.id, x.id, 'change', diff.map((k) => was(k, (r as Record<string, unknown>)[k], want[k])).join('; '), diff, () => void this.fleet.handle('PATCH', `/v1/fleet/rules/${x.id}`, new URLSearchParams(), Object.fromEntries(diff.map((k) => [k, want[k]])), now * 1000));
      else it('rule', x.id, x.id, 'keep');
    }
    // layouts
    const have = new Map(this.fleet.savedLayouts().map((l) => [`${l.page}|${l.device}`, l]));
    for (const x of doc.layouts ?? []) {
      const label = `${x.page} · ${x.device}`;
      const old = have.get(`${x.page}|${x.device}`);
      const strip = (l: Record<string, unknown>) => stable({ ...l, saved_at: null });
      if (old && strip(old as unknown as Record<string, unknown>) === strip(x.doc)) it('layout', label, label, 'keep');
      else it('layout', label, label, old ? 'change' : 'add', null, [], () => void this.fleet.handle('PUT', `/v1/fleet/layouts/${encodeURIComponent(x.page)}`, new URLSearchParams({ device: x.device }), { ...(x.doc as unknown as Layout), saved_at: null }, now * 1000));
    }
    if (mode === 'replace')
      for (const [k, l] of have) if (!(doc.layouts ?? []).some((x) => `${x.page}|${x.device}` === k)) it('layout', `${l.page} · ${l.device}`, `${l.page} · ${l.device}`, 'remove', 'back to the page’s default', [], () => this.fleet.dropLayout(l.page, l.device));
    // settings
    for (const [k, v] of Object.entries(doc.settings ?? {})) {
      if (!k.startsWith('app.')) it('setting', k, k, 'conflict', 'not an app setting (app.…)');
      else if (stable(this.settings[k]) === stable(v)) it('setting', k, k, 'keep');
      else it('setting', k, k, k in this.settings ? 'change' : 'add', null, [], () => void (this.settings[k] = v));
    }
    const counts = { add: 0, change: 0, remove: 0, keep: 0, conflict: 0 };
    for (const i of items) counts[i.op] += 1;
    const blocked = counts.add + counts.change + counts.remove ? null : 'nothing to change: the registry is as the file has it';
    const same = doc.from?.fingerprint === 'c0'.repeat(32);
    const plan: ImportPlan = {
      contract_version: FLEET_CONTRACT, mode, from: { name: doc.from?.name ?? null, fingerprint: doc.from?.fingerprint ?? null, exported_at: doc.exported_at ?? null, release: doc.release ?? null, same_controller: same },
      identity: `this controller's certificate, its own node's address and trust, and its admin password stay as they are${same ? '' : '; the file comes from another controller: the nodes it adds trust that one, pair them here'}`,
      items, counts, allowed: blocked == null, blocked, confirm: null, expires_at: null,
    };
    if (blocked == null && mint) {
      const token = `imp-${++this.seq}`;
      this.tokens.set(token, { file: stable(doc), mode, state: this.state(now), until: now + CONFIRM_S });
      plan.confirm = token;
      plan.expires_at = now + CONFIRM_S;
    }
    return { status: 200, body: plan, apply };
  }

  applyImport(body: unknown, now: number): Reply {
    const b = body as { config?: unknown; mode?: ImportMode; confirm?: unknown } | null;
    if (!b || typeof b !== 'object' || !b.confirm) return bad(428, 'confirm_required', 'an import needs its plan’s confirm: POST /v1/fleet/app/import first and send its `confirm`');
    const t = typeof b.confirm === 'string' ? this.tokens.get(b.confirm) : undefined;
    if (typeof b.confirm === 'string') this.tokens.delete(b.confirm);
    if (!t || t.until <= now || t.file !== stable(b.config) || t.mode !== (b.mode ?? 'merge')) return bad(409, 'confirm_invalid', 'that confirm is spent, too old or another file’s: read the plan again');
    if (t.state !== this.state(now)) return bad(409, 'confirm_invalid', 'the fleet changed since the plan: read the plan again');
    const p = this.plan({ config: b.config, mode: b.mode }, now, false);
    if (p.status !== 200) return p;
    const plan = p.body as ImportPlan;
    for (const fn of p.apply ?? []) fn();
    const applied = { add: plan.counts.add, change: plan.counts.change, remove: plan.counts.remove };
    this.fleet.addEvent(now, 'controller', 'host', `Fleet configuration imported (${plan.mode}): ${applied.add} added, ${applied.change} changed, ${applied.remove} removed, ${plan.counts.conflict} skipped`, `from ${plan.from.name ?? 'a file'}`);
    const out: ImportResult = { contract_version: FLEET_CONTRACT, mode: plan.mode, applied, skipped: plan.counts.conflict, items: plan.items };
    return { status: 200, body: out };
  }

  parts(): AppParts {
    const on = this.on.chat;
    return {
      contract_version: FLEET_CONTRACT,
      chat: { on, switched: this.switched.chat, install: 'off', runner: on ? 'own' : null, running: on ? true : null, sandbox: on ? 'none' : null, note: on ? 'No sandbox runtime on this host: chats run without their sandbox tools (files, shell, browser).' : 'Chat is off: no chatd runs here and the Chat tab says so.' },
      conductor: { on: this.on.conductor, switched: this.switched.conductor, install: 'off', dir: this.on.conductor ? '~/.tandemllm/conductor' : null },
      engines: this.fleet.engines.paired(),
      backends: [],
      mcp: { where: 'chat', available: on },
    };
  }

  private partPlan(part: PartId, on: boolean): PartPlan {
    const name = part === 'chat' ? 'Chat' : 'The conductor';
    if (this.on[part] === on) return { contract_version: FLEET_CONTRACT, part, on, changed: false, needs_confirm: false, confirm: null, changes: [`${name} is ${on ? 'on' : 'off'} already: nothing changes.`] };
    const changes = part === 'chat'
      ? on ? ['chatd starts on this host and is watched here.', 'The Chat tab opens.'] : ['chatd stops; the chats stay.', 'The Chat tab says Chat is off.']
      : on ? ['The conductor runs in front of the backends from the next request.'] : ['Every backend is relayed as it is.'];
    return { contract_version: FLEET_CONTRACT, part, on, changed: true, needs_confirm: true, confirm: `part-${part}-${on ? 1 : 0}-${this.on[part] ? 1 : 0}`, changes };
  }

  switchPart(part: string, body: unknown): Reply {
    if (part !== 'chat' && part !== 'conductor') return bad(404, 'not_found', `no part '${part}': chat or conductor`);
    const b = body as { on?: unknown; confirm?: unknown } | null;
    if (!b || typeof b !== 'object' || typeof b.on !== 'boolean' || Object.keys(b).some((k) => k !== 'on' && k !== 'confirm') || ('confirm' in b && typeof b.confirm !== 'string')) return bad(400, 'bad_request', 'the body is {on: true|false, confirm?}');
    const plan = this.partPlan(part, b.on);
    if (!('confirm' in b) || !plan.changed) return { status: 200, body: plan };
    if (b.confirm !== plan.confirm) return bad(409, 'stale_plan', 'the state changed since this plan, or the confirm is not its: plan again');
    this.on[part] = b.on;
    this.switched[part] = true;
    return { status: 200, body: { ...this.parts(), done: { part, on: b.on, said: b.on ? 'switched on' : 'switched off' } } };
  }

  handle(method: string, parts: string[], body: unknown, now: number): Reply {
    const p = parts.slice(1);
    if (method === 'GET' && p.join('/') === 'parts') return { status: 200, body: this.parts() };
    if (method === 'POST' && p.length === 2 && p[0] === 'parts') return this.switchPart(p[1], body);
    if (method === 'GET' && !p.length) return { status: 200, body: this.view() };
    if (method === 'POST' && p.join('/') === 'permissions/local-network') {
      this.fleet.discovery.checkLan();
      return { status: 200, body: this.view() };
    }
    if (method === 'GET' && p.join('/') === 'export') return { status: 200, body: this.export(now) };
    if (method === 'POST' && p.join('/') === 'import') {
      const r = this.plan(body, now);
      return { status: r.status, body: r.body };
    }
    if (method === 'POST' && p.join('/') === 'import/apply') return this.applyImport(body, now);
    return bad(404, 'not_found', `no route ${method} /v1/fleet/${parts.join('/')}`);
  }
}
