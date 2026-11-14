#!/usr/bin/env bash
# Removes the custom templates scripts/move_ui.sh installed, restoring Gitea's stock SSH-key form.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/gitea_container.sh"

if docker exec "$GITEA_CONTAINER" test -e "$CUSTOM_TEMPLATES_DIR/$MOVED_TEMPLATE"; then
  docker exec "$GITEA_CONTAINER" rm -f "$CUSTOM_TEMPLATES_DIR/$MOVED_TEMPLATE"
  restart_gitea
fi
echo "SSH key form restored"
