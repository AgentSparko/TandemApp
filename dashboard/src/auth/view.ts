// <qse-auth>: the controller's sign-in, its first-run setup and the password step (SEC-26), shown
// only when its accounts are on (/health sends X-Tandem-Auth: accounts). Its own chunk: a page
// that never signs in never loads it.
//
//   signin    username and password -> POST /v1/auth/session
//   setup     no owner yet: the setup code, a name and a password -> POST /v1/auth/setup
//   password  signed in with a one-time code: a new password -> PUT /v1/auth/password
//   account   #/account: who is signed in, a new password, sign out; in local mode the step that
//             sets the owner's password (POST /v1/auth/setup without a code), which ends it
//
// The fetches here are plain: a 401 is an answer of the form, not the shell's "signed out". The
// shell keeps the route, so after the step the page asked for opens.

import { html, nothing, type TemplateResult } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { setCsrf } from '../lib/csrf';
import { LightElement } from '../ui/base';
import '../styles/auth.css';
import { T, errorOf, send, type AuthNeed } from './logic';

export { needOf, readSession, type AuthNeed, type AuthSession } from './logic';

@customElement('qse-auth')
export class QseAuth extends LightElement {
  @property() need: AuthNeed = 'signin';
  @property({ type: Boolean }) local = false;
  @property() who = '';
  @state() private busy = false;
  @state() private error: string | null = null;

  private val(name: string): string {
    return this.querySelector<HTMLInputElement>(`[name="${name}"]`)?.value ?? '';
  }

  private async submit(e: Event): Promise<void> {
    e.preventDefault();
    if (this.busy) return;
    const n = this.need;
    const pw = this.val('password');
    if ((n !== 'signin') && pw !== this.val('again')) {
      this.error = T.differ;
      return;
    }
    this.busy = true;
    this.error = null;
    const [st, out] =
      n === 'signin'
        ? await send('POST', '/v1/auth/session', { username: this.val('username'), password: pw })
        : n === 'setup' || (n === 'account' && this.local)
          ? await send('POST', '/v1/auth/setup', { ...(n === 'setup' ? { code: this.val('code') } : {}), username: this.val('username'), password: pw })
          : await send('PUT', '/v1/auth/password', { current: this.val('current'), password: pw });
    this.busy = false;
    this.error = errorOf(st, out);
    if (this.error) return;
    setCsrf(out?.csrf);
    for (const f of this.querySelectorAll('input')) f.value = '';
    window.dispatchEvent(new CustomEvent('qse-signed-in'));
  }

  private async signOut(): Promise<void> {
    await send('DELETE', '/v1/auth/session');
    setCsrf(null);
    location.hash = '#/overview';
    window.dispatchEvent(new CustomEvent('qse-signed-in'));
  }

  private field(name: string, label: string, type = 'password', auto = 'new-password'): TemplateResult {
    return html`<label class="au-field"><span>${label}</span><input name=${name} type=${type} autocomplete=${auto} required spellcheck="false" autocapitalize="off" /></label>`;
  }

  render() {
    const n = this.need;
    const setLocal = n === 'account' && this.local;
    const title = n === 'signin' ? T.signin : n === 'setup' ? 'Create the owner' : n === 'password' || setLocal ? T.password : `Signed in as ${this.who}`;
    const body = n === 'setup' ? html`<p>${T.setup}</p><p class="au-note">${T.where}</p>` : n === 'password' ? html`<p>${T.once}</p>` : setLocal ? html`<p>${T.local}</p>` : nothing;
    const fields =
      n === 'signin'
        ? html`${this.field('username', 'Username', 'text', 'username')}${this.field('password', 'Password', 'password', 'current-password')}`
        : html`${n === 'setup' ? this.field('code', 'Setup code', 'text', 'one-time-code') : nothing}
          ${n === 'setup' || setLocal ? this.field('username', 'Owner’s username', 'text', 'username') : this.field('current', n === 'password' ? 'One-time code' : 'Current password', 'password', 'current-password')}
          ${this.field('password', 'New password')}${this.field('again', 'New password again')}`;
    return html`<main class="au-wrap" data-auth=${n}>
      <form class="panel au-card" @submit=${(e: Event) => void this.submit(e)}>
        <h1 class="au-title"><span class="brand-mark" aria-hidden="true"><i></i><i></i></span>${title}</h1>
        ${body} ${fields}
        ${this.error ? html`<p class="login-error" role="alert" data-auth-error>${this.error}</p>` : nothing}
        <div class="au-foot">
          <button class="btn btn-primary" type="submit" ?disabled=${this.busy}>${n === 'signin' ? 'Sign in' : n === 'setup' ? 'Create and sign in' : 'Save the password'}</button>
          ${n === 'account' ? html`${this.local ? nothing : html`<button class="btn" type="button" data-sign-out @click=${() => void this.signOut()}>Sign out</button>`}<a href="#/overview">Back</a>` : nothing}
        </div>
      </form>
    </main>`;
  }
}
