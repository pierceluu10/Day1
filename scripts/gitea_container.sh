# Sourced by move_ui.sh and restore_ui.sh. GITEA_CONTAINER overrides the target (e.g. a throwaway Gitea).
GITEA_CONTAINER="${GITEA_CONTAINER:-gitea}"
CUSTOM_TEMPLATES_DIR="/data/gitea/templates"
MOVED_TEMPLATE="user/settings/keys_ssh.tmpl"
HEALTH_URL="http://localhost:3000/api/healthz"
RESTART_ATTEMPTS=60

restart_gitea() {
  local started=$SECONDS
  docker restart "$GITEA_CONTAINER" >/dev/null
  for _ in $(seq "$RESTART_ATTEMPTS"); do
    if docker exec "$GITEA_CONTAINER" curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
      echo "gitea restarted in $((SECONDS - started))s"
      return 0
    fi
    sleep 1
  done
  echo "gitea did not come back within ${RESTART_ATTEMPTS}s" >&2
  return 1
}
