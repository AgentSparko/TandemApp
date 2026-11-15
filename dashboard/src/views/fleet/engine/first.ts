// Add engine's last step (VIS-278): the engine serves a model, so one message goes out. The page
// names the engine and its model, takes the message (a suggestion stands in the field) and hands
// it to the Chat tab, which sends it and shows the answer. With Chat off (OPS-288, a new
// TandemApp's default) the step says so and offers the Playground, which asks the engine through
// the gateway, and the switch on the App page. A template only, so it renders in a test without a
// browser; the wizard (views/fleet/engine-wizard.ts) holds the text and navigates.

import { html, nothing, type TemplateResult } from 'lit';
import type { EngineView } from '../../../api/engines';
import { icon } from '../../../ui/bits';
import { engineSummary } from './after';

/** The last step: the engine serves; one message, sent by the Chat tab. */
export function chatView(e: EngineView | null, text: string, on: { text(v: string): void; send(): void }, method?: string | null, chatOn: boolean | null = null): TemplateResult {
  const model = e?.status?.engine.model ?? null;
  if (chatOn === false)
    return html`${e ? engineSummary(e, method) : nothing}
      <section class="panel eh-first" data-panel="first-message" data-chat="off" aria-labelledby="eh-first-t">
        <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-first-t" tabindex="-1">Try the engine</h2>
          <p class="panel-sub">${e ? html`${e.alias} serves ${model ?? 'its model'}. ` : nothing}This TandemApp runs without Chat, as a new one does: ask the engine in the Playground, or switch Chat on first.</p></div></header>
        <div class="eh-actions"><a class="btn btn-primary" href="#/playground" data-act="playground">Open the Playground</a><a class="btn btn-ghost" href="#/app" data-act="chat-on">Switch Chat on…</a><a class="btn btn-ghost" href="#/overview" data-act="overview">Look around first</a></div>
      </section>
      <p class="small muted eh-more">More machines? <a href="#/add-engine" data-act="another">Add another engine</a>.</p>`;
  return html`${e ? engineSummary(e, method) : nothing}
    <section class="panel eh-first" data-panel="first-message" aria-labelledby="eh-first-t">
      <header class="panel-head"><div class="panel-titles"><h2 class="panel-title" id="eh-first-t" tabindex="-1">Send a first message</h2>
        <p class="panel-sub">${e ? html`${e.alias} serves ${model ?? 'its model'}. ` : nothing}The Chat tab opens, sends your message, and the answer arrives there.</p></div></header>
      <form class="eh-first-form" @submit=${(ev: Event) => (ev.preventDefault(), on.send())}>
        <label class="field field-block"><span>Your message</span><textarea data-in="first" name="first" rows="2" maxlength="2000" .value=${text} @input=${(ev: Event) => on.text((ev.target as HTMLTextAreaElement).value)}
          @keydown=${(ev: KeyboardEvent) => ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && (ev.preventDefault(), on.send())}></textarea></label>
        <div class="eh-actions"><button type="submit" class="btn btn-primary" data-act="send" ?disabled=${!text.trim()}>${icon('send')} Send in Chat</button><a class="btn btn-ghost" href="#/overview" data-act="overview">Look around first</a></div>
      </form>
    </section>
    <p class="small muted eh-more">More machines? <a href="#/add-engine" data-act="another">Add another engine</a>.</p>`;
}

