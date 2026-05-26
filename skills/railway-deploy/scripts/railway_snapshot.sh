#!/usr/bin/env bash
set -euo pipefail

repo_root="${1:-$PWD}"
cd "$repo_root"

section() {
  printf '\n== %s ==\n' "$1"
}

section "git"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  printf 'root=%s\n' "$(git rev-parse --show-toplevel)"
  printf 'branch=%s\n' "$(git branch --show-current || true)"
  printf 'head=%s\n' "$(git rev-parse --short HEAD || true)"
  printf 'dirty_count=%s\n' "$(git status --short | wc -l | tr -d ' ')"
  git remote -v | sed -E 's#(https?://)[^/@]+@#\1[redacted]@#'
else
  echo "not a git worktree"
fi

section "railway"
if ! command -v railway >/dev/null 2>&1; then
  echo "railway CLI not found"
  exit 0
fi

railway --version 2>/dev/null || true

echo "-- status --"
railway status 2>&1 | sed -E \
  -e 's#(postgres(ql)?://)[^[:space:]]+#\1[redacted]#Ig' \
  -e 's#(DATABASE_URL=)[^[:space:]]+#\1[redacted]#Ig' \
  -e 's#(RAILWAY_[A-Z_]*TOKEN=)[^[:space:]]+#\1[redacted]#g' || true

echo "-- status json available --"
if railway status --json >/tmp/railway-status-snapshot.json 2>/tmp/railway-status-snapshot.err; then
  python3 - <<'PY'
import json
from pathlib import Path

data = json.loads(Path("/tmp/railway-status-snapshot.json").read_text())

def pick(obj, keys):
    out = {}
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            out[key] = obj[key]
    return out

print(json.dumps(pick(data, [
    "workspace",
    "project",
    "environment",
    "service",
    "services",
]), indent=2, sort_keys=True))
PY
else
  sed -n '1,12p' /tmp/railway-status-snapshot.err
fi

rm -f /tmp/railway-status-snapshot.json /tmp/railway-status-snapshot.err

section "local config files"
for path in railway.json Dockerfile Procfile package.json pyproject.toml requirements.txt requirements-prod.txt; do
  [ -e "$path" ] && printf '%s\n' "$path"
done
