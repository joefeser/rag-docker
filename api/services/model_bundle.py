"""Copying Ollama models into and out of an export package.

Ollama stores a model as a manifest plus content-addressed blobs:

    models/manifests/registry.ollama.ai/library/<name>/<tag>   (JSON)
    models/blobs/sha256-<hex>                                  (config + layers)

The manifest names every blob the model needs, so bundling resolves blobs
*through the manifest* rather than copying the blob store. The store here holds
2.3 GB across 9 blobs for two models; a naive copy would sweep up unrelated
weights, and on a machine with other models pulled it would be far worse.

Copying the files verbatim is deliberate. Offline there is no registry to pull
from, and rebuilding a model through Ollama's HTTP API would mean reconstructing
its template, params and license from the config blob — a reproduction, not the
model that produced the vectors.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from config import settings

_log = logging.getLogger(__name__)

REGISTRY = "registry.ollama.ai"
NAMESPACE = "library"
DEFAULT_TAG = "latest"


def _root() -> Path:
    return Path(settings.ollama_models_dir) / "models"


def split_ref(model: str) -> tuple[str, str]:
    """'phi3.5' -> ('phi3.5', 'latest'); 'phi3.5:3.8b' -> ('phi3.5', '3.8b')."""
    name, _, tag = model.partition(":")
    return name, (tag or DEFAULT_TAG)


def manifest_path(model: str) -> Path:
    name, tag = split_ref(model)
    return _root() / "manifests" / REGISTRY / NAMESPACE / name / tag


def blob_path(digest: str) -> Path:
    # Manifests write 'sha256:<hex>'; the filename on disk is 'sha256-<hex>'.
    return _root() / "blobs" / digest.replace(":", "-")


def store_available() -> bool:
    """False when the model store is not mounted, e.g. an older compose file."""
    return _root().is_dir()


def is_installed(model: str) -> bool:
    """A model counts as installed only if every blob it names is present."""
    mp = manifest_path(model)
    if not mp.is_file():
        return False
    try:
        for digest in _digests(json.loads(mp.read_text())):
            if not blob_path(digest).is_file():
                return False
    except (OSError, ValueError, KeyError):
        return False
    return True


def _digests(manifest: dict) -> list[str]:
    digests = []
    config = manifest.get("config") or {}
    if config.get("digest"):
        digests.append(config["digest"])
    for layer in manifest.get("layers") or []:
        if layer.get("digest"):
            digests.append(layer["digest"])
    return digests


def export_model(model: str, dest: Path) -> list[tuple[str, Path]]:
    """Files to place under `models/<model>/`, as (relative path, source).

    Returns them rather than copying so the caller can digest each file into the
    package manifest as it is written.
    """
    mp = manifest_path(model)
    if not mp.is_file():
        raise FileNotFoundError(f"Ollama has no manifest for '{model}' at {mp}")
    manifest = json.loads(mp.read_text())

    name, _ = split_ref(model)
    files: list[tuple[str, Path]] = [(f"models/{name}/manifest.json", mp)]
    for digest in _digests(manifest):
        blob = blob_path(digest)
        if not blob.is_file():
            raise FileNotFoundError(
                f"Model '{model}' references blob {digest} which is not in the store")
        files.append((f"models/{name}/blobs/{digest.replace(':', '-')}", blob))
    return files


def bundled_models(pkg: Path) -> list[str]:
    d = pkg / "models"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "manifest.json").is_file())


def install_model(pkg: Path, model: str) -> None:
    """Copy a bundled model into the live store, blobs before the manifest.

    Order matters: the manifest is what makes Ollama consider the model
    present, so writing it last means an interrupted install leaves unreferenced
    blobs rather than a model that cannot be served.
    """
    name, tag = split_ref(model)
    src = pkg / "models" / name
    src_manifest = src / "manifest.json"
    if not src_manifest.is_file():
        raise FileNotFoundError(f"Package does not bundle '{model}'")
    manifest = json.loads(src_manifest.read_text())

    blobs_dir = _root() / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    for digest in _digests(manifest):
        filename = digest.replace(":", "-")
        target = blobs_dir / filename
        if target.is_file():
            continue                      # content-addressed: identical by name
        source = src / "blobs" / filename
        if not source.is_file():
            raise FileNotFoundError(
                f"Bundled model '{model}' is missing blob {digest}")
        tmp = target.with_name(filename + ".partial")
        shutil.copyfile(source, tmp)
        tmp.replace(target)

    dest_manifest = manifest_path(model)
    dest_manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_manifest.with_name(tag + ".partial")
    shutil.copyfile(src_manifest, tmp)
    tmp.replace(dest_manifest)
    _log.info("Installed model %r from package", model)
