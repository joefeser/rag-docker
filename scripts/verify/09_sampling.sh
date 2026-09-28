#!/usr/bin/env bash
# UUID selection through the real SDK; owned fixtures and supplied vectors.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Evaluation sampling across iterator pages"
(cd ../.. && docker compose exec -T api python - < scripts/verify/chunk_sampling.py)
check "seeded selection, iterator paging, bounds and owned-fixture cleanup" $?
summary
