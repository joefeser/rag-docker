#!/usr/bin/env bash
# Durable session edits, generation interleaving and visible recovery diagnostics.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Durable evaluation session updates"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/session_persistence.py)
check "concurrent HTTP edits, fresh-process reload, write failures and diagnostics" $?
summary
