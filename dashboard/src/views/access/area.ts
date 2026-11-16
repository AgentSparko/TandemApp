// <qse-access>: the account and access pages (SRV-875), a chunk of their own that loads when one
// of them opens. #/account: the password and the browsers one is signed in on; #/account/tokens:
// My tokens. For an admin, #/access: Users, with All tokens, API exposure, Sessions and the
// sign-in audit below it. The menu shows a page only to a role that may open it; a direct link
// without the right shows "Not allowed" and the right that is missing.
//
// A 401 on any call is the shell's: its sign-in screen takes the page and, once signed in, the
// same route opens again (lib/csrf.ts tells the shell). The forms are read when they are sent and
// emptied after; a token or a one-time code is held only while its dialog shows.

import { html, nothing, type PropertyValues } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { auth, type AuthApi, type Role } from '../../api/auth';
import { pageOf, type TokenForm } from '../../lib/access';
import { AccessFlow } from '../../lib/access-flow';
import { setCsrf } from '../../lib/csrf';
import { setParams } from '../../lib/router';
import { LightElement } from '../../ui/base';
import { accessView, dialogView, type On } from './view';
import '../../styles/access.css';

const again = (): boolean => window.dispatchEvent(new CustomEvent('qse-signed-in'));

@customElement('qse-access')
export class QseAccess extends LightElement {
  @property() view: 'account' | 'access' = 'account';
  @property({ attribute: false }) path: string[] = [];
  @property({ attribute: false }) params: URLSearchParams = new URLSearchParams();
  @property({ attribute: false }) api: AuthApi = auth;
  private flow: AccessFlow | null = null;

  connectedCallback(): void {
    super.connectedCallback();
    this.flow = new AccessFlow(this.api, () => this.requestUpdate());
    this.asked = false;
  }

  protected willUpdate(changed: PropertyValues): void {
    if (changed.has('view') || changed.has('path') || changed.has('params')) this.enter();
  }

  private enter(): void {
    const page = pageOf(this.view, this.path);
    void this.flow?.open(page, this.params.get(page === 'tokens' ? 'owner' : 'user'));
  }

  private get dialog(): HTMLDialogElement | null {
    return this.querySelector('dialog[data-dialog="access"]');
  }

  protected updated(): void {
    const d = this.dialog;
    const f = this.flow;
    if (!d || !f) return;
    const kind = f.dialog?.kind ?? null;
    if (f.dialog && !d.open) d.showModal();
    else if (!f.dialog && d.open) d.close();
    // a dialog that opened, or went on to its next step: the focus goes to its first field, to
    // Cancel before a change that cannot be undone, else to its title (never to a button that
    // would close a token shown once)
    if (kind && kind !== this.shown) (d.querySelector<HTMLElement>('input, select, [data-act="dlg-cancel"]') ?? d.querySelector<HTMLElement>('#ac-dlg-t'))?.focus();
    this.shown = kind;
    // a session of one's own ended, or the password changed elsewhere: the shell asks who is here
    if (f.me && !f.me.authenticated && !this.asked) {
      this.asked = true;
      again();
    }
  }
  private asked = false;
  /** the kind of the dialog that shows */
  private shown: string | null = null;

  private on: On = {
    submit: (what, e) => {
      e.preventDefault();
      const f = this.flow!;
      const form = e.currentTarget as HTMLFormElement;
      const d = new FormData(form);
      const s = (k: string): string => String(d.get(k) ?? '');
      if (what === 'password')
        void f.password(s('current'), s('password'), s('again'), setCsrf).then((ok) => ok && form.reset());
      else if (what === 'token') {
        const limits: TokenForm['limits'] = {};
        for (const [k, v] of d) if (k.startsWith('limit.')) limits[k.slice(6) as keyof TokenForm['limits']] = String(v);
        void f.createToken({ label: s('label'), scopes: d.getAll('scopes').map(String), models: s('models'), days: s('days'), limits });
      } else if (what === 'rotate') {
        if (f.dialog?.kind === 'token-rotate') void f.rotateToken(f.dialog.token, Number(s('grace')));
      } else void f.addUser(s('username'), s('role') as Role, s('display'));
    },
    signOut: () =>
      void this.flow?.signOut().then(() => {
        setCsrf(null);
        again();
      }),
    filter: (id) => setParams({ [this.flow?.page === 'tokens' ? 'owner' : 'user']: id }),
    retry: () => (this.flow?.me && !this.flow.me.authenticated ? void again() : this.enter()),
  };

  render() {
    const f = this.flow;
    if (!f) return nothing;
    return html`${accessView(f, this.on)}
      <dialog class="act-dialog" aria-labelledby="ac-dlg-t" data-dialog="access" @close=${() => f.close()} @cancel=${(e: Event) => f.busy === 'dialog' && e.preventDefault()}>
        <div class="act-body">${dialogView(f, this.on, location.origin)}</div>
      </dialog>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'qse-access': QseAccess;
  }
}
