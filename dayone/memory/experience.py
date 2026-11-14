"""Experience memory on Atlas: exact fingerprint, then $vectorSearch (adapting apt -> apk), then bounded LLM reasoning."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.operations import SearchIndexModel

from dayone.config import Settings
from dayone.contracts import (
    LLM,
    REPO,
    REPO_DIR,
    CheckResult,
    Embedder,
    EventBus,
    EventType,
    Failure,
    FixProposal,
    FixSource,
    Gitea,
    LocatorHint,
    MachineFacts,
    Resolution,
    StepKind,
)

EXPERIENCES = "experiences"
VECTOR_INDEX = "experience_embedding"
VERIFIED = "verified"
FAILED = "failed"
DOCUMENT = "document"
QUERY = "query"

INDEX_POLL_S = 5.0
INDEX_WAIT_S = 300.0
NUM_CANDIDATES = 100
HINT_COUNT = 3
SEMANTIC_MIN_SIMILARITY = 0.8  # Atlas cosine score; Phase 0 saw 0.847 for the right fix, 0.73 for the next one
MAX_REASONING_ATTEMPTS = 3
MAX_TOKENS = 1024
ERROR_TAIL_CHARS = 4000  # pip/apt failures put the cause at the end
ISSUE_ERROR_LINES = 15
ISSUE_TITLE_COMMAND_CHARS = 120
README_MIN_HIRES = 2

APT_INSTALL = re.compile(r"^(apt-get|apt)\s+install\b")
ASSUME_YES = re.compile(r"\s(-y|--yes|--assume-yes)\b")
SUDO = "sudo "
APT_NONINTERACTIVE = "DEBIAN_FRONTEND=noninteractive "

FIX_RULES = f"""Commands run as root with `sh -lc` in the new hire's throwaway container, in {REPO_DIR} when it exists.
Use the machine's own package manager. Non-interactive only (-y, --no-cache), no sudo, one command per string,
do not chain with && or ;. Commands from earlier attempts already ran; only add what is still needed.
Reply with strict JSON and nothing else: {{"commands": ["..."], "rationale": "one sentence"}}"""

REASON_SYSTEM = f"You fix failed setup steps on a new hire's development machine.\n{FIX_RULES}"
ADAPT_SYSTEM = (
    "You adapt a setup fix that worked on one machine to a different machine (for example apt to apk), "
    f"keeping its intent and translating package names.\n{FIX_RULES}"
)


class LLMResponseError(ValueError):
    """The model's reply was not the strict JSON fix we asked for."""


class IndexNotReady(TimeoutError):
    pass


@dataclass
class _Progress:
    initial: CheckResult
    attempts: int = 0
    llm_calls: int = 0
    applied: list[str] = field(default_factory=list)  # every command run since the failure, in order
    last_check: CheckResult | None = None

    def resolution(self, proposal: FixProposal | None) -> Resolution:
        return Resolution(
            fixed=proposal is not None,
            proposal=proposal,
            attempts=self.attempts,
            llm_calls=self.llm_calls,
            last_check=self.last_check or self.initial,
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _tail(text: str) -> str:
    return text[-ERROR_TAIL_CHARS:]


def _memory_text(error_text: str, context_summary: str) -> str:
    return f"{_tail(error_text)}\n{context_summary}"


def _facts_from(context: dict[str, Any]) -> MachineFacts:
    return MachineFacts(
        os_family=context["os_family"],
        os_version=context["os_version"],
        package_manager=context["package_manager"],
        tools=context.get("tools", {}),
    )


def _proposal_data(proposal: FixProposal) -> dict[str, Any]:
    return {**asdict(proposal), "commands": list(proposal.commands)}


def guard_commands(commands: list[str]) -> tuple[str, ...]:
    """Make model-written commands runnable unattended: one per line, no sudo, apt installs non-interactive."""
    guarded = []
    for raw in commands:
        for line in raw.splitlines():
            command = line.strip().removeprefix(SUDO).strip()
            if not command or command.startswith("#"):
                continue
            if APT_INSTALL.match(command):
                if not ASSUME_YES.search(command):
                    command = command.replace(" install", " install -y", 1)
                command = APT_NONINTERACTIVE + command
            guarded.append(command)
    if not guarded:
        raise LLMResponseError("no runnable commands in the reply")
    return tuple(guarded)


def parse_fix(reply: str) -> tuple[tuple[str, ...], str]:
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise LLMResponseError(f"no JSON object in reply: {reply[:200]!r}")
    try:
        body = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as error:
        raise LLMResponseError(f"invalid JSON: {error}") from error
    commands, rationale = body.get("commands"), body.get("rationale", "")
    if not isinstance(commands, list) or not all(isinstance(command, str) for command in commands):
        raise LLMResponseError("`commands` must be a list of strings")
    return guard_commands(commands), str(rationale)


class ExperienceMemory:
    def __init__(
        self, db: Database, llm: LLM, embedder: Embedder, events: EventBus, gitea: Gitea, settings: Settings
    ) -> None:
        self._experiences = db[EXPERIENCES]
        self._llm = llm
        self._embedder = embedder
        self._events = events
        self._gitea = gitea
        self._dimensions = settings.embedding_dimensions

    # --- indexes ----------------------------------------------------------------

    def ensure_indexes(self) -> None:
        self._experiences.create_index("fingerprint")  # also creates the collection the search index needs
        if not list(self._experiences.list_search_indexes(VECTOR_INDEX)):
            definition = {
                "fields": [
                    {"type": "vector", "path": "embedding", "numDimensions": self._dimensions, "similarity": "cosine"},
                    {"type": "filter", "path": "outcome"},
                    {"type": "filter", "path": "kind"},
                ]
            }
            self._experiences.create_search_index(
                SearchIndexModel(definition=definition, name=VECTOR_INDEX, type="vectorSearch")
            )
        deadline = time.monotonic() + INDEX_WAIT_S
        while not all(index.get("queryable") for index in self._experiences.list_search_indexes(VECTOR_INDEX)):
            if time.monotonic() > deadline:
                raise IndexNotReady(f"{VECTOR_INDEX} not queryable after {INDEX_WAIT_S:.0f}s")
            time.sleep(INDEX_POLL_S)

    # --- terminal fixes -----------------------------------------------------------

    def resolve(self, failure: Failure, try_fix: Callable[[FixProposal], CheckResult]) -> Resolution:
        progress = _Progress(initial=CheckResult(ok=False, evidence="step check failed", error_text=failure.error_text))

        exact = self._exact(failure)
        if exact:
            proposal = FixProposal(
                commands=tuple(exact["fix_commands"]),
                source=FixSource.EXACT_MEMORY,
                rationale=exact.get("rationale", ""),
                experience_id=str(exact["_id"]),
                source_hire=exact["hire"],
            )
            self._events.emit(EventType.MEMORY_HIT, failure.step_id, **_proposal_data(proposal), uses=exact["uses"])
            if self._try(failure, proposal, try_fix, progress):
                self._record_reuse(exact["_id"], failure.hire)
                return progress.resolution(proposal)

        neighbours = self._nearest(failure, exclude=exact["_id"] if exact else None)
        semantic = neighbours[0] if neighbours and neighbours[0]["score"] >= SEMANTIC_MIN_SIMILARITY else None
        if semantic:
            proposal = self._from_semantic(failure, semantic, progress)
            if proposal:
                self._events.emit(EventType.MEMORY_HIT, failure.step_id, **_proposal_data(proposal))
                if self._try(failure, proposal, try_fix, progress):
                    if proposal.adapted_from:
                        saved = self._save(failure, progress, proposal, adapted_from_experience=semantic["_id"])
                        proposal = replace(proposal, experience_id=str(saved))
                    else:
                        self._record_reuse(semantic["_id"], failure.hire)
                    return progress.resolution(proposal)

        if not exact and not semantic:
            self._events.emit(EventType.MEMORY_MISS, failure.step_id, fingerprint=failure.fingerprint)
        return self._reason(failure, try_fix, neighbours, progress)

    def _exact(self, failure: Failure) -> dict[str, Any] | None:
        return self._experiences.find_one(
            {
                "fingerprint": failure.fingerprint,
                "outcome": VERIFIED,
                "kind": failure.kind,
                "context.package_manager": failure.facts.package_manager,
            },
            sort=[("uses", DESCENDING)],
        )

    def _nearest(self, failure: Failure, exclude: ObjectId | None) -> list[dict[str, Any]]:
        [vector] = self._embedder.embed([_memory_text(failure.error_text, failure.context_summary())], QUERY)
        pipeline: list[dict[str, Any]] = [
            {
                "$vectorSearch": {
                    "index": VECTOR_INDEX,
                    "path": "embedding",
                    "queryVector": vector,
                    "numCandidates": NUM_CANDIDATES,
                    "limit": HINT_COUNT + 1,  # room for the excluded exact match
                    "filter": {"outcome": VERIFIED, "kind": failure.kind},
                }
            },
            {"$set": {"score": {"$meta": "vectorSearchScore"}}},
            {"$project": {"embedding": 0}},
        ]
        if exclude:
            pipeline.append({"$match": {"_id": {"$ne": exclude}}})
        pipeline.append({"$limit": HINT_COUNT})
        return list(self._experiences.aggregate(pipeline))

    def _from_semantic(self, failure: Failure, hit: dict[str, Any], progress: _Progress) -> FixProposal | None:
        base = FixProposal(
            commands=tuple(hit["fix_commands"]),
            source=FixSource.SEMANTIC_MEMORY,
            rationale=hit.get("rationale", ""),
            experience_id=str(hit["_id"]),
            similarity=hit["score"],
            source_hire=hit["hire"],
        )
        original = _facts_from(hit["context"])
        if original.package_manager == failure.facts.package_manager:
            return base
        prompt = (
            f"This fix was verified on {original.summary()} for this error:\n{_tail(hit['error_text'])}\n\n"
            "Commands that fixed it there:\n" + "\n".join(hit["fix_commands"]) + "\n\n"
            f"Now machine {failure.facts.summary()} fails step {failure.step_id} with:\n{_tail(failure.error_text)}\n\n"
            f"Rewrite the commands for {failure.facts.os_family} using {failure.facts.package_manager}."
        )
        progress.llm_calls += 1
        try:
            commands, rationale = parse_fix(self._llm.complete(ADAPT_SYSTEM, prompt, MAX_TOKENS))
        except LLMResponseError:
            return None
        return replace(base, commands=commands, rationale=rationale, adapted_from=original.package_manager)

    def _reason(
        self,
        failure: Failure,
        try_fix: Callable[[FixProposal], CheckResult],
        hints: list[dict[str, Any]],
        progress: _Progress,
    ) -> Resolution:
        self._events.emit(EventType.REASONING_STARTED, failure.step_id, hints=[str(hint["_id"]) for hint in hints])
        hint_text = "\n\n".join(
            f"- on {_facts_from(hint['context']).summary()} (similarity {hint['score']:.2f}), error:\n"
            f"{_tail(hint['error_text'])}\n  fixed by: " + " ; ".join(hint["fix_commands"])
            for hint in hints
        )
        history: list[str] = []
        error = failure.error_text
        for _ in range(MAX_REASONING_ATTEMPTS):
            prompt = (
                f"Machine: {failure.facts.summary()}\nStep {failure.step_id} fails with:\n{_tail(error)}\n\n"
                + (f"Past verified fixes for similar errors (hints, may not apply):\n{hint_text}\n\n" if hints else "")
                + ("Earlier attempts in this session:\n" + "\n\n".join(history) + "\n\n" if history else "")
                + "Propose the commands that make this step pass."
            )
            progress.llm_calls += 1
            try:
                commands, rationale = parse_fix(self._llm.complete(REASON_SYSTEM, prompt, MAX_TOKENS))
            except LLMResponseError as reply_error:
                history.append(f"Your reply was rejected: {reply_error}")
                continue
            proposal = FixProposal(commands=commands, source=FixSource.REASONING, rationale=rationale)
            if self._try(failure, proposal, try_fix, progress):
                saved = self._save(failure, progress, proposal)
                return progress.resolution(replace(proposal, experience_id=str(saved)))
            check = progress.last_check
            error = check.error_text or check.evidence
            self._save_failed(failure, commands, check)
            history.append("Ran:\n" + "\n".join(commands) + f"\nThe step still fails with:\n{_tail(error)}")
        return progress.resolution(None)

    def _try(
        self,
        failure: Failure,
        proposal: FixProposal,
        try_fix: Callable[[FixProposal], CheckResult],
        progress: _Progress,
    ) -> bool:
        check = try_fix(proposal)
        progress.attempts += 1
        progress.applied.extend(proposal.commands)
        progress.last_check = check
        self._events.emit(
            EventType.FIX_ATTEMPT,
            failure.step_id,
            source=proposal.source,
            commands=list(proposal.commands),
            ok=check.ok,
            evidence=check.evidence,
        )
        return check.ok

    # --- persistence ----------------------------------------------------------------

    def _context(self, failure: Failure) -> dict[str, Any]:
        return {"step_id": failure.step_id, **asdict(failure.facts)}

    def _save(
        self,
        failure: Failure,
        progress: _Progress,
        proposal: FixProposal,
        adapted_from_experience: ObjectId | None = None,
    ) -> ObjectId:
        [vector] = self._embedder.embed([_memory_text(failure.error_text, failure.context_summary())], DOCUMENT)
        document = {
            "kind": failure.kind,
            "fingerprint": failure.fingerprint,
            "error_text": failure.error_text,
            "context": self._context(failure),
            "fix_commands": list(progress.applied),
            "rationale": proposal.rationale,
            "source": proposal.source,
            "adapted_from": proposal.adapted_from,
            "adapted_from_experience": adapted_from_experience,
            "outcome": VERIFIED,
            "verified_by_check": progress.last_check.evidence,
            "hire": failure.hire,
            "hires": [failure.hire],
            "uses": 1,
            "embedding": vector,
            "created_at": _now(),
        }
        experience_id = self._experiences.insert_one(document).inserted_id
        self._events.emit(
            EventType.EXPERIENCE_SAVED,
            failure.step_id,
            experience_id=str(experience_id),
            fingerprint=failure.fingerprint,
            commands=document["fix_commands"],
            source=proposal.source,
        )
        return experience_id

    def _save_failed(self, failure: Failure, commands: tuple[str, ...], check: CheckResult) -> None:
        # No embedding: failed attempts are kept for the record and must never come back as a fix.
        self._experiences.insert_one(
            {
                "kind": failure.kind,
                "fingerprint": failure.fingerprint,
                "error_text": failure.error_text,
                "context": self._context(failure),
                "fix_commands": list(commands),
                "outcome": FAILED,
                "result_error": check.error_text or check.evidence,
                "hire": failure.hire,
                "created_at": _now(),
            }
        )

    def _record_reuse(self, experience_id: ObjectId, hire: str) -> None:
        self._experiences.update_one(
            {"_id": experience_id}, {"$inc": {"uses": 1}, "$addToSet": {"hires": hire}, "$set": {"last_used_at": _now()}}
        )

    # --- browser locators -------------------------------------------------------------

    def find_locator(self, goal: str, page_summary: str) -> LocatorHint | None:
        # Exact goal only, no vector search: every goal on one page shares its page summary and near-identical
        # wording ("sign-in page: username field" vs "...: remember this device checkbox"), so a semantic hit hands
        # one goal another goal's element. A same-goal hit is already found here, newest first, whatever the layout;
        # a moved element is caught when the locator fails and the browser reasons over the page (page_summary).
        document = self._experiences.find_one(
            {"kind": StepKind.BROWSER, "outcome": VERIFIED, "goal": goal}, sort=[("updated_at", DESCENDING)]
        )
        if document is None:
            return None
        bbox = document.get("bbox")
        return LocatorHint(
            goal=document["goal"],
            locator=document["locator"],
            bbox=tuple(bbox) if bbox else None,
            page_summary=document["page_summary"],
            source_hire=document["hire"],
            similarity=None,
        )

    def save_locator(self, hint: LocatorHint, hire: str) -> None:
        self._experiences.update_one(
            {"kind": StepKind.BROWSER, "outcome": VERIFIED, "goal": hint.goal, "locator": hint.locator},
            {
                "$set": {
                    "bbox": list(hint.bbox) if hint.bbox else None,
                    "page_summary": hint.page_summary,
                    "updated_at": _now(),
                },
                "$inc": {"uses": 1},
                "$addToSet": {"hires": hire},
                "$setOnInsert": {"hire": hire, "created_at": _now()},
            },
            upsert=True,
        )

    # --- closing the loop ---------------------------------------------------------------

    def close_loop(self) -> list[str]:
        repeated = self._experiences.aggregate(
            [
                {"$match": {"kind": StepKind.TERMINAL, "outcome": VERIFIED}},
                {"$match": {"$expr": {"$gte": [{"$size": "$hires"}, README_MIN_HIRES]}}},
                {"$project": {"embedding": 0}},
                {"$sort": {"created_at": 1}},
            ]
        )
        urls = []
        for experience in repeated:
            title, body = self._readme_issue(experience)
            issue = self._gitea.find_issue(REPO, title)
            if issue is None:
                issue = self._gitea.create_issue(REPO, title, body)
                self._events.emit(
                    EventType.README_ISSUE_OPENED,
                    experience["context"]["step_id"],
                    url=issue["html_url"],
                    title=title,
                    hires=experience["hires"],
                    experience_id=str(experience["_id"]),
                )
            urls.append(issue["html_url"])
        return urls

    def _readme_issue(self, experience: dict[str, Any]) -> tuple[str, str]:
        context = experience["context"]
        facts = _facts_from(context)
        commands = experience["fix_commands"]
        summary = "; ".join(commands)
        if len(summary) > ISSUE_TITLE_COMMAND_CHARS:
            summary = summary[: ISSUE_TITLE_COMMAND_CHARS - 1] + "…"
        title = f"README: {context['step_id']} on {facts.os_family} needs `{summary}`"
        error_lines = "\n".join(experience["error_text"].strip().splitlines()[-ISSUE_ERROR_LINES:])
        command_lines = "\n".join(commands)
        body = (
            f"{len(experience['hires'])} new hires ({', '.join(experience['hires'])}) on {facts.summary()} "
            f"hit this during `{context['step_id']}`, and the README does not mention it:\n\n"
            f"```\n{error_lines}\n```\n\n"
            f"Day One fixed it with these commands, verified by the step's check (`{experience['verified_by_check']}`):\n\n"
            f"```sh\n{command_lines}\n```\n\n"
            "Proposed change: add these commands to the README's setup section for this platform.\n\n"
            f"Opened by Day One from experience `{experience['_id']}`, applied {experience['uses']} times."
        )
        return title, body
