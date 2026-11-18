# Status lines, the voice and the shadow reader

A client shows the model's reasoning and its answer. What the conductor does around them, the engine's queue, a long prefill, a hidden close, was visible only on the dashboard. This page covers the lines that show it in the client, the small model that can write them, and a second use of small models: the shadow reader that keeps its own exact copy of a session.

## Status lines

With a profile's `ui.states: on` (the shipped profiles), each step the conductor takes is one short line the client renders, at the moment it happens:

```
› code edit, maximum effort
› reading 20,745 tokens, about 18 s left
› waiting: another request is running, position 1, about 40 s
› thinking 25 s: designing a 140x15 tile map with pits, coins, enemies and platforms
› writing render.js, 211 lines so far
› checking the tool call
```

| kind | template | when |
|-|-|-|
| `route` | `deciding: code_edit, high effort` | a classifier answered this turn |
| `package` | `retrieving: skill photo-to-3d` | the note carries a skill |
| `cap` | `thinking closed at the 16,384 cap` | the turn's cap closes the reasoning |
| `backstop` | `thinking closed at the 32,768 backstop` | D2 closes it |
| `loop` | `loop detected, steering` | D1 closes a reasoning loop |
| `continue` | `empty answer, continuing` | E1's hidden continue |
| `check` | `checking the tool call` | SP3 checks the held calls |
| `repair` | `repairing the tool call` | C4 repairs a call |
| `queue` | `waiting: ..., position 1` | the request waits for the engine |
| `prefill` | `reading 35,189 tokens at ~750 tok/s, about 47 s left` | a long prefill, every 10 s |
| `thinking` | `thinking 4 min, 9,800 tokens` | every 2 minutes or 4,096 tokens of thinking |

*Kinds of status line.*

`ui.states_channel` says where the lines go. `reasoning` puts them inside the reasoning the client renders. `content` sends each as a content delta of its own, before, between and after the reasoning's parts. `inline`, the coding profile's value, is built for opencode: each line opens a reasoning part of its own, titled by the line, in time order, and the answer's text part opens after them. It works because opencode's provider ends an open reasoning part when an empty `tool_calls` delta arrives and opens a new one at the next reasoning delta, which the server sends as `**› line**`. That was checked by running recorded streams through opencode's own parser, taken out of its binary.

The model never reads a line. Every line is framed so that the server can find it exactly, and whenever the conductor is configured the server takes every framed line out of whatever a client sends back, before the router, the monitor or the template read it. The prompt, the resident prefix it resumes and the notes' replay positions are exactly what they would be without the lines. The lines are not tokens: never in `usage`, the suffix store or the response cache's key. The journal row keeps each one with its kind and its time.

Open WebUI times a reasoning block from its first reasoning piece to its first answer piece and shows that as “Thought for N seconds”, so the waiting and reading lines counted as thinking: a summary with thinking off that read 12,874 tokens showed “Thought for 14 seconds”. For the client kinds in `QWEN38_STATUS_CLIENTS` (default `open-webui`) every line said before the model's first generated token goes out as an Open WebUI status event, the line under the model's name that its tools use, and one last event at the first token says what the wait was (“Read 12,874 tokens in 14 s”). The reasoning opens with the model's own first piece, and a turn that does not think shows no “Thought for”. Lines after the first token keep the profile's channel.

The progress lines (`queue`, `prefill`, `thinking`) are templates with numbers: the queue position and an estimate of the wait, a long prefill's rate and time left, a long thinking's time and tokens (`ui.progress`). A request that waits for the engine opens its stream at its first waiting line, so a refusal after that ends the stream with an `error` object and `[DONE]`.

## The narrator and the voice

`ui.narrator` picks who writes the lines that are not progress templates.

| value | lines |
|-|-|
| `off` | the templates |
| `on` | each line rewritten by a small model, in at most 150 ms, else the template |
| `thoughts` | one summary per stretch of thinking of at least 128 tokens: “Thought 1m 43s: planned the level layout and decided on a fixed timestep” |
| `live` | the voice: what the model is doing right now |

*The narrator's modes.*

The voice (`live`, the coding and chat profiles) separates facts from words. The engine writes the facts: the timer, a tool call's action and target read from the open call block as the model writes it, a write's line count, the queue's place, the prefill's tokens. The small model writes only the short phrase after them, from a window of the model's newest tokens: what the thinking is about now (every 25 s from 15 s), what a thought of at least 10 s decided, what a write is for. Every phrase passes a check before it is said: a file name or number it holds must be in the text the small model read, and so must most of its content words; no first person, no markup, no copy of an example, nothing like the last four lines, and the user's language (English or German). A phrase that fails is not said. Nothing is said for a short turn: no thinking line before 15 s and no tool line for a call done within 1.5 s.

The small model is `QWEN38_NARRATOR_MODEL`, a Qwen3.5 model (`qwen3.5-2b` for the voice). It runs on the engine's own Qwen3.5 forward (`engine/generative/qwen35.py`) and reads the model's own token ids without a second tokenisation, because the Qwen3.5 small models share Qwen3.8's vocabulary and merges; the server checks that at load against the served checkpoint's tokenizer. Its decode step runs from a CUDA graph captured at load, in a memory pool and on a stream of its own. It loads at the start through the residency planner (role `narrator`, a 6 GiB claim) and stays loaded.

Its time is rationed. The phrases are written in slices of at most 150 ms on the request's own thread, between two of the model's rounds, with the device synchronised first, so nothing of the small model runs beside the big one. A request's small-model time is one share: 3 % of the time since the model's first token (`ui.live.share`; 2.5 % in the coding profile, which also runs the shadow reader from the same share).

Measured on the engine with Qwen3.5-2B (the 0.4.0 series, the same 2,000-token text and 504 blocks in every run, 3 runs an arm, `--no-learn-cost`): 63.47 tok/s with the voice off, 62.26 on, -1.90 % (measured). On four recorded opencode sessions the 2B wrote a phrase in 308 to 369 ms at the median, in slices of at most 150 ms (measured, `tools/voice_replay.py`).

## The shadow reader

A small Qwen3.5 model keeps its own state of each coding session: its KV rows and its Gated DeltaNet state for every token the session holds, the served prompt and the model's output. It reads each turn's new tokens and nothing else, so a small model can answer a question about a 100,000-token session in one forward of the question. It is off until `QWEN38_SHADOW_MODEL` names a model (`qwen3.5-0.8b`).

Two cheaper ways to give a small model the big model's context were tried first and failed: a few soft tokens made from the 27B's hidden rows answered at chance, and a per-layer map from the 27B's state into a small model's reached a held-out R² of about 0.5, far from what agreement needs. The small model's own state of the same tokens is exact, and that is what the shadow keeps.

**Exact on a grid.** The state advances only in whole chunks of 512 rows at absolute positions k · 512. A fresh prefill of the same ids on the same grid runs the same chunks on the same inputs, so the two states are equal bit for bit, however many turns it took. Measured on the GPU on two real conversations of 41,802 and 58,278 tokens: every KV row, the recurrent state and the convolution state equal a fresh prefill on the same grid (measured, the 0.4.0 series). When a new prompt differs from what was read (a template drops an earlier turn's reasoning), the reader goes back to the newest checkpoint at or below the difference, one kept at each turn's prompt end.

**When it reads.** While the big model decodes, only it and its drafters use the GPU. The reader has two windows. In the idle window it looks every 0.2 s, and when no request runs or waits it reads for at most 2 s, checking before every chunk whether a request waits; a request that arrives waits at most one chunk, 36 to 50 ms on the 0.8B. Inside a turn it may use 2 % of the time since the model's first token, in slices between two rounds. The idle window alone is not enough for an agent: in the ledger's opencode sessions the median gap between one request's end and the next one's start is 0.31 s.

**What it costs.** With the shadow on, the big model's decode on a 16,350-token prompt went from 36.02 to 35.28 tok/s, -2.05 %, and the time to first token did not move (measured, `tools/shadow_ab.py`, 6 runs an arm, the 0.4.0 series). That is the 2 % share, as designed.

| session depth (tokens) | 0 | 16,384 | 33,000 | 41,000 |
|-|-|-|-|-|
| Qwen3.5-0.8B, ms per 512-row chunk | 36.4 | 41.1 | | 49.9 |
| Qwen3.5-2B, ms per 512-row chunk | 50.5 | 56.1 | 69.1 | |

*The shadow reader's read rate: milliseconds per 512-row chunk at three depths of the session (the 2B's last point at 33k). Measured: `tools/shadow_measure.py` with the served process's arithmetic flags, median of 3, the 0.4.0 series.*

**Who asks.**

`ask(session, question, choices)`, also `POST /v1/conductor/shadow/ask`, is a typed decision of the small model over the whole session: each choice is a letter, and one forward of the question gives the letters' softmax. At the end of a 41k session the router's 9-class question took 68 ms on the 0.8B (measured). In the coding profile the router's class question is asked of the shadow in shadow mode on turns of at least 8,192 tokens and journaled beside the router's own class; nothing of the turn changes.

What that journal showed is a result in its own right. On 58 journaled turns, the small models agreed with the router's class no better than a constant answer does:

| model | classes with descriptions | short labels only |
|-|-|-|
| Qwen3.5-0.8B | 8 of 58 | 2 of 58 |
| Qwen3.5-2B | 25 of 58 | 8 of 58 |
| Qwen3.5-4B | 19 of 58 | 44 of 58 |

*The router's question asked of the shadow on 58 journaled turns of 6 opencode sessions. A constant `code_edit` scores 42 of 58. (measured), `tools/shadow_router_eval.py`, 2026-10-05.*

The shadow therefore keeps the 0.8B (1.5 GiB of weights, the fastest read) and the question stays in shadow. A question the small models can answer, such as two or three coarse classes asked only where the router decides fresh, is the next thing to try.

**Memory.** Resident states are capped at `QWEN38_SHADOW_GB` (4 GiB). Past the cap, the least recently used session goes to the NVMe (`QWEN38_SHADOW_DISK`, one file a session, 48 h) and comes back exact. A 58k session is 0.88 GiB on the 0.8B. The model loads through the residency planner in an idle window soon after the start, and its own pool, up to its claim, is left out of the engine's memory ceiling. A live memory pool never gives its free segments back, so after an idle burst that leaves more than `QWEN38_SHADOW_POOL_SLACK_GIB` (2) free in the pool, the shadow parks its sessions on the disk and unloads the model; the next turn loads it again and reads on from the disk.

