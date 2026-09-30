#!/usr/bin/env bash
# One verify run at a time against a stack.
#
# Sourced by all.sh and lib.sh, never executed. Every run shares the $PREFIX
# collection names, the /tmp/vfy_*.json scratch files and the fixtures folder,
# which all.sh deletes and rebuilds when it starts. Two runs at once overwrite
# each other: one run's upload finds its fixture gone, or its chunks land in the
# other run's recreated collection (#95).
#
# The first script to source this holds the lock for its whole process tree:
# it exports RAG_VERIFY_LOCK_HELD, so the suites all.sh starts don't try again.
# A lock left by a run that died is taken over once its process is gone.

if [ -z "${RAG_VERIFY_LOCK_HELD:-}" ]; then
  RAG_VERIFY_LOCK="${RAG_VERIFY_LOCK:-/tmp/rag-verify.lock}"
  if ! mkdir "$RAG_VERIFY_LOCK" 2>/dev/null; then
    _holder=$(cat "$RAG_VERIFY_LOCK/pid" 2>/dev/null || true)
    if [ -n "$_holder" ] && kill -0 "$_holder" 2>/dev/null; then
      printf '\nAnother verify run (pid %s) is using this machine. Wait for it to finish.\n\n' "$_holder" >&2
      exit 3
    fi
    rm -rf "$RAG_VERIFY_LOCK"
    mkdir "$RAG_VERIFY_LOCK" 2>/dev/null || { printf '\nCould not take the verify lock %s.\n\n' "$RAG_VERIFY_LOCK" >&2; exit 3; }
  fi
  echo $$ > "$RAG_VERIFY_LOCK/pid"
  export RAG_VERIFY_LOCK_HELD=1
  # Released on exit. A suite that sets its own EXIT trap replaces this one;
  # the lock is then taken over by the next run, because its pid is gone.
  trap 'rm -rf "$RAG_VERIFY_LOCK"' EXIT
fi
