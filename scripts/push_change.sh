#!/usr/bin/env bash
# Lands a commit on acme/acme-api mid-run: a source-only dependency (python-ldap, no wheels on PyPI)
# that needs LDAP/SASL headers, plus a test that uses it. Running it again is a no-op.
# usage: scripts/push_change.sh [owner/repo]   (default acme/acme-api)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHANGE_DIR="$ROOT/infra/push_change"
TOKEN_FILE="$ROOT/.dayone/gitea_admin_token"
REPO="${1:-acme/acme-api}"
BRANCH="main"
REQUIREMENT="python-ldap==3.4.8"
COMMIT_MESSAGE="Add employee directory DNs (python-ldap)"
COMMIT_AUTHOR_NAME="Acme Engineering"
COMMIT_AUTHOR_EMAIL="engineering@acme.test"
COMMIT_DATE="2026-11-14T12:00:00+00:00"

GITEA_URL="$(grep -E '^GITEA_URL=' "$ROOT/.env" | head -n1 | cut -d= -f2-)"
AUTH_HEADER="Authorization: token $(cat "$TOKEN_FILE")"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git -c http.extraHeader="$AUTH_HEADER" clone -q --branch "$BRANCH" "${GITEA_URL%/}/$REPO.git" "$WORK/repo"
cd "$WORK/repo"
OLD_HEAD="$(git rev-parse HEAD)"

if grep -qx "$REQUIREMENT" requirements.txt; then
  echo "already applied: HEAD $OLD_HEAD"
  exit 0
fi

cp -R "$CHANGE_DIR"/. .
echo "$REQUIREMENT" >> requirements.txt
git add -A
GIT_AUTHOR_DATE="$COMMIT_DATE" GIT_COMMITTER_DATE="$COMMIT_DATE" \
  git -c user.name="$COMMIT_AUTHOR_NAME" -c user.email="$COMMIT_AUTHOR_EMAIL" commit -qm "$COMMIT_MESSAGE"
git -c http.extraHeader="$AUTH_HEADER" push -q origin "$BRANCH"
NEW_HEAD="$(git rev-parse HEAD)"
echo "remote HEAD moved ${OLD_HEAD:0:7} -> ${NEW_HEAD:0:7}"
