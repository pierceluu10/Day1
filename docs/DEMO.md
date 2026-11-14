# Demo: what we build toward

Every session builds for this run-of-show. If a feature does not appear here or in an acceptance test, it is not needed. Agent 7 (QA) owns this file after Phase 0 and keeps the timings real.

## Cast

| Order | Hire | Machine | Role in the demo |
|---|---|---|---|
| 1 | `ada` | ubuntu | First hire. Hits all four snags, solves them by LLM reasoning. Run before the demo; shown from Atlas, labelled "recorded earlier today". |
| 2 | `grace` | debian | Training hire. Same snags, mostly fixed from memory; the runtime fix is adapted from ubuntu. Run before the demo. |
| 3 | `linus` | ubuntu | Run before the demo. Second hire with the same fixes, which triggers the README issue (same fix applied for 2+ hires). |
| 4 | `barbara` | ubuntu | **Live.** Fixes from memory, killed with `kill -9` mid-run, resumed, then `push_change.sh` lands mid-run. |
| 5 | `ken` | alpine | **Live (or started in parallel at 1:30).** Unseen machine: apt fixes retrieved semantically and adapted to apk. |

Gitea is reset and hires 1 to 3 are run between 15:15 and 16:00 so the memory in Atlas is real and from today.

## Screen layout

- Left two thirds: the headed Chromium window the agent drives, with `intern_overlay.js` rings (green = acting from memory, amber = reasoning, red "This moved." = a saved locator failed).
- Right third: the Day One page (`http://localhost:8000`): step list with status and one evidence line each, the streaming terminal below it, and one line "ada (hire 1): 9m 12s, 6 errors, 0 from memory · barbara: 2m 05s, 6 errors, 6 from memory", computed from Atlas.
- A terminal tab for `scripts/move_ui.sh`, `kill -9` and `scripts/push_change.sh`.

## Run-of-show (3 minutes)

`scripts/move_ui.sh` restarts Gitea, so it runs only before Start (never once the agent is past `b_sign_in`: a clone or `ssh -T` during the restart fails). `scripts/restore_ui.sh` undoes it; `scripts/reset.sh` calls it.

| Time | What the audience sees | Track 3 question |
|---|---|---|
| 0:00 | One sentence: every new hire hits the same broken setup steps and the fix is lost. Day One fixes that. Page shows ada's recorded run: 9 minutes, 6 errors, all solved by reasoning. | |
| 0:10 | In the terminal tab: `scripts/move_ui.sh` ("we just changed Gitea's SSH-key form"). Gitea restarts for 2 to 3 s; wait for it to return before Start. | Q3 |
| 0:15 | Type `barbara`, pick ubuntu, Start. Chromium signs in with a green ring (locator from memory). At the key form the saved locator fails: red "This moved.", then amber while it reasons over the page's accessibility text, finds the new fields, adds the key and saves the new locator. | Q3, Q1 |
| 0:35 | SSH key step turns verified only after two independent checks: "API lists key SHA256:…; ssh -T: authenticated". Clone verified: "local HEAD 3f2a1c9 = API HEAD 3f2a1c9". | Q1 |
| 0:50 | `pip install` fails with `pg_config executable not found`. Instantly: "fixed from memory: exact match, from ada, applied 2×". No LLM call. | memory |
| 1:05 | In the terminal tab: `kill -9` the agent mid-step. Page shows the run stopped with one step `pending`. | Q2 |
| 1:15 | `dayone resume`. Every finished step is re-checked and shown `skipped (verified, not re-run)`. The pending step is checked first, not blindly re-run. Run counts stay 1. | Q2 |
| 1:35 | `scripts/push_change.sh` adds a dependency that needs another system package. The agent reports "remote HEAD moved 3f2a1c9 → 8be04d1", re-verifies clone and downstream steps, hits the new error, reasons (amber), fixes it, saves the experience. | Q3 |
| 2:10 | Tests pass: "14 passed, exit 0". Welcome issue checklist fully ticked in Chromium. Comparison line: ada 9m vs barbara 2m. | |
| 2:20 | Switch to `ken` on alpine (started at 1:30 in parallel, or pre-run and labelled). `apk` error retrieved semantically: "similarity 0.85, from ada (ubuntu, apt) → adapted to apk add postgresql-dev". | memory, Q3 |
| 2:40 | Gitea: the issue Day One opened on `acme-api` proposing the README change, citing the hires who hit it. | closing the loop |
| 2:50 | One sentence: state in Atlas, verified by independent checks, memory by vector search. | |

## Snags in `acme-api` (what the agent must discover; the README mentions none)

| Snag | Fails as | Ubuntu 22.04 fix | Debian 12 | Alpine 3.20 |
|---|---|---|---|---|
| Runtime version: `requires-python >=3.12` | `ERROR: Package 'acme-api' requires a different Python: 3.10.12 not in '>=3.12'` (pip, from `-e .`) | install Python 3.12 (e.g. `uv python install 3.12`) | 3.11: same failure, same fix | 3.12 ships: no failure |
| System package: `psycopg2` from source | `pg_config executable not found` / missing `gcc` | `apt-get install -y libpq-dev gcc python3-dev` | same | `apk add postgresql-dev gcc musl-dev python3-dev` (semantic + adapted) |
| Settings file | `SettingsError: settings.toml not found (copy settings.example.toml)` | `cp settings.example.toml settings.toml` | same | same |
| Database init | `sqlite3.OperationalError: no such table: users` | `python -m acme_api.db init` | same | same |
| Mid-run change (`push_change.sh`) | new dep needs e.g. `libffi`/`libxml2` headers | `apt-get install -y libxml2-dev libxslt1-dev` | same | `apk add libxml2-dev libxslt-dev` |

Exact names and versions are the infra session's call; this table is updated to match what really fails.

## What must never be faked

Every status shown has a check that ran. Every number comes from an Atlas query (`dayone/api` computes them; QA test 7 compares). Pre-recorded runs are labelled on screen.
