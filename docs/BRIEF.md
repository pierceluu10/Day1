# Day One: master brief (MongoDB hackathon, Track 3)

This is the human's brief, kept verbatim in substance. Every session reads it first. `docs/DEMO.md` is the demo we build toward; `STATUS.md` holds ownership and progress; `dayone/contracts.py` is the shared interface.

Hard stop: feature freeze 16:00 local, submission 17:00.

## 0. Non-negotiables

- Track 3 "Finish the Job" is the product. Track statement: "Build an agent that completes a long multi-step task across several systems, using MongoDB Atlas to persist verified task state and Vector Search to retrieve relevant past experience, so it survives crashes and system changes and knows what actually happened." Every feature must serve one of its three questions:
  1. How does it tell an action that took effect from one that was only sent?
  2. How does it resume after a crash without replaying its whole history?
  3. How does it adapt when a system's layout, fields or workflow changes underneath it?
  Refuse any work that serves none of them.
- Judging: Technical demo 35% (working, well-engineered), Implementation difficulty 30% (depth beyond a few prompts), Impact 20%, Creativity 15%. A working narrow build beats a broad broken one.
- Rules: all code is written today in this repo (third-party libraries are fine). The repo will be public: never commit secrets; `.env` is git-ignored. No Streamlit. No project where a dashboard is the main feature: no charts, gauges or metric tiles. Must use the hackathon's MongoDB Atlas cluster via `MONGODB_URI`.
- Honesty: never report a step as done unless its check ran and passed. Every number shown is computed from Atlas. Anything pre-recorded is labelled on screen.

## 1. What Day One is

New hires lose their first days going back and forth with an AI chat to set up their machine. Each one hits the same broken steps, solves them alone, and the fix is lost.

Day One is an agent the new hire starts with one command. It sets them up end to end, in the browser (account and access on the company's Git service) and in the terminal (their machine), until the team's tests pass. It confirms every step really took effect, resumes after a kill or restart with nothing done twice, adapts when the repository changes mid-setup, and saves every error-and-fix so the next hire never hits it. Hire five is set up in a fraction of the time of hire one, including on a machine type it has never seen.

## 2. Systems (all local, real, no mocks of the systems the agent acts on)

- Git service: Gitea in Docker, pinned to `gitea/gitea:1.24.7`, admin user created by CLI, no install wizard. Organization `acme`, team `engineering`, repository `acme/acme-api`, five hire accounts, one "Welcome" issue per hire with a task-list checklist.
- The new hire's laptop: a fresh Docker container per hire on the same Docker network as Gitea. Images: `ubuntu:22.04` and `debian:12` for training hires, `alpine:3.20` held out as the unseen machine. Containers start with almost nothing installed.
- MongoDB Atlas: task state, experience memory, vector index.
- Demo repository `acme-api`: a small FastAPI service with a pytest suite, written today, with four realistic setup snags the README does not mention: a dependency that needs a system package, a required language/runtime version, a settings file that must be copied from an example, and a database that must be initialised before tests pass. The project README states this repo is a demo built to mirror common onboarding problems.

## 3. The task: an ordered checklist of steps, each with a check

Browser steps (Playwright, Python, driven directly, headed): sign in as the hire; add the laptop's SSH public key in account settings; open the Welcome issue; tick checklist items as steps complete. Terminal steps (`docker exec` into the hire's container, with timeouts): generate an SSH key; install base tools; clone over SSH; install the system package; install dependencies; create the settings file; initialise the database; run tests.

Every step is `{id, kind, check(), run(), evidence}` and follows these rules:

- Reconcile, do not replay (Kubernetes controllers, Ansible, Terraform): run `check()` first. If it passes, mark verified and skip. This is the resume mechanism.
- Intent before effect (Temporal, DBOS, write-ahead logs): write the step as `pending` in Atlas before `run()`. On restart, a `pending` step is checked, never blindly re-run.
- Independent verification: the check uses a different route from the action. The SSH key is added in the browser and confirmed by the Gitea API listing it and by `ssh -T` from the container. The clone is confirmed by comparing local HEAD with the API's HEAD. Tests are confirmed by exit code and passed count.
- Idempotent writes (Stripe idempotency keys): any action that is not naturally repeatable carries a deterministic key derived from (hire, step).
- Evidence: store the check's real output (version string, HTTP status, test count) on the step.
- Fail before pass: a check that never failed for this hire is flagged untrusted.

## 4. Experience memory

When a step fails after `run()`:

1. Capture the error text and context (step id, OS family, package manager, tool versions).
2. Fingerprint it (Sentry-style normalisation: strip paths, versions, timestamps).
3. Look up an exact fingerprint match. On a miss, run Atlas `$vectorSearch` over past experiences filtered to `outcome: "verified"`, embedding = error text + context summary.
4. Try the retrieved fix. If none or it fails, ask the LLM to reason, with the nearest experiences as hints, bounded to 3 attempts.
5. When the check passes, save `{fingerprint, error_text, context, fix_commands, outcome, verified_by_check, hire, uses, embedding}`.

Browser experience works the same way: for each goal on a page, save the working locator, the element's bounding box and a short text summary of the page; try it first; on failure reason over the page's accessibility text, then save the new one.

Semantic retrieval must do visible work: a fix learned with `apt` on Ubuntu must be found and adapted for `apk` on Alpine. Show the similarity score and which past hire it came from.

Adapting to change mid-run: `scripts/push_change.sh` commits a change to `acme-api` while a hire is being set up (a new dependency needing another system package, or a changed setup step). The agent must detect that remote HEAD moved or that a previously verified check no longer passes, re-verify downstream steps, and adapt. A human triggers this live.

Closing the loop: when the same fix has been applied for two or more hires, the agent opens an issue on `acme-api` proposing the README change. No extra UI.

## 5. Interface

- `dayone start --hire NAME --machine ubuntu|debian|alpine`, `dayone resume`, `dayone reset`.
- One local page served by the backend: name field, machine choice, Start. Main area: the live run as a plain list of steps with status (pending, verified, skipped, fixed-from-memory, fixed-by-reasoning) and one line of evidence each, plus the streaming terminal. No charts. A small "hire 1 vs this hire" line: elapsed time, errors hit, fixes from memory, computed from Atlas.
- The browser window the agent drives is the visual star. Inject `intern_overlay.js` (repo root) with `add_init_script` and `bypass_csp=True`: green ring when acting from memory, amber when reasoning, red "This moved." when a saved locator fails.

## 6. Stack

Python 3.11+, FastAPI with Server-Sent Events, Playwright (Python, direct, not via MCP), pymongo, Docker via subprocess, plain HTML/JS for the page. LLM and embedding providers behind thin wrappers; all model names, keys and timeouts from environment variables, none hardcoded. Explicit embeddings; the Atlas vector index is created in code. Lease-style fields are checked in a normal query, not in the vector filter. Do not depend on transactions or change streams (both verified available on this M10, but unused).

Collections: `hires`, `steps` (unique on hire + step id), `experiences` (vector index on `embedding`, filter on `outcome` and `kind`), `events`.

## 8. Checkpoints (local time) and cut order

- 13:15: one terminal step and one browser step run, are checked, and are in Atlas.
- 14:30: full onboarding on Ubuntu passes end to end; kill and resume works with zero repeated actions. If not, all other work stops until it does.
- 15:15: hire 2 is measurably faster than hire 1 through retrieved fixes; the mid-run commit is handled.
- 15:45: Alpine as the unseen machine; overlay; repeated-fix issue.
- 16:00: freeze. Only bug fixes, rehearsal support and README after this.

Cut in this order when behind: repeated-fix issue, overlay polish, Alpine, ticking the Welcome checklist, the mid-run commit (replace with re-running after a manual change and say so). Never cut: checks, the ledger, resume.

## 9. Acceptance tests

1. Empty Ubuntu container to passing tests with every step verified and evidence stored.
2. `kill -9` at five different points, then resume: no step's `run()` executes twice once verified; a pending step is checked before any re-run; final state identical.
3. The SSH key step is only verified when both the API lists the key and `ssh -T` succeeds.
4. Hire 2 on a fresh container hits the same errors and resolves them from memory with zero LLM fix calls for those errors.
5. Alpine: at least one fix is retrieved semantically from an `apt` experience and adapted.
6. `push_change.sh` mid-run: the agent notices, re-verifies, adapts, finishes.
7. All displayed numbers match a direct Atlas query.
8. `git grep` finds no secrets; `.env` is ignored.

## Every session reports

What it built, the exact commands it ran to verify and their output, and what remains unverified. "Should work" is not accepted.
