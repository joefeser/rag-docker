#!/usr/bin/env bash
# Shared helpers for the verification suites.
#
# Sourced, never executed. Every suite expects a running stack and leaves the
# instance as it found it.
#
# A note on JSON and the shell: never round-trip an API response through `echo`.
# zsh and bash interpret backslash escapes differently, and an LLM-generated
# answer containing \n will be silently corrupted into invalid JSON. Pipe curl
# straight into python3, or capture to a file. This cost real debugging time
# more than once.

set -uo pipefail

# One verify run at a time; see lock.sh.
. "$(dirname "${BASH_SOURCE[0]}")/lock.sh"

API="${RAG_API:-http://localhost:8080/api}"
# Collections and packages created by the suites all carry this prefix so
# cleanup can find them without guessing.
PREFIX="${RAG_TEST_PREFIX:-Vfy}"
# Suites that need an LLM call are slow (20-60s each). Set RAG_SKIP_SLOW=1 for
# a structural-only run.
SKIP_SLOW="${RAG_SKIP_SLOW:-0}"

PASS=0; FAIL=0; SKIP=0
FAILED_NAMES=()

_c_pass=$'\033[32m'; _c_fail=$'\033[31m'; _c_skip=$'\033[33m'; _c_off=$'\033[0m'
[ -t 1 ] || { _c_pass=""; _c_fail=""; _c_skip=""; _c_off=""; }

section() { printf '\n  %s\n' "$1"; }

# check <name> <condition-exit-status> [detail]
check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS+1)); printf '    %sPASS%s  %s\n' "$_c_pass" "$_c_off" "$name"
  else
    FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    printf '    %sFAIL%s  %s%s\n' "$_c_fail" "$_c_off" "$name" "${detail:+  — $detail}"
  fi
}

# check_eq <name> <actual> <expected>
check_eq() {
  local name="$1" actual="$2" expected="$3"
  [ "$actual" = "$expected" ]
  check "$name" $? "got '$actual', wanted '$expected'"
}

skip() {
  SKIP=$((SKIP+1)); printf '    %sSKIP%s  %s%s\n' "$_c_skip" "$_c_off" "$1" "${2:+  — $2}"
}

# jq-free JSON field read: api_get <path> | jfield <expr>
# `expr` is python indexing against the parsed document, e.g. ['status']
jfield() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

api_get()  { curl -s -m 120 "$API$1"; }
api_code() { curl -s -o /dev/null -m 120 -w '%{http_code}' "$@"; }
api_post() { curl -s -m 600 -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }
api_post_code() { curl -s -o /dev/null -m 600 -w '%{http_code}' -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }

require_stack() {
  local code
  code=$(api_code "$API/health")
  if [ "$code" != "200" ]; then
    printf '\n  Cannot reach a healthy API at %s (HTTP %s).\n' "$API" "$code"
    printf '  Start the verify project first:  bash scripts/verify/stack.sh up\n\n'
    exit 2
  fi
}

# wait_for_job <url-path> [timeout-seconds] — polls until status is terminal.
# Echoes the final status.
wait_for_job() {
  local path="$1" limit="${2:-600}" waited=0 status=""
  while [ "$waited" -lt "$limit" ]; do
    status=$(api_get "$path" | jfield "['status']")
    case "$status" in
      completed|failed|partial|cancelled) printf '%s' "$status"; return 0 ;;
    esac
    sleep 2; waited=$((waited+2))
  done
  printf 'timeout'; return 1
}

make_collection() {
  api_post "/collections" "{\"name\":\"$1\",\"index_type\":\"${2:-hnsw}\",\"distance_metric\":\"${3:-cosine}\",\"hnsw_config\":{\"efConstruction\":128,\"maxConnections\":64,\"ef\":64}}" >/dev/null
}

drop_collection() { curl -s -o /dev/null -m 120 -X DELETE "$API/collections/$1?confirm=true"; }

# Remove every collection and package this run created.
cleanup_prefixed() {
  local names
  names=$(api_get "/collections" | python3 -c "
import json,sys
for c in json.load(sys.stdin)['collections']:
    if c['name'].startswith('$PREFIX'): print(c['name'])" 2>/dev/null)
  for n in $names; do drop_collection "$n"; done
  rm -f "${RAG_EXPORTS_DIR:-${REPO_ROOT:-.}/exports}"/ragpkg-"$(echo "$PREFIX" | tr '[:upper:]' '[:lower:]')"*.tar.gz 2>/dev/null || true
}

summary() {
  printf '\n  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
  if [ "$FAIL" -gt 0 ]; then
    printf '  failed:\n'
    printf '    - %s\n' "${FAILED_NAMES[@]}"
    return 1
  fi
  return 0
}

# ── the live stack is off limits (#152) ──────────────────────────────────────
# Verification runs on the disposable `rag-verify` project that
# scripts/verify/stack.sh brings up, never on the live `rag-docker` stack that
# holds real data. A target is live when the compose project these scripts
# would act on is rag-docker, or when RAG_API uses the live port 8080.

# Prints why the current target is the live stack, or nothing.
live_target_reason() {
  local project="${COMPOSE_PROJECT_NAME:-}" root port
  if [ -z "$project" ]; then
    # What compose would use: a `name:` in the compose file, else the folder
    # name, lowercased with everything outside [a-z0-9_-] dropped.
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    project=$(sed -n 's/^name:[[:space:]]*["'"'"']\{0,1\}\([^"'"'"'[:space:]]*\).*/\1/p' "$root/docker-compose.yml" 2>/dev/null | head -1)
    [ -n "$project" ] || project=$(basename "$root")
  fi
  project=$(printf '%s' "$project" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
  port=$(python3 -c '
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
try:
    port = url.port
except ValueError:
    port = None
print(port if port is not None else (443 if url.scheme == "https" else 80))' "$API" 2>/dev/null)
  if [ "$project" = "rag-docker" ]; then
    printf 'the compose project is rag-docker, the live rag-docker stack'
  elif [ "$port" = "8080" ]; then
    printf 'RAG_API (%s) uses port 8080, the live rag-docker stack' "$API"
  fi
}

# Refuses a live target unless RAG_VERIFY_LIVE=1, which only warns.
live_guard() {
  local reason
  reason=$(live_target_reason)
  [ -n "$reason" ] || return 0
  if [ "${RAG_VERIFY_LIVE:-0}" = "1" ]; then
    printf '\n  WARNING: RAG_VERIFY_LIVE=1, so this run targets the live rag-docker stack:\n  %s.\n\n' "$reason" >&2
    return 0
  fi
  printf '\n  Refusing to verify against the live rag-docker stack: %s.\n' "$reason" >&2
  printf '  Run it on the disposable verify project:  bash scripts/verify/stack.sh run\n' >&2
  printf '  (RAG_VERIFY_LIVE=1 overrides this; see scripts/verify/README.md.)\n\n' >&2
  exit 2
}

# Prints why a restart must not run here, or nothing. Restarts never reach the
# live project, whatever RAG_VERIFY_LIVE says.
restart_refusal_reason() {
  local project
  project=$(printf '%s' "${COMPOSE_PROJECT_NAME:-}" | tr '[:upper:]' '[:lower:]')
  if [ -z "$project" ]; then
    printf 'COMPOSE_PROJECT_NAME is not set, so a restart could reach the live rag-docker stack; run it through scripts/verify/stack.sh'
  elif [ "$project" = "rag-docker" ]; then
    printf 'restarts never run against the live rag-docker project (#152)'
  fi
}

# Suites (NN_*.sh) are guarded as soon as they source this file. Other scripts
# that borrow these helpers are not suites and are left alone.
case "$(basename "$0")" in
  [0-9][0-9]_*.sh) live_guard ;;
esac
