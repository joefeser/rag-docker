# Verification suite

Integration tests that run the acceptance criteria in `SPECIFICATIONS.md` §10
and `RAG_EXPORT_SPECIFICATIONS.md` §13 against a live stack.

```bash
docker compose up -d          # they need the stack running

bash scripts/verify/all.sh                    # everything, ~20 min
RAG_SKIP_SLOW=1 bash scripts/verify/all.sh    # skip LLM work, ~3 min
bash scripts/verify/all.sh 02 04              # only the named suites
```

Exits non-zero if any check fails.

## Why integration tests

Every defect this project has actually produced was invisible to a unit test of
the same code:

- `.md` files could never be ingested — the `markdown` package was missing from
  the image, so `unstructured.partition.md` failed at import time. The function
  was correct.
- `PATCH /goldstandard/.../pair/...` returned 200 and silently rewrote approved
  content. The handler did exactly what it was written to do.
- Explicit vectors survive Weaviate's vectorizer — the entire import design
  rests on that, and only the database can confirm it.
- A recreated collection inherited a deleted one's chunking settings, because
  deletion removed two of the three things it should have.

These tests talk to the running API, the real Weaviate and the real model, and
drive the UI in a real browser.

## Layout

| File | Covers |
|---|---|
| `all.sh` | entry point; runs the suites and aggregates |
| `lib.sh` | shared helpers: checks, job polling, cleanup |
| `fixtures.py` | the test corpus — six file types plus edge cases, stdlib only |
| `01_infrastructure.sh` | §10.5 — ports, health, config lifecycle, startup sweeps |
| `02_ingest.sh` | §10.1 — six types, ZIP, five strategies, merge rule, partial failure |
| `03_query.sh` | §10.2 — four retrieval modes, citations, latencies, answer style |
| `04_goldstandard.sh` | §10.3 — generation, the 409 and 422 guards, export schema |
| `05_transfer.sh` | export/import/tuning — E5–E20, plus shared-template drift |
| `06_ui.sh` + `browser/` | §10.4 — roles, gating, explainer, delete guard, help page |
| `14_reindex.sh` | exact-record reindex: 24 record/cutover/vectorizer/concurrency cases, fourteen writer/import/recovery cases, four async lifecycle/parent-cleanup cases, two polling-deadline cases and 44 real Weaviate/handler/restart checks with a refused embedding endpoint; run by `05_transfer.sh` |
| `reindex_cases.py` / `reindex.py` | owned controlled cases / actual backend and ASGI job handlers; only scoped synthetic fixtures, canonical local session IDs and exact successful-creation ownership. Verifier collection names deliberately lie outside the parent prefix-sweep namespace. Polling is bounded to 300s, cleanup settlement to 30s; a still-active job reports its ID/status and preserves a durable exact-name fixture receipt/directory before standalone exit; inspection must confirm terminal writer state before exact-name cleanup |
| `compose_target.py` | refuses a remote or mismatched API/Compose target before the new acceptance suite runs |
| `validate_package.py` | one export package against `RAG_EXPORT_SPECIFICATIONS.md` §4 |

## Environment

| Variable | Default | Effect |
|---|---|---|
| `RAG_API` | `http://localhost:8080/api` | where the API is |
| `RAG_SKIP_SLOW` | `0` | `1` skips everything that needs an LLM call |
| `RAG_ALLOW_RESTART` | `0` | `1` allows suites to restart the stack (persistence checks) |
| `RAG_GS_SAMPLE` | `3` | gold-standard pairs to generate |
| `RAG_FORMAT_TRIALS` | `3` | paired trials for the answer-length comparison |
| `RAG_NETWORK` | detected | compose network for the browser container |

## Writing a check

`check <name> <exit-status> [detail]` — pass `$?` straight in:

```bash
[ "$status" = completed ] && [ "$chunks" -gt 0 ]
check "the job stored chunks" $? "status=$status chunks=$chunks"
```

Two rules the hard way:

- **Never pipe an API response through `echo`.** Shells interpret backslash
  escapes, and an LLM-generated answer containing `\n` becomes invalid JSON.
  Pipe `curl` straight into `python3`, or capture to a file.
- **Make a failing assertion fail loudly.** A check that compares two empty
  strings passes and proves nothing. Several early versions of these tests
  passed vacuously — asserting on a selector that matched nothing, or comparing
  a count to itself.

## Cleaning up

Suites create collections prefixed `Vfy` (`RAG_TEST_PREFIX`) and remove them at
the end. If a run is interrupted:

```bash
curl -s localhost:8080/api/collections | python3 -c \
  "import json,sys;[print(c['name']) for c in json.load(sys.stdin)['collections']]" \
  | grep '^Vfy' | xargs -I{} curl -s -X DELETE "localhost:8080/api/collections/{}?confirm=true"
```

`reindex_verifier_cases.py` checks actual async failure cleanup and the parent shell cleanup predicate without a backend/model call. The registered suite transports its helper, the actual `lib.sh`, and these controlled tests into an owned temporary API directory. `test_collection_writes.py` checks waiting writers, case aliases, reentrancy, failures, independent collections and actual import/backend entry points, a paused complete replace-import cutover and sidecar restoration, positive ownership before import staging, and ordinary staging failure/recovery cleanup. The live suite refuses altered property vectorization and removes exact durably owned import scratch after an independent process exits without finally.

The concurrency HTTP check uses supplied-vector ingestion fixtures while keeping the upload handler, parser, chunker, worker, source retention and actual backend writes real. Its reindex source check pauses under the writer guard; the upload remains queued until final copy verification. Recovery is separately forced to fail at final creation and verified through an independent API lifespan. These cases do not claim generative model quality.

Tuning normalizes the backend first-character alias for active jobs and ownership, while preserving the caller-spelled identity for source/config/session sidecars. All tuning operations register positive staging ownership before creation and retain recovery before cutover. Explicit deletion of an exact positively owned recovery collection retires its matching journal and metadata snapshots; unrelated or invalid journals remain. Startup alone does not discard retained snapshots merely because a backend collection is missing. Interrupted explicit cleanup remains durable and is resumed at startup.
