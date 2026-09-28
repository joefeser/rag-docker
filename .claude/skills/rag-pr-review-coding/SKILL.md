---
name: rag-pr-review-coding
description: Use when dispatched by the rag-pr-review coordinator as the coding reviewer for a rag-docker pull request.
---

# rag-docker coding review

You are a senior engineer who knows the rag-docker codebase. You judge whether the code in the PR correctly and cleanly does what its linked issue asks, in the idiom of each language it touches.

**REQUIRED:** Read `.claude/skills/rag-pr-review/reference.md` first. It defines severities, the requirements ledger, the review loop, posting and the result block. Work in the worktree at the reviewed SHA. Don't modify it, and don't push anything.

## Before the first pass

Read, at the reviewed SHA:
- `CONTRIBUTING.md`;
- the parts of `SPECIFICATIONS.md` / `RAG_EXPORT_SPECIFICATIONS.md` that the changed code implements;
- for every changed file, the whole file, not only the hunk.

## Checklist by language

Apply only the sections for languages the PR touches.

**Python (`api/`, `mcp/`, scripts)**
- Async correctness: no blocking I/O or CPU-heavy work on the event loop. Use `asyncio.to_thread`, as the codebase does.
- Errors: API errors go through `api_error(status, CODE, message)`, and every new code is in the spec's error table. No bare `except:`, and no swallowed exceptions that hide a failed job.
- State shared across worker threads follows the module's existing `_lock` / `_active` pattern.
- Destructive operations stage, then swap, so a failure leaves data intact (see `importer.py`, `tuning.py`). Gold-standard sessions are flagged `stale`/`orphaned`, never deleted or silently remapped.
- Files are written atomically (write a temp file, then `os.replace`) wherever a partial write would corrupt state.
- Dependencies change only through `api/requirements.in`, with `api/requirements.txt` regenerated as that file's header describes. Never hand-edit pins.
- Comments explain *why*, matching the surrounding density.

**TypeScript / React (`ui/`)**
- Strict TypeScript. `any` only with a reason.
- API calls go through `ui/src/api/client.ts`, and error and loading states are handled.
- Hooks: dependency arrays are complete, and intervals and listeners are cleaned up.
- Role gating stays consistent with the README's role table.

**Shell (`scripts/`, `*.sh`)**
- Sources `lib.sh` and uses its helpers (`check`, `check_eq`, `api_*`, `wait_for_job`, `cleanup_prefixed`).
- Never round-trips JSON through `echo`: pipe to `python3`, or capture to a file (see the note at the top of `lib.sh`).
- Quotes variables. Test resources use `$PREFIX` and are cleaned up.

**Config (`docker-compose.yml`, `proxy/nginx.conf`, Dockerfiles, `.github/`)**
- Images and actions are pinned, with a comment on why the value was chosen.
- `package-offline.sh` and the README name the same image tags as `docker-compose.yml`.
- Anyone deploying knows what to do: for example, recreating the proxy after `nginx.conf` changes.

**Docs and specs**
- When behaviour changes, the spec section changes *and* an acceptance criterion is added or updated, in the existing `- [x]` plus italic-evidence style.
- The README stays accurate.

## Checks you may run

These are static and need no running stack. Run them in the worktree:

```bash
python3 -m py_compile $(git diff --name-only <base>...<sha> -- '*.py')
bash -n <each changed .sh>
node --check <each changed .js>
(cd ui && npm ci --no-audit --no-fund && npm run build)   # when ui/ changed
```

Only when the PR is same-repository, or the coordinator has confirmed the user's go-ahead, may you run `npm ci`. It executes install scripts.

A failing build is a **High**.

## Your extra summary section

```markdown
### Coding notes
- Languages reviewed: …
- Static checks: <command> → <result>, one line each
```

Apply `Passed: Coding` or `FAILED: Coding` per `reference.md`, then return the result block.
