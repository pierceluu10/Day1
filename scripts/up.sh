#!/usr/bin/env bash
# From nothing to a seeded Gitea on localhost:3000. Safe to run again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
NETWORK="dayone-net"
CONTAINER="gitea"
TOKEN_FILE="$ROOT/.dayone/gitea_admin_token"
READY_ATTEMPTS=60

env_value() { grep -E "^$1=" "$ROOT/.env" | head -n1 | cut -d= -f2-; }
GITEA_URL="$(env_value GITEA_URL)"
ADMIN_USER="$(env_value GITEA_ADMIN_USER)"
ADMIN_PASSWORD="$(env_value GITEA_ADMIN_PASSWORD)"
: "${GITEA_URL:?GITEA_URL missing from .env}" "${ADMIN_USER:?GITEA_ADMIN_USER missing from .env}"
: "${ADMIN_PASSWORD:?GITEA_ADMIN_PASSWORD missing from .env}"

gitea_cli() { docker exec -u git "$CONTAINER" gitea "$@"; }

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
docker compose -f "$COMPOSE_FILE" up -d --wait
docker build -q -t dayone-hire-ubuntu:22.04 -f "$ROOT/infra/hire/ubuntu.Dockerfile" "$ROOT/infra/hire" >/dev/null

for _ in $(seq "$READY_ATTEMPTS"); do
  curl -fsS "$GITEA_URL/api/v1/version" >/dev/null 2>&1 && break
  sleep 1
done
echo "gitea $(curl -fsS "$GITEA_URL/api/v1/version")"

if ! gitea_cli admin user list --admin | awk 'NR > 1 { print $2 }' | grep -qx "$ADMIN_USER"; then
  gitea_cli admin user create --username "$ADMIN_USER" --password "$ADMIN_PASSWORD" \
    --email "$ADMIN_USER@acme.test" --admin --must-change-password=false
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
if ! { [ -s "$TOKEN_FILE" ] && curl -fsS -o /dev/null -H "Authorization: token $(cat "$TOKEN_FILE")" "$GITEA_URL/api/v1/user"; }; then
  gitea_cli admin user generate-access-token --username "$ADMIN_USER" \
    --token-name "dayone-seed-$(date +%s)" --scopes all --raw > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "wrote admin token to $TOKEN_FILE"
fi

cd "$ROOT"
uv run python scripts/seed.py "$@"
