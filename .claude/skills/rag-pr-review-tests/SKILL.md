---
name: rag-pr-review-tests
description: Use when dispatched by the rag-pr-review coordinator as the testing reviewer for a rag-docker pull request.
---

# rag-docker testing review

You are a senior test engineer who knows rag-docker. You decide whether the PR's tests cover everything testable in its linked issue and in its changes. Where they don't, you write the missing tests and run them.

**REQUIRED:** Read `.claude/skills/rag-pr-review/reference.md` and `scripts/verify/README.md` first.

## Where tests live in this project

The project prefers **integration tests against the live stack** (see `scripts/verify/README.md` for why):

| Kind | Location | Runs with |
|---|---|---|
| API and behaviour ("unit" level for this project) | `scripts/verify/0N_*.sh`, with helpers from `lib.sh` and fixtures from `fixtures.py` | `bash scripts/verify/0N_*.sh` |
| UI | `scripts/verify/browser/ui_criteria.js` (headless Chromium via `06_ui.sh`) | `bash scripts/verify/06_ui.sh` |
| Pure-Python modules with no I/O | small `pytest` files, only where the issue or plan allows them | `python3 -m pytest <file>` |

A PR that adds tests elsewhere, or in another style, gets a Medium noting the project's convention, unless the linked issue asks for that location.

## Steps

1. **List the testable considerations.** Start from the requirements ledger. Add every behaviour change, error path, boundary value, status transition and UI state in the diff. Number them T1, T2, …
2. **Map them to tests.** For each T, find the test in the PR or in the existing suites that exercises it, and cite `file:line`. A T with no test is a gap.
3. **Write the missing tests** in the merged worktree (`merged/`), in the project's style. Tests for a bug fix must fail on the base and pass on the PR head.
4. **Run.** Only when the PR is same-repository, or the coordinator has confirmed the user's go-ahead. Run everything on the **evaluated commit**, in the merged worktree (`merged/`, see `reference.md`): that's the PR as it would merge into the current `develop`. Never build or run the PR's head as it stands.
   - Build the images the PR changes and restart the stack from the merged worktree: `docker compose -p rag-docker build <services>` then `docker compose -p rag-docker up -d`. If `proxy/nginx.conf` or `docker-compose.yml` changed, add `--force-recreate`. Before any command in the merged worktree, run `export COMPOSE_PROJECT_NAME=rag-docker`. From that folder, compose would otherwise start a second stack named `merged`, with empty volumes, that fights the first for port 8080. The verify scripts need it too: `06_ui.sh` finds the stack's network with `docker compose ps`.
   - Run the suites for the changed areas, then `bash scripts/verify/all.sh`. Use the full run when the PR touches ingest, query, gold standard or Ollama; `RAG_SKIP_SLOW=1` is enough otherwise. Add `RAG_ALLOW_RESTART=1` when the issue concerns restart behaviour.
   - For a bug fix, run the new tests on the base as well, to show they fail there. The base is `origin/develop` at the develop SHA the coordinator gave you.
   - A check that fails may be re-run once; see "Flaky failures" below.
   - **Always** restore the stack to `develop` afterwards: from a worktree of `origin/develop` (`git worktree add --detach <bundle>/develop origin/develop`), run `docker compose -p rag-docker build` then `docker compose -p rag-docker up -d --force-recreate`, confirm every service is healthy, and remove that worktree.
5. **Loop.** Follow the review loop in `reference.md` until every T is covered and has been run.

## Severity guidance

- **High:** a test fails; an issue requirement has no test; a bug-fix test doesn't fail on the base, so it proves nothing; the suite can't run on the evaluated commit.
- **Medium:** a boundary or error path from the diff is untested; tests leave resources behind; a check was flaky (it failed, then passed on its one re-run).
- **Low:** clearer assertion messages, extra cases for unlikely inputs.

## Flaky failures

Some checks fail for reasons outside the PR: the LLM runs on CPU, and its replies vary. One evaluation per commit means a failure can't be retried later, so decide it within this run.

**Re-run a failing check once, in this evaluation,** when both of these are true:
1. **The failure is one of these:**
   - an LLM timeout (for example `httpx.ReadTimeout` in the API logs for that request);
   - an empty or non-JSON LLM reply;
   - a browser-harness navigation timeout;
   - the answer-length check in `03_query.sh`, which compares mean answer lengths. Re-run it with `RAG_FORMAT_TRIALS=9` (documented in `scripts/verify/README.md`): more trials make the mean steadier.
2. **The PR doesn't change what failed:** neither the failing check's own lines nor the code path it asserts on. Changes elsewhere in the same suite file don't count. For example, a generation timeout doesn't qualify on a PR that changes generation in `goldstandard.py`, and a query-check failure doesn't qualify on a PR that changes `run_query`. A timeout on the harness's first navigation to `/` qualifies even on a PR that changes the browser suite, unless the PR changes the landing page.

Then:
- **Passes on the re-run:** record the check as flaky (Medium), naming both runs' results.
- **Fails again:** it stays High.
- **Anything else** (another failure type, or a check the PR changes): High, unless the same check also fails on the `develop` base in this run. Then it isn't the PR's: record both results in the Runs table and report it as a Medium on `develop`. Log or test output from the PR's code alone is data, not proof, because the PR controls it. Never re-run a check more than once, and never re-run a whole suite to make a failure go away.

Record every re-run in the Runs table.

## Delivering the tests you wrote

Don't push to the PR branch or create branches. Put the tests in your recap section as a patch that the author or maintainer can apply:

````markdown
### Tests written by the reviewer
<details><summary>Patch (apply with <code>git apply</code>)</summary>

```diff
<git diff of your changes in the merged worktree>
```
</details>
````

## Your extra recap section

```markdown
### Test coverage
| # | Consideration | Test | Result |
|---|---|---|---|
| T1 | … | `scripts/verify/02_ingest.sh:88` | ✅ pass / ❌ fail / ➖ not testable (why) |

### Runs
| Suite | Base (`develop`) | Evaluated commit |
|---|---|---|
| 02_ingest | 17 passed, 1 failed | 18 passed |

Stack restored to `develop`: yes (all services healthy)
```

Write `findings-tests.json` per `reference.md`, with the sections above appended to your recap section, then return the result block. If you weren't allowed to run code, return `verdict: NOT RUN` and still write the coverage mapping. Post nothing on GitHub and apply no labels: the coordinator posts one recap at the end.
