"""Experience memory against real Atlas Vector Search, Voyage and Claude."""

from __future__ import annotations

import hashlib
import re

import pytest
from bson import ObjectId

from dayone.contracts import (
    CheckResult,
    EventType,
    Failure,
    FixProposal,
    FixSource,
    LocatorHint,
    MachineFacts,
    StepKind,
)
from dayone.memory.experience import SEMANTIC_MIN_SIMILARITY, LLMResponseError, guard_commands, parse_fix

from .conftest import wait_until_searchable

STEP = "t_system_package"
UBUNTU = MachineFacts("ubuntu", "22.04", "apt", {"python": "3.12.7", "pip": "24.2"})
DEBIAN = MachineFacts("debian", "12", "apt", {"python": "3.12.7", "pip": "24.2"})
ALPINE = MachineFacts("alpine", "3.20", "apk", {"python": "3.12.8", "pip": "24.0"})

UBUNTU_PG_CONFIG = """Collecting psycopg2==2.9.9 (from -r requirements.txt (line 4))
  Downloading psycopg2-2.9.9.tar.gz (384 kB)
  Preparing metadata (setup.py): started
  Preparing metadata (setup.py): finished with status 'error'
  error: subprocess-exited-with-error

  × python setup.py egg_info did not run successfully.
  │ exit code: 1
  ╰─> [23 lines of output]
      running egg_info
      creating /tmp/pip-pip-egg-info-k2x9f1q3/psycopg2.egg-info
      writing /tmp/pip-pip-egg-info-k2x9f1q3/psycopg2.egg-info/PKG-INFO

      Error: pg_config executable not found.

      pg_config is required to build psycopg2 from source.  Please add the directory
      containing pg_config to the $PATH or specify the full executable path with the
      option:

          python setup.py build_ext --pg-config /path/to/pg_config build ...

      or with the pg_config option in 'setup.cfg'.

      If you prefer to avoid building psycopg2 from source, please install the PyPI
      'psycopg2-binary' package instead.
      [end of output]

  note: This error originates from a subprocess, and is likely not a problem with pip.
error: metadata-generation-failed
"""

ALPINE_PG_CONFIG = """Collecting psycopg2==2.9.9
  Using cached psycopg2-2.9.9.tar.gz (384 kB)
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Getting requirements to build wheel: started
  Getting requirements to build wheel: finished with status 'error'
  error: subprocess-exited-with-error

  × Getting requirements to build wheel did not run successfully.
  │ exit code: 1
  ╰─> [21 lines of output]
      /root/acme-api/.venv/lib/python3.12/site-packages/setuptools/dist.py:330: SetuptoolsDeprecationWarning
      running egg_info
      writing psycopg2.egg-info/PKG-INFO

      Error: pg_config executable not found.

      pg_config is required to build psycopg2 from source.  Please add the directory
      containing pg_config to the $PATH or specify the full executable path with the
      option:

          python setup.py build_ext --pg-config /path/to/pg_config build ...

      If you prefer to avoid building psycopg2 from source, please install the PyPI
      'psycopg2-binary' package instead.
      [end of output]

  note: This error originates from a subprocess, and is likely not a problem with pip.
error: subprocess-exited-with-error
"""

PASSED = CheckResult(ok=True, evidence="pip wheel psycopg2==2.9.9: built psycopg2-2.9.9-cp312-linux_x86_64.whl")


def failure(hire: str, facts: MachineFacts, error_text: str) -> Failure:
    # Stand-in for session 3's fingerprint(): distinct texts get distinct fingerprints, which is all these tests need.
    return Failure(
        hire=hire,
        step_id=STEP,
        kind=StepKind.TERMINAL,
        error_text=error_text,
        fingerprint=hashlib.sha256(error_text.encode()).hexdigest()[:16],
        facts=facts,
    )


def runs(pattern: str, proposal: FixProposal) -> bool:
    return any(re.search(pattern, command) for command in proposal.commands)


def apt_fix_passes(calls: list[FixProposal]):
    """Passes only when an apt command installs the libpq headers, as the real Ubuntu check would."""

    def try_fix(proposal: FixProposal) -> CheckResult:
        calls.append(proposal)
        if runs(r"^(DEBIAN_FRONTEND=noninteractive )?apt(-get)? install .*libpq-dev", proposal):
            return PASSED
        return CheckResult(ok=False, evidence="pip wheel failed", error_text=UBUNTU_PG_CONFIG)

    return try_fix


def save_ada_ubuntu_fix(memory, calls: list[FixProposal]):
    resolution = memory.resolve(failure("ada", UBUNTU, UBUNTU_PG_CONFIG), apt_fix_passes(calls))
    assert resolution.fixed, resolution
    assert resolution.proposal.source == FixSource.REASONING
    return resolution


def test_same_error_twice_second_is_exact_memory_with_zero_llm_calls(harness):
    memory, llm, events, _, experiences = harness
    calls: list[FixProposal] = []
    first = save_ada_ubuntu_fix(memory, calls)
    assert first.llm_calls == len(llm.prompts) >= 1

    prompts_before = len(llm.prompts)
    calls.clear()
    second = memory.resolve(failure("linus", UBUNTU, UBUNTU_PG_CONFIG), apt_fix_passes(calls))

    assert second.fixed
    assert second.proposal.source == FixSource.EXACT_MEMORY
    assert second.llm_calls == 0 and len(llm.prompts) == prompts_before, "exact memory must not call the LLM"
    assert second.attempts == 1
    assert second.proposal.source_hire == "ada"
    assert second.proposal.experience_id == first.proposal.experience_id
    saved = experiences.find_one({"outcome": "verified"})
    assert calls == [second.proposal] and list(second.proposal.commands) == saved["fix_commands"]
    assert saved["uses"] == 2 and saved["hires"] == ["ada", "linus"]
    assert saved["verified_by_check"] == PASSED.evidence
    assert saved["context"]["package_manager"] == "apt" and len(saved["embedding"]) == 1024
    hit = events.of(EventType.MEMORY_HIT)[-1]
    assert hit["source"] == FixSource.EXACT_MEMORY and hit["source_hire"] == "ada"


def test_ubuntu_apt_fix_is_found_semantically_and_adapted_to_apk_on_alpine(harness):
    memory, llm, events, _, experiences = harness
    ada = save_ada_ubuntu_fix(memory, [])
    wait_until_searchable(experiences, 1)

    calls: list[FixProposal] = []

    def alpine_check(proposal: FixProposal) -> CheckResult:
        calls.append(proposal)
        installs = runs(r"^apk add .*(postgresql\d*-dev|libpq-dev)", proposal)
        if installs and not runs(r"\bapt(-get)?\b", proposal):
            return PASSED
        return CheckResult(ok=False, evidence="pip wheel failed", error_text=ALPINE_PG_CONFIG)

    prompts_before = len(llm.prompts)
    resolution = memory.resolve(failure("ken", ALPINE, ALPINE_PG_CONFIG), alpine_check)

    assert resolution.fixed, resolution
    proposal = resolution.proposal
    print(f"similarity {proposal.similarity:.3f}, from {proposal.source_hire}, commands {proposal.commands}")
    assert proposal.source == FixSource.SEMANTIC_MEMORY
    assert proposal.adapted_from == "apt"
    assert proposal.source_hire == "ada"
    assert proposal.similarity >= SEMANTIC_MIN_SIMILARITY
    assert resolution.attempts == 1 and [call.commands for call in calls] == [proposal.commands]
    assert calls[0].experience_id == ada.proposal.experience_id, "the tried proposal names ada's experience"
    assert proposal.experience_id != ada.proposal.experience_id, "the result names the new apk experience"
    assert resolution.llm_calls == 1 == len(llm.prompts) - prompts_before, "one adaptation call, no reasoning"
    assert "apt" in llm.prompts[-1] and "apk" in llm.prompts[-1]

    adapted = experiences.find_one({"_id": ObjectId(proposal.experience_id)})
    assert adapted["context"]["package_manager"] == "apk" and adapted["adapted_from"] == "apt"
    assert adapted["fix_commands"] == list(proposal.commands) and adapted["hire"] == "ken"
    hit = events.of(EventType.MEMORY_HIT)[-1]
    assert hit["similarity"] == proposal.similarity and hit["adapted_from"] == "apt"
    assert not events.of(EventType.REASONING_STARTED)[1:], "only ada's first resolve may reason"


def test_reasoning_stops_after_three_attempts_and_feeds_back_each_new_error(harness):
    memory, llm, events, _, experiences = harness
    new_errors = [f"E: Unable to locate package attempt-{n}-missing-headers" for n in range(1, 4)]
    calls: list[FixProposal] = []

    def always_fails(proposal: FixProposal) -> CheckResult:
        calls.append(proposal)
        return CheckResult(ok=False, evidence="pip wheel failed", error_text=new_errors[len(calls) - 1])

    resolution = memory.resolve(failure("ada", UBUNTU, UBUNTU_PG_CONFIG), always_fails)

    assert not resolution.fixed and resolution.proposal is None
    assert resolution.attempts == len(calls) == 3
    assert resolution.llm_calls == len(llm.prompts) == 3
    assert resolution.last_check.error_text == new_errors[2]
    assert new_errors[0] in llm.prompts[1] and new_errors[1] in llm.prompts[2]
    assert all(command in llm.prompts[1] for command in calls[0].commands), "prior commands are fed back"
    assert len(events.of(EventType.MEMORY_MISS)) == 1 and len(events.of(EventType.REASONING_STARTED)) == 1
    assert [attempt["ok"] for attempt in events.of(EventType.FIX_ATTEMPT)] == [False] * 3
    assert experiences.count_documents({"outcome": "failed"}) == 3
    assert experiences.count_documents({"outcome": "verified"}) == 0


RUNTIME_ERROR = """Processing /root/acme-api
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
ERROR: Package 'acme-api' requires a different Python: 3.10.12 not in '>=3.12'
"""


def test_semantic_retrieval_never_crosses_steps(harness):
    memory, llm, events, _, experiences = harness
    runtime = Failure(
        hire="ada",
        step_id="t_runtime",
        kind=StepKind.TERMINAL,
        error_text=RUNTIME_ERROR,
        fingerprint="runtime-python",
        facts=UBUNTU,
    )
    assert memory.resolve(runtime, lambda proposal: PASSED).fixed
    wait_until_searchable(experiences, 1)

    calls: list[FixProposal] = []
    resolution = memory.resolve(failure("grace", UBUNTU, UBUNTU_PG_CONFIG), apt_fix_passes(calls))

    assert resolution.fixed and resolution.proposal.source == FixSource.REASONING
    assert calls[0].source == FixSource.REASONING, "ada's t_runtime fix must not be tried for t_system_package"
    assert not [hit for hit in events.of(EventType.MEMORY_HIT)]
    assert events.of(EventType.REASONING_STARTED)[-1]["hints"] == [], "other steps are not even hints"
    assert "3.10.12" not in llm.prompts[-1]


def test_saved_fix_is_the_reasoning_chain_without_a_failed_memory_proposal(harness):
    memory, _, _, _, experiences = harness
    ada = save_ada_ubuntu_fix(memory, [])
    wrong = ["rm -rf .venv", "python3.12 -m venv .venv"]  # what memory wrongly held in ada's integrated run
    experiences.update_one({"_id": ObjectId(ada.proposal.experience_id)}, {"$set": {"fix_commands": wrong}})
    wait_until_searchable(experiences, 1)

    calls: list[FixProposal] = []
    apt_fix = apt_fix_passes([])

    def check(proposal: FixProposal) -> CheckResult:
        calls.append(proposal)
        if proposal.source == FixSource.SEMANTIC_MEMORY:
            return CheckResult(ok=False, evidence="pip wheel failed", error_text=UBUNTU_PG_CONFIG)
        return apt_fix(proposal)

    debian_error = UBUNTU_PG_CONFIG.replace("k2x9f1q3", "p0w8z7m2")
    resolution = memory.resolve(failure("grace", DEBIAN, debian_error), check)

    assert resolution.fixed and calls[0].source == FixSource.SEMANTIC_MEMORY
    assert list(calls[0].commands) == wrong
    saved = experiences.find_one({"_id": ObjectId(resolution.proposal.experience_id)})
    reasoning_fixes = [command for call in calls[1:] for command in call.commands if not command.endswith("|| true")]
    print("saved", saved["fix_commands"])
    assert saved["fix_commands"] == reasoning_fixes
    assert not set(wrong) & set(saved["fix_commands"]), "the failed memory proposal is not part of the fix"


def test_close_loop_opens_one_issue_per_repeated_fix_and_is_idempotent(harness):
    memory, _, events, gitea, experiences = harness
    save_ada_ubuntu_fix(memory, [])
    assert memory.close_loop() == [], "a fix used by one hire is not repeated yet"

    memory.resolve(failure("linus", UBUNTU, UBUNTU_PG_CONFIG), apt_fix_passes([]))
    wait_until_searchable(experiences, 1)
    debian_error = UBUNTU_PG_CONFIG.replace("k2x9f1q3", "p0w8z7m2")  # new tmp dir: new fingerprint, same apt fix
    debian = memory.resolve(failure("barbara", DEBIAN, debian_error), apt_fix_passes([]))
    assert debian.proposal.source == FixSource.SEMANTIC_MEMORY and debian.proposal.adapted_from is None
    assert debian.llm_calls == 0

    first = memory.close_loop()
    second = memory.close_loop()

    assert len(first) == 1 and second == first
    assert len(gitea.issues) == 1 and len(events.of(EventType.README_ISSUE_OPENED)) == 1
    [issue] = gitea.issues.values()
    print(issue["title"], issue["body"], sep="\n")
    assert issue["title"].startswith(f"README: {STEP} on ubuntu needs `")
    assert "ada, linus, barbara" in issue["body"] and "libpq-dev" in issue["body"] and PASSED.evidence in issue["body"]


def test_locator_is_only_ever_returned_for_its_own_goal(harness):
    memory, _, _, _, experiences = harness
    summary = "Gitea sign-in page: Username or Email Address input, Password input, Remember This Device checkbox"
    username = "sign-in page: username field"
    memory.save_locator(LocatorHint(username, "#user_name", (10.0, 20.0, 300.0, 32.0), summary, None, None), "ada")
    memory.save_locator(LocatorHint(username, "#user_name", (10.0, 20.0, 300.0, 32.0), summary, None, None), "grace")

    found = memory.find_locator(username, summary)
    assert found.locator == "#user_name" and found.source_hire == "ada" and found.similarity is None
    saved = experiences.find_one({"kind": "browser"})
    assert saved["uses"] == 2 and saved["hires"] == ["ada", "grace"]

    # The integrated ada run crashed here: a semantic hit gave the checkbox goal the username input.
    assert memory.find_locator("sign-in page: remember this device checkbox", summary) is None

    memory.save_locator(LocatorHint(username, "#login-name", None, "redesigned " + summary, None, None), "linus")
    assert memory.find_locator(username, summary).locator == "#login-name", "the newest working locator wins"


def test_guard_makes_model_commands_runnable_unattended():
    assert guard_commands(["sudo apt-get install libpq-dev gcc\napk add --no-cache gcc", "  # note", ""]) == (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y libpq-dev gcc",
        "apk add --no-cache gcc",
    )
    assert guard_commands(["apt-get install --yes gcc"]) == ("DEBIAN_FRONTEND=noninteractive apt-get install --yes gcc",)
    inspect, commands, rationale = parse_fix(
        '```json\n{"inspect": ["grep -rn init ."], "commands": ["apt-get update"], "rationale": "refresh"}\n```'
    )
    assert inspect == ("grep -rn init . || true",), "a grep with no match must not abort the attempt"
    assert commands == ("apt-get update",) and rationale == "refresh"
    for reply in (
        "no json here",
        '{"commands": "apt-get update"}',
        '{"commands": ["  "]}',
        '{"inspect": ["ls"]}',
        '{"inspect": "ls", "commands": ["true"]}',
    ):
        with pytest.raises(LLMResponseError):
            parse_fix(reply)
