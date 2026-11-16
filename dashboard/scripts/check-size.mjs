// Prints the gzip size of every file in dist/ and fails above the budget: 300 KB before the Chat
// tab, 400 KB with it (VIS-99: its own chunk of about 40 KB gzip plus the mock it loads on
// demand), 405 KB since the terminal pane runs xterm.js (VIS-105, 2026-10-12: an 81 KB chunk that
// loads only when a terminal opens) and the PWA's worker and notifications (VIS-109), 410 KB since
// 0.8.0's phone bar and composer (rc-mobile, 405.0 KB) and the Chat tab's Beta pill took it to the limit
// (2026-10-13, 405.1 KB): no feature cut, as decided for the release. 415 KB since the community
// weights section of the setup guide (2026-10-13, 412.3 KB: the cards, their search and sort, the
// GitHub sync state and six inline glyphs for the networks). Room looked for first: terser is
// larger than oxc here, top-level mangling is already on, no CSS rule is dead, and one combined
// CSS file would save 2.5 KB but load every view's CSS up front; the module-preload polyfill went.
// 420 KB since 0.9.0 (2026-10-14: 415.1 KB as released, 416.4 KB with the panes' plus menu, the
// pages a tool call wrote and the Context panel's reasons).
// 423 KB since the media players (VIS-159, a 1.4 KB chunk loaded on first use) and the Context
// panel's memory notes (SRV-471): 422.2 KB. 424 KB with the Setup Guide's job log and change list
// (VIS-151, VIS-155) on top: 423.7 KB; a lazy install view measured 424.0 KB, so no split.
// 425 KB with the sandbox's reason on a call and the attachment chip's tokens (VIS-167, VIS-169):
// 424.2 KB.
// 428 KB with a live run's reasons on the Now figures and the slot row's owner (VIS-181) and the
// Chat tab's panel for a box whose chatd is down, its re-probe and banner (VIS-179): 427.3 KB.
// 430 KB with the preview's target, its waiting state and Start button, the page's report and the
// new-server watch (SRV-511): 429.7 KB. 431 KB with the Chat tab's design pass on top (VIS-185: the
// figures line as readings, the panes' tab strip that scrolls on its own, the thread's wrap rules): 430.3 KB.
// 433 KB with the Setup Guide's parallel-chats stepper (VIS-199: the field, its reasons, the cost line,
// Next's refusal and the fit search): 432.4 KB. 434 KB with its two window steppers on top (the ladder,
// typed 128k and 1m, the limits named): 433.6 KB. 435 KB with the verify phase on top (SRV-532: the
// harness's check card, the Checked / Not checked block, the composer's switch): 434.2 KB.
// 445 KB with the Setup Guide's restart card, finished screen, Retry and diagnostics and its weights list
// on top (VIS-209, VIS-211, VIS-213, VIS-215, 2026-10-17: 442.5 KB).
// 456 KB with a main agent's workers (VIS-217, VIS-219, 2026-10-17: 455.1 KB from 442.7): the Task card,
// its report, chatd's check and the transcript sheet, the worker's part of the Context panel and the
// Agents switch add 5.9 KB to the chat chunk; the mock's scripted worker and its four seeded tasks
// 5.7 KB to the mock chunk, which loads only with ?chat=mock.
// 458 KB with each running worker's live figures (SRV-579, VIS-219, 2026-10-17: 457.3 KB from
// 455.1): its own Now in the Context panel, a row per worker in the Now dock, the rate on its card,
// and the mock's two workers at once.
// #130 (2026-10-23): the size is pako's gzip at level 6 (scripts/gzip-size.mjs), the same on every Node, OS and CPU.
// 463 KB since the guide learned a simple mode (#103, 2026-11-03: 462.2 KB, 473,274 bytes, on the pako measure, rebuilt
// against main): the mode's own chunk (the three-page flow, the folded settings page, the card tiles and their
// note-line trim) and the Finish gate's plan-check round trip.
// 465 KB with the Log panel's export button (SRV-803, 2026-11-10: 464.4 KB, 475,542 bytes, from 462.9 KB with
// 122 bytes left): the fetch of the engine's file and its time limit, the save step, the panel's error block,
// the line after a save and their styles.
// 485 KB with TandemApp's shell (VIS-242, VIS-244, 2026-10-27: 481.3 KB from 460.4): the v3 tokens, the fleet
// contract's client, the scope, node chip and fleet chart, the grouped menu, both sidebars, the scope bar and
// the phone's bar; the new pages and the fleet chart sit in their own chunk. A split only adds to this total.
// 508 KB with the window manager (VIS-256, VIS-257, 2026-10-27: 504.7 KB from 481.3): the docks, floating windows,
// the tray, the menus and each page's layout in the main chunk (9.7 KB) with their CSS (2.5 KB); the Logs, Chart and
// inspector, the sandbox's and the model's windows in three chunks of their own (8.7 KB), loaded on first use.
// 534 KB with the Monitor pages (VIS-248, 2026-10-28: 530.8 KB from 505.1): Overview with the topology graph, Hardware
// with every metric group, each Spark's sheet and the host, Events & Alerts with the rule editor, in the new pages'
// chunk (22 KB) with their CSS (4.3 KB); the fleet client's sample and host calls and the alert notifications in the
// main chunk (2.4 KB).
// 547 KB with the Sparks pages (VIS-250, 2026-10-28: 543.6 KB from 531.1): the list, the full topology with each link's
// test, a Spark's page with its edit in place and the action dialog in the new pages' chunk (10.2 KB) with their CSS
// (1.5 KB); the client's node actions, the drained status word and the chart's clustered markers in the main chunk
// (0.4 KB). A use of lit's `query` decorator split shared code into another chunk (2.6 KB more): left out.
// 563 KB with the Add Spark wizard (VIS-246, 2026-10-28: 560.0 KB from 543.7): Find, Connect, Check, Install,
// Link, Name and Done with the session's client, its logic and its stylesheet, in a chunk of its own (13.7 KB js
// and 2.3 KB css gzipped) that loads when Settings → Add Spark opens.
// 572 KB with the controller's host and the fleet update (OPS-234, 2026-10-28: 569.3 KB from 560.1): App
// settings with the move and the take in a chunk of its own, Update fleet's plan dialog and progress in the
// Sparks pages' chunk, their client and the planner, the mixed-version marker on the fleet pill.
// 590 KB with the Models pages (VIS-252, 2026-10-28: 586.8 KB from 569.6): the deployment cards with their
// plan dialog, Serve a model with its placement step, the weights matrix and its copies in the new pages'
// chunk (14.1 KB js) with their stylesheet; a Spark's Setup Guide through the controller and its confirm
// in the setup chunk (1.2 KB). The tile rules of the Monitor pages and the guide are scoped to each.
// 610 KB with the fleet in every page (VIS-254, 2026-10-28: 603.7 KB from 587.1): Performance's Now line, a
// strip per Spark, History per Spark and Usage's By Spark in a chunk of their own with their stylesheet; the
// lanes grouped by deployment and Spark, the ledger switch and the Spark filters, Dev's sources and columns,
// Playground's targets and Compare, Conductor's deployment, Chat's place line, the Done step's figures.
// 611 KB with the data plane through the controller (SRV-733, 2026-10-28: 610.1 KB from 609.9): the fleet store
// asks once whether the page's /v1 reaches the controller, and Usage then reads its ledger by default with the
// switch and its words on one Spark too.
// 615 KB after the rebase onto main's 0.15.4 (SRV-747, 2026-11-12: 613.9 KB, 628,681 bytes, from 610.1): main's
// simple Setup Guide mode and the Log panel's export button on top of TandemApp's pages, rebuilt from the merged
// sources.
// 618 KB with a Split on the Models pages (VIS-272, 2026-11-13: 616.3 KB, 631,140 bytes, from 613.9): its two
// stage bars, the link, the plan before the start with every part's basis, the phases in words.
// 619 KB with Get started and the backends as fleet members (SRV-749, 2026-11-13: 618.3 KB, 633,106 bytes, from 613.9):
// the first-open page with its connect-a-server form (a chunk of its own, loaded when it opens), a backend's place
// in the scope bar and on Overview, Hardware's no-host-figures state. Room looked for first: eleven style rules no
// page used went (167 bytes gzip); the form is already its own chunk, but the budget counts every file.
// 622 KB with both on one line (2026-11-13: 621.1 KB, 635,979 bytes): a Split's view and Get started
// together, plus the Models tiles that count a Split's stages and the plan's measured table (410
// bytes). Room looked for first: no style rule is left that no page uses. One budget for both.
// 643 KB with the setup wizard (VIS-278, SRV-827, SRV-829, SRV-831, SRV-833, 2026-11-15: 641.8 KB, 657,202 bytes,
// from 621.3 KB): Add engine, pairing by code, the other ways, the model step and the first message, with the
// engine host routes' client and the steps' logic, in a chunk of their own (17.6 KB js, 2.6 KB css) that loads
// when Add engine opens; the route, the menu entry and the first message's hand-over to Chat in the chunks that
// had their kind. Room looked for first: the hand-over's two functions sit with the routes, so no chunk of its
// own carries them.
// 647 KB with What runs here (OPS-288, VIS-280, 2026-11-15: 645.6 KB, 661,074 bytes, from 641.8 KB): the App
// page's switches for Chat and the conductor with their plan dialog, the engine hosts' own mode, the backends
// and MCP servers with disable and remove, the Chat tab's Chat-is-off state and the wizard's Playground step.
// Room looked for first: the panel sits in the App page's chunk, and the words are one module the tests share.
// 671 KB with the settings pages (VIS-280, 2026-11-16: 669.3 KB, 685,366 bytes, from 645.6 KB): a page a scope of
// the settings interface drawn from its schema, the staged change set with its bar, the run sheet (plan, second
// confirm, result, restore), the restart card and its line in the dock with the countdown and Cancel, versions and
// the audit list, with the interface's client, the draft, the flow and every refusal's words. Room looked for first: the area is one
// chunk with the App page it now holds (47.3 KB js, 3.1 KB css), loaded when Settings opens;
// What runs here lost its own plan dialog for the two switches; the tab strip is styled in the chunk, so the
// Conductor's stylesheet stays out of it.
// 715 KB with the hooks pages (VIS-282, 2026-11-16: 714.5 KB, 731,617 bytes, from 670.9 KB): the pipeline of a
// request's stages with its time budgets, add a hook with the secret shown once, a hook's page with its playground
// and journal, the trace, and the decision providers with their health, question mapping, test and agreement
// report, with both APIs' client, the two flows and every refusal's words. They are a chunk of their own (39.6 KB
// js, 3.7 KB css) that loads when Settings → Hooks opens. The entry chunk stays at 106.0 KB; the settings area's
// chunk grows by 0.2 KB for the import and the page that holds them. Room looked for first: the pages use the
// settings area's draft, run sheet, refusals and row styles, not copies of their own, and read the stage table
// from the controller; the budget counts every file, so a further split saves nothing.
// 731 KB with the health page (SRV-835, 2026-11-16: 729.9 KB, 747,368 bytes, from 714.4 KB): the verdict, a card a
// target with its line of checks, the findings with their fixes, the dialog of Upgrade and the notes a fix leaves,
// with the health routes' client, the flow and the page's words. It is a chunk of its own (11.8 KB js, 3.0 KB css)
// that loads when Monitor → Health opens. The entry chunk stays at 106.0 KB (33 bytes more: the route, the menu
// entry and its icon). Room looked for first: the page uses the engine routes' refusals and the sign-in card, not
// copies of its own; the budget counts every file, so a further split saves nothing.
// 752 KB with the plugins pages (VIS-284, 2026-11-16: 749.4 KB, 767,421 bytes, from 714.5 KB): the installed list,
// an install with its checks, the sha256, the capabilities in words with their risk and what a plugin adds, a
// plugin's page with its state, the form drawn from its settings schema, its secrets, grants, log and files, and
// a panel's frame with the page's side of its bridge, with the API's client, the flow and every refusal's words.
// They are a chunk of their own (29.8 KB js, 2.1 KB css) that loads when Settings → Plugins opens. The entry
// chunk stays at 106.0 KB. The small templates they share with the hooks pages moved into a chunk of 1.0 KB
// that both load (the hooks pages' chunk went from 39.6 to 39.1 KB); the settings area's chunk carries the
// capability words its review sheet uses and the page that holds the plugins pages. Room looked for first: the
// pages use the settings area's draft, run sheet, versions and rows and the hooks pages' cards, fields and
// stylesheet, not copies of their own; the panel the mock serves is not in the bundle; the budget counts every
// file, so a further split saves nothing.
// 768 KB with both (2026-11-16: 765.9 KB, 784,253 bytes, from 714.5 KB): the health page and the plugins pages
// merged, each a chunk of its own that loads when its page opens (11.8 KB js and 31.2 KB js). The plugins pages'
// chunk grew from 29.8 KB with a wheel's upload, the list's run state, the controller's word on which grants ask
// twice and the words of the trust routes' refusals. The entry chunk stays at 106.0 KB.
// OPS-246: dist/ is a build output; CI runs this after the build and a total over the budget fails
// the job. An optional argument names another folder (the size test's fixture).
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { BUDGET, distSizes } from './gzip-size.mjs';

const dist = process.argv[2] ? resolve(process.argv[2]) : new URL('../dist/', import.meta.url).pathname;
if (!existsSync(dist)) {
  console.error(`no ${dist}: build the page first (python3 tools/dashboard_bundle.py build)`);
  process.exit(2);
}
const { files, total } = distSizes(dist);
for (const f of files) console.log(`${(f.gzip / 1024).toFixed(1).padStart(7)} KB  ${f.path}`);
console.log(`${(total / 1024).toFixed(1).padStart(7)} KB  total gzip (budget ${BUDGET / 1024} KB, ${BUDGET - total} bytes left)`);
if (total > BUDGET) {
  console.error(`over budget: ${total} bytes gzip (${(total / 1024).toFixed(1)} KB) > budget ${BUDGET} bytes (${BUDGET / 1024} KB)`);
  process.exit(1);
}
