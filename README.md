# Day One

Every new hire loses their first days to the same broken setup steps, solves them alone, and the fix is lost. Day One is an agent the new hire starts with one command. It sets up their Git account in a real browser and their laptop in a real terminal until the team's tests pass. It confirms every step really took effect, resumes after `kill -9` with nothing done twice, adapts when the repository changes mid-setup, and remembers every error-and-fix in MongoDB Atlas so the next hire never solves it again, even on a machine type it has never seen.

Built today for the MongoDB hackathon, Track 3 "Finish the Job". `demo_repo/` (`acme-api`) is a demo service built today to mirror common onboarding problems; its README mentions none of the four snags the agent has to find.

## Run it

Needs Docker, [uv](https://docs.astral.sh/uv/), and a MongoDB Atlas cluster with Vector Search.

```bash
cp .env.example .env        # fill in MONGODB_URI, ANTHROPIC_API_KEY, VOYAGE_API_KEY and the local Gitea passwords
uv sync
uv run playwright install chromium
scripts/up.sh               # local Gitea 1.24.7 seeded with org acme, repo acme/acme-api, five hires and their Welcome issues
uv run dayone serve         # the page on http://localhost:8000: name, machine, Start
```

Or from the command line:

```bash
uv run dayone start --hire ada --machine ubuntu     # ubuntu | debian | alpine
uv run dayone resume --hire ada                     # after a crash or kill -9
uv run dayone reset ada                             # container, ledger and Gitea state for one hire
scripts/push_change.sh                              # commit a new dependency to acme-api while a hire is mid-run
scripts/reset.sh                                    # put Gitea and acme-api back to the seed
```

## What it does

Thirteen ordered steps across three systems: a fresh Docker container per hire (`ubuntu:22.04`, `debian:12`, or `alpine:3.20` held out as the unseen machine), Gitea driven through headed Chromium with Playwright, and Atlas.

| Terminal (`docker exec`) | Browser (Playwright) |
|---|---|
| install base tools, generate an SSH key, clone over SSH, Python runtime, system package, dependencies, settings file, database init, run tests | sign in, add the SSH key in account settings, open the Welcome issue, tick its checklist |

Every step is `{id, kind, check(), run(), evidence}`. The `run()` is the naive action the README implies; the check is what says it worked.

## How it answers Track 3's three questions

**1. How does it tell an action that took effect from one that was only sent?** Every check takes a different route from its action, and its real output is stored on the step as evidence.

- The SSH key is added in the browser, then verified by the Gitea API listing its fingerprint *and* `ssh -T` authenticating from the container. Either alone fails the check (acceptance test 3 removes each in turn).
- The clone is verified by comparing local HEAD with the API's branch HEAD: `local HEAD 2143c85 = API HEAD 2143c85`.
- Tests are verified by exit code and passed equals collected: `14 passed of 14, exit 0 at 2143c85`.
- Fail before pass: a check that never failed for this hire is flagged untrusted, so a check that always passes cannot hide.

**2. How does it resume after a crash without replaying its whole history?** Atlas holds a ledger, not a log to replay.

- Intent before effect: each step is written `pending` in `steps` before `run()`. On restart a pending step is checked first and never blindly re-run.
- Reconcile, do not replay: every step's `check()` runs first. If it passes, the step is verified and skipped.
- A lease on the `hires` document (10 s TTL, renewed every 3 s) keeps two agents off one hire; `resume` waits it out after `kill -9`.
- Non-repeatable actions carry an idempotency key derived from (hire, step): the SSH key's comment and title.
- Acceptance test 2 kills the agent at five points chosen from live events, including mid-terminal output, inside a browser step and between drift and re-verify. It then asserts that no verified step's `run()` ran again, that every pending step was checked before a re-run, and that the final state matches a clean run.

**3. How does it adapt when a system's layout, fields or workflow changes underneath it?**

- Repository change: `t_clone`'s check is also the drift probe. When remote HEAD moves mid-run, the agent emits `drift_detected {from, to}`, marks `t_clone` and every later step stale, and re-verifies them. New errors go through memory, and then reasoning.
- UI change: for each browser goal the working locator, bounding box and a page summary are saved. When `scripts/move_ui.sh` changes Gitea's SSH-key form, the saved locator fails, the overlay shows a red "This moved.", and the agent reasons over the page's accessibility text, finds the new fields and saves the new locator.
- Machine change: see memory below. Fixes learned with `apt` are found by vector search and adapted to `apk`.

## Experience memory (Atlas Vector Search)

When a step's check still fails after `run()`, the agent:

1. Captures the error (the last 4,000 characters, where pip and apt put the cause) and its context: step, OS family, package manager and tool versions.
2. Fingerprints the error Sentry-style, stripping paths, versions and timestamps.
3. Looks for an exact match: same fingerprint, same package manager. On a match it replays that fix with no LLM call.
4. On a miss, runs `$vectorSearch` over `experiences` filtered to `outcome: "verified"`, with a cosine score of at least 0.8. A hit from another machine is adapted by the LLM; the page shows the similarity score and the hire it came from.
5. Otherwise, or if the retrieved fix fails, asks Claude to reason, with the nearest experiences as hints. It gets at most 3 attempts, and each attempt is fed the previous commands' output.
6. Saves the fix only once the step's check passes. The record holds the fingerprint, error text, context, fix commands, `verified_by_check` (the evidence string) and the hires that used it.

When the same fix has helped two or more hires, the agent opens an issue on `acme-api` proposing the README change.

Collections: `hires` (lease, container, run), `steps` (unique on hire and step), `events` (ordered by timestamp and sequence; the page streams these over SSE) and `experiences` (vector index on `embedding`, created in code).

## Measured (from Atlas, `MONGODB_DB=dayone`)

Fresh run of the training hires on the restored `acme-api`, measured with `uv run python tests/acceptance/measure.py`, which reads only Atlas.

| Hire | Machine | Elapsed | Errors | Fixed from memory | LLM calls |
|---|---|---|---|---|---|
| ada | ubuntu | TBD | TBD | TBD | TBD |
| grace | debian | TBD | TBD | TBD | TBD |
| linus | ubuntu | TBD | TBD | TBD | TBD |

From the acceptance database (`dayone_acceptance`) runs today:

- Hire 2 against hire 1 (test 4, both ubuntu): 200.8 s, 4 errors, 0 from memory, 6 LLM calls, against 135.6 s, 4 errors, 4 from exact memory, 0 LLM calls.
- Alpine, never seen before (test 5, 15:20 run): three of barbara's ubuntu fixes were retrieved by vector search (similarity 0.94, 0.94, 0.93). The `apt-get install` for psycopg2 became `apk add --no-cache postgresql-dev python3-dev build-base musl-dev`; the settings copy and db init carried over unchanged.
- `push_change.sh` mid-run (test 6): drift was detected (2955521 → 2143c85) and 8 steps were marked stale and re-verified. The new python-ldap error was fixed by reasoning with `apt-get install -y libldap2-dev libsasl2-dev` after the remembered fix failed. The run ended at 14 passed of 14.

## Acceptance tests

```bash
uv run python -u -m pytest --acceptance tests/acceptance -v -s     # real containers, real Gitea, real Atlas in dayone_acceptance
```

| # | What it asserts | Result |
|---|---|---|
| 1 | Empty Ubuntu container to passing tests; every step done with evidence; event order and run counts agree with the ledger | pass |
| 2 | `kill -9` at five live points, then resume: nothing verified re-runs, pending is checked first, final state equals a clean run | pass |
| 3 | SSH key verified only when the API lists it and `ssh -T` succeeds | pass |
| 4 | Hire 2 hits hire 1's errors and fixes each from exact memory with 0 LLM calls, and is faster | pass |
| 5 | Alpine: an `apt` install retrieved semantically and adapted to `apk`; distro-independent fixes carry over unchanged | pass on the 15:20 run's Atlas data; that pytest run failed on a test bug, fixed in 302cf6b |
| 6 | `push_change.sh` mid-run: drift detected, downstream re-verified, new error fixed, all tests pass | pass |
| 7 | Every number the API serves equals a direct Atlas query | pass |
| 8 | No `.env` value or common key format in any commit; `.env` ignored and never tracked | pass |

## Built today vs pre-recorded

Built today in this repo: the agent (engine, ledger, memory, browser and terminal steps), the page and API, the demo repository `acme-api` and its four snags, the Gitea seed and scripts, the overlay, and the acceptance tests. Third-party: Gitea, Playwright, FastAPI, pymongo, Claude (reasoning and adapting) and Voyage `voyage-3.5` (embeddings via Atlas).

Pre-recorded for the demo: the runs of ada, grace and linus are made before the demo so that memory holds real fixes from today, and the page labels them with when they finished ("finished earlier today at" and the time). Barbara (kill, resume, mid-run change) and ken (Alpine) run live.

Not done: TBD.
