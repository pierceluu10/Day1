"""Sentry-style error fingerprints: normalise away what differs between machines, then hash.

Memory embeds the raw error text; the fingerprint is only for exact-match lookup.
"""

from __future__ import annotations

import hashlib
import re

FINGERPRINT_LENGTH = 16
# Only the lines that state the error take part, so progress and download noise around it does not.
ERROR_LINE = re.compile(r"error|fatal|not found|exception|failed|no such|denied|cannot|unable|requires", re.I)

# Order matters: broad shapes (timestamps, URLs, paths) go before the numbers inside them.
SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<time>"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I), "<url>"),
    (re.compile(r"SHA256:[A-Za-z0-9+/=]+"), "<keyfp>"),
    (re.compile(r"(?:~|\.{1,2})?(?:/[\w.@+~-]+)+/?"), "<path>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:[KMGT]i?B|kB|B|bytes)\b"), "<size>"),
    (re.compile(r"\bline \d+"), "line <n>"),
    (re.compile(r"\bv?\d+(?:\.\d+)+(?:[a-z]+\d*)?\b"), "<version>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"[ \t]+"), " "),
)


def normalise(error_text: str) -> str:
    lines = [line.strip() for line in error_text.splitlines() if line.strip()]
    lines = [line for line in lines if ERROR_LINE.search(line)] or lines
    normalised = []
    for line in lines:
        for pattern, replacement in SUBSTITUTIONS:
            line = pattern.sub(replacement, line)
        if line not in normalised:
            normalised.append(line)
    return "\n".join(normalised)


def fingerprint(error_text: str) -> str:
    return hashlib.sha256(normalise(error_text).encode()).hexdigest()[:FINGERPRINT_LENGTH]
