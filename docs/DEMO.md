# Demo: what we build toward

Every session builds for this run-of-show. If a feature does not appear here or in an acceptance test, it is not needed. Agent 7 (QA) owns this file after Phase 0 and keeps the timings real.

## Cast

| Order | Hire | Machine | Role in the demo |
|---|---|---|---|
| 1 | `ada` | ubuntu | First hire. Hits all four snags and solves them by reasoning: 7 LLM calls, 2m 55s. Run before the demo; the page labels it "finished earlier today at …". |
| 2 | `grace` | debian | Training hire. All four fixes found by vector search from ada (similarity 0.92 to 0.95) and adapted, one LLM call each, no reasoning. 3m 54s. Run before the demo. |
| 3 | `linus` | ubuntu | Run before the demo. All four fixes by exact match from ada, 0 LLM calls, 2m 36s. That second use of each fix opened README issues #10 to #13. |
| 4 | `barbara` | ubuntu | **Live.** Fixes from memory, killed with `kill -9` mid-run, resumed, then `push_change.sh` lands mid-run. |
| 5 | `ken` | alpine | **Live (or pre-run and labelled).** Unseen machine: the apt fix is retrieved by vector search and adapted to apk. |

Hires 1 to 3 were run fresh at 15:15 on `acme-api` c9d8d7f, so the memory in Atlas is real and from today.

## Screen layout

- Left two thirds: the headed Chromium window the agent drives, with `intern_overlay.js` rings (green = acting from memory, amber = reasoning, red "This moved." = a saved locator failed).
- Right third: the Preflight page (`http://localhost:8000`). It shows the step list with status and one evidence line each, the streaming terminal below it, and the comparison lines computed from Atlas, e.g. "ada, hire 1 (ubuntu): 2m 55s, 4 errors, 0 from memory, 7 LLM calls, finished earlier today at …" and "linus (ubuntu): 2m 36s, 4 errors, 4 from memory, 0 LLM calls".
- A terminal tab for `scripts/move_ui.sh`, `kill -9` and `scripts/push_change.sh`.

## What to say about time

Lead with LLM calls, not minutes: 7 → 4 → 0. Same-OS wall time only drops 2m 55s → 2m 36s. That is because installing Python 3.12 and compiling psycopg2 take about 75 s, and a remembered fix still has to do both. Say so if asked; never claim a big speed-up.

## Run-of-show (3 minutes)

`scripts/move_ui.sh` restarts Gitea, so it runs only before Start (never once the agent is past `b_sign_in`: a clone or `ssh -T` during the restart fails). `scripts/restore_ui.sh` undoes it; `scripts/reset.sh` calls it.

`push_change.sh` must land after "acme-api cloned" is verified and before "Tests pass" starts, or there is no drift to see.

| Time | What the audience sees (real strings) | Track 3 question | If it goes wrong |
|---|---|---|---|
| 0:00 | One sentence: every new hire hits the same broken setup steps and the fix is lost. Preflight fixes that. Point at ada's line: 4 errors, all solved by reasoning, 7 LLM calls. | | |
| 0:10 | In the terminal tab: `scripts/move_ui.sh` ("we just changed Gitea's SSH-key form"). Gitea restarts for 2 to 3 s; wait for it to return before Start. | Q3 | If Gitea does not answer within 10 s, skip the UI move and say "the form change is shown in the recording". |
| 0:15 | Type `barbara`, pick ubuntu, Start. Chromium signs in with a green ring (locator from memory). At the key form the saved locator fails: red "This moved.", then amber while it reasons over the page's accessibility text, finds the new fields, adds the key and saves the new locator. | Q3, Q1 | If reasoning takes all 3 attempts, keep talking about why: "it is reading the page, not guessing a selector". |
| 0:35 | "SSH key added in browser" turns verified only after two independent checks: "API lists key SHA256:…; ssh -T: authenticated as barbara". "acme-api cloned": "local HEAD c9d8d7f = API HEAD c9d8d7f". | Q1 | |
| 0:45 | "Python runtime" fails: "/root/acme-api/.venv/bin/python is Python 3.10.12, but /root/acme-api/pyproject.toml requires-python = '>=3.12'". Instantly: "exact match from ada, applied N×". No LLM call. The same follows for system packages, settings and database. | memory | |
| 1:05 | In the terminal tab: `kill -9` the agent mid-step. Page shows the run stopped with one step `pending`. | Q2 | |
| 1:15 | `dayone resume` (or Resume on the page). Notice: "Resumed: every finished step is re-checked before anything runs." Finished steps show "skipped, not re-run". The pending step is checked first, not blindly re-run. | Q2 | The resume waits up to 11 s for the dead agent's lease: say "it is waiting out the lock the dead process held". |
| 1:35 | `scripts/push_change.sh` adds python-ldap, which needs another system package. Notice: "Remote HEAD moved c9d8d7f… → ….". "acme-api cloned" and every later step turn stale and are re-verified. The new build error goes to memory, then to reasoning (amber), which installs `libldap2-dev libsasl2-dev` and saves the experience. | Q3 | Fallback: say "the remembered fix did not cover the new package, so it reasons", which is what test 6 showed. |
| 2:10 | "Tests pass": "14 passed of 14, exit 0 at …". "Welcome checklist ticked": "API issue body: 6/6 items ticked". | | |
| 2:20 | Switch to `ken` on alpine. The psycopg2 error is retrieved by vector search: "similar error from barbara, similarity 0.94, adapted from ubuntu 22.04, apt: apk add --no-cache postgresql-dev python3-dev build-base musl-dev". | memory, Q3 | If not run live, it is labelled as recorded. |
| 2:40 | Gitea: the README issues Preflight opened on `acme-api` (#10 to #13), one per snag applied for two or more hires. | closing the loop | These four predate the rename and say "Opened by Day One": "that was its working name"; new issues say Preflight. |
| 2:50 | One sentence: state in Atlas, verified by independent checks, memory by vector search. | | |

## Snags in `acme-api` (what the agent must discover; the README mentions none)

| Snag | Fails as (verbatim, from fresh containers) | Ubuntu 22.04 fix | Debian 12 | Alpine 3.20 |
|---|---|---|---|---|
| Runtime: `requires-python = ">=3.12"` in `pyproject.toml` (`requirements.txt` is `-e .`) | `ERROR: Package 'acme-api' requires a different Python: 3.10.12 not in '>=3.12'`. Ubuntu's stock pip 22.0.2 reports this only once `pg_config` is present; without it the first error is psycopg2's (next row). | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, `uv python install 3.12`, `uv venv --seed --python 3.12 .venv` | 3.11.2 with pip 23.0.1: `... requires a different Python: 3.11.2 not in '>=3.12'` (reported first); same fix | 3.12.13 ships: no failure |
| System package: `psycopg2==2.9.10` (source only) | `Error: pg_config executable not found.`; with `libpq-dev` but no compiler: `error: [Errno 2] No such file or directory: 'cc'` | `apt-get install -y libpq-dev gcc` (with the uv Python no `python3-dev` is needed) | same as Ubuntu (not run on Debian) | `Error: pg_config executable not found.`; with `postgresql-dev` alone: `fatal error: Python.h: No such file or directory`. Fix: `apk add postgresql-dev gcc musl-dev python3-dev` |
| Settings file | `acme_api.settings.SettingsError: settings.toml not found at /root/acme-api/settings.toml (copy settings.example.toml to settings.toml)` (10 errors, 3 passed) | `cp settings.example.toml settings.toml` | same | same |
| Database init | `sqlite3.OperationalError: no such table: users` (10 errors, 3 passed) | `python -m acme_api.db init` (prints `created schema for sqlite:///acme.db`), then `13 passed` | same | same |
| Mid-run change (`scripts/push_change.sh`): adds `python-ldap==3.4.8` (sdist only on PyPI) and `tests/test_directory.py` | `Modules/common.h:15:10: fatal error: lber.h: No such file or directory` | `apt-get install -y libldap2-dev libsasl2-dev`, then `14 passed` | same as Ubuntu (not run on Debian) | `apk add openldap-dev`, then `14 passed` |

Proven in fresh `ubuntu:22.04` and `alpine:3.20` containers (Debian 12 for the runtime row only). Test count: 13 at seed, 14 after `push_change.sh`. pytest shows one harmless starlette deprecation warning.

Exact names and versions are the infra session's call; this table is updated to match what really fails.

## What must never be faked

Every status shown has a check that ran. Every number comes from an Atlas query (`dayone/api` computes them; QA test 7 compares). Pre-recorded runs are labelled on screen.
