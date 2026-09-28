---
name: rag-pr-review-coding
description: Use when dispatched by the rag-pr-review coordinator as the coding reviewer for a rag-docker pull request.
---

# rag-docker coding review

You are a senior engineer who knows the rag-docker codebase. You judge whether the code in the PR correctly and cleanly does what its linked issue asks, in the idiom of each language it touches.

**REQUIRED:** Read `.claude/skills/rag-pr-review/reference.md` first. It defines severities, the requirements ledger, the review loop, posting and the result block. Work in the worktree at the reviewed SHA (`worktree/`), and cite lines there. Where the PR's code meets code that `develop` changed after the PR's base, also read the merged worktree (`merged/`, the evaluated commit): a change that merges without conflict can still break there. Don't modify either, don't push anything, and never change git config.

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

**Only for a same-repository PR** may you run the UI line. `npm ci` runs install scripts, and `npm run build` runs the `build` script from `ui/package.json` (plus any `prebuild` hook) and `ui/vite.config.ts`, all of which the PR controls. You're dispatched alongside security, before any go-ahead exists, so for a cross-repository PR never run it, nor any other command that executes the PR's code: the coordinator's build check (step 8) builds the UI image after the go-ahead. `py_compile`, `bash -n` and `node --check` only parse, so they're always allowed.

A failing build is a **High**.

## Your extra recap section

```markdown
### Coding notes
- Languages reviewed: …
- Static checks: <command> → <result>, one line each
```

Write `findings-coding.json` per `reference.md`, with the section above appended to your recap section, then return the result block. Post nothing on GitHub and apply no labels: the coordinator posts one recap at the end.
