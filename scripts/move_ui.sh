#!/usr/bin/env bash
# Moves Gitea's SSH-key form (custom template: new field ids, key above name) so saved locators stop matching.
# Undo with scripts/restore_ui.sh. Gitea only reads custom templates at startup, so this restarts it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/gitea_container.sh"

docker cp "$ROOT/infra/moved_ui/templates/." "$GITEA_CONTAINER:$CUSTOM_TEMPLATES_DIR/"
docker exec "$GITEA_CONTAINER" chown -R git:git "$CUSTOM_TEMPLATES_DIR"
restart_gitea
echo "SSH key form moved"
