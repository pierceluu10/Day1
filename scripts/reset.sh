#!/usr/bin/env bash
# Removes every dayone-* hire container and restores Gitea to its freshly seeded state (stock UI included).
# Atlas is untouched (`dayone reset` does that). The admin user and token survive.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HIRE_CONTAINER_PREFIX="dayone-"

containers="$(docker ps -aq --filter "name=^${HIRE_CONTAINER_PREFIX}")"
if [ -n "$containers" ]; then
  docker rm -f $containers >/dev/null
  echo "removed $(echo "$containers" | wc -l | tr -d ' ') hire container(s)"
fi

"$ROOT/scripts/up.sh" --reset
"$ROOT/scripts/restore_ui.sh"
