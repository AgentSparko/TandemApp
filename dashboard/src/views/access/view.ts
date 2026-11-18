// The account and access pages' templates (SRV-875): Account (the password, the browsers one is
// signed in on), My tokens, and for an admin Users, All tokens (with the Review list of imported
// keys and the service identities), API exposure, Sessions and the sign-in audit. Every list is
// rows that stack on a narrow screen, never a table that scrolls sideways. Templates only, drawn
// from the flow (lib/access-flow.ts), so they render in a test without a browser.

import { html, nothing, type TemplateResult } from 'lit';
import { LIMIT_KEYS, type Finding, type Session, type Token, type User } from '../../api/auth';
import type { AccessFlow, Dialog } from '../../lib/access-flow';
import { DAY_CHOICES, EXPOSURE_WORDS, LIMIT_WORDS, PAGES, PROTOCOLS, REFUSED_WORDS, ROLE_WORDS, ago, auditWords, browserOf, can, endsSoon, findingsFor, isImported, isLoopback, isSignIn, lastOwner, limitText, mayChange, mayCreate, mine, nameOf, ownersOf, pageDef, peopleOf, reviewOf, roleOf, rolesFor, scopeLabel, scopesFor, servicesOf, snippets, stateWords, tokensFor, unpricedText, useText } from '../../lib/access';
import { copyButton, icon, skeleton } from '../../ui/bits';

export interface On {
  /** a form of the page or the dialog was sent: `what` names it */
  submit(what: 'password' | 'token' | 'rotate' | 'user', e: Event): void;
  signOut(): void;
  filter(id: string | null): void;
  retry(): void;
}

export const T = {
  once: 'This token is shown once. Copy it now.',
  never: 'You will not see it again: TandemApp keeps only its last 4 characters.',
  imported: 'This key was imported from an older setup. Confirm it or revoke it.',
  viewers: 'An admin must allow viewers to create tokens.',
  everywhere: 'Sign out everywhere',
  notAllowed: 'Not allowed',
};

const tag = (text: string, tone = ''): TemplateResult => html`<span class="tag ${tone}">${text}</span>`;
const row = (id: string, main: TemplateResult, acts: TemplateResult | typeof nothing = nothing, cls = ''): TemplateResult => html`<li class="ac-row ${cls}" data-row=${id}><div class="ac-main">${main}</div>${acts === nothing ? nothing : html`<div class="ac-acts">${acts}</div>`}</li>`;
const list = (rows: TemplateResult[], empty: string): TemplateResult => (rows.length ? html`<ul class="ac-list">${rows}</ul>` : html`<p class="small muted" data-empty>${empty}</p>`);
function panel(title: string, body: TemplateResult, o: { sub?: string | TemplateResult; tools?: TemplateResult | typeof nothing; data?: string } = {}): TemplateResult {
  return html`<section class="panel" data-panel=${o.data ?? nothing}>
    <header class="panel-head"><div class="panel-titles"><h2 class="panel-title">${title}</h2>${o.sub ? html`<p class="panel-sub">${o.sub}</p>` : nothing}</div>${o.tools ? html`<div class="panel-tools">${o.tools}</div>` : nothing}</header>
    ${body}
  </section>`;
}
const field = (label: string, control: TemplateResult, hint: string | TemplateResult = ''): TemplateResult => html`<label class="field field-block ac-field"><span>${label}</span>${control}${hint ? html`<small class="muted">${hint}</small>` : nothing}</label>`;
const pw = (name: string, auto: string): TemplateResult => html`<input name=${name} type="password" autocomplete=${auto} required spellcheck="false" autocapitalize="off" />`;
const secret = (value: string, label: string, data: string): TemplateResult => html`<div class="ac-secret"><code class="num" data-secret=${data}>${value}</code>${copyButton(value, label)}</div>`;
const utc = (at: string | null): string => (at ? `${at.slice(0, 16).replace('T', ' ')} UTC` : '');

// ---- account --------------------------------------------------------------------------------------

function sessionRow(f: AccessFlow, s: Session, all: boolean): TemplateResult {
  const now = f.now();
  return row(
    s.id,
    html`<div class="ac-t"><b>${all ? s.username : browserOf(s.agent)}</b>${s.current ? tag('this browser', 'ac-on') : nothing}</div>
      <p class="small muted">${all ? `${browserOf(s.agent)} · ` : ''}from <span class="num">${s.address || 'an unknown address'}</span> · signed in ${ago(s.created_at, now)} · last seen ${ago(s.last_seen_at, now)}</p>`,
    html`<button type="button" class="btn btn-sm" data-act="end" @click=${() => f.endSession(s)}>${s.current ? 'Sign out' : 'End'}</button>`,
  );
}

function accountPage(f: AccessFlow, on: On): TemplateResult {
  const me = f.me!;
  return html`${panel(
      `Signed in as ${nameOf(me)}`,
      html`<p class="small" data-role=${roleOf(me) ?? nothing}>Your role is <b>${roleOf(me)}</b>: ${roleOf(me) ? ROLE_WORDS[roleOf(me)!] : ''}.</p>`,
      { data: 'me', tools: html`<button type="button" class="btn" data-act="sign-out" @click=${on.signOut}>${icon('out')} Sign out</button>` },
    )}
    ${panel(
      'Password',
      html`<form class="ac-form" data-form="password" @submit=${(e: Event) => on.submit('password', e)}>
        ${field('Current password', pw('current', 'current-password'))} ${field('New password', pw('password', 'new-password'))} ${field('New password again', pw('again', 'new-password'))}
        <div class="ac-foot"><button class="btn btn-primary" type="submit" ?disabled=${f.busy === 'password'}>Save the password</button></div>
      </form>`,
      { data: 'password', sub: 'A new password signs out every other browser.' },
    )}
    ${panel(
      'Where you are signed in',
      f.sessions ? list(mine(f.sessions).map((s) => sessionRow(f, s, false)), 'No session.') : skeleton(2),
      { data: 'sessions', tools: html`<button type="button" class="btn" data-act="everywhere" @click=${() => f.signOutEverywhere()}>${T.everywhere}</button>` },
    )}`;
}

// ---- tokens ---------------------------------------------------------------------------------------

function tokenMain(f: AccessFlow, t: Token, owner: boolean): TemplateResult {
  const now = f.now();
  return html`<div class="ac-t"><b>${t.label}</b>${t.state !== 'active' ? tag(t.state, 'ac-off') : endsSoon(t, now) ? tag('ends soon', 'ac-warn') : nothing}${isImported(t) ? tag('imported') : nothing}</div>
    <p class="small"><span class="num ac-wrap" data-hint>${t.hint ?? 'no hint'}</span>${owner ? html` · ${t.owner ?? 'no owner'}` : nothing}</p>
    <p class="ac-tags">${t.scopes.map((s) => tag(scopeLabel(s)))}</p>
    <p class="small muted" data-runs>${t.backends?.length ? html`backends <span class="num ac-wrap">${t.backends.join(', ')}</span> · ` : nothing}${t.models.length ? html`models <span class="num ac-wrap">${t.models.join(', ')}</span>` : t.backends?.length ? 'every model there' : 'every model'} · ${stateWords(t, now)} · last used ${ago(t.last_used_at, now)}${Object.keys(t.limits).length ? html` · limits ${LIMIT_KEYS.filter((k) => t.limits[k] != null).map((k) => `${LIMIT_WORDS[k].toLowerCase()} ${t.limits[k]}`).join(', ')}` : nothing}</p>`;
}
/** SRV-873: what the token used in 30 days, its limits' state, models no spend limit holds, and
 *  its last refusals */
function useBlock(f: AccessFlow, t: Token): TemplateResult | typeof nothing {
  const u = f.usage[t.id];
  if (!u) return nothing;
  const lims = LIMIT_KEYS.filter((k) => u.limits.limits[k]);
  const free = unpricedText(u);
  const last = u.refusals.slice(0, 3);
  return html`<div class="ac-use" data-usage=${t.id}>
    <p class="small" data-use>${useText(u)}</p>
    ${lims.length ? html`<p class="ac-tags" data-limits>${lims.map((k) => tag(limitText(k, u.limits.limits[k]!), u.limits.limits[k]!.reached ? 'ac-warn' : ''))}</p>` : nothing}
    ${free ? html`<p class="small muted" data-unpriced>${free}</p>` : nothing}
    ${last.length ? html`<p class="small muted" data-refusals>Last refused: ${last.map((r) => `${utc(r.at)} ${REFUSED_WORDS[r.code] ?? r.code}${r.model ? ` (${r.model})` : ''}`).join('; ')}</p>` : nothing}
  </div>`;
}
const revoke = (f: AccessFlow, t: Token): TemplateResult => html`<button type="button" class="btn btn-sm" data-act="revoke" @click=${() => f.revokeToken(t)}>Revoke</button>`;
function tokenRow(f: AccessFlow, t: Token, owner: boolean): TemplateResult {
  const own = t.owner_id != null && t.owner_id === f.me?.account?.id;
  const acts = t.state === 'active' ? html`${own ? html`<button type="button" class="btn btn-sm" data-act="rotate" @click=${() => f.show({ kind: 'token-rotate', token: t })}>Rotate</button>` : nothing}${revoke(f, t)}` : nothing;
  return row(t.id, html`${tokenMain(f, t, owner)}${useBlock(f, t)}`, acts, t.state !== 'active' ? 'is-off' : '');
}

function myTokensPage(f: AccessFlow): TemplateResult {
  const may = mayCreate(f.me);
  return panel('Tokens', html`${may ? nothing : html`<p class="note" data-no-create>${T.viewers}</p>`}${f.tokens ? list(f.tokens.map((t) => tokenRow(f, t, false)), 'No token yet.') : skeleton(3)}`, {
    data: 'my-tokens',
    sub: 'A token is shown once, when it is made. Here: what each may do, when it ends and when it was last used.',
    tools: may ? html`<button type="button" class="btn btn-primary" data-act="token-new" @click=${() => f.show({ kind: 'token-new' })}>${icon('plus')} Create a token</button>` : nothing,
  });
}

function allTokensPage(f: AccessFlow, on: On): TemplateResult {
  const ts = f.tokens;
  if (!ts) return panel('Tokens', skeleton(4));
  const review = reviewOf(ts);
  const owners = ownersOf(ts);
  return html`${review.length
      ? panel(
          'Review',
          list(
            review.map((t) =>
              row(
                t.id,
                html`${tokenMain(f, t, true)}<p class="small" data-imported>${T.imported}</p>`,
                html`<button type="button" class="btn btn-sm btn-primary" data-act="confirm" ?disabled=${f.busy === `confirm:${t.id}`} @click=${() => void f.confirmToken(t)}>Confirm</button>${revoke(f, t)}`,
              ),
            ),
            '',
          ),
          { data: 'review', sub: `${review.length} imported ${review.length === 1 ? 'key waits' : 'keys wait'} for a decision.` },
        )
      : nothing}
    ${panel('People’s tokens', list(peopleOf(ts, f.filter).map((t) => tokenRow(f, t, true)), 'No token.'), {
      data: 'tokens',
      tools: html`<label class="field"><span>Owner</span><select data-in="owner" @change=${(e: Event) => on.filter((e.target as HTMLSelectElement).value || null)}><option value="">Everyone</option>${owners.map((o) => html`<option value=${o.id} ?selected=${o.id === f.filter}>${o.name}</option>`)}</select></label>`,
    })}
    ${panel('Service identities', list(servicesOf(ts).map((t) => row(t.id, html`${tokenMain(f, t, false)}`, nothing, t.state !== 'active' ? 'is-off' : '')), 'None.'), { data: 'services', sub: 'TandemApp makes these for its own parts and renews them itself. They are listed, never changed here.' })}
    <p class="small muted">Whether viewers may create tokens, and how long a token may live, are set under <a href="#/app/api">Settings, API</a>.</p>`;
}

// ---- users ----------------------------------------------------------------------------------------

function userRow(f: AccessFlow, u: User, all: User[]): TemplateResult {
  const me = f.me;
  const self = u.id === me?.account?.id;
  const last = lastOwner(u, all);
  const may = mayChange(me, u);
  const busy = f.busy === `user:${u.id}`;
  const now = f.now();
  return row(
    u.id,
    html`<div class="ac-t"><b>${u.username}</b>${u.display_name ? html`<span class="muted">${u.display_name}</span>` : nothing}${tag(u.role)}${self ? tag('you', 'ac-on') : nothing}${u.enabled ? nothing : tag('disabled', 'ac-off')}${u.must_change ? tag('one-time code', 'ac-warn') : nothing}</div>
      <p class="small muted">last sign-in ${ago(u.last_login_at, now)} · made ${ago(u.created_at, now)} · <a href="#/access/sessions?user=${u.id}">sessions</a> · <a href="#/access/tokens?owner=${u.id}">tokens</a></p>
      ${last ? html`<p class="small" data-last-owner>The last owner: it cannot be disabled, demoted or removed. Make another owner first.</p>` : !may ? html`<p class="small muted" data-owner-only>Only an owner changes an owner.</p>` : nothing}`,
    html`<label class="field"><span class="sr-only">Role of ${u.username}</span><select data-in="role" ?disabled=${!may || last || busy} @change=${(e: Event) => {
        // a role the controller refuses goes back to what it is
        const sel = e.target as HTMLSelectElement;
        void f.setRole(u, sel.value as User['role']).then((ok) => ok || (sel.value = u.role));
      }}>${[...new Set([...rolesFor(me), u.role])].map((r) => html`<option value=${r} ?selected=${r === u.role}>${r}</option>`)}</select></label>
      <button type="button" class="btn btn-sm" data-act=${u.enabled ? 'disable' : 'enable'} ?disabled=${!may || last || busy} @click=${() => f.setEnabled(u, !u.enabled)}>${u.enabled ? 'Disable' : 'Enable'}</button>
      <button type="button" class="btn btn-sm" data-act="reset" ?disabled=${!may || self || u.provider !== 'local'} @click=${() => f.resetUser(u)}>Reset password</button>
      <button type="button" class="btn btn-sm" data-act="remove" ?disabled=${!may || last} @click=${() => f.removeUser(u)}>Remove</button>`,
    u.enabled ? '' : 'is-off',
  );
}

function usersPage(f: AccessFlow): TemplateResult {
  const us = f.users;
  return panel('Accounts', us ? list(us.map((u) => userRow(f, u, us)), 'No account.') : skeleton(4), {
    data: 'users',
    sub: 'viewer, operator, admin, owner: each role holds the rights of the ones before it.',
    tools: html`<button type="button" class="btn btn-primary" data-act="user-new" @click=${() => f.show({ kind: 'user-new' })}>${icon('plus')} Add a user</button>`,
  });
}

function sessionsPage(f: AccessFlow): TemplateResult {
  const all = f.sessions;
  if (!all) return panel('Sessions', skeleton(4));
  const who = f.filter ? (all.find((s) => s.account_id === f.filter)?.username ?? null) : null;
  const shown = f.filter ? all.filter((s) => s.account_id === f.filter) : all;
  return panel('Signed-in browsers', list(shown.map((s) => sessionRow(f, s, true)), f.filter ? 'This account is signed in nowhere.' : 'Nobody is signed in.'), {
    data: 'all-sessions',
    sub: f.filter ? html`One account’s alone. <a href="#/access/sessions">Show everyone</a>` : 'Ending a session signs that browser out at once.',
    tools: who ? html`<button type="button" class="btn" data-act="end-all" @click=${() => f.signOutEverywhere({ id: f.filter!, username: who } as User)}>End every session of ${who}</button>` : nothing,
  });
}

function auditPage(f: AccessFlow): TemplateResult {
  const rows = (f.audit ?? []).filter((r) => f.auditOnly === 'all' || isSignIn(r));
  const seg = (id: 'all' | 'sign-ins', label: string) => html`<button type="button" class="seg-btn ${f.auditOnly === id ? 'is-on' : ''}" aria-pressed=${f.auditOnly === id} data-only=${id} @click=${() => f.only(id)}>${label}</button>`;
  return panel(
    'The last 200',
    f.audit
      ? list(
          rows.map((r) => {
            const w = auditWords(r);
            return html`<li class="ac-row ac-audit" data-audit=${r.action}><div class="ac-main"><div class="ac-t"><b class=${w.bad ? 'err-ink' : ''}>${r.who || 'someone'} ${w.what}</b></div><p class="small muted ac-wrap">${w.about}</p></div><time class="small muted num">${utc(r.at)}</time></li>`;
          }),
          'Nothing yet.',
        )
      : skeleton(5),
    { data: 'audit', sub: 'Newest first. No row holds a password, a code or a token.', tools: html`<div class="seg" role="group" aria-label="Which rows">${seg('all', 'All')}${seg('sign-ins', 'Sign-ins')}</div>` },
  );
}

// ---- exposure -------------------------------------------------------------------------------------

const finding = (x: Finding): TemplateResult => html`<p class="note ac-finding is-${x.severity}" data-finding=${x.code}>${icon(x.severity === 'info' ? 'info' : 'warn')}<span>${x.sentence} <span class="muted">${x.fix}</span></span></p>`;

function exposurePage(f: AccessFlow): TemplateResult {
  const x = f.exposure;
  if (!x) return panel('The switches', skeleton(4));
  const me = f.me;
  const local = !!me?.local_mode;
  const owner = can(me, 'owner') && x.writable;
  const ts = f.tokens ?? [];
  return html`${local
      ? html`<section class="panel" data-password-first role="status"><h2 class="panel-title">Set a password first</h2><p class="small">No password is set, so TandemApp trusts this machine and nothing else. Before another device is let in, the owner gets a password. From then on every browser signs in.</p><div class="ac-foot"><a class="btn btn-primary" href="#/account">Set a password</a></div></section>`
      : !owner
        ? html`<p class="note" data-owner-only>Only an owner changes these switches.</p>`
        : nothing}
    ${PROTOCOLS.map((p) => {
      const v = x.values[p.id];
      const n = tokensFor(ts, p.id);
      return panel(
        p.label,
        html`<p class="small muted num ac-wrap">${p.routes}</p>
          <div class="seg ac-switch" role="group" aria-label=${`Who may use the ${p.label}`}>
            ${(['this-machine', 'network'] as const).map((id) => html`<button type="button" class="seg-btn ${v === id ? 'is-on' : ''}" aria-pressed=${v === id} data-value=${id} ?disabled=${!owner || local || v === id || v === null} @click=${() => f.expose(p.id, p.label, id)}>${EXPOSURE_WORDS[id].label}</button>`)}
          </div>
          <p class="small" data-now=${v ?? 'unknown'}>${v ? EXPOSURE_WORDS[v].text : 'This TandemApp does not say how this API is switched.'}${v === 'this-machine' ? html` <span class="muted">Other devices cannot use this API.</span>` : nothing}</p>
          <p class="small muted" data-tokens=${n}>${n ? `${n} ${n === 1 ? 'token holds' : 'tokens hold'} this API.` : html`No token holds this API yet. <a href="#/account/tokens">Create one under My tokens.</a>`}</p>
          ${findingsFor(f.findings, p.fact).map(finding)}`,
        { data: `exposure-${p.id}` },
      );
    })}
    ${panel(
      'What is true now',
      html`<p class="small" data-listen>${x.listen ? html`TandemApp listens on <b class="num">${x.listen}</b>.` : 'The listen address is not known here.'}${isLoopback(x.listen) ? ' Other devices cannot reach it, whatever the switches say.' : ''} <span class="muted">The address is changed under <a href="#/app/api">Settings, API</a> and applies at the next restart.</span></p>
        ${findingsFor(f.findings, null).map(finding)}`,
      { data: 'exposure-now' },
    )}`;
}

// ---- the page -------------------------------------------------------------------------------------

function stopped(f: AccessFlow, on: On): TemplateResult | null {
  const def = pageDef(f.page);
  switch (f.stop) {
    case 'off':
      return html`<section class="panel" data-stop="off"><h2 class="panel-title">This TandemApp runs without accounts</h2><p class="small">Sign-in, users and API tokens are switched on with <code>TANDEM_AUTH=accounts</code> where TandemApp starts.</p></section>`;
    case 'signed-out':
      return html`<section class="panel" data-stop="signed-out" role="status"><h2 class="panel-title">You are signed out</h2><div class="ac-foot"><button type="button" class="btn btn-primary" @click=${on.retry}>Sign in</button></div></section>`;
    case 'not-allowed':
      return html`<section class="panel" data-stop="not-allowed" role="alert"><h2 class="panel-title">${T.notAllowed}</h2><p class="small">${def.title} needs the right “${def.right}”. You are signed in as ${nameOf(f.me)}, and your role is ${roleOf(f.me)}.</p><div class="ac-foot"><a class="btn" href="#/account">Open Account</a></div></section>`;
    case 'password-first':
      return html`<section class="panel" data-stop="password-first" role="status"><h2 class="panel-title">Set a password first</h2><p class="small">No password is set, so TandemApp trusts this machine and nothing else: there is nobody to sign in, and no token to hand out. Set the owner’s password to use ${def.title}.</p><div class="ac-foot"><a class="btn btn-primary" href="#/account">Set a password</a></div></section>`;
  }
  return null;
}

/** The whole page: its head, the strip of its section's pages, then what the flow holds. */
export function accessView(f: AccessFlow, on: On): TemplateResult {
  const def = pageDef(f.page);
  const own = def.href.startsWith('#/account');
  const stop = stopped(f, on);
  // the strip: the pages of this section the person may open (nobody signed in: none)
  const tabs = f.me?.authenticated ? PAGES.filter((p) => p.href.startsWith('#/account') === own && (own ? !f.me?.local_mode : can(f.me, p.right))) : [];
  let body: TemplateResult;
  if (f.me === undefined) body = f.error ? html`<section class="panel" role="alert" data-unread><p class="small err-ink">${f.error}</p><div class="ac-foot"><button type="button" class="btn" @click=${on.retry}>${icon('refresh')} Try again</button></div></section>` : html`<section class="panel" data-loading>${skeleton(4)}</section>`;
  else if (stop) body = stop;
  else
    body = html`${f.error ? html`<p class="login-error" role="alert" data-unread>${f.error} <button type="button" class="btn btn-sm" @click=${on.retry}>Try again</button></p>` : nothing}
      ${f.failed ? html`<p class="login-error" role="alert" data-failed>${f.failed}</p>` : nothing} ${f.note ? html`<p class="note note-info" role="status" data-note>${f.note}</p>` : nothing}
      ${f.page === 'account' ? accountPage(f, on) : f.page === 'my-tokens' ? myTokensPage(f) : f.page === 'users' ? usersPage(f) : f.page === 'tokens' ? allTokensPage(f, on) : f.page === 'sessions' ? sessionsPage(f) : f.page === 'audit' ? auditPage(f) : exposurePage(f)}`;
  return html`<header class="page-head" data-page="access" data-access=${f.page}><div class="eyebrow">${own ? 'Account' : 'Users & access'}</div><h1>${def.title}</h1><p class="page-sub">${def.sub}</p></header>
    ${tabs.length > 1 ? html`<nav class="ac-tabs" aria-label=${own ? 'Account pages' : 'Users and access pages'}>${tabs.map((p) => html`<a class="ac-tab ${p.id === f.page ? 'is-on' : ''}" href=${p.href} aria-current=${p.id === f.page ? 'page' : nothing} data-tab=${p.id}>${p.label}</a>`)}</nav>` : nothing}
    ${body}`;
}

// ---- the dialog -----------------------------------------------------------------------------------

function dialogBody(f: AccessFlow, d: Dialog, on: On, origin: string): { title: string; body: TemplateResult } {
  const busy = f.busy === 'dialog';
  const err = f.dialogError ? html`<p class="login-error" role="alert" data-dialog-error>${f.dialogError}</p>` : nothing;
  const foot = (go: string, o: { submit?: boolean; click?: () => void; cancel?: string | null } = {}) => html`<footer class="act-foot">
    ${o.cancel === null ? nothing : html`<button type="button" class="btn btn-ghost" data-act="dlg-cancel" ?disabled=${busy} @click=${() => f.close()}>${o.cancel ?? 'Cancel'}</button>`}
    <button type=${o.submit ? 'submit' : 'button'} class="btn btn-primary" data-act="dlg-go" ?disabled=${busy} @click=${o.click ?? nothing}>${busy ? 'Working…' : go}</button>
  </footer>`;
  switch (d.kind) {
    case 'token-new': {
      const admin = can(f.me, 'users');
      return {
        title: 'Create a token',
        body: html`<form class="ac-form" data-form="token" novalidate @submit=${(e: Event) => on.submit('token', e)}>
          ${field('Name', html`<input name="label" maxlength="64" required autocomplete="off" spellcheck="false" />`, 'What it is for, such as “laptop” or “backup script”.')}
          <fieldset class="ac-set"><legend>What it may do</legend>
            ${scopesFor(f.me).map((s) => html`<label class="ac-check"><input type="checkbox" name="scopes" value=${s.id} /><span><b>${s.label}</b> <span class="muted">${s.text}</span></span></label>`)}
          </fieldset>
          ${field('Models', html`<input name="models" class="num" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="every model" />`, 'Leave empty for every model. Separate names with commas; a name may end in * to match many.')}
          ${field('Expires after', html`<select name="days">${DAY_CHOICES.map((n) => html`<option value=${n} ?selected=${n === 90}>${n} days</option>`)}${admin ? html`<option value="never">Never</option>` : nothing}</select>`)}
          <details class="ac-limits"><summary class="small">Limits</summary>
            <p class="small muted">Empty: your role's limit, when an admin set one; never more than it. Cost in dollars, for models with a price.</p>
            <div class="ac-grid">${LIMIT_KEYS.map((k) => field(LIMIT_WORDS[k], html`<input name=${`limit.${k}`} class="num" inputmode="decimal" autocomplete="off" placeholder="none" />`))}</div>
          </details>
          ${err} ${foot('Create the token', { submit: true })}
        </form>`,
      };
    }
    case 'token-once':
      return {
        title: d.rotated ? `${d.token.label}: the new token` : `${d.token.label} is made`,
        body: html`<p data-once><b>${T.once}</b></p>
          ${secret(d.token.token, 'Copy the token', 'token')}
          <p class="small">${T.never}</p>
          <dl class="ac-snips">${snippets(origin).map((s) => html`<div><dt class="small muted">${s.label}</dt><dd>${secret(s.value, `Copy: ${s.label}`, 'url')}</dd></div>`)}</dl>
          ${foot('I copied it', { click: () => f.close(), cancel: null })}`,
      };
    case 'token-rotate':
      return {
        title: `Rotate ${d.token.label}`,
        body: html`<form class="ac-form" data-form="rotate" @submit=${(e: Event) => on.submit('rotate', e)}>
          <p class="small">A new secret under the same name and rights. It is shown once.</p>
          ${field('The old one stops', html`<select name="grace"><option value="0">At once</option><option value="3600">In an hour</option><option value="86400">In a day</option></select>`)}
          ${err} ${foot('Rotate', { submit: true })}
        </form>`,
      };
    case 'user-new':
      return {
        title: 'Add a user',
        body: html`<form class="ac-form" data-form="user" novalidate @submit=${(e: Event) => on.submit('user', e)}>
          ${field('Username', html`<input name="username" maxlength="64" required autocomplete="off" spellcheck="false" autocapitalize="off" />`)}
          ${field('Name', html`<input name="display" maxlength="120" autocomplete="off" />`, 'Optional: the name shown beside the username.')}
          ${field('Role', html`<select name="role">${rolesFor(f.me).map((r) => html`<option value=${r}>${r}: ${ROLE_WORDS[r]}</option>`)}</select>`)}
          <p class="small muted">You type no password for someone else. You get a one-time code to hand over; they sign in with it and choose their own.</p>
          ${err} ${foot('Add the user', { submit: true })}
        </form>`,
      };
    case 'user-code':
      return {
        title: d.reset ? `One-time code for ${d.data.user.username}` : `${d.data.user.username} is added`,
        body: html`<p data-once><b>This code is shown once. Copy it now.</b></p>
          ${secret(d.data.code, 'Copy the code', 'code')}
          <p class="small">${d.data.user.username} signs in with it in place of a password, within ${Math.round(d.data.code_valid_s / 3600)} hours, and then sets a password of their own.</p>
          ${foot('I copied it', { click: () => f.close(), cancel: null })}`,
      };
    case 'confirm':
      return { title: d.title, body: html`<p class="small" data-confirm>${d.text}</p>${err} ${foot(d.go, { click: () => void f.go() })}` };
  }
}

/** The one dialog of the pages: a form, a secret shown once, or a question before a change. */
export function dialogView(f: AccessFlow, on: On, origin: string): TemplateResult | typeof nothing {
  const d = f.dialog;
  if (!d) return nothing;
  const { title, body } = dialogBody(f, d, on, origin);
  return html`<header class="act-head"><h2 id="ac-dlg-t" tabindex="-1">${title}</h2><button type="button" class="btn btn-ghost btn-icon" aria-label="Close" data-act="dlg-x" ?disabled=${f.busy === 'dialog'} @click=${() => f.close()}>${icon('x')}</button></header>
    <div class="ac-dlg" data-dialog-kind=${d.kind}>${body}</div>`;
}
