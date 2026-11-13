# Decoupled question bank, flow-state DDA and travel mode

The study workstation can now pre-generate a question pool on the host, cache a
deck in the browser, and run a full quiz session offline on a phone. Memory
state stays in the same FSRS tables the rest of the app uses, so a question
answered on the train updates the concept card exactly like a desk review.

## Components

| Path | Responsibility |
| --- | --- |
| `engine/db.py` | Adds `quiz_bank`, `flow_review_logs`, `flow_state`, `schema_meta`, `game_scores` to `data/recall.sqlite`. Additive only. |
| `engine/gemini_batch.py` | JSON batch client: thinking config, full-jitter backoff, `Retry-After`, 400/404 → fallback model, 401/403 → stop. |
| `engine/quiz_factory.py` | Micro-batch generator (4–6 questions per chunk), schema validation, checkpoint per batch, `--target-pool` CLI. |
| `engine/flow_scheduler.py` | Interleaving, travel filtering/postponement, momentum DDA, pool statistics. |
| `engine/fsrs_bridge.py` | Applies a review to `fsrs_cards`/`fsrs_logs`; postpones due time without a lapse. |
| `engine/sync_bridge.py` | Deck bundling (<2 MB), idempotent review upload, manual export. |
| `engine/routes.py` | `/api/quiz/*`, `/api/sync/*` and `/api/games/*` endpoints. |
| `engine/games.py` | Topic legend, seeded Sudoku generator, Trap Hunt and Concept Match payloads, personal bests. |
| `static/flow.js` | Bank tab, five-point flow selector, postpone action, IndexedDB cache, sync modal. |
| `static/games.js` | Games hub, Sudoku/Trap Hunt/Concept Match UI, offline score queue. |
| `static/sw.js` | Service worker: app shell, vendor bundles, cached GET API fallback. |

## Cognitive mix

The 20-slot rotation in `quiz_factory.ROTATION` is exactly 30 % INTUITION,
35 % CALCULATION_STEP, 20 % ADVERSARIAL_TRAP and 15 % SYSTEM_APPLICATION
(6/7/4/3), and no two adjacent slots share a type, so any five consecutive
slots form a legal micro-batch. `CALCULATION_STEP` is the only type with
`requires_scratchpad = 1`.

## Flow rating → FSRS

| Rating | Label | Δ momentum | FSRS grade |
| --- | --- | --- | --- |
| 1 | 😴 Too Easy | +1.5 | 4 Easy |
| 2 | 😌 Comfortable | +0.5 | 3 Good |
| 3 | ⚡ Flow State | 0.0 | 3 Good |
| 4 | 🧗 Challenging | −0.5 | 2 Hard |
| 5 | 🤯 Overwhelmed | −1.5 | 1 Again |

`M(t+1) = 0.7·M(t) + 0.3·Δ`. Above +1 the target tier rises one step, below −1
it falls one step. Momentum is persisted per session in `flow_state`.

## Travel mode

`mode=travel` filters `requires_scratchpad = 0` in SQL (not in the ranking) and
moves every due paper question, plus its concept card, forward 24 hours.
`postponed_until` expires and `revive_postponed()` returns the row to `STAGED`;
stability, difficulty and review history are untouched and no `fsrs_logs` row
is written.

## Commands

```bash
# generate until the pool holds 20 questions (verification run)
python3 -m engine.quiz_factory --target-pool 20

# keep filling towards the long-term pool, checkpointing each batch
python3 -m engine.quiz_factory --target-pool 500 --pause 2

# preview prompts without calling Gemini
python3 -m engine.quiz_factory --target-pool 20 --dry-run --max-batches 1

# start the app
bash run_app.sh

# install/refresh the click-to-open Desktop shortcut
./install_desktop_shortcut.sh
```

The shortcut installer writes `Recall Study.desktop` (plus a `Recall Study.sh`
fallback) to the XDG Desktop folder, marks both executable and trusted, and
points them at `launch_recall.sh`. That launcher starts the server only when
`/api/health` is not answering, waits for readiness, then opens
`http://127.0.0.1:8787/` with `xdg-open`. Override the browser with
`RECALL_BROWSER=<command>`; launcher output is appended to `data/launcher.log`.

Environment overrides live in `.env.example`: `QUIZ_PRIMARY_MODEL`,
`QUIZ_FALLBACK_MODEL`, `QUIZ_THINKING_BUDGET`.

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/quiz/stats` | pool / synced / completed / postponed counters |
| GET | `/api/quiz/queue?limit=25&mode=all\|travel` | interleaved queue for the session |
| GET | `/api/sync/pull_deck?limit=100&mode=all\|travel` | marks returned rows `SYNCED_TO_MOBILE`, trims to <2 MB |
| GET | `/api/sync/export_deck?limit=200` | file download, no status change |
| POST | `/api/sync/push_reviews` | appends logs, updates FSRS, sets `ANSWERED`; idempotent by `client_id` |
| POST | `/api/quiz/postpone` | defers question + card 24 h, no lapse |
| POST | `/api/quiz/flow` | updates momentum/tier |
| POST | `/api/quiz/generate` | background job; adds N questions to the pool |
| GET | `/api/games/topics` | topic folders, question counts and their concepts |
| GET | `/api/games/sudoku?topic=&difficulty=&seed=` | unique board + nine-concept legend |
| GET | `/api/games/trap_hunt?topic=&limit=` | desk-free adversarial questions for a timed round |
| GET | `/api/games/match?topic=&pairs=` | shuffled concept/clue cards |
| GET | `/api/games/scores?topic=` | personal bests and the five latest rounds |
| POST | `/api/games/score` | stores a round; idempotent by `client_id` |

## Topic games

Three games reuse the bank, so nothing new has to be generated:

- **Topic Sudoku** maps digits 1–9 onto nine concepts. The generator fills a
  complete grid with randomised MRV backtracking, then removes symmetric pairs
  while a second solver proves the solution stays unique. `difficulty` is a
  target (easy 42, medium 34, hard 28 givens); the payload reports the count it
  actually reached. A topic with fewer than nine concepts borrows distinct
  concepts from other folders before using neutral `Term N` labels, and
  `from_topic`/`mixed` tell the UI to say so. The same seed reproduces the same
  board.
- **Trap Hunt** serves only `ADVERSARIAL_TRAP` rows with
  `requires_scratchpad = 0`; a correct answer adds `10 × combo` and raises the
  combo to ×5, a miss resets it. The round lasts 60 seconds or until the
  questions run out.
- **Concept Match** pairs a concept with the first explanation line. Thin
  topics are topped up from other folders and flagged `mixed`.

Scores live in `game_scores` (`client_id UNIQUE`). A round finished offline is
written to the `game_scores` IndexedDB store and uploaded on reconnect; the
same `client_id` is ignored on retry, so a best is never double-counted.
An unfinished Sudoku board (placements, hints, mistake count, selection and
timer start) is saved in local storage and can be resumed after closing or
reloading the app; the latest generated board can also be replayed offline.
Sudoku scoring subtracts mistakes, hints and seconds from a difficulty base;
Trap Hunt scores the combo points; Concept Match rewards fewer moves and less
time. Every game keeps a per-game, per-topic personal best.

## Adaptive session, LAN sync and mobile

**Interleaving.** Open the **Bank** tab for the local adaptive session (the
**Quiz** tab is the older Gemini-generated exam and spends metered data).
`/api/quiz/queue` round-robins note domains
(`maths/03. Algebra`, `Mlaidlgenai_robo/DL`, `Robotics`, ...) and rotates the
four cognitive modes inside each domain, so a 50-question session never serves
one topic in a block. The response includes a `domains` histogram.

**Reinforcement.** A missed question, or a *Challenging* / *Overwhelmed*
rating, calls `POST /api/quiz/weakness`; the host returns a same-concept
variant to insert 8-12 slots later. A correct second pass retires it; a second
failure marks the concept high priority for the next session.

**LAN sync.** The host binds `0.0.0.0` by default (`RECALL_HOST=127.0.0.1` to
stay loopback-only). `GET /api/network/info` lists the phone-dialable URLs and
mDNS state; `GET /api/sync/pull_batch?count=100` returns a compact (<1.5 MB)
deck and `POST /api/sync/push_reviews` accepts offline reviews. Host headers
are limited to loopback, `*.local` and private addresses.

On this laptop `ufw` is enabled with `DEFAULT_INPUT_POLICY=DROP`, so the phone
cannot open `10.119.153.19:8787` until the port is allowed:

```bash
sudo ufw allow 8787/tcp
```

The USB path (`adb reverse tcp:8787 tcp:8787`) does not need a firewall change
and is the reliable sync route.

**USB deployment.** `bash 00_orchestrator/deploy_mobile_adb.sh` detects an
authorised device, runs `adb reverse tcp:8787 tcp:8787`, and opens the app at
`http://localhost:8787` (a secure context, so the service worker works with the
cable unplugged). An APK in `dist/*.apk` is installed with `adb install -r`
when present; otherwise the PWA path is used and nothing is downloaded.

**Question buffer.** `bash run_factory.sh --once` tops the staged pool up to
500 questions in 4-6 question batches. It is opt-in because it spends metered
data; the web app never starts it. `/api/quiz/factory/status` reports the last
run. Once questions are in `quiz_bank`, the quiz loop is fully offline.

**Corpus OCR.** `03_rtx_compression_ocr/audit_pdf_text_coverage.py` reports
pages with fewer than 50 selectable characters;
`backfill_searchable_text.py --resume` OCRs only those pages, verifies the
staged PDF, and atomically replaces the corpus file. Pass 2 adds a
`--refresh` mode and tries 90°/270° rotations, because many handwritten pages
are scanned sideways. Current corpus state: **272 PDFs / 9,080 pages, 263
sparse pages (down from 399), 0 unreadable**. Then run
`bash rebuild_index_offline.sh` to refresh BM25 and dense vectors without any
network call. Current index: **272 sources, 17,914 chunks, 17,914 vectors,
384-dim local bge-small embeddings, 100% searchable**. Tests:
`source 00_orchestrator/runtime.sh && TMPDIR=/tmp/sam_win_tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$OPENCODE_PYTHON" -m pytest -q` → **162
passed**. Sourcing `runtime.sh` supplies the CUDA library path the local
embedder needs; without it one semantic test fails on `libcudnn`.

## Phone installation

The app is a PWA (`manifest.webmanifest` + `sw.js` + IndexedDB). Service
workers require a secure context, so install it from `http://127.0.0.1:8787`
with port forwarding (`adb reverse tcp:8787 tcp:8787`), from `localhost` on the
phone, or behind an HTTPS tunnel. Without HTTPS the quiz still runs online, but
there is no offline cache.

1. Open the app over USB (`adb reverse`) or over Wi-Fi after
   `sudo ufw allow 8787/tcp`; then tap **Sync** → **Sync Mobile**.
2. In Firefox open the menu → **More** → **Add to Home screen** → **Add
   automatically**. The shortcut opens `http://127.0.0.1:8787/` and shares the
   same Firefox storage and service-worker cache.
3. Enable **Travel / Commute** before boarding; paper questions are hidden and
   postponed.
4. Answers made offline queue in IndexedDB and upload automatically on
   reconnect (or via **Upload Study Progress**).

Verified on the I2011: the home-screen shortcut opened the app with the host
stopped, the Bank tab served 12 cached questions, and an offline review queued
in IndexedDB and uploaded with `synced_from_client=1` after reconnect.
