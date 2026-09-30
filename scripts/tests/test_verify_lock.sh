#!/usr/bin/env bash
# Tests scripts/verify/lock.sh's mutual-exclusion behaviour (#95, #97).
#
# T1-T4 need no stack: they exercise lock.sh directly through a scratch
# RAG_VERIFY_LOCK path, never the real /tmp/rag-verify.lock. T5 needs a live
# stack (01_infrastructure.sh calls require_stack) and is skipped without one.
#
# Run: bash scripts/tests/test_verify_lock.sh
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 2
REPO_ROOT="$(pwd)"
VERIFY="$REPO_ROOT/scripts/verify"

TESTLOCK_HOME=$(mktemp -d "${TMPDIR:-/tmp}/rag-lock-test.XXXXXX")
cleanup() { rm -rf "$TESTLOCK_HOME"; }
trap cleanup EXIT

# Borrow lib.sh's check/check_eq/skip/section/summary helpers without taking
# the real lock: point RAG_VERIFY_LOCK at a scratch path first, then drop what
# sourcing it acquired so the tests below start from a clean slate.
export RAG_VERIFY_LOCK="$TESTLOCK_HOME/harness.lock"
# shellcheck disable=SC1091
. "$VERIFY/lib.sh"
unset RAG_VERIFY_LOCK_HELD
rm -rf "$RAG_VERIFY_LOCK"

section "verify/lock.sh — mutual exclusion"

# --- T1: a second run is refused with exit 3, naming the holder's pid. -----
L1="$TESTLOCK_HOME/t1.lock"
RAG_VERIFY_LOCK="$L1" bash -c '. "'"$VERIFY"'/lock.sh"; sleep 5' &
holder_pid=$!
waited=0
while [ ! -f "$L1/pid" ] && [ "$waited" -lt 50 ]; do sleep 0.1; waited=$((waited + 1)); done
held_pid=$(cat "$L1/pid" 2>/dev/null || true)
check_eq "T1: lock file records the holder's own pid" "$held_pid" "$holder_pid"

out1=$(RAG_VERIFY_LOCK="$L1" bash -c '. "'"$VERIFY"'/lock.sh"; echo ran' 2>&1)
rc1=$?
check_eq "T1: a second run while the lock is held exits 3" "$rc1" "3"
case "$out1" in
  *"$held_pid"*) named=0 ;;
  *) named=1 ;;
esac
check "T1: the refusal names the holder's pid" "$named" "expected pid $held_pid in: $out1"
case "$out1" in
  *ran*) leaked=1 ;;
  *) leaked=0 ;;
esac
check "T1: the second run's own commands never execute" "$leaked" "output: $out1"

wait "$holder_pid" 2>/dev/null

# --- T2: a child suite started under an already-held lock still runs. ------
L2="$TESTLOCK_HOME/t2.lock"
out2=$(RAG_VERIFY_LOCK="$L2" bash -c '
  . "'"$VERIFY"'/lock.sh"
  RAG_VERIFY_LOCK="'"$L2"'" bash -c ". \"'"$VERIFY"'/lock.sh\"; echo child-ran"
' 2>&1)
rc2=$?
check_eq "T2: a suite sourced under an already-held lock (RAG_VERIFY_LOCK_HELD) exits 0" "$rc2" "0"
case "$out2" in
  *child-ran*) ran=0 ;;
  *) ran=1 ;;
esac
check "T2: the child suite's own commands ran (not refused as a second holder)" "$ran" "output: $out2"

# --- T3: the lock is released when the holder exits. ------------------------
L3="$TESTLOCK_HOME/t3.lock"
RAG_VERIFY_LOCK="$L3" bash -c '. "'"$VERIFY"'/lock.sh"; exit 0'
[ ! -e "$L3" ]
check "T3: the lock directory is gone after the holder exits normally" "$?" "still present: $L3"

# --- T4: a stale lock (dead pid) is taken over, not refused. ---------------
L4="$TESTLOCK_HOME/t4.lock"
mkdir "$L4"
bash -c 'exit 0' &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
echo "$dead_pid" >"$L4/pid"
out4=$(RAG_VERIFY_LOCK="$L4" bash -c '. "'"$VERIFY"'/lock.sh"; echo took-over; cat "'"$L4"'/pid"' 2>&1)
rc4=$?
check_eq "T4: sourcing lock.sh against a stale (dead-pid) lock exits 0" "$rc4" "0"
case "$out4" in
  *took-over*) took=0 ;;
  *) took=1 ;;
esac
check "T4: the run proceeds past the stale lock instead of being refused" "$took" "output: $out4"
new_pid=$(printf '%s\n' "$out4" | tail -n1)
[ "$new_pid" != "$dead_pid" ] && [ -n "$new_pid" ]
check "T4: the pid file is rewritten with the new holder's pid" "$?" "old=$dead_pid new=$new_pid"

# --- T5: 01_infrastructure.sh sets its own EXIT trap, which replaces ------
# lock.sh's cleanup trap (bash's `trap ... EXIT` does not chain), so a
# standalone run leaves the lock directory behind with a now-dead pid; the
# next run must still take it over rather than being refused. Needs a live
# stack, since 01_infrastructure.sh calls require_stack.
section "verify/lock.sh vs. 01_infrastructure.sh's own EXIT trap"
API="${RAG_API:-http://localhost:8080/api}"
health_code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$API/health" 2>/dev/null)
if [ "$health_code" != "200" ]; then
  skip "T5: 01_infrastructure.sh EXIT-trap interaction" "no live stack at $API (HTTP $health_code)"
else
  L5="$TESTLOCK_HOME/t5.lock"
  T5_OUT="$TESTLOCK_HOME/t5.out"
  # 01 inspects the stack's containers through compose, which would otherwise
  # name the project after whatever folder this checkout is in.
  RAG_VERIFY_LOCK="$L5" RAG_API="$API" COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-rag-docker}" \
    bash "$VERIFY/01_infrastructure.sh" >"$T5_OUT" 2>&1
  rc5=$?
  [ "$rc5" = 0 ]
  check "T5: 01_infrastructure.sh run standalone completes" "$?" "exit $rc5; see $T5_OUT"
  [ -d "$L5" ]
  check "T5: its own EXIT trap replaces lock.sh's, so the lock dir is left behind" "$?" "expected $L5 to still exist"
  # (Its pid is expected to be dead by now, since the run above already
  # finished; not asserted directly, since a long run gives the OS time to
  # recycle the pid and that would flake the test. T5's real assertion is the
  # next one: the stale lock still gets taken over, never refused.)
  out5=$(RAG_VERIFY_LOCK="$L5" bash -c '. "'"$VERIFY"'/lock.sh"; echo second-run-ok' 2>&1)
  rc5b=$?
  check_eq "T5: a second run afterwards still succeeds (stale takeover, not refusal)" "$rc5b" "0"
  case "$out5" in
    *second-run-ok*) ok5=0 ;;
    *) ok5=1 ;;
  esac
  check "T5: the second run actually proceeds" "$ok5" "output: $out5"
fi

summary
