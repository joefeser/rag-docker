#!/usr/bin/env bash
#
# Build a clean, distributable zip of this project.
#
#   bash package.sh [output.zip]
#
# Invoke with `bash package.sh` rather than `./package.sh` so the script does
# not depend on its own executable bit surviving a file transfer.
#
# What the archive contains: source, Dockerfiles, compose file, both lock files
# (ui/package-lock.json and api/requirements.txt) and the docs. That is
# everything needed to rebuild the stack from scratch.
#
# What it deliberately does NOT contain: Docker images, the Weaviate index, and
# the Ollama model weights. Those are rebuilt or re-downloaded on the target
# machine -- roughly 8-10 GB of network traffic on first run.
#
# THE TARGET MACHINE MUST HAVE INTERNET ACCESS for this package to work.
# For an air-gapped install, use package-offline.sh instead: it ships the
# pre-built images and model weights, and needs no network on the target.
# See "Moving to another Mac" in README.md.

set -euo pipefail

cd "$(dirname "$0")"

OUT="${1:-rag-docker.zip}"
rm -f "$OUT"

zip -r -q "$OUT" . \
  -x '*.DS_Store' \
  -x '__MACOSX/*' \
  -x '*/node_modules/*' \
  -x '*/__pycache__/*' \
  -x '*.pyc' \
  -x '*.pyo' \
  -x '*/dist/*' \
  -x '*/.venv/*' \
  -x '*/venv/*' \
  -x '*/.pytest_cache/*' \
  -x '*/.mypy_cache/*' \
  -x '*/.ruff_cache/*' \
  -x '*/uploads/*' \
  -x 'ingest-inbox/*' \
  -x 'exports/*' \
  -x '.env' \
  -x '*/.env' \
  -x '*/.env.*' \
  -x '.git/*' \
  -x '*.zip'

# The blanket ingest-inbox exclusion also drops the directory itself. Add the
# placeholder back so the bind-mount target exists on the target machine; without
# it Docker creates the directory as root and the user cannot drop files in.
zip -q "$OUT" ingest-inbox/.gitkeep
# Same for ./exports: it is the bind mount export packages are written to.
# Its contents are somebody's corpus and never belong in a source archive,
# but the directory itself must exist or Docker creates it as root.
zip -q "$OUT" exports/.gitkeep

echo "Created $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "On the target Mac:"
echo "  unzip $OUT -d rag-docker && cd rag-docker"
echo "  docker compose up -d"
echo
echo "First run pulls base images, builds both services and downloads ~2.5 GB of"
echo "model weights, so the target machine needs internet access. Allow 20 GB of"
echo "Docker disk (32 GB recommended) -- see README.md."
echo
echo "No internet on the target? Use: bash package-offline.sh"
