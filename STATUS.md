# STATUS

Integration branch: `claude/day-one-mongodb-agent-6cc839` (orchestrator session). Each build session works on its own branch in its own worktree and reports when a piece is verified; the orchestrator merges it here and runs the whole flow. Nothing merges to `main` until the human says so.

## Ownership (only touch your paths; ask the orchestrator for anything else)

| # | Session | Owns | Done when |
|---|---|---|---|
| 0 | Orchestrator | `dayone/contracts.py`, `dayone/config.py`, `pyproject.toml`, `intern_overlay.js`, `STATUS.md`, `docs/BRIEF.md`, `.env.example`, `.gitignore` | Integration green at each checkpoint |
| 1 | Infra and seed | `infra/`, `scripts/`, `demo_repo/`, `dayone/gitea.py` (implements `Gitea`) | One command takes a clean machine to a seeded Gitea; `scripts/reset.sh` restores it |
| 2 | Core engine | `dayone/engine/`, `dayone/store/` (implements `Store`, `EventBus`) | A fake two-step task survives `kill -9` between run and check with nothing repeated |
| 3 | Terminal | `dayone/terminal/` (implements `Executor`, `fingerprint()`), `dayone/steps/terminal_*.py` | All terminal steps pass on Ubuntu from an empty container |
| 4 | Browser | `dayone/browser/` (implements `BrowserSession`), `dayone/steps/browser_*.py` | The SSH key added in the browser lets the container clone |
| 5 | Memory | `dayone/memory/` (implements `Memory`), `dayone/llm/` (implements `LLM`, `Embedder`) | A fix saved from Ubuntu is retrieved and adapted for the equivalent Alpine error |
| 6 | Launcher, API, page | `dayone/api/`, `dayone/cli.py`, `web/` | Start on the page launches a real run and steps update live |
| 7 | QA, measurement, demo | `tests/` (except `tests/test_overlay.py`), `docs/DEMO.md`, `README.md` | Acceptance tests 1 to 8 automated; real numbers for hires 1 to 4; 3-minute run-of-show |

`dayone/steps/__init__.py` (the ordered `STEPS` list built from `STEP_IDS`) is written by the orchestrator at integration from sessions 3 and 4's step modules.

## Concrete classes the engine wires together (names and constructors are fixed)

| Contract | Class | Constructor |
|---|---|---|
| `Store` | `dayone.store.atlas.AtlasStore` | `AtlasStore(settings)`; also exposes `.db` (pymongo Database) |
| `EventBus` | `dayone.store.events.AtlasEventBus` | `AtlasEventBus(db, hire: Hire)` |
| `Gitea` | `dayone.gitea.GiteaClient` | `GiteaClient(settings)` (admin token from `.dayone/gitea_admin_token`) |
| `Executor` | `dayone.terminal.docker.DockerExecutor` | `DockerExecutor(hire: Hire, events: EventBus, timeout_s: float)` |
| fingerprint | `dayone.terminal.fingerprint.fingerprint` | `fingerprint(error_text: str) -> str` |
| `BrowserSession` | `dayone.browser.session.PlaywrightSession` | `PlaywrightSession(hire: Hire, settings, events: EventBus, memory: Memory)` (Chromium starts lazily on first browser step) |
| `Memory` | `dayone.memory.experience.ExperienceMemory` | `ExperienceMemory(db, llm: LLM, embedder: Embedder, events: EventBus, gitea: Gitea, settings)` |
| `LLM` / `Embedder` | `dayone.llm.anthropic.AnthropicLLM` / `dayone.llm.voyage.VoyageEmbedder` | `(settings)` |
| steps | `dayone.steps.terminal_steps.TERMINAL_STEPS`, `dayone.steps.browser_steps.BROWSER_STEPS` | `dict[str, Step]` keyed by step id |
| engine | `dayone.engine.runner.run_hire` | `run_hire(name: str, machine: Machine \| None, resume: bool) -> bool`; `dayone.engine.runner.reset(name: str \| None)` |

## Shared resources (do not trample each other)

| Resource | Rule |
|---|---|
| Gitea `http://localhost:3000`, SSH `localhost:2222`, container `gitea` on network `dayone-net` | Session 1 creates and seeds it. Others never reset it; ask session 1 (or the orchestrator) for a reset. |
| Gitea hires `ada grace linus barbara ken` | Dev testing: session 3 uses `grace`, session 4 uses `linus`. `ada`, `barbara`, `ken` are kept clean for integration and the demo. |
| Hire containers `dayone-<hire>` | Only for the hire you own in dev. |
| Atlas database | Each session sets `MONGODB_DB=dayone_dev_<session number>` in its own `.env` copy. `dayone` is integration only. |
| Ports | 3000 and 2222 Gitea, 8000 the page/API. |
| `.env` | Copy from the orchestrator worktree: `cp /Users/pierceluu/mongohackathon/.claude/worktrees/day-one-mongodb-agent-6cc839/.env .` Never commit it. |

## Verified (Phase 0, 12:10 to 12:35)

- Atlas M10 (9.0.7): connect, write, transactions, change streams; vector index queryable ~50 s after creation; filter on `outcome` + `kind` works.
- An Alpine `pg_config not found` query returns the Ubuntu apt fix first (score 0.847 vs 0.73).
- Voyage `voyage-3.5` at `https://ai.mongodb.com/v1` (Atlas key; `api.voyageai.com` returns 403). 1024 dims.
- `claude-sonnet-5-5` answers in ~1.3 s.
- `gitea/gitea:1.24.7`: boots with `INSTALL_LOCK=true`, admin by CLI, API answers.
- Images pulled: `gitea/gitea:1.24`, `ubuntu:22.04`, `debian:12`, `alpine:3.20`.
- Playwright Chromium headed; `add_init_script` runs after navigation (not on `set_content`); `intern_overlay.js` passes `tests/test_overlay.py` under a `default-src 'none'` CSP.

## In progress / blocked / next

Updated 13:25.

All seven branches merged into integration and their suites pass there (engine, memory, overlay, infra, API, terminal fast tests; browser in-session).

**Ada (hire 1, ubuntu) green end to end at 13:23**: 13/13 steps done in Atlas `dayone`. Four snags fixed by reasoning (runtime, system package, settings, db init; 5 LLM calls). Crash (13:17, failed step) then `dayone resume`: 10 done steps reconciled in 15 s with run_count unchanged, failed step retried and fixed. SSH key verified by API + `ssh -T`; clone `8094472 = 8094472`; Welcome checklist 6/6 via API.

Open:
- Memory (5): saved `t_init_db` fix kept the failed `make migrate`; blocks hire 2 zero-LLM replay.
- Terminal (3): `t_run_tests` check runs pytest itself, so it is always `skipped`/untrusted; moving to a HEAD-keyed junit report written by run().
- Page (6): testing Start with a real grace run.

Next: grace (debian, hire 2) from memory; kill -9 harness (QA); push_change live; ken on alpine; measure.
