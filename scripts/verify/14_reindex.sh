#!/usr/bin/env bash
# Exact-record reindex and unavailable-embedding acceptance.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$bindings" || exit 2
require_stack
section "Exact-record reindex"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/verify/reindex_cases.py)
check "owned reindex preservation and failure regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/reindex.py)
check "real reindex with embedding endpoint unavailable" $?
summary
