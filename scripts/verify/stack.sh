#!/usr/bin/env bash
#
# Run the verification suite on a disposable compose project, never on the
# live stack (#152).
#
#   bash scripts/verify/stack.sh run [--checkout DIR] [suite ...]
#   bash scripts/verify/stack.sh up [--checkout DIR] [--pull]
#   bash scripts/verify/stack.sh down
#
# `run` brings the verify project up, runs that checkout's all.sh against it
# (RAG_SKIP_SLOW and RAG_ALLOW_RESTART pass through) and always tears it down,
# exiting with all.sh's status. `up` leaves the project running and prints the
# environment that points docker compose and the suites at it; `down` removes
# it. --checkout picks the checkout to build and test (default: this one).
#
# The verify project is compose project `rag-verify` on port RAG_VERIFY_PORT
# (default 8081; 8080 is refused), with its own empty volumes, its own image
# tags (rag-verify-api, rag-verify-ui) and its own exports folder. The live
# `rag-docker` stack holds real data, and nothing here builds, starts, stops or
# writes to it. Its only contact with it: the Ollama models are copied from
# rag-docker_ollama_models, mounted read-only in a throwaway container, into
# the volume rag-verify-ollama-models, which is kept between runs and checked
# by sha256 on every `up`.
#
# The overlay docker-compose.verify.yml and this script come from the checkout
# that holds this script, so an evaluation can run a trusted copy against a PR's
# checkout. Before anything is built, the resolved configuration is checked: a
# compose file that names a live volume or image, publishes another port, binds
# a path outside the checkout or mounts the Docker socket is refused.
#
# What this does not do: the suites, and anything else the checkout runs on the
# host, still have full access to Docker. It keeps verification away from the
# live stack by accident, not from hostile code; that is the security review's.
set -uo pipefail

PROJECT=rag-verify
LIVE_MODELS=rag-docker_ollama_models
MODELS=rag-verify-ollama-models
HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SCRATCH="${TMPDIR:-/tmp}"
SCRATCH="${SCRATCH%/}"
[ -n "$SCRATCH" ] || SCRATCH=/tmp
EXPORTS="$SCRATCH/rag-verify-exports"

usage() {
  sed -n '6,8p' "${BASH_SOURCE[0]}" | sed 's/^#  //' >&2
  exit 2
}

fail() {
  printf '\nstack.sh: %s\n\n' "$1" >&2
  exit 2
}

# ── arguments, checked before any Docker command ─────────────────────────────
[ "$#" -ge 1 ] || usage
CMD="$1"; shift
case "$CMD" in up|down|run) ;; *) usage ;; esac
CHECKOUT="$HARNESS"
PULL=""
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --checkout)
      [ "$CMD" != down ] && [ "$#" -ge 2 ] || usage
      CHECKOUT="$2"; shift 2 ;;
    --pull)
      [ "$CMD" = up ] || usage
      PULL=1; shift ;;
    -*) usage ;;
    *)
      [ "$CMD" = run ] || usage
      ARGS+=("$1"); shift ;;
  esac
done

PORT="${RAG_VERIFY_PORT:-8081}"
case "$PORT" in
  ''|*[!0-9]*) fail "RAG_VERIFY_PORT must be a port number, not '$PORT'." ;;
esac
[ "${#PORT}" -le 5 ] || fail "RAG_VERIFY_PORT must be from 1024 to 65535, not $PORT."
PORT=$((10#$PORT))
{ [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ]; } || fail "RAG_VERIFY_PORT must be from 1024 to 65535, not $PORT."
[ "$PORT" -ne 8080 ] || fail "RAG_VERIFY_PORT can't be 8080: that is the live rag-docker stack's port."

if [ "$CMD" != down ]; then
  { [ -d "$CHECKOUT" ] && [ -f "$CHECKOUT/docker-compose.yml" ]; } \
    || fail "--checkout must be a checkout of this project (a folder with docker-compose.yml): $CHECKOUT"
  CHECKOUT="$(cd "$CHECKOUT" && pwd -P)"
fi

# ── one verify run per machine ───────────────────────────────────────────────
# Held for the whole command; the all.sh started below inherits it.
. "$HARNESS/scripts/verify/lock.sh"
TEARDOWN=0
on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$TEARDOWN" = 1 ]; then
    TEARDOWN=0
    do_down || rc=2
  fi
  _rag_lock_release
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── the verify environment ───────────────────────────────────────────────────
unset COMPOSE_PATH_SEPARATOR
export COMPOSE_PROJECT_NAME="$PROJECT"
export COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$HARNESS/docker-compose.verify.yml"
export RAG_VERIFY_PORT="$PORT"
export RAG_API="http://localhost:$PORT/api"
export RAG_EXPECTED_PROXY_PORT="$PORT"
export RAG_EXPORTS_DIR="$EXPORTS"

# ── down: remove everything of the verify project, except the model copy ────
do_down() {
  local filter="label=com.docker.compose.project=$PROJECT" ids images="" img left
  # Project name only, from a neutral folder: no compose file is needed.
  (cd / && env -u COMPOSE_FILE docker compose -p "$PROJECT" down -v --remove-orphans) >/dev/null 2>&1
  ids=$(docker ps -aq --filter "$filter")
  [ -z "$ids" ] || docker rm -f $ids >/dev/null
  ids=$(docker network ls -q --filter "$filter")
  [ -z "$ids" ] || docker network rm $ids >/dev/null
  ids=$(docker volume ls -q --filter "$filter" | grep -vx "$MODELS")
  [ -z "$ids" ] || docker volume rm $ids >/dev/null
  for img in rag-verify-api:latest rag-verify-ui:latest; do
    docker image inspect "$img" >/dev/null 2>&1 && images="$images $img"
  done
  [ -z "$images" ] || docker image rm $images >/dev/null
  if [ -L "$EXPORTS" ]; then
    printf 'stack.sh: %s is a symlink; not removing it.\n' "$EXPORTS" >&2
    return 2
  fi
  [ ! -d "$EXPORTS" ] || rm -rf "$EXPORTS"
  left="$(docker ps -aq --filter "$filter")$(docker network ls -q --filter "$filter")$(docker volume ls -q --filter "$filter" | grep -vx "$MODELS")"
  for img in rag-verify-api:latest rag-verify-ui:latest; do
    docker image inspect "$img" >/dev/null 2>&1 && left="$left $img"
  done
  if [ -n "$left" ]; then
    printf 'stack.sh: the verify project was not fully removed: %s\n' "$(printf '%s' "$left" | tr '\n' ' ')" >&2
    return 2
  fi
  printf 'verify project %s removed (the model copy %s is kept)\n' "$PROJECT" "$MODELS"
}

# ── the models: a copy of the live store, checked on every up ────────────────
# The image that runs the copy is the ollama image pinned by the harness's own
# docker-compose.yml, already on this machine, not one the checkout picks.
seed_image() {
  python3 - "$HARNESS/docker-compose.yml" <<'IMAGEPY'
import sys
inside = False
for line in open(sys.argv[1]):
    if line.rstrip('\n') == '  ollama:':
        inside = True
    elif inside and line.startswith('  ') and not line.startswith('   ') and line.strip():
        break
    elif inside and line.startswith('    image:'):
        print(line.split(':', 1)[1].strip().strip('"\''))
        sys.exit(0)
sys.exit(1)
IMAGEPY
}

# Runs in the throwaway container: /live is the live store (read-only), /copy
# the verify copy. Every file is compared by sha256 and copied when missing or
# different; files the live store doesn't have are removed.
SYNC='set -euo pipefail
copied=0; repaired=0; removed=0
cd /live
while IFS= read -r -d "" f; do
  f="${f#./}"
  want=$(sha256sum "/live/$f" | cut -d" " -f1)
  case "$f" in
    */blobs/sha256-*) [ "${f##*/sha256-}" = "$want" ] || echo "warning: live blob $f does not match its name" >&2 ;;
  esac
  if [ -f "/copy/$f" ] && [ ! -L "/copy/$f" ]; then
    [ "$(sha256sum "/copy/$f" | cut -d" " -f1)" = "$want" ] && continue
    repaired=$((repaired + 1))
  else
    copied=$((copied + 1))
  fi
  mkdir -p "/copy/$(dirname "$f")"
  rm -rf "/copy/$f"
  cp -p "/live/$f" "/copy/$f"
done < <(find . -type f -print0)
cd /copy
while IFS= read -r -d "" f; do
  f="${f#./}"
  if [ ! -f "/live/$f" ] || [ -L "/live/$f" ]; then rm -f "/copy/$f"; removed=$((removed + 1)); fi
done < <(find . \( -type f -o -type l \) -print0)
find /copy -mindepth 1 -type d -empty -delete
echo "models: copied=$copied repaired=$repaired removed=$removed"'

do_seed() {
  local img
  if ! docker volume inspect "$MODELS" >/dev/null 2>&1; then
    docker volume create --label rag-verify.models=1 "$MODELS" >/dev/null || fail "could not create the volume $MODELS."
  fi
  if ! docker volume inspect "$LIVE_MODELS" >/dev/null 2>&1; then
    printf 'note: there is no %s volume to copy the models from, so the verify\n' "$LIVE_MODELS"
    printf 'project'"'"'s ollama will pull them into %s (this needs the internet).\n' "$MODELS"
    return 0
  fi
  img=$(seed_image) || fail "could not find the ollama image in $HARNESS/docker-compose.yml."
  docker image inspect "$img" >/dev/null 2>&1 \
    || fail "the image $img, used to copy the models, isn't on this machine; it isn't pulled for this."
  printf 'checking the model copy %s against %s (read-only)...\n' "$MODELS" "$LIVE_MODELS"
  docker run --rm --network none --entrypoint /bin/bash \
    -v "$LIVE_MODELS:/live:ro" -v "$MODELS:/copy" "$img" -c "$SYNC" \
    || fail "copying the models into $MODELS failed."
}

# ── the guard on the resolved configuration ──────────────────────────────────
do_guard() {
  local config rc
  config=$(mktemp "$SCRATCH/rag-verify-config.XXXXXX") || fail "could not create a scratch file."
  if ! docker compose -p "$PROJECT" config --format json > "$config"; then
    rm -f "$config"
    fail "docker compose config failed for $CHECKOUT."
  fi
  python3 - "$CHECKOUT" "$EXPORTS" "$PORT" "$config" <<'GUARDPY'
import json, os, sys

checkout, exports, port, path = sys.argv[1:5]
config = json.load(open(path))
services = config.get('services') or {}
volumes = config.get('volumes') or {}
real = os.path.realpath
problems = []

def inside(child, parent):
    child, parent = real(child), real(parent)
    return child == parent or child.startswith(parent.rstrip('/') + '/')

# (a) volumes: the project's own, or the external model copy, nothing else
for key, volume in volumes.items():
    volume = volume or {}
    name = volume.get('name')
    if key == 'ollama_models' or name == 'rag-verify-ollama-models':
        ok = (name == 'rag-verify-ollama-models' and volume.get('external') is True
              and not volume.get('driver_opts'))
    else:
        ok = (name == 'rag-verify_' + key and not volume.get('external')
              and not volume.get('driver_opts'))
    if not ok:
        problems.append(f"(a) volume {key!r} resolves to {name!r}"
                        f"{' (external)' if volume.get('external') else ''}"
                        f"{' (driver_opts)' if volume.get('driver_opts') else ''}; "
                        f"only rag-verify_{key} or the external rag-verify-ollama-models are allowed")
for svc, service in services.items():
    for mount in service.get('volumes') or []:
        if mount.get('type') == 'volume' and mount.get('source') and mount['source'] not in volumes:
            problems.append(f"(a) service {svc!r} mounts undeclared volume {mount['source']!r}")

# (b) images: never the live stack's tags
for svc, service in services.items():
    image = service.get('image') or ''
    if image.startswith('rag-docker-') or image.startswith('rag-docker:'):
        problems.append(f"(b) service {svc!r} uses the live image {image!r}")
for svc, want in (('api', 'rag-verify-api:latest'), ('ui', 'rag-verify-ui:latest')):
    if svc in services and services[svc].get('image') != want:
        problems.append(f"(b) service {svc!r} must use {want}, not {services[svc].get('image')!r}")

# (c) exactly one published port: the proxy, on loopback, the verify port
ports = [(svc, p) for svc, service in services.items() for p in service.get('ports') or []]
if not (len(ports) == 1 and ports[0][0] == 'proxy'
        and ports[0][1].get('host_ip') == '127.0.0.1'
        and str(ports[0][1].get('published')) == port
        and ports[0][1].get('target') == 80
        and ports[0][1].get('protocol', 'tcp') == 'tcp'):
    shown = ', '.join(f"{svc}:{p.get('host_ip', '')}:{p.get('published')}->{p.get('target')}/{p.get('protocol', 'tcp')}"
                      for svc, p in ports) or 'none'
    problems.append(f"(c) published ports must be exactly proxy:127.0.0.1:{port}->80/tcp, not {shown}")

# (d) binds only from the checkout or the exports folder; (e) never the socket
sockets = {'/var/run/docker.sock', '/run/docker.sock'}
for svc, service in services.items():
    for mount in service.get('volumes') or []:
        source, target = mount.get('source') or '', mount.get('target') or ''
        if source in sockets or target in sockets or (source and real(source) in {real(s) for s in sockets}):
            problems.append(f"(e) service {svc!r} mounts the Docker socket")
            continue
        if mount.get('type') == 'bind' and not (inside(source, checkout) or real(source) == real(exports)):
            problems.append(f"(d) service {svc!r} binds {source!r}, outside the checkout and the exports folder")

# (f) the API's exports are the verify project's own folder
mounts = [m for m in (services.get('api', {}).get('volumes') or []) if m.get('target') == '/app/exports']
if not (len(mounts) == 1 and mounts[0].get('type') == 'bind' and real(mounts[0].get('source') or '/') == real(exports)):
    problems.append(f"(f) the api's /app/exports must be bound from {exports}, not "
                    f"{[m.get('source') for m in mounts] or 'nothing'}")

if problems:
    print('The resolved compose configuration is refused:')
    for problem in problems:
        print('  ' + problem)
    sys.exit(2)
GUARDPY
  rc=$?
  rm -f "$config"
  [ "$rc" -eq 0 ] || fail "the verify project's configuration is refused (see above); nothing was built or started."
}

# ── up ───────────────────────────────────────────────────────────────────────
do_up() {
  local i code=000 service state health
  printf 'clearing any leftover verify project...\n'
  do_down >/dev/null || fail "could not remove a leftover verify project."
  mkdir -p "$EXPORTS" || fail "could not create $EXPORTS."
  do_seed
  do_guard
  printf 'building the verify project from %s...\n' "$CHECKOUT"
  docker compose -p "$PROJECT" build ${PULL:+--pull} || fail "the build failed."
  if ! docker compose -p "$PROJECT" up -d --wait --wait-timeout 900; then
    # Weaviate can be slow to report healthy (#130): one more try.
    printf 'the first start failed; trying once more...\n'
    if ! docker compose -p "$PROJECT" up -d --wait --wait-timeout 900; then
      docker compose -p "$PROJECT" ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
        | while read -r service state health; do
            case "$state/${health:-}" in
              running/healthy|running/) ;;
              *) printf '\n── %s (%s %s) ──\n' "$service" "$state" "${health:-}"
                 docker compose -p "$PROJECT" logs --tail 50 "$service" ;;
            esac
          done
      fail "the verify project did not come up; it is left running for inspection (stack.sh down removes it)."
    fi
  fi
  for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$RAG_API/health" 2>/dev/null)
    [ "$code" = 200 ] && break
    sleep 1
  done
  [ "$code" = 200 ] || fail "$RAG_API/health did not return 200 within 60 seconds (last: $code)."
  printf '\nThe verify project is up at http://localhost:%s. To point commands at it:\n\n' "$PORT"
  for var in COMPOSE_PROJECT_NAME COMPOSE_FILE RAG_API RAG_EXPECTED_PROXY_PORT RAG_EXPORTS_DIR RAG_VERIFY_PORT; do
    printf 'export %s=%q\n' "$var" "${!var}"
  done
  printf '\n'
}

case "$CMD" in
  down)
    do_down || exit 2 ;;
  up)
    do_up ;;
  run)
    TEARDOWN=1
    do_up
    rc=0
    bash "$CHECKOUT/scripts/verify/all.sh" ${ARGS[@]+"${ARGS[@]}"} || rc=$?
    exit "$rc" ;;
esac
