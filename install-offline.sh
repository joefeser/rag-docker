#!/usr/bin/env bash
#
# Install this project on a machine with NO internet access.
#
#   bash install-offline.sh
#
# Run it from inside the extracted bundle directory. It loads the pre-built
# Docker images, restores the Ollama model weights into the project's volume,
# and starts the stack without building or pulling anything.
#
# Requires only Docker Desktop (running) on the target Mac.

set -euo pipefail
cd "$(dirname "$0")"

[ -f offline/images.tar.gz ] || { echo "ERROR: offline/images.tar.gz missing. This is the source-only package, not the offline bundle (see package-offline.sh)." >&2; exit 1; }
[ -f offline/ollama_models.tar.gz ] || { echo "ERROR: offline/ollama_models.tar.gz missing." >&2; exit 1; }

docker info >/dev/null 2>&1 || { echo "ERROR: Docker is not running. Start Docker Desktop and retry." >&2; exit 1; }

echo "==> Loading Docker images (no network used)"
docker load -i offline/images.tar.gz

echo "==> Restoring Ollama model weights"
# `compose run` resolves the project's ollama_models volume for us, so this does
# not depend on the directory name. The ollama image ships tar and was just
# loaded above, so no extra image needs pulling.
docker compose run --rm --no-deps --entrypoint sh \
  -v "$PWD/offline:/backup:ro" \
  ollama -c 'tar xzf /backup/ollama_models.tar.gz -C /root/.ollama && ls /root/.ollama'

echo "==> Starting the stack (--no-build: nothing is compiled or pulled)"
docker compose up -d --no-build

echo
echo "Waiting for services to report healthy..."
for i in $(seq 1 60); do
  running=$(docker compose ps --services --filter status=running | wc -l | tr -d ' ')
  [ "$running" = 5 ] && break
  sleep 5
done
docker compose ps

echo
echo "If all five services are up, open http://localhost:8080"
echo "Verify the models were restored (no download should occur):"
echo "  docker compose exec ollama ollama list"
