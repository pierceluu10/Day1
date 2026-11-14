"""SSH from the hire's laptop to Gitea."""

from __future__ import annotations

import re

from dayone.contracts import GITEA_HOSTNAME, CheckResult, StepContext

SSH_OPTIONS = "-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10"
SSH_TIMEOUT_S = 60.0
SSH_AUTHENTICATED = re.compile(r"Hi there, (?P<user>[\w.-]+)! You've successfully authenticated")


def ssh_auth_ok(ctx: StepContext) -> CheckResult:
    """`ssh -T` from the laptop. Gitea exits non-zero even on success, so the greeting decides."""
    result = ctx.executor.exec(f"ssh -T {SSH_OPTIONS} git@{GITEA_HOSTNAME}", SSH_TIMEOUT_S)
    match = SSH_AUTHENTICATED.search(result.output)
    if match is None or match["user"] != ctx.hire.name:
        detail = {"exit_code": result.exit_code}
        return CheckResult(False, "ssh -T: not authenticated", detail, result.output.strip())
    return CheckResult(True, f"ssh -T: authenticated as {match['user']}", {"ssh_user": match["user"]})
