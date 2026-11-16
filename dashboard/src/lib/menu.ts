// The menu (concept §4, VIS-242): Chat, then four groups -- Monitor, Fleet, Tune, Settings -- and the
// sub-pages of the section you are in. One model for the icon strip, the panel beside it, the phone's
// bottom bar and its More sheet, and the crumb in the top bar. Kept pure so it can be tested; the
// shell (app.ts) draws it.

import type { Route, View } from './router';
import { currentScope, scopeParams } from './scope';

export type Section = 'chat' | 'overview' | 'performance' | 'usage' | 'hardware' | 'events' | 'health' | 'sparks' | 'models' | 'api' | 'conductor' | 'playground' | 'dev' | 'add-engine' | 'add-spark' | 'start' | 'app' | 'account' | 'access';

export interface MenuItem {
  id: Section;
  label: string;
}

export interface MenuGroup {
  /** null for Chat, which stands above the groups */
  label: string | null;
  items: MenuItem[];
}

export const MENU: MenuGroup[] = [
  { label: null, items: [{ id: 'chat', label: 'Chat' }] },
  {
    label: 'Monitor',
    items: [
      { id: 'overview', label: 'Overview' },
      { id: 'performance', label: 'Performance' },
      { id: 'usage', label: 'Usage' },
      { id: 'hardware', label: 'Hardware' },
      { id: 'events', label: 'Events & Alerts' },
      // SRV-835: what is wrong, in words, and the fixes TandemApp runs itself
      { id: 'health', label: 'Health' },
    ],
  },
  {
    label: 'Fleet',
    items: [
      { id: 'sparks', label: 'Sparks' },
      { id: 'models', label: 'Models' },
      { id: 'api', label: 'API' },
    ],
  },
  {
    label: 'Tune',
    items: [
      { id: 'conductor', label: 'Conductor' },
      { id: 'playground', label: 'Playground' },
      { id: 'dev', label: 'Dev' },
    ],
  },
  {
    label: 'Settings',
    items: [
      // SRV-749: the three ways into a fleet (a server by URL, a Spark, the engine); the first open
      { id: 'start', label: 'Get started' },
      // VIS-278: the setup wizard: an engine host by the code it printed; the first open
      { id: 'add-engine', label: 'Add engine' },
      { id: 'add-spark', label: 'Add Spark' },
      { id: 'app', label: 'App' },
    ],
  },
];

/** SRV-875: the entries a controller with accounts adds to Settings: one's own account for
 *  everyone, and the users, tokens, exposure and sessions for who holds the right `users` */
export const ACCOUNT_MENU: (MenuItem & { right: string })[] = [
  { id: 'account', label: 'Account', right: 'self' },
  { id: 'access', label: 'Users & access', right: 'users' },
];
/** the pages below them: [page, hash, label, the right it needs] */
export const ACCESS_PAGES: [string, string, string, string][] = [
  ['account', '#/account', 'Password and sessions', 'self'],
  ['my-tokens', '#/account/tokens', 'My tokens', 'self'],
  ['users', '#/access', 'Users', 'users'],
  ['tokens', '#/access/tokens', 'All tokens', 'users'],
  ['exposure', '#/access/exposure', 'API exposure', 'users'],
  ['sessions', '#/access/sessions', 'Sessions', 'users'],
  ['audit', '#/access/audit', 'Sign-ins', 'users'],
];
/** the rights of who is signed in; null while the controller runs without accounts */
let rights: string[] | null = null;
export function setRights(r: string[] | null): void {
  rights = r;
}
/** The menu as this person sees it: without accounts today's, with them the entries their rights
 *  open (a viewer: Account alone). */
export function menuNow(): MenuGroup[] {
  const r = rights;
  if (!r) return MENU;
  const add = ACCOUNT_MENU.filter((x) => r.includes(x.right)).map(({ id, label }) => ({ id, label }));
  return MENU.map((g) => (g.label === 'Settings' ? { ...g, items: [...g.items, ...add] } : g));
}

/** the pages that watch the fleet: the scope bar stands above them and their links keep the scope */
export const MONITOR: Section[] = ['overview', 'performance', 'usage', 'hardware', 'events'];
/** VIS-252: the Monitor pages and Models (its deployments and weights follow the scope too) */
export const SCOPED: Section[] = [...MONITOR, 'models'];

/** the phone's bottom bar: Chat one tap away, the two pages watched daily, then Windows and More */
export const PHONE_BAR: Section[] = ['chat', 'overview', 'performance'];

/** the section a view belongs to: today's System, Backends and setup guide sit below theirs */
const PARENT: Partial<Record<View, Section>> = { system: 'hardware', backends: 'api', setup: 'models' };

export function sectionOf(view: View): Section {
  return PARENT[view] ?? (view as Section);
}

export function menuItem(id: Section): MenuItem {
  for (const g of MENU) for (const it of g.items) if (it.id === id) return it;
  return ACCOUNT_MENU.find((x) => x.id === id) ?? { id, label: id };
}

/** what the top bar's crumb says: the group and the page */
export function crumb(view: View): { group: string | null; label: string } {
  const s = sectionOf(view);
  const g = MENU.find((x) => x.items.some((it) => it.id === s));
  return { group: g?.label ?? (ACCOUNT_MENU.some((x) => x.id === s) ? 'Settings' : null), label: menuItem(s).label };
}

/** the scope's params (what is not the default) and `extra`, as a query */
function withScope(extra: Record<string, string> = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...scopeParams(currentScope()), ...extra })) if (v != null) q.set(k, v);
  const s = q.toString();
  return s ? '?' + s : '';
}

/** A section's link. A Monitor page keeps the scope you set on the one before it. */
export function sectionHref(id: Section): string {
  return `#/${id}${SCOPED.includes(id) ? withScope() : ''}`;
}

/** The nine screens of the Conductor (VIS-42): its own tab strip and the panel's sub-pages. */
export const CONDUCTOR_SCREENS = [
  { id: 'overview', label: 'Overview' },
  { id: 'routing', label: 'Routing' },
  { id: 'monitor', label: 'Monitor' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'packages', label: 'Packages' },
  { id: 'deciders', label: 'Deciders' },
  { id: 'sessions', label: 'Sessions' },
  { id: 'memory', label: 'Memory' },
  { id: 'compare', label: 'Compare' },
] as const;
export type ConductorScreen = (typeof CONDUCTOR_SCREENS)[number]['id'];

/** The pages of the settings area (VIS-280): App, a page a scope of the settings interface, and
 *  History. Its own tab strip and the panel's sub-pages; `scope`: the scope the page edits. */
export const SETTINGS_TABS = [
  { id: 'app', label: 'App', scope: 'app' },
  { id: 'backends', label: 'Backends', scope: 'backends' },
  { id: 'conductor', label: 'Conductor', scope: 'conductor' },
  { id: 'engines', label: 'Engines', scope: 'engine' },
  { id: 'hooks', label: 'Hooks', scope: 'hooks' },
  { id: 'plugins', label: 'Plugins', scope: 'plugins' },
  { id: 'history', label: 'History', scope: null },
] as const;
export type SettingsTab = (typeof SETTINGS_TABS)[number]['id'];

/** SRV-875: the two pages a controller with accounts adds: the `auth` and the `api` scope */
export const ACCOUNT_TABS = [
  { id: 'signin', label: 'Sign-in', scope: 'auth' },
  { id: 'api', label: 'API', scope: 'api' },
] as const;
export type AnyTab = SettingsTab | (typeof ACCOUNT_TABS)[number]['id'];
/** The settings pages of this controller: with accounts, Sign-in and API stand after App. */
export function settingsTabs(accounts: boolean = rights !== null): readonly { id: AnyTab; label: string; scope: string | null }[] {
  return accounts ? [SETTINGS_TABS[0], ...ACCOUNT_TABS, ...SETTINGS_TABS.slice(1)] : SETTINGS_TABS;
}

/** A settings page's link; `target`: an engine host's own page. */
export function settingsHref(tab: AnyTab, target?: string): string {
  return tab === 'app' ? '#/app' : `#/app/${tab}${target ? `/${encodeURIComponent(target)}` : ''}`;
}

export interface SubPage {
  label: string;
  href: string;
  on: boolean;
}

/** The sub-pages of the route's section, the one you are on marked; [] for a section without
 *  any. Sparks lists every node of the fleet after its List and Topology. */
export function subPages(r: Route, nodes: { id: string; alias: string }[] = []): SubPage[] {
  const tab = r.params.get('tab');
  const p0 = r.path[0] ?? null;
  switch (sectionOf(r.view)) {
    case 'performance':
      return [
        { label: 'Live', href: `#/performance${withScope()}`, on: tab !== 'requests' && tab !== 'history' },
        { label: 'Requests', href: `#/performance${withScope({ tab: 'requests' })}`, on: tab === 'requests' },
        { label: 'History', href: `#/performance${withScope({ tab: 'history' })}`, on: tab === 'history' },
      ];
    case 'hardware':
      return [
        { label: 'Fleet', href: `#/hardware${withScope()}`, on: r.view === 'hardware' },
        { label: 'System', href: '#/hardware/system', on: r.view === 'system' },
      ];
    case 'sparks':
      return [
        { label: 'List', href: '#/sparks', on: p0 === null },
        { label: 'Topology', href: '#/sparks/topology', on: p0 === 'topology' },
        ...nodes.map((n) => ({ label: n.alias, href: `#/sparks/${encodeURIComponent(n.id)}`, on: p0 === n.id })),
      ];
    case 'models':
      return [
        { label: 'Deployments', href: `#/models${withScope()}`, on: r.view === 'models' && p0 === null },
        // VIS-252: placement first; the Setup Guide (one Spark's, #/models/serve) beside it
        { label: 'Serve a model', href: '#/models/place', on: r.view === 'models' && p0 === 'place' },
        { label: 'Setup Guide', href: '#/models/serve', on: r.view === 'setup' },
        { label: 'Weights', href: `#/models/weights${withScope()}`, on: r.view === 'models' && p0 === 'weights' },
      ];
    case 'api':
      return [
        { label: 'Endpoint and keys', href: '#/api', on: r.view === 'api' },
        { label: 'Backends', href: '#/api/backends', on: r.view === 'backends' },
      ];
    case 'app': {
      const tabs = settingsTabs();
      const on = tabs.find((t) => t.id === p0)?.id ?? 'app';
      return tabs.map((t) => ({ label: t.label, href: settingsHref(t.id), on: t.id === on }));
    }
    case 'account':
    case 'access': {
      const own = r.view === 'account';
      const on = own ? (p0 === 'tokens' ? '#/account/tokens' : '#/account') : (ACCESS_PAGES.find((x) => x[1] === `#/access/${p0}`)?.[1] ?? '#/access');
      return ACCESS_PAGES.filter((x) => x[1].startsWith('#/account') === own && (rights ?? []).includes(x[3])).map((x) => ({ label: x[2], href: x[1], on: x[1] === on }));
    }
    case 'conductor': {
      const screen = r.params.get('screen') ?? 'overview';
      return CONDUCTOR_SCREENS.map((s) => ({ label: s.label, href: s.id === 'overview' ? '#/conductor' : `#/conductor?screen=${s.id}`, on: s.id === screen }));
    }
    default:
      return [];
  }
}
