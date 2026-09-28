# RAG Docker — Implementation

**Version:** 1.2  
**Date:** 2026-09-10  
**Depends on:** SPECIFICATIONS.md v1.4

---

## 1. Quick Start

```bash
# Clone and enter the project directory
cd rag-docker

# Start the full stack. No chmod is needed: the ollama service runs
# ["/bin/bash", "/entrypoint.sh"], so the script's executable bit is irrelevant.
docker compose up -d

# Tail logs to watch progress
docker compose logs -f ollama

# Once healthy, open the UI (the proxy publishes 8080 on the host)
open http://localhost:8080
```

On first run Docker pulls ~3.8 GB of base images, builds the api and ui images,
then Ollama downloads `phi3.5` (2.2 GB) and `nomic-embed-text` (274 MB) — roughly
10–20 minutes in total, almost entirely network-bound. Subsequent starts are
immediate because the images are built and the models are cached in the
`ollama_models` Docker volume.

**Docker must be allocated at least 10 GB of memory** (Settings → Resources →
Memory). phi3.5 is ~6 GB resident; below that it is evicted and reloaded between
calls and queries time out. `curl http://localhost:8080/api/health` reports the
allocated figure against the recommendation.

---

## 2. Infrastructure

### docker-compose.yml

```yaml
services:
  weaviate:
    # 1.27.0 is the minimum supported by weaviate-client 4.23.1 (pinned in
    # api/requirements.txt); 1.25.x fails at connect with WeaviateStartUpError.
    image: semitechnologies/weaviate:1.39.6
    environment:
      QUERY_DEFAULTS_LIMIT: 25
      AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: 'true'
      PERSISTENCE_DATA_PATH: /var/lib/weaviate
      ENABLE_MODULES: 'text2vec-ollama'
      TEXT2VEC_OLLAMA_APIENDPOINT: http://ollama:11434
      TEXT2VEC_OLLAMA_MODEL: nomic-embed-text
      # Weaviate 1.25 persists Raft cluster state keyed by node identity. Without
      # a fixed CLUSTER_HOSTNAME it derives that identity from the container, so
      # the IP recorded in weaviate_data no longer matches after `compose down`
      # and startup dies with:
      #   "could not open cloud meta store: bootstrap: context deadline exceeded"
      # Pinning the name keeps the identity stable across container recreation.
      CLUSTER_HOSTNAME: 'node1'
      RAFT_BOOTSTRAP_EXPECT: 1
    volumes:
      - weaviate_data:/var/lib/weaviate
    ports: []
    networks: [rag-internal]
    healthcheck:
      # The weaviate image has no curl; busybox wget is what it ships.
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8080/v1/.well-known/ready"]
      interval: 10s
      timeout: 5s
      retries: 10

  ollama:
    image: ollama/ollama:0.3.14
    # Invoked via bash rather than as ["/entrypoint.sh"] so the script does not
    # need its executable bit. That bit is not reliably preserved when the
    # project is distributed as a zip (Finder compress, cloud storage, or a
    # Windows machine in the transfer path can all drop it), which would
    # otherwise fail with "permission denied" on a fresh install.
    entrypoint: ["/bin/bash", "/entrypoint.sh"]
    volumes:
      - ollama_models:/root/.ollama
      - ./ollama/entrypoint.sh:/entrypoint.sh:ro
    networks: [rag-internal]
    healthcheck:
      # The ollama image has no curl/wget; the CLI is the only probe available.
      test: ["CMD-SHELL", "ollama list | grep -q phi3.5 && ollama list | grep -q nomic-embed-text"]
      interval: 20s
      timeout: 15s
      retries: 20
      start_period: 120s
    # Optional GPU support (uncomment on GPU host):
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - capabilities: [gpu]

  api:
    build: ./api
    # Explicit tag so the image name does not depend on the directory name.
    # Without it compose derives "<project>-api" from the folder, and an
    # offline install extracted to a differently named folder would not find
    # the loaded image and would try to rebuild (which needs the internet).
    image: rag-docker-api:latest
    environment:
      WEAVIATE_HOST: weaviate
      WEAVIATE_PORT: 8080
      OLLAMA_HOST: ollama
      OLLAMA_PORT: 11434
      LLM_MODEL: phi3.5
      EMBED_MODEL: nomic-embed-text
      UPLOAD_DIR: /app/uploads
      SOURCES_DIR: /app/sources
      EXPORTS_DIR: /app/exports
      OLLAMA_MODELS_DIR: /ollama
      # Shown by /health and compared against the memory Docker actually
      # provides. Raise this if you allocate more to Docker Desktop.
      RECOMMENDED_MEMORY_GB: 10
    volumes:
      - ingest_uploads:/app/uploads
      # Retained source documents; grows with the corpus.
      - rag_sources:/app/sources
      # Bind mount, deliberately: an export is only useful if the user can
      # reach the file from the host without going through Docker.
      - ./exports:/app/exports
      # Ollama's model store, shared read-write so a package can carry models
      # into an air-gapped machine. Installing a model means writing its exact
      # manifest and blobs; there is no registry to pull from offline, and
      # rebuilding a model through the HTTP API would not reproduce its
      # template, params and license faithfully.
      - ollama_models:/ollama
    networks: [rag-internal]
    depends_on:
      weaviate: { condition: service_healthy }
      ollama: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  ui:
    build: ./ui
    image: rag-docker-ui:latest
    networks: [rag-internal]
    depends_on:
      api: { condition: service_healthy }

  proxy:
    image: nginx:1.29-alpine
    ports:
      - "8080:80"
    volumes:
      - ./proxy/nginx.conf:/etc/nginx/nginx.conf:ro
    networks: [rag-internal]
    depends_on: [ui, api]

# ── MCP server: PARKED ────────────────────────────────────────────────────────
# The MCP server is built and tested but intentionally NOT wired into the stack.
# Its source lives in ./mcp and its design in MCP_ANALYSIS.md,
# MCP_SPECIFICATIONS.md and MCP_IMPLEMENTATION.md. Nothing here depends on it,
# and the offline bundle does not ship its image.
#
# To bring it back: restore the service block recorded in MCP_IMPLEMENTATION.md
# §7, add rag-docker-mcp:latest to the IMAGES array in package-offline.sh, and
# rebuild. No code changes are needed -- it passed all 16 acceptance criteria.

volumes:
  weaviate_data:
  ollama_models:
  ingest_uploads:
  rag_sources:

networks:
  rag-internal:
    driver: bridge
```

### ollama/entrypoint.sh

```bash
#!/bin/bash
set -e

ollama serve &
OLLAMA_PID=$!

# The ollama/ollama image ships only the `ollama` binary -- no curl, wget, nc or
# python3 -- so the CLI is the only available readiness probe. `ollama list`
# exits non-zero until the server is accepting requests.
until ollama list > /dev/null 2>&1; do
  sleep 2
done

# Pull a model with retry. Ollama resumes partial downloads so retries are cheap.
# timeout 7200 (2 hours) kills a truly hung connection while allowing slow downloads.
pull_with_retry() {
    local model="$1"

    # Skip if already downloaded. `ollama list` prints NAME as "<model>:latest",
    # so anchor the match to the start of the line.
    if ollama list | grep -q "^${model}"; then
        echo "ollama: ${model} already present, skipping pull."
        return 0
    fi

    local attempt=1
    local max_attempts=5
    until timeout 7200 ollama pull "${model}"; do
        if [ "${attempt}" -ge "${max_attempts}" ]; then
            echo "ERROR: failed to pull ${model} after ${max_attempts} attempts." >&2
            exit 1
        fi
        echo "ollama: pull of ${model} failed or timed out (attempt ${attempt}/${max_attempts}), retrying in 15s..."
        attempt=$((attempt + 1))
        sleep 15
    done
    echo "ollama: ${model} ready."
}

pull_with_retry phi3.5
pull_with_retry nomic-embed-text

wait $OLLAMA_PID
```

### proxy/nginx.conf

```nginx
events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    upstream api {
        server api:8000;
    }

    upstream ui {
        server ui:3000;
    }

    server {
        listen 80;

        location /api/ {
            # nginx's default request-body limit is 1 MB, which rejected most
            # real PDFs with a 413 before the API saw them (issue #21). 512 MB
            # covers a ZIP of a firm's documents in one upload. It is the limit
            # for the whole request, so a multi-file upload counts every file.
            # ui/src/api/client.ts holds the same number to warn before sending;
            # change both together.
            client_max_body_size 512m;
            # Stream uploads straight to the API instead of spooling each one to
            # nginx's temp directory first. Buffering would write up to 512 MB
            # into the container's writable layer, on Docker's often-small
            # virtual disk, before the API even starts reading. The limit above
            # still applies: nginx checks Content-Length up front.
            proxy_request_buffering off;
            proxy_pass http://api/;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_read_timeout 300s;
            proxy_connect_timeout 10s;
        }

        location / {
            proxy_pass http://ui;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_intercept_errors on;
            error_page 404 = /index.html;
        }
    }
}
```

---

## 3. API Backend

### api/Dockerfile

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    poppler-utils \
    tesseract-ocr \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only torch before anything else pulls it in.
#
# sentence-transformers and unstructured both depend on torch, and the default
# PyPI wheel for linux/aarch64 declares the entire NVIDIA CUDA dependency set:
# ~3.3 GB of nvidia-* packages plus ~800 MB of triton. None of it can execute
# without an NVIDIA GPU, so on Apple Silicon (and any CPU-only host) it is 4.1 GB
# of dead weight. Installing from the PyTorch CPU index first satisfies the torch
# requirement, so the pip run below leaves it alone rather than resolving the
# CUDA build from PyPI.
#
# These two pins are the counterpart to requirements.txt, which is a generated
# lock file that deliberately omits torch and torchvision -- their "+cpu" local
# versions are not published on PyPI. Keep the pins here in step with the
# versions recorded in the requirements.txt header when you re-lock.
#
# If a future dependency requires a newer torch than is pinned here, pip will
# satisfy it from PyPI with the CUDA build and the image balloons back to
# ~7.6 GB. Bump these pins rather than dropping them.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    torch==2.14.0 \
    torchvision==0.29.0

# requirements.txt is a generated lock (see api/requirements.in to change deps).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-cache sentence-transformers model at build time
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### api/requirements.txt

```
# ---------------------------------------------------------------------------
# LOCK FILE -- do not hand-edit.
#
# Fully pinned dependency set (direct + transitive), generated from a verified build:
#     docker compose build api
#     docker run --rm rag-docker-api pip freeze
#
# Direct dependencies (the human-maintained intent) live in requirements.in.
# To change a dependency: edit requirements.in, rebuild, re-freeze, commit both.
#
# torch and torchvision are deliberately ABSENT. They are installed from the
# PyTorch CPU index in api/Dockerfile because the default PyPI wheel pulls in
# ~4.1 GB of unusable NVIDIA CUDA packages. Their resolved versions are:
#     torch==2.14.0+cpu
#     torchvision==0.29.0+cpu
# Pinning them here would break the build, since the "+cpu" local version
# identifiers are not published on PyPI.
# ---------------------------------------------------------------------------

Authlib==1.8.0
Jinja2==3.1.6
Markdown==3.10.3
MarkupSafe==3.0.3
PyYAML==6.0.3
Pygments==2.21.0
RapidFuzz==3.14.6
accelerate==1.15.0
aiofiles==25.1.0
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
beautifulsoup4==4.15.0
blis==1.3.3
catalogue==2.0.10
certifi==2026.7.22
cffi==2.1.1
charset-normalizer==3.5.1
click==8.5.0
cloudpathlib==0.25.0
cloudpickle==3.1.2
confection==1.3.3
contourpy==1.3.3
cryptography==50.0.1
cycler==0.12.1
cymem==2.0.13
distro==1.9.0
emoji==2.15.0
fastapi==0.141.1
filelock==3.32.3
filetype==1.2.0
flatbuffers==25.12.19
fonttools==4.65.0
fsspec==2026.7.0
google-api-core==2.36.0
google-auth==2.58.0
google-cloud-vision==3.15.0
googleapis-common-protos==1.75.3
grpcio-status==1.78.0
grpcio==1.78.0
h11==0.16.0
hf-xet==1.6.0
html5lib==1.1
httpcore2==2.12.0
httpcore==1.0.9
httptools==0.8.0
httpx2==2.12.0
httpx==0.28.1
huggingface_hub==1.31.0
idna==3.19
installer==0.7.0
joblib==1.6.0
joserfc==1.7.5
jsonpatch==1.33
jsonpointer==3.1.1
kiwisolver==1.5.1
langchain-core==1.6.3
langchain-protocol==0.0.19
langchain-text-splitters==1.1.2
langdetect==1.0.9
langsmith==0.12.4
llvmlite==0.49.0
lxml==6.1.3
markdown-it-py==4.2.0
matplotlib==3.11.2
mdurl==0.1.2
ml_dtypes==0.6.0
mpmath==1.3.0
murmurhash==1.0.15
narwhals==2.26.0
networkx==3.6.1
nh3==0.3.7
numba==0.67.0
numpy==2.4.6
olefile==0.47
onnx==1.22.0
onnxruntime==1.30.0
opencv-python==5.0.0.93
opentelemetry-api==1.44.0
orjson==3.12.0
packaging==26.3
pandas==2.3.3
pdf2image==1.17.0
pdfminer.six==20260107
pi_heif==1.4.0
pikepdf==10.13.0.post1
pillow==12.3.0
preshed==3.0.13
proto-plus==1.28.4
protobuf==6.33.6
psutil==7.2.2
pyasn1==0.6.4
pyasn1_modules==0.4.2
pycparser==3.0
pydantic-settings==2.15.0
pydantic==2.13.5
pydantic_core==2.46.5
pyparsing==3.3.2
pypdf==6.18.1
pypdfium2==5.13.0
python-dateutil==2.9.0.post0
python-docx==1.2.0
python-dotenv==1.2.3
python-iso639==2026.7.23
python-magic==0.4.27
python-multipart==0.0.32
python-oxmsg==0.0.2
pytz==2026.3.post1
regex==2026.9.10
requests-toolbelt==1.0.0
requests==2.34.2
rich==15.0.0
safetensors==0.8.0
scikit-learn==1.9.1
scipy==1.17.1
sentence-transformers==6.0.1
shellingham==1.5.4
six==1.17.0
smart_open==8.0.1
sniffio==1.3.1
soupsieve==2.9.2
spacy-legacy==3.0.12
spacy-loggers==1.0.5
spacy==3.8.16
srsly==2.5.3
starlette==1.6.0
sympy==1.14.0
tenacity==9.1.4
thinc==8.3.13
threadpoolctl==3.6.0
timm==1.0.29
tokenizers==0.23.2
tqdm==4.70.1
transformers==5.17.0
truststore==0.10.4
typer==0.27.2
typing-inspection==0.4.4
typing_extensions==4.16.0
tzdata==2026.4
unstructured-client==0.46.2
unstructured.pytesseract==0.3.15
unstructured==0.27.5
unstructured_inference==1.6.13
urllib3==2.7.0
uuid_utils==0.17.1
uvicorn==0.52.4
uvloop==0.22.1
validators==0.35.0
wasabi==1.1.3
watchfiles==1.2.0
weasel==1.0.0
weaviate-client==4.23.1
webencodings==0.6.1
websockets==17.1
wrapt==2.4.1
xxhash==4.0.1
zstandard==0.25.0
```

### api/requirements.in

```text
# ---------------------------------------------------------------------------
# Direct dependencies -- the human-maintained intent. EDIT THIS FILE.
#
# This file is NOT installed by the Dockerfile. The build installs the fully
# pinned set (direct + transitive) from requirements.txt, generated from it.
#
# To add, remove, or bump a dependency -- run these from the REPO ROOT:
#   1. edit api/requirements.in
#   2. resolve it against the CURRENT image, which already has CPU-only torch:
#        docker run --rm -v "$PWD/api/requirements.in:/r.in:ro" rag-docker-api \
#          pip install --quiet --dry-run --report /dev/stdout -r /r.in \
#          | python -c 'import json,sys; d=json.load(sys.stdin); \
#              print("\n".join(i["metadata"]["name"]+"=="+i["metadata"]["version"] \
#                               for i in d.get("install",[])))'
#      That prints exactly what is missing from the lock. Add those lines to
#      api/requirements.txt, keeping it LC_ALL=C sorted.
#   3. docker compose build api   # confirm the lock installs cleanly
#   4. docker run --rm rag-docker-api pip freeze \
#        | grep -viE '^(torch|torchvision)==' | LC_ALL=C sort > /tmp/pins.txt
#      diff that against requirements.txt to confirm nothing else moved.
#   5. commit BOTH files together
#
# An earlier version of this note said to edit requirements.in and rebuild, then
# freeze. That does not work: api/Dockerfile installs requirements.txt and never
# reads requirements.in, so the rebuild resolves nothing new and the freeze
# returns the old set unchanged. The `.md` ingest dependency was missing for
# exactly this reason -- `unstructured[pdf,docx,csv]` omitted the `md` extra, and
# no rebuild would ever have revealed it.
#
# Note the drift between the floors below and what actually resolved: these
# ranges are open-ended, so re-locking can pull major versions (for example
# langchain-text-splitters 0.3 -> 1.1.2). Always test after re-locking.
#
# torch and torchvision are not listed here. They arrive transitively via
# sentence-transformers and unstructured, and api/Dockerfile installs them from
# the PyTorch CPU index first to avoid ~4.1 GB of unusable NVIDIA CUDA packages.
# ---------------------------------------------------------------------------

fastapi>=0.111                    # resolved: 0.141.1
uvicorn[standard]>=0.30           # resolved: 0.52.4
pydantic-settings>=2.3            # resolved: 2.15.0
weaviate-client>=4.6              # resolved: 4.23.1
httpx>=0.27                       # resolved: 0.28.1
unstructured[pdf,docx,csv,md]>=0.14  # resolved: 0.27.5 -- 'md' extra pulls `markdown`,
                                  # without which .md ingestion fails at runtime
langchain-text-splitters>=0.3     # resolved: 1.1.2
sentence-transformers>=3.0        # resolved: 6.0.1
python-multipart>=0.0.9           # resolved: 0.0.32
```

### api/utils.py

```python
from __future__ import annotations
from fastapi.responses import JSONResponse


def api_error(status_code: int, code: str, message: str, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )
```

### api/config.py

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    weaviate_host: str = "localhost"
    weaviate_port: int = 8080
    ollama_host: str = "localhost"
    ollama_port: int = 11434
    llm_model: str = "phi3.5"
    embed_model: str = "nomic-embed-text"
    upload_dir: str = "/app/uploads"
    # Retained original documents, content-addressed per collection. Kept in
    # its own volume because it grows with the corpus, unlike upload_dir which
    # holds only small config and session files.
    sources_dir: str = "/app/sources"
    # Written export packages. A host bind mount, not a named volume: the
    # whole point is that the user can pick the file up and carry it away.
    exports_dir: str = "/app/exports"
    # Ollama's model store, mounted from the same volume the ollama service
    # uses. Only touched when a package bundles models.
    ollama_models_dir: str = "/ollama"
    # Reported by /health and compared against the memory Docker actually
    # provides. Raise it in docker-compose.yml; no rebuild required.
    recommended_memory_gb: float = 10.0

    class Config:
        env_file = ".env"


settings = Settings()
```

### api/models/__init__.py

```python

```

### api/models/schemas.py

```python
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, field_validator, model_validator


# ── Collections ──────────────────────────────────────────────────────────────

class HnswConfig(BaseModel):
    efConstruction: int = 128
    maxConnections: int = 64
    ef: int = 64


class CreateCollectionRequest(BaseModel):
    name: str
    index_type: str = "hnsw"
    distance_metric: str = "cosine"
    hnsw_config: HnswConfig = HnswConfig()


class CollectionInfo(BaseModel):
    name: str
    object_count: int
    index_type: str
    distance_metric: str
    created_at: Optional[str]


class CollectionsResponse(BaseModel):
    collections: list[CollectionInfo]


# ── Ingest ────────────────────────────────────────────────────────────────────

class IngestUploadResponse(BaseModel):
    job_id: str
    status: str
    files_queued: int
    collection: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    files_total: int
    files_completed: int
    files_failed: int
    chunks_stored: int
    errors: list[str]
    # Files the caller sent that could not be parsed at all; they never
    # became part of files_total, so without this they vanish silently.
    skipped: list[str] = []


class IngestConfig(BaseModel):
    chunking_strategy: str = "overlap"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    similarity_threshold: Optional[float] = None
    min_chunk_size: int = 100


class IngestConfigResponse(BaseModel):
    collection: str
    chunking_strategy: str
    chunk_size: int
    chunk_overlap: int
    similarity_threshold: Optional[float]
    min_chunk_size: int
    is_default: bool


# ── Retrieval config ──────────────────────────────────────────────────────────

class SaveRetrievalConfigBody(BaseModel):
    collection: str
    retrieval_mode: str = "hnsw"
    top_k: int = 5
    alpha: float = 0.75
    ef: Optional[int] = None
    response_format: str = "end_user"

    @field_validator("retrieval_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        # "semantic" routes to Weaviate near_text in rag_pipeline.run_query;
        # it is offered on the Retrieval page, so the stored config must accept it.
        allowed = ("hnsw", "flat", "hybrid", "semantic")
        if v not in allowed:
            raise ValueError(f"retrieval_mode must be one of {allowed}, got {v!r}")
        return v

    @field_validator("response_format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        allowed = ("end_user", "engineer")
        if v not in allowed:
            raise ValueError(f"response_format must be one of {allowed}, got {v!r}")
        return v

    @field_validator("top_k")
    @classmethod
    def _top_k(cls, v: int) -> int:
        if not 1 <= v <= 50:
            raise ValueError("top_k must be between 1 and 50")
        return v

    @field_validator("alpha")
    @classmethod
    def _alpha(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        return v


class RetrievalConfigResponse(BaseModel):
    collection: str
    retrieval_mode: str
    top_k: int
    alpha: float
    ef: Optional[int]
    response_format: str
    is_default: bool


class HelpResponse(BaseModel):
    topic: str
    markdown: str


# ── Export / Import ───────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    collection: str
    include_models: bool = False


class ExportStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str


class ExportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    chunks_written: int
    filename: Optional[str]
    size_bytes: Optional[int]
    source_document_count: Optional[int]
    fidelity: Optional[str]
    # What the package actually carries. A request for models that could not be
    # honoured comes back false, with the reason in `warnings`.
    models_bundled: Optional[bool]
    retrieve_script: Optional[bool]
    warnings: list[str]
    error: Optional[str]


class ImportRequest(BaseModel):
    filename: str
    # Required, no default (spec §6.4): silently picking a conflict policy could
    # delete a collection the caller did not mean to touch.
    on_conflict: str

    @field_validator("on_conflict")
    @classmethod
    def _on_conflict(cls, v: str) -> str:
        allowed = ("abort", "rename", "replace")
        if v not in allowed:
            raise ValueError(f"on_conflict must be one of {allowed}, got {v!r}")
        return v


class ImportStartResponse(BaseModel):
    job_id: str
    status: str
    filename: str


class ImportJobStatusResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    on_conflict: str
    # The name actually imported under; differs from original_collection when
    # on_conflict='rename' resolved a collision.
    collection: Optional[str]
    original_collection: Optional[str]
    chunks_written: int
    fidelity: Optional[str]
    renamed: bool
    notes: list[str]
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class PackageSummary(BaseModel):
    filename: str
    size_bytes: int
    # Null when the archive could not be read; the listing still shows the file
    # so the user is not left wondering where it went.
    collection: Optional[str]
    chunk_count: Optional[int]
    fidelity: Optional[str]
    created_at: Optional[str]
    readable: bool


class PackageListResponse(BaseModel):
    packages: list[PackageSummary]


# ── Tuning ────────────────────────────────────────────────────────────────────

CHUNKING_STRATEGIES = ("fixed", "overlap", "language", "context_aware", "semantic")


class _ChunkingFields(BaseModel):
    chunking_strategy: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    similarity_threshold: Optional[float] = None
    min_chunk_size: Optional[int] = None

    @field_validator("chunking_strategy")
    @classmethod
    def _strategy(cls, v):
        if v is not None and v not in CHUNKING_STRATEGIES:
            raise ValueError(f"chunking_strategy must be one of {CHUNKING_STRATEGIES}, got {v!r}")
        return v

    def has_chunking(self) -> bool:
        return any(getattr(self, f) is not None for f in
                   ("chunking_strategy", "chunk_size", "chunk_overlap",
                    "similarity_threshold", "min_chunk_size"))

    def chunking(self) -> dict:
        """Defaults match the ingest pipeline, so an omitted field means 'as before'."""
        return {
            "strategy": self.chunking_strategy or "overlap",
            "chunk_size": self.chunk_size if self.chunk_size is not None else 1000,
            "chunk_overlap": self.chunk_overlap if self.chunk_overlap is not None else 200,
            "similarity_threshold": (self.similarity_threshold
                                     if self.similarity_threshold is not None else 0.85),
            "min_chunk_size": self.min_chunk_size if self.min_chunk_size is not None else 100,
        }


class RechunkRequest(_ChunkingFields):
    collection: str


class ReembedRequest(_ChunkingFields):
    """Chunking fields are optional here, and only legal with `with-sources`."""
    collection: str


class ReindexRequest(BaseModel):
    collection: str
    index_type: Optional[str] = None
    distance_metric: Optional[str] = None

    @field_validator("index_type")
    @classmethod
    def _index(cls, v):
        if v is not None and v not in ("hnsw", "flat"):
            raise ValueError(f"index_type must be 'hnsw' or 'flat', got {v!r}")
        return v

    @field_validator("distance_metric")
    @classmethod
    def _distance(cls, v):
        allowed = ("cosine", "dot", "l2-squared")
        if v is not None and v not in allowed:
            raise ValueError(f"distance_metric must be one of {allowed}, got {v!r}")
        return v


class TuneStartResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str


class TuneJobStatusResponse(BaseModel):
    job_id: str
    status: str
    collection: str
    operation: str
    chunks_total: int
    chunks_written: int
    notes: list[str]
    error: Optional[str]
    error_code: Optional[str]
    error_detail: Optional[dict]


class TuneOptionsResponse(BaseModel):
    collection: str
    fidelity: str
    source_document_count: int
    can_rechunk: bool
    can_reembed: bool
    can_reindex: bool
    note: str


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    collection: str
    retrieval_mode: str = "hnsw"
    top_k: int = 5
    alpha: float = 0.75
    include_citations: bool = False
    response_format: str = "end_user"


class Citation(BaseModel):
    source_file: str
    chunk_index: int
    score: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    citations: Optional[list[Citation]]
    retrieval_latency_ms: int
    llm_latency_ms: int
    chunks_retrieved: int


# ── Gold Standard ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    collection: str
    sample_size: int = 20
    seed: Optional[int] = None


class GenerateResponse(BaseModel):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0


class GoldPair(BaseModel):
    pair_id: str
    question: str
    answer: str
    contexts: list[str]
    ground_truth: str
    source_file: str
    chunk_index: int
    status: str


class SessionResponse(BaseModel):
    session_id: str
    status: str
    pairs_total: int
    # `attempted` reaches `total` even when a pair fails, so it drives progress;
    # `completed` is how many pairs actually exist.
    pairs_attempted: int = 0
    pairs_completed: int
    pairs_failed: int = 0
    pairs: list[GoldPair]
    collection: str
    errors: list[str] = []


class PatchPairRequest(BaseModel):
    status: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    ground_truth: Optional[str] = None

    @model_validator(mode="after")
    def _content_edit_needs_edited_status(self):
        """Rewriting a pair's content must be recorded as an edit.

        Without this a caller could change the question while the pair still
        reads "approved", and the export would ship content nobody approved
        under a status that says otherwise.
        """
        content = [f for f in ("question", "answer", "ground_truth")
                   if getattr(self, f) is not None]
        if content and self.status != "edited":
            raise ValueError(
                f"changing {', '.join(content)} requires status='edited'; "
                f"got status={self.status!r}")
        return self

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"approved", "edited", "rejected", "pending"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v


class RegenerateRequest(BaseModel):
    session_id: str
    pair_id: str


class SaveRequest(BaseModel):
    session_id: str
    filename: Optional[str] = None


class SaveResponse(BaseModel):
    filename: str
    pairs_saved: int
    pairs_excluded: int
    download_url: str


# ── Metrics ───────────────────────────────────────────────────────────────────

class LatencyStats(BaseModel):
    p50: float
    p95: float
    p99: float
    mean: float
    count: int


class LatencyRecord(BaseModel):
    """One recorded query, for plotting latency over time."""
    timestamp: str
    collection: str
    retrieval_mode: str
    retrieval_ms: int
    llm_ms: int
    total_ms: int


class MetricsResponse(BaseModel):
    total_records: int
    retrieval_latency: LatencyStats
    llm_latency: LatencyStats
    total_latency: LatencyStats
    # Most recent records, oldest first so a chart reads left to right.
    # Bounded by the `limit` query parameter: the ring buffer holds up to 500
    # entries and the UI polls every 30s, so returning all of them every time
    # would be wasteful.
    history: list[LatencyRecord]


# ── Error ─────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
```

### api/services/__init__.py

```python

```

### api/services/sources.py

```python
"""Retention of original uploaded documents.

Ingest previously parsed uploads and deleted them, so only chunked text survived.
Export, re-chunking and re-embedding all need the originals, so accepted files
are copied here instead.

Files are content-addressed: the SHA-256 of the raw bytes is the storage key, so
re-ingesting the same document stores one copy and records the extra logical
name. Storage is proportional to the corpus rather than to upload count.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
INDEX_VERSION = 1


def _root() -> Path:
    return Path(settings.sources_dir)


def collection_dir(collection: str) -> Path:
    return _root() / collection


def _index_path(collection: str) -> Path:
    return collection_dir(collection) / INDEX_NAME


def load_index(collection: str) -> dict:
    p = _index_path(collection)
    if not p.exists():
        return {"version": INDEX_VERSION, "documents": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable source index for %r; treating as empty", collection)
        return {"version": INDEX_VERSION, "documents": {}}
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("documents", {})
    return data


def _save_index(collection: str, index: dict) -> None:
    p = _index_path(collection)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file in the same directory so a crash cannot truncate an
    # existing index.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
    tmp.replace(p)


def store(collection: str, filename: str, data: bytes, media_type: str | None = None) -> str:
    """Retain one accepted upload. Returns its sha256."""
    digest = hashlib.sha256(data).hexdigest()
    target = collection_dir(collection) / digest
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        tmp = target.with_name(digest + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    now = datetime.now(timezone.utc).isoformat()
    index = load_index(collection)
    entry = index["documents"].get(digest)
    if entry is None:
        index["documents"][digest] = {
            "filenames": [filename],
            "size": len(data),
            "media_type": media_type,
            "first_seen": now,
            "last_seen": now,
        }
    else:
        if filename not in entry["filenames"]:
            entry["filenames"].append(filename)
        entry["last_seen"] = now
    _save_index(collection, index)
    return digest


def digests_for_filename(collection: str, filename: str) -> list[str]:
    """All digests ever stored under this logical filename.

    Returns more than one when the same name was ingested with different
    content. Callers must decide what that means rather than assume one.
    """
    index = load_index(collection)
    return [d for d, e in index["documents"].items() if filename in e.get("filenames", [])]


def has_sources(collection: str) -> bool:
    """True when the collection has retained originals (export fidelity)."""
    return bool(load_index(collection)["documents"])


def stats(collection: str) -> dict:
    docs = load_index(collection)["documents"]
    return {
        "document_count": len(docs),
        "total_bytes": sum(e.get("size", 0) for e in docs.values()),
    }


def delete(collection: str) -> None:
    """Remove every retained source for a collection.

    Called when the collection is deleted. Without this the volume leaks
    silently: it is surfaced nowhere in the UI.
    """
    shutil.rmtree(collection_dir(collection), ignore_errors=True)
```

### api/services/ingest_config.py

```python
"""Per-collection chunking settings.

Extracted from the ingest router because three places needed the same on-disk
convention: the router that serves it, the exporter that packages it, and
collection deletion that must remove it. The third was missing, so a new
collection silently inherited the chunking settings of a deleted one with the
same name -- and each copy of the path logic was a chance for them to drift.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

CHUNKING_STRATEGIES = ("fixed", "overlap", "language", "context_aware", "semantic")

DEFAULTS = {
    "chunking_strategy": "overlap",
    "chunk_size": 1000,
    "chunk_overlap": 200,
    "similarity_threshold": None,
    "min_chunk_size": 100,
}

_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(settings.upload_dir) / "ingest_configs"
        _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _path(collection: str) -> Path:
    return _dir() / f"{_safe_name(collection)}.json"


def load(collection: str) -> dict | None:
    """Saved config, or None when the collection has never been configured."""
    p = _path(collection)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable ingest config for %r; falling back to defaults", collection)
        return None


def resolve(collection: str) -> tuple[dict, bool]:
    """Return (config, is_default). Never raises; always usable."""
    saved = load(collection)
    if saved is None:
        return {"collection": collection, **DEFAULTS}, True
    merged = {"collection": collection, **DEFAULTS, **saved}
    merged["collection"] = collection
    return merged, False


def save(config: dict) -> dict:
    collection = config["collection"]
    p = _path(collection)
    # Written via a temp file in the same directory so a crash cannot leave a
    # half-written config behind.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True))
    tmp.replace(p)
    return config


def delete(collection: str) -> None:
    """Remove a collection's chunking settings.

    Called when the collection is deleted (spec §8 rule 1). Without this a
    recreated collection of the same name picks up settings the user never
    chose for it.
    """
    _path(collection).unlink(missing_ok=True)
```

### api/services/retrieval_config.py

```python
"""Per-collection retrieval settings.

Retrieval mode, top_k, alpha and ef previously existed only in the browser's
sessionStorage, so a user's tuning died with the tab and there was nothing on the
server to export. They are persisted here, beside ingest_configs, so a collection
can record how it is meant to be queried.

Kept in a service rather than in the router because the exporter needs
programmatic access: an export package ships a retrieval script carrying these
parameters.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

RETRIEVAL_MODES = ("hnsw", "flat", "hybrid", "semantic")
RESPONSE_FORMATS = ("end_user", "engineer")

DEFAULTS = {
    "retrieval_mode": "hnsw",
    "top_k": 5,
    "alpha": 0.75,
    "ef": None,
    "response_format": "end_user",
}

_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(settings.upload_dir) / "retrieval_configs"
        _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _path(collection: str) -> Path:
    return _dir() / f"{_safe_name(collection)}.json"


def load(collection: str) -> dict | None:
    """Saved config, or None when the collection has never been tuned."""
    p = _path(collection)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable retrieval config for %r; falling back to defaults", collection)
        return None


def resolve(collection: str) -> tuple[dict, bool]:
    """Return (config, is_default). Never raises; always usable."""
    saved = load(collection)
    if saved is None:
        return {"collection": collection, **DEFAULTS}, True
    # Fill in any key added since the file was written, so an older config does
    # not lose a field that callers now expect.
    merged = {"collection": collection, **DEFAULTS, **saved}
    merged["collection"] = collection
    return merged, False


def save(config: dict) -> dict:
    collection = config["collection"]
    p = _path(collection)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True))
    tmp.replace(p)
    return config


def delete(collection: str) -> None:
    """Remove a collection's retrieval config; called when it is deleted."""
    _path(collection).unlink(missing_ok=True)
```

### api/services/weaviate_client.py

```python
from __future__ import annotations
import asyncio
import logging
import threading

import weaviate
from weaviate.classes.config import Configure, Property, DataType, VectorDistances
from weaviate.classes.query import MetadataQuery

from services import collection_writes, collection_recovery
from config import settings
from services import ingest_config
from services import retrieval_config
from services import sources

log = logging.getLogger(__name__)

_client: weaviate.WeaviateClient | None = None
_client_lock = threading.Lock()

DISTANCE_MAP = {
    "cosine": VectorDistances.COSINE,
    "dot": VectorDistances.DOT,
    "l2-squared": VectorDistances.L2_SQUARED,
}

COLLECTION_PROPERTIES = [
    Property(name="content", data_type=DataType.TEXT, index_searchable=True, index_filterable=True),
    Property(name="source_file", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="source_type", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="chunk_index", data_type=DataType.INT, index_filterable=True),
    Property(name="chunk_strategy", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="chunk_size", data_type=DataType.INT, index_filterable=True),
    Property(name="chunk_overlap", data_type=DataType.INT, index_filterable=True),
    Property(name="created_at", data_type=DataType.DATE, index_filterable=True),
]


def get_client() -> weaviate.WeaviateClient:
    global _client
    with _client_lock:
        if _client is None or not _client.is_connected():
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            _client = weaviate.connect_to_custom(
                http_host=settings.weaviate_host,
                http_port=settings.weaviate_port,
                http_secure=False,
                grpc_host=settings.weaviate_host,
                grpc_port=50051,
                grpc_secure=False,
            )
    return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _check_health_sync() -> bool:
    """Liveness check that exercises the real client, not just HTTP readiness.

    get_client() performs the actual connect handshake, which is where a
    client/server version mismatch surfaces as WeaviateStartUpError. A plain
    GET of /v1/.well-known/ready does NOT catch that case: the server happily
    answers "ready" while every client call fails, so health reports green
    during a total outage of Weaviate functionality.
    """
    client = get_client()
    return bool(client.is_ready())


async def check_health() -> bool:
    return await asyncio.to_thread(_check_health_sync)


@collection_writes.serialized("name")
def _create_collection_sync(
    name: str,
    index_type: str,
    distance_metric: str,
    hnsw_config: dict,
) -> None:
    client = get_client()
    dist = DISTANCE_MAP.get(distance_metric, VectorDistances.COSINE)

    if index_type == "flat":
        vector_index = Configure.VectorIndex.flat(distance_metric=dist)
    else:
        vector_index = Configure.VectorIndex.hnsw(
            distance_metric=dist,
            ef_construction=hnsw_config.get("efConstruction", 128),
            max_connections=hnsw_config.get("maxConnections", 64),
            ef=hnsw_config.get("ef", 64),
        )

    vectorizer = Configure.Vectorizer.text2vec_ollama(
        api_endpoint=f"http://{settings.ollama_host}:{settings.ollama_port}",
        model=settings.embed_model,
        vectorize_collection_name=False,
    )

    client.collections.create(
        name=name,
        vectorizer_config=vectorizer,
        vector_index_config=vector_index,
        properties=COLLECTION_PROPERTIES,
    )


async def create_collection(
    name: str,
    index_type: str = "hnsw",
    distance_metric: str = "cosine",
    hnsw_config: dict | None = None,
) -> None:
    await asyncio.to_thread(
        _create_collection_sync, name, index_type, distance_metric, hnsw_config or {}
    )


def _collection_exists_sync(name: str) -> bool:
    return get_client().collections.exists(name)


async def collection_exists(name: str) -> bool:
    return await asyncio.to_thread(_collection_exists_sync, name)


@collection_writes.serialized("name")
def _delete_collection_sync(name: str) -> int:
    client = get_client()
    coll = client.collections.get(name)
    count = coll.aggregate.over_all(total_count=True).total_count
    client.collections.delete(name)
    collection_recovery.retire_deleted(collection_writes.canonical(name), client)
    # Retained originals must go with the collection. The sources volume is
    # surfaced nowhere in the UI, so a leak here would be invisible.
    sources.delete(name)
    retrieval_config.delete(name)
    ingest_config.delete(name)
    # Gold-standard sessions are kept and flagged, never deleted (spec §8 rule 4):
    # they are evaluation work the user may still want, and the pairs stay
    # readable even with the collection gone. Imported here rather than at module
    # level because goldstandard imports this module.
    from services import goldstandard
    goldstandard.mark_orphaned(
        name, f"collection '{name}' was deleted")
    return count or 0


async def delete_collection(name: str) -> int:
    return await asyncio.to_thread(_delete_collection_sync, name)


def _get_collections_sync() -> list[dict]:
    client = get_client()
    all_cols = client.collections.list_all()
    result = []
    for col_name in all_cols:
        coll = client.collections.get(col_name)
        count = coll.aggregate.over_all(total_count=True).total_count or 0

        # list_all() returns _CollectionConfigSimple, which does NOT carry
        # vector_index_config (weaviate-client 4.x dropped it from the reduced
        # config). Fetch the full per-collection config for the index details.
        vector_config = coll.config.get().vector_index_config
        index_type = "flat" if "flat" in type(vector_config).__name__.lower() else "hnsw"

        distance_attr = getattr(vector_config, "distance_metric", VectorDistances.COSINE)
        distance_str = {
            VectorDistances.COSINE: "cosine",
            VectorDistances.DOT: "dot",
            VectorDistances.L2_SQUARED: "l2-squared",
        }.get(distance_attr, "cosine")

        result.append({
            "name": col_name,
            "object_count": count,
            "index_type": index_type,
            "distance_metric": distance_str,
        })
    return result


async def get_collections() -> list[dict]:
    return await asyncio.to_thread(_get_collections_sync)


# Startup only removes scratch with a durable positive ownership record.
# Legacy/unowned marker names are preserved; names alone cannot distinguish
# abandoned scratch from deliberately retained recovery.
STAGING_MARKERS = ("__importing_", "__tuning_")


def _sweep_staging_sync() -> list[str]:
    return collection_recovery.sweep(get_client())


async def sweep_staging() -> list[str]:
    """Remove positively owned scratch; preserve recovery and unowned names."""
    return await asyncio.to_thread(_sweep_staging_sync)


def _meta_sync() -> dict:
    """Server metadata. `version` goes into the export manifest."""
    try:
        return get_client().get_meta() or {}
    except Exception:
        return {}


async def get_meta() -> dict:
    return await asyncio.to_thread(_meta_sync)


def _collection_config_sync(name: str) -> dict:
    """The collection's schema and index settings, as plain JSON.

    Shaped to match the body `POST /collections` accepts, so an import can
    recreate the collection by feeding this straight back in.
    """
    coll = get_client().collections.get(name)
    cfg = coll.config.get()
    vi = cfg.vector_index_config

    index_type = "flat" if "flat" in type(vi).__name__.lower() else "hnsw"
    distance = {
        VectorDistances.COSINE: "cosine",
        VectorDistances.DOT: "dot",
        VectorDistances.L2_SQUARED: "l2-squared",
    }.get(getattr(vi, "distance_metric", VectorDistances.COSINE), "cosine")

    # Absent on a flat index; the defaults mirror _create_collection_sync so a
    # flat collection imported as hnsw would still be built sanely.
    hnsw = {
        "efConstruction": getattr(vi, "ef_construction", 128),
        "maxConnections": getattr(vi, "max_connections", 64),
        "ef": getattr(vi, "ef", 64),
    }

    return {
        "name": name,
        "index_type": index_type,
        "distance_metric": distance,
        "hnsw_config": hnsw,
        "properties": [
            {"name": p.name, "data_type": getattr(p.data_type, "value", str(p.data_type))}
            for p in (cfg.properties or [])
        ],
        # Recorded for information. Import always rebuilds the collection with
        # this instance's vectorizer, because the vectors come from the package.
        "vectorizer": str(getattr(cfg, "vectorizer", "") or ""),
    }


async def get_collection_config(name: str) -> dict:
    return await asyncio.to_thread(_collection_config_sync, name)


def _validate_reindex_vectorizer_sync(name: str) -> None:
    """Fail before staging if recreation would change the stored vector space."""
    cfg = get_client().collections.get(name).config.get()
    vectorizer = getattr(cfg, "vectorizer_config", None)
    kind = getattr(vectorizer, "vectorizer", None)
    model = getattr(vectorizer, "model", None)
    expected_model = {"model": settings.embed_model,
                      "apiEndpoint": f"http://{settings.ollama_host}:{settings.ollama_port}"}
    compatible = (getattr(kind, "value", kind) == "text2vec-ollama"
                  and model == expected_model
                  and getattr(vectorizer, "vectorize_collection_name", None) is False
                  and not getattr(cfg, "vector_config", None))
    # Property names/types and skip/name flags also determine provider input.
    # Refuse unknown module options and custom properties instead of copying
    # old vectors into the fixed schema with different future insert rules.
    expected_properties = {p.name: p._to_dict() for p in COLLECTION_PROPERTIES}
    properties = list(getattr(cfg, "properties", None) or [])
    compatible = compatible and len(properties) == len(expected_properties) and {p.name for p in properties} == set(expected_properties)
    for prop in properties:
        expected = expected_properties.get(prop.name)
        rules = getattr(prop, "vectorizer_config", None)
        compatible = compatible and bool(
            expected
            and getattr(prop.data_type, "value", prop.data_type) == expected["dataType"][0]
            and getattr(prop, "vectorizer", None) == "text2vec-ollama"
            and not getattr(prop, "vectorizer_configs", None)
            and rules is not None
            and rules.skip == expected["skip_vectorization"]
            and rules.vectorize_property_name == expected["vectorize_property_name"]
            and not getattr(prop, "nested_properties", None))
    if not compatible:
        raise ValueError("Reindex would change the collection's vectorizer configuration; "
                         "re-embed with the configured model first")


@collection_writes.serialized("collection_name")
def _insert_chunks_sync(collection_name: str, chunks: list[dict]) -> None:
    client = get_client()
    coll = client.collections.get(collection_name)
    with coll.batch.dynamic() as batch:
        for chunk in chunks:
            batch.add_object(properties=chunk)
        if batch.number_errors > 0:
            raise RuntimeError(f"{batch.number_errors} batch error(s) inserting into '{collection_name}'")


async def insert_chunks(collection_name: str, chunks: list[dict]) -> None:
    await asyncio.to_thread(_insert_chunks_sync, collection_name, chunks)


def _near_vector_query_sync(
    collection_name: str, vector: list[float], top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.near_vector(
        near_vector=vector,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        dist = obj.metadata.distance or 0.0
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": dist,
        })
    return rows


async def near_vector_query(
    collection_name: str, vector: list[float], top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_near_vector_query_sync, collection_name, vector, top_k)


def _near_text_query_sync(
    collection_name: str, query: str, top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.near_text(
        query=query,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        dist = obj.metadata.distance or 0.0
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": dist,
        })
    return rows


async def near_text_query(
    collection_name: str, query: str, top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_near_text_query_sync, collection_name, query, top_k)


def _hybrid_query_sync(
    collection_name: str, query: str, alpha: float, top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.hybrid(
        query=query,
        alpha=alpha,
        limit=top_k,
        return_metadata=MetadataQuery(score=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": obj.metadata.score or 0.0,
        })
    return rows


async def hybrid_query(
    collection_name: str, query: str, alpha: float, top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_hybrid_query_sync, collection_name, query, alpha, top_k)


def _sample_chunks_sync(collection_name: str, limit: int) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.fetch_objects(
        limit=limit,
        return_properties=["content", "source_file", "chunk_index"],
    )
    return [
        {
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
        }
        for obj in result.objects
    ]


async def sample_chunks(collection_name: str, limit: int) -> list[dict]:
    return await asyncio.to_thread(_sample_chunks_sync, collection_name, limit)
```

### api/services/ollama_client.py

```python
from __future__ import annotations
import time

import httpx

from config import settings

_BASE = f"http://{settings.ollama_host}:{settings.ollama_port}"


async def embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_BASE}/api/embeddings",
            json={"model": settings.embed_model, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


async def chat(system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{_BASE}/api/chat",
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


async def check_health() -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE}/api/tags")
            resp.raise_for_status()
            latency_ms = int((time.monotonic() - start) * 1000)
            models = {m["name"].split(":")[0] for m in resp.json().get("models", [])}
            llm_ok = settings.llm_model.split(":")[0] in models
            embed_ok = settings.embed_model.split(":")[0] in models
            return {
                "llm": {"status": "ok" if llm_ok else "error", "latency_ms": latency_ms, "model": settings.llm_model},
                "embed": {"status": "ok" if embed_ok else "error", "latency_ms": latency_ms, "model": settings.embed_model},
            }
    except Exception:
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "llm": {"status": "error", "latency_ms": latency_ms, "model": settings.llm_model},
            "embed": {"status": "error", "latency_ms": latency_ms, "model": settings.embed_model},
        }
```

### api/services/chunker.py

```python
from __future__ import annotations
import threading
from typing import Any

from langchain_text_splitters import CharacterTextSplitter, RecursiveCharacterTextSplitter

_semantic_model = None
_semantic_model_lock = threading.Lock()


def _get_semantic_model():
    global _semantic_model
    if _semantic_model is None:
        with _semantic_model_lock:
            if _semantic_model is None:
                from sentence_transformers import SentenceTransformer
                _semantic_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _semantic_model


def _enforce_min_chunk_size(chunks: list[str], min_size: int) -> list[str]:
    if not chunks or min_size <= 0:
        return chunks
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) < min_size and result:
            result[-1] = result[-1] + " " + chunk
        else:
            result.append(chunk)
    return [c for c in result if c.strip()] or chunks


def chunk_fixed(text: str, chunk_size: int, min_chunk_size: int) -> list[str]:
    splitter = CharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=0, separator="")
    chunks = splitter.split_text(text)
    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_overlap(text: str, chunk_size: int, chunk_overlap: int, min_chunk_size: int) -> list[str]:
    splitter = CharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_text(text)
    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_language(
    text: str, chunk_size: int, chunk_overlap_size: int, min_chunk_size: int
) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap_size,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_text(text)
    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_context_aware(
    elements: list[Any], chunk_size: int, min_chunk_size: int
) -> list[str]:
    chunks: list[str] = []
    buffer = ""

    for el in elements:
        category = getattr(el, "category", "NarrativeText")
        text = str(el).strip()
        if not text:
            continue

        if category == "Table":
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            chunks.append(text)
            continue

        if buffer and len(buffer) + len(text) + 1 > chunk_size:
            chunks.append(buffer.strip())
            buffer = text
        else:
            buffer = (buffer + " " + text).strip() if buffer else text

    if buffer.strip():
        chunks.append(buffer.strip())

    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk_semantic(
    text: str, similarity_threshold: float, min_chunk_size: int
) -> list[str]:
    import numpy as np

    model = _get_semantic_model()

    raw_sentences = [s.strip() for s in text.replace("\n", " ").split(". ") if s.strip()]
    if not raw_sentences:
        return [text] if text.strip() else []

    embeddings = model.encode(raw_sentences, convert_to_numpy=True)

    chunks: list[str] = []
    current: list[str] = [raw_sentences[0]]

    for i in range(1, len(raw_sentences)):
        sim = float(
            np.dot(embeddings[i - 1], embeddings[i])
            / (np.linalg.norm(embeddings[i - 1]) * np.linalg.norm(embeddings[i]) + 1e-10)
        )
        if sim >= similarity_threshold:
            current.append(raw_sentences[i])
        else:
            chunks.append(". ".join(current) + ".")
            current = [raw_sentences[i]]

    if current:
        chunks.append(". ".join(current) + ".")

    return _enforce_min_chunk_size(chunks, min_chunk_size)


def chunk(
    text: str,
    strategy: str,
    chunk_size: int = 1000,
    chunk_overlap_size: int = 200,
    similarity_threshold: float = 0.85,
    min_chunk_size: int = 100,
    elements: list[Any] | None = None,
) -> list[str]:
    if strategy == "fixed":
        return chunk_fixed(text, chunk_size, min_chunk_size)
    elif strategy == "overlap":
        return chunk_overlap(text, chunk_size, chunk_overlap_size, min_chunk_size)
    elif strategy == "language":
        return chunk_language(text, chunk_size, chunk_overlap_size, min_chunk_size)
    elif strategy == "context_aware":
        if elements is None:
            return chunk_language(text, chunk_size, chunk_overlap_size, min_chunk_size)
        return chunk_context_aware(elements, chunk_size, min_chunk_size)
    elif strategy == "semantic":
        return chunk_semantic(text, similarity_threshold, min_chunk_size)
    else:
        return chunk_overlap(text, chunk_size, chunk_overlap_size, min_chunk_size)
```

### api/services/ingest_pipeline.py

```python
from __future__ import annotations
import asyncio
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import collection_writes
from config import settings
from services.chunker import chunk as do_chunk
from services import sources
from services import weaviate_client as wc

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}

_jobs: dict[str, dict] = {}
_log = logging.getLogger(__name__)


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _save_upload(src, dest: Path) -> None:
    with dest.open("wb") as fh:
        shutil.copyfileobj(src, fh, length=1024 * 1024)


def _parse_file(path: Path) -> tuple[str, list[Any]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from unstructured.partition.pdf import partition_pdf
        elements = partition_pdf(filename=str(path))
    elif ext == ".docx":
        from unstructured.partition.docx import partition_docx
        elements = partition_docx(filename=str(path))
    elif ext == ".txt":
        from unstructured.partition.text import partition_text
        elements = partition_text(filename=str(path))
    elif ext == ".md":
        from unstructured.partition.md import partition_md
        elements = partition_md(filename=str(path))
    elif ext == ".csv":
        from unstructured.partition.csv import partition_csv
        elements = partition_csv(filename=str(path))
    elif ext == ".json":
        from unstructured.partition.json import partition_json
        elements = partition_json(filename=str(path))
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    text = "\n".join(str(el) for el in elements)
    return text, elements


@collection_writes.serialized("collection")
def _process_job_sync(
    job_id: str,
    file_paths: list[Path],
    tmp_dir: Path,
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    try:
        for path in file_paths:
            try:
                text, elements = _parse_file(path)
                ext = path.suffix.lower().lstrip(".")
                source_type = ext

                chunks = do_chunk(
                    text=text,
                    strategy=strategy,
                    chunk_size=chunk_size,
                    chunk_overlap_size=chunk_overlap,
                    similarity_threshold=similarity_threshold,
                    min_chunk_size=min_chunk_size,
                    elements=elements if strategy == "context_aware" else None,
                )

                now = datetime.now(timezone.utc).isoformat()
                weaviate_chunks = [
                    {
                        "content": c,
                        "source_file": path.name,
                        "source_type": source_type,
                        "chunk_index": i,
                        "chunk_strategy": strategy,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "created_at": now,
                    }
                    for i, c in enumerate(chunks)
                ]

                wc._insert_chunks_sync(collection, weaviate_chunks)

                # Retain the original only after the file has been parsed,
                # chunked and stored. A file that fails any of those steps
                # leaves no orphan source behind.
                try:
                    sources.store(
                        collection,
                        path.name,
                        path.read_bytes(),
                        mimetypes.guess_type(path.name)[0],
                    )
                except OSError as exc:
                    # Retention failing must not fail an otherwise good ingest;
                    # the chunks are already stored. It does cost this
                    # collection its full-fidelity export, so it is logged loudly.
                    _log.error("Could not retain source %s for %r: %s", path.name, collection, exc)

                job["chunks_stored"] += len(chunks)
                job["files_completed"] += 1
            except Exception as exc:
                job["files_failed"] += 1
                job["errors"].append(f"{path.name}: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if job["files_failed"] == 0:
        job["status"] = "completed"
    elif job["files_failed"] == job["files_total"]:
        job["status"] = "failed"
    else:
        job["status"] = "partial"


async def start_ingest_job(
    files: list[Any],
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
) -> str:
    job_id = str(uuid.uuid4())[:8]

    tmp_dir = Path(tempfile.mkdtemp(dir=settings.upload_dir))
    file_paths: list[Path] = []
    # Files the caller sent that this build cannot parse. Dropping them without
    # a word makes a ZIP of 8 files silently report 6, with nothing saying which
    # two were ignored or why.
    skipped: list[str] = []

    try:
        for upload in files:
            raw_name = upload.filename or ""
            safe_name = Path(raw_name).name
            if not safe_name:
                continue
            dest = tmp_dir / safe_name
            # Copy in blocks rather than `await upload.read()`, which held the
            # whole file in memory. Uploads can now reach 512 MB (issue #21),
            # and this container already runs close to its memory budget. The
            # copy blocks, so it runs off the event loop.
            await asyncio.to_thread(_save_upload, upload.file, dest)

            if safe_name.lower().endswith(".zip"):
                resolved_tmp = tmp_dir.resolve()
                with zipfile.ZipFile(dest, "r") as z:
                    for member in z.infolist():
                        if member.is_dir():
                            continue
                        member_path = (tmp_dir / member.filename).resolve()
                        if not member_path.is_relative_to(resolved_tmp):
                            continue
                        if member_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                            z.extract(member, tmp_dir)
                            file_paths.append(member_path)
                        else:
                            skipped.append(
                                f"{safe_name}:{member.filename} "
                                f"(unsupported type '{member_path.suffix.lower() or 'none'}')")
                os.remove(dest)
            elif Path(safe_name).suffix.lower() in SUPPORTED_EXTENSIONS:
                file_paths.append(dest)
            else:
                skipped.append(
                    f"{safe_name} (unsupported type "
                    f"'{Path(safe_name).suffix.lower() or 'none'}')")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if not file_paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        detail = "; ".join(skipped)
        raise ValueError(
            "No supported files found in upload."
            + (f" Skipped: {detail}" if detail else ""))

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "files_total": len(file_paths),
        "files_completed": 0,
        "files_failed": 0,
        "chunks_stored": 0,
        "errors": [],
        "skipped": skipped,
    }

    def _on_done(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            _log.error("ingest job %s failed: %s", job_id, exc)
            job = _jobs.get(job_id)
            if job and job["status"] not in ("completed", "partial", "failed"):
                job["status"] = "failed"
                job["errors"].append(str(exc))

    future = asyncio.get_running_loop().run_in_executor(
        None,
        _process_job_sync,
        job_id,
        file_paths,
        tmp_dir,
        collection,
        strategy,
        chunk_size,
        chunk_overlap,
        similarity_threshold,
        min_chunk_size,
    )
    future.add_done_callback(_on_done)

    return job_id
```

### api/services/rag_pipeline.py

```python
from __future__ import annotations
import time

from services import ollama_client as ollama
from services import weaviate_client as wc

REFORMULATE_SYSTEM = (
    "You are a search query optimizer. Given a user's question, rewrite it as a concise "
    "search query that maximizes retrieval of relevant text chunks from a vector database. "
    "Return only the rewritten query with no explanation."
)

SYNTHESIS_END_USER_SYSTEM = (
    "You are a helpful assistant. Answer the user's question using only the provided context. "
    "If the context does not contain enough information to answer the question, say so clearly. "
    "Do not use any knowledge outside the provided context. "
    "Write in plain, clear language for a non-technical reader."
)

SYNTHESIS_ENGINEER_SYSTEM = (
    "You are a precise technical assistant. Answer the user's question using only the provided context. "
    "Include relevant technical details. Indicate the confidence level of your answer (high/medium/low) "
    "based on how directly the context addresses the question. "
    "If the context is insufficient, state this explicitly."
)


def _build_context_end_user(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] {c['content']}")
    return "\n\n".join(lines)


def _build_context_engineer(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] (source: {c['source_file']}, chunk {c['chunk_index']}) {c['content']}")
    return "\n\n".join(lines)


async def run_query(
    question: str,
    collection: str,
    retrieval_mode: str,
    top_k: int,
    alpha: float,
    include_citations: bool,
    response_format: str,
) -> dict:
    reformulated = await ollama.chat(REFORMULATE_SYSTEM, f"Original question: {question}")
    reformulated = reformulated.strip()

    t0 = time.monotonic()
    if retrieval_mode in ("hnsw", "flat"):
        vector = await ollama.embed(reformulated)
        chunks = await wc.near_vector_query(collection, vector, top_k)
    elif retrieval_mode == "hybrid":
        chunks = await wc.hybrid_query(collection, reformulated, alpha, top_k)
    else:
        chunks = await wc.near_text_query(collection, reformulated, top_k)
    retrieval_ms = int((time.monotonic() - t0) * 1000)

    t1 = time.monotonic()
    if response_format == "engineer":
        context = _build_context_engineer(chunks)
        answer = await ollama.chat(SYNTHESIS_ENGINEER_SYSTEM, f"Context:\n{context}\n\nQuestion: {question}")
    else:
        context = _build_context_end_user(chunks)
        answer = await ollama.chat(SYNTHESIS_END_USER_SYSTEM, f"Context:\n{context}\n\nQuestion: {question}")
    llm_ms = int((time.monotonic() - t1) * 1000)

    citations = None
    if include_citations:
        citations = [
            {
                "source_file": c["source_file"],
                "chunk_index": c["chunk_index"],
                "score": round(c["score"], 4),
                "excerpt": c["content"][:200],
            }
            for c in chunks
        ]

    return {
        "answer": answer.strip(),
        "citations": citations,
        "retrieval_latency_ms": retrieval_ms,
        "llm_latency_ms": llm_ms,
        "chunks_retrieved": len(chunks),
    }
```

### api/services/goldstandard.py

```python
from __future__ import annotations
import asyncio
import json
import logging

import httpx
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from services import ollama_client as ollama
from services import weaviate_client as wc

GS_SYSTEM = (
    "You are creating evaluation data for a RAG system. Given a text chunk, generate one question "
    "that can be answered from this chunk, the correct answer based only on this chunk, and a ground "
    "truth answer (same as the answer). Return a JSON object with keys: question, answer, ground_truth. "
    "Do not include any text outside the JSON object."
)
GS_RETRY_SUFFIX = (
    " Your previous response was not valid JSON. Return ONLY the JSON object with keys: "
    "question, answer, ground_truth. No markdown, no explanation."
)

log = logging.getLogger(__name__)


class GoldStandardError(Exception):
    """Carries an API error code so the router does not have to guess."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

_sessions: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def _sessions_dir() -> Path:
    p = Path(settings.upload_dir) / "goldstandard_sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.json"


def _save_session_sync(session: dict) -> None:
    _session_path(session["session_id"]).write_text(json.dumps(session, indent=2))


async def _save_session(session: dict) -> None:
    await asyncio.to_thread(_save_session_sync, session)


def load_sessions_from_disk() -> None:
    for p in _sessions_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
            _sessions[data["session_id"]] = data
        except Exception:
            pass


def sessions_for(collection: str) -> list[dict]:
    """Every session generated against a collection, in-memory and on disk."""
    found = {sid: sess for sid, sess in _sessions.items()
             if sess.get("collection") == collection}
    # A session written by an import may not be in memory yet.
    for path in _sessions_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if data.get("collection") == collection and data["session_id"] not in found:
            found[data["session_id"]] = data
            _sessions[data["session_id"]] = data
    return list(found.values())


def store_session(session: dict) -> None:
    """Write a session to disk *and* into the in-memory cache.

    Anything outside this module that writes a session file directly will be
    silently undone: the cache still holds the previous version, and the next
    flagging pass writes that back over the file. Import learned this the hard
    way — a restored session reverted to its pre-import orphaned state.
    """
    _sessions[session["session_id"]] = session
    _save_session_sync(session)


def _flag_sessions(collection: str, flag: str, reason: str) -> int:
    """Mark every session for a collection, on disk and in memory.

    Sessions are never deleted and never remapped. A remap that guesses which
    new chunk replaces an old one corrupts an evaluation baseline silently,
    which is worse than an honest flag the user can act on.
    """
    now = datetime.now(timezone.utc).isoformat()
    marked = 0
    for session in sessions_for(collection):
        session[flag] = True
        session[f"{flag}_reason"] = reason
        session[f"{flag}_at"] = now
        try:
            _save_session_sync(session)
        except OSError:
            log.exception("Could not flag gold-standard session %s", session["session_id"])
            continue
        marked += 1
    return marked


def mark_stale(collection: str, reason: str) -> int:
    """Chunk identity changed, so the pairs no longer describe what is stored."""
    return _flag_sessions(collection, "stale", reason)


def mark_orphaned(collection: str, reason: str) -> int:
    """The collection is gone. Retained rather than deleted — see spec §8 rule 4."""
    return _flag_sessions(collection, "orphaned", reason)


def get_session(session_id: str) -> dict | None:
    return _sessions.get(session_id)


def _parse_gs_json(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    The model reliably returns a correct object and then keeps talking --
    "Extra data: line 6 column 1" was the single most common generation
    failure, costing pairs on nearly every session. `raw_decode` reads the
    leading value and ignores whatever follows, so trailing commentary is no
    longer fatal. A reply that opens with prose is still handled, by starting
    at the first brace.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    decoder = json.JSONDecoder()
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    if not positions:
        raise ValueError("model reply contained no JSON object")
    # Anchoring on the first brace is not enough: a reply that explains itself
    # first ("return an object like { this }") puts a brace before the real
    # payload. Try each candidate and keep the first that decodes.
    last_error: Exception | None = None
    for start in positions:
        try:
            result, _ = decoder.raw_decode(text[start:])
        except ValueError as exc:
            last_error = exc
            continue
        if isinstance(result, dict):
            return result
        last_error = ValueError(f"Expected JSON object, got {type(result).__name__}")
    raise last_error or ValueError("model reply contained no usable JSON object")


async def _chat_once(system: str, user: str) -> str:
    """One chat call, retried once if the transport fails.

    Ollama serialises requests per model, so generating while someone is
    querying can push a call past the client timeout. `httpx.ReadTimeout`
    carries an empty message, which is why these used to be recorded as an
    empty string. A transient timeout should cost a retry, not a pair.
    """
    try:
        return await ollama.chat(system, user)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("Ollama call failed (%s); retrying once", type(exc).__name__)
        return await ollama.chat(system, user)


# One initial attempt plus two reprompts. The model's failure mode is malformed
# JSON (a missing comma, an unterminated string), which is independent between
# attempts, so a second reprompt converts most remaining failures into pairs.
# Each attempt costs an LLM call, so the budget is small and fixed.
_GENERATION_ATTEMPTS = 3


async def _generate_pair(chunk: dict) -> dict:
    user_msg = f"Chunk:\n{chunk['content']}"
    data = None
    last_error: Exception | None = None
    for attempt in range(_GENERATION_ATTEMPTS):
        system = GS_SYSTEM if attempt == 0 else GS_SYSTEM + GS_RETRY_SUFFIX
        raw = await _chat_once(system, user_msg)
        try:
            data = _parse_gs_json(raw)
            break
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            log.info("Pair generation attempt %d/%d did not yield valid JSON: %s",
                     attempt + 1, _GENERATION_ATTEMPTS, exc)
    if data is None:
        raise last_error or ValueError("no usable reply from the model")

    return {
        "pair_id": f"p_{uuid.uuid4().hex[:8]}",
        "question": data.get("question", ""),
        "answer": data.get("answer", ""),
        "contexts": [chunk["content"]],
        "ground_truth": data.get("ground_truth", data.get("answer", "")),
        "source_file": chunk.get("source_file", ""),
        "chunk_index": chunk.get("chunk_index", 0),
        "status": "pending",
    }


async def _run_generation(session_id: str, chunks: list[dict]) -> None:
    session = _sessions[session_id]
    cancelled = False
    try:
        for chunk in chunks:
            try:
                pair = await _generate_pair(chunk)
                session["pairs"].append(pair)
                session["pairs_completed"] += 1
                await _save_session(session)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                # Some of these carry an empty str(), which produced sessions
                # whose only record of a lost pair was an empty string.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Gold-standard pair generation failed: %s", reason, exc_info=True)
                session["pairs_failed"] = session.get("pairs_failed", 0) + 1
                session.setdefault("errors", []).append(reason)
                try:
                    await _save_session(session)
                except Exception:
                    pass
            finally:
                if not cancelled:
                    session["pairs_attempted"] = session.get("pairs_attempted", 0) + 1
    finally:
        if cancelled:
            if session.get("status") == "generating":
                session["status"] = "cancelled"
                _save_session_sync(session)
        else:
            if session.get("status") == "generating":
                if not session["pairs"] and session.get("errors"):
                    session["status"] = "failed"
                else:
                    session["status"] = "completed"
            try:
                await _save_session(session)
            except Exception:
                _save_session_sync(session)


async def start_generation(
    collection: str,
    sample_size: int,
    seed: int | None,
) -> dict:
    all_chunks = await wc.sample_chunks(collection, limit=sample_size)
    actual_size = len(all_chunks)

    session_id = f"gs_{uuid.uuid4().hex[:8]}"
    session = {
        "session_id": session_id,
        "collection": collection,
        "status": "generating",
        "pairs_total": actual_size,
        # `attempted` drives progress and always reaches `total`; `completed`
        # counts pairs that actually exist. Reporting one number for both made
        # a session with a failed pair read "3/3" while holding 2.
        "pairs_attempted": 0,
        "pairs_completed": 0,
        "pairs_failed": 0,
        "pairs": [],
    }
    _sessions[session_id] = session
    await _save_session(session)

    task = asyncio.create_task(_run_generation(session_id, all_chunks))
    _tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            s = _sessions.get(session_id)
            if s and s.get("status") == "generating":
                s["status"] = "failed"
                s.setdefault("errors", []).append(str(exc))
                _save_session_sync(s)

    task.add_done_callback(_on_task_done)

    return {
        "session_id": session_id,
        "status": "generating",
        "pairs_total": actual_size,
        "pairs_completed": 0,
    }


async def update_pair(session_id: str, pair_id: str, updates: dict) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    for pair in session["pairs"]:
        if pair["pair_id"] == pair_id:
            pair.update({k: v for k, v in updates.items() if v is not None})
            await _save_session(session)
            return pair
    return None


async def regenerate_pair(session_id: str, pair_id: str) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.get("status") == "generating":
        # The generation loop is appending to session["pairs"] and saving it;
        # regenerating underneath that races with it and can lose a pair.
        raise GoldStandardError(
            "GENERATION_IN_PROGRESS",
            f"Session '{session_id}' is still generating. Wait for it to finish "
            "before regenerating a pair.", 409)
    for i, pair in enumerate(session["pairs"]):
        if pair["pair_id"] == pair_id:
            chunk = {
                "content": pair["contexts"][0],
                "source_file": pair["source_file"],
                "chunk_index": pair["chunk_index"],
            }
            try:
                new_pair = await _generate_pair(chunk)
            except Exception as exc:                  # noqa: BLE001
                # The model regularly returns unparseable JSON. Generation
                # records that and moves on; regeneration used to let it escape
                # as a bare 500. Some of these carry an empty str(), so the
                # type name is always included or the message says nothing.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Regeneration of pair %s failed: %s", pair_id, reason, exc_info=True)
                raise GoldStandardError(
                    "PAIR_GENERATION_FAILED",
                    f"The model did not return a usable question/answer pair "
                    f"({reason}). The existing pair is unchanged; try again.", 502) from exc
            new_pair["pair_id"] = pair_id
            session["pairs"][i] = new_pair
            await _save_session(session)
            return new_pair
    return None


def _save_export_sync(out_path: Path, ragas: list[dict]) -> None:
    out_path.write_text(json.dumps(ragas, indent=2))


async def save_session(session_id: str, filename: str | None) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None

    approved = [p for p in session["pairs"] if p["status"] in ("approved", "edited")]
    excluded = len(session["pairs"]) - len(approved)

    collection = session.get("collection", "export")
    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_]", "", collection.replace(" ", "_"))
        filename = f"{safe}_{ts}.json"

    # Sanitize: only the basename; no path traversal
    filename = Path(filename).name
    out_dir = Path(settings.upload_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / filename).resolve()
    if not str(out_path).startswith(str(out_dir) + "/"):
        raise ValueError("Invalid filename.")

    ragas = [
        {
            "question": p["question"],
            "answer": p["answer"],
            "contexts": p["contexts"],
            "ground_truth": p["ground_truth"],
        }
        for p in approved
    ]
    await asyncio.to_thread(_save_export_sync, out_path, ragas)

    return {
        "filename": filename,
        "pairs_saved": len(approved),
        "pairs_excluded": excluded,
        "download_url": f"/api/goldstandard/download/{filename}",
    }
```

### api/services/packager.py

```python
"""The on-disk export package format: naming, digests, manifest, assembly.

Deliberately separate from the export *job* (`exporter.py`): import reuses the
reader here rather than reimplementing it, which is what keeps the two halves
from drifting.

Format reference: RAG_EXPORT_SPECIFICATIONS.md §4.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from config import settings
from services import ingest_config
from services import model_bundle
from services import retrieval_config
from services import sources
from services import weaviate_client as wc

_log = logging.getLogger(__name__)

PACKAGE_FORMAT = 1
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Files generated *from* the manifest, so they cannot appear in its `files` map:
# the manifest cannot contain its own digest, and `<id8>` is the manifest digest,
# which README.md and retrieve.py both embed. Including them would be circular.
UNDIGESTED = ("manifest.json", "README.md", "retrieve.py")


def exports_dir() -> Path:
    p = Path(settings.exports_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Naming (spec §4.1) ────────────────────────────────────────────────────────

def slug(collection: str) -> str:
    """Lossy label for the filename. Never parsed back — see spec §4.1."""
    s = re.sub(r"[^a-z0-9]+", "-", collection.lower()).strip("-")
    return s or "collection"


def timestamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def package_stem(collection: str, when: datetime, id8: str) -> str:
    return f"ragpkg-{slug(collection)}-{timestamp(when)}-{id8}"


# ── Digests ───────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json_default(value: Any) -> Any:
    """Weaviate returns DATE properties as datetime, which json cannot encode."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


# ── Reading the collection (spec §4.5) ────────────────────────────────────────

def read_chunks(collection: str) -> Iterator[dict]:
    """One record per chunk, streamed. Never materialises the collection.

    A 100k-chunk corpus is roughly 880 MB of JSON, so nothing here accumulates.
    """
    client = wc.get_client()
    col = client.collections.get(collection)

    # digests_for_filename() reloads the index on every call, which would be
    # O(chunks x documents). Build the reverse map once.
    by_filename: dict[str, list[str]] = {}
    for digest, entry in sources.load_index(collection)["documents"].items():
        for name in entry.get("filenames", []):
            by_filename.setdefault(name, []).append(digest)

    for obj in col.iterator(include_vector=True):
        vector = obj.vector
        # Verified against the live stack: iterator() yields a dict keyed by
        # vector name, not a bare list. Code written for a list breaks here.
        if isinstance(vector, dict):
            vector = vector.get("default") or next(iter(vector.values()), None)

        props = dict(obj.properties or {})
        # source_sha256 is a sibling field, not a stored property: spec §4.5
        # keeps the eight chunk properties unchanged. Resolve it from retention.
        digests = by_filename.get(props.get("source_file"), [])
        yield {
            "id": str(obj.uuid),
            "vector": vector,
            "properties": props,
            # More than one digest means the same filename was ingested with
            # different content. Recording null is honest; picking one is not.
            "source_sha256": digests[0] if len(digests) == 1 else None,
        }


# ── Assembly ──────────────────────────────────────────────────────────────────

class _Builder:
    """Accumulates files and their digests as they are written."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}

    def add(self, rel: str, write: Callable[[Path], None]) -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target)
        self.files[rel] = "sha256:" + sha256_file(target)
        return target

    def add_json(self, rel: str, data: Any) -> Path:
        return self.add(rel, lambda p: p.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=_json_default)))


def _resolve_includes(text: str, depth: int = 0) -> str:
    """Expand `@@INCLUDE:name@@` from templates/partials/<name>.md.

    Partials are the single source the package README and the in-app help page
    both render from (spec §10). Without that, the two drift — this project has
    already found five places where documentation and implementation had.
    """
    if depth > 5:
        raise RuntimeError("template includes nested too deeply")
    def swap(match: re.Match) -> str:
        name = match.group(1)
        path = _TEMPLATE_DIR / "partials" / f"{name}.md"
        if not path.is_file():
            raise RuntimeError(f"no such template partial: {name}")
        return _resolve_includes(path.read_text().rstrip("\n"), depth + 1)
    return re.sub(r"@@INCLUDE:([a-z_0-9]+)@@", swap, text)


def _render(template: str, values: dict[str, str]) -> str:
    text = _resolve_includes((_TEMPLATE_DIR / template).read_text())
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", str(value))
    left = re.findall(r"@@[A-Z_0-9]+(?::[a-z_0-9]+)?@@", text)
    if left:
        raise RuntimeError(f"{template}: unsubstituted placeholders {sorted(set(left))}")
    return text


def render_help(embed_dimensions: int | str) -> str:
    """The /help/transfer page, from the same partials as a package README.

    Dimensions are passed in rather than assumed: the only honest source is an
    actual embedding call, which the caller makes.
    """
    return _render("help_transfer.md.tmpl", {
        "EMBED_MODEL": settings.embed_model,
        "EMBED_DIMENSIONS": embed_dimensions,
        "LLM_MODEL": settings.llm_model,
    })


def _ingest_config(collection: str) -> dict | None:
    """The collection's saved chunking settings, or None."""
    return ingest_config.load(collection)


def _goldstandard_sessions(collection: str) -> list[dict]:
    out = []
    d = Path(settings.upload_dir) / "goldstandard_sessions"
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            _log.warning("Skipping unreadable gold-standard session %s", p.name)
            continue
        if data.get("collection") == collection:
            out.append(data)
    return out


def _fidelity_note(fidelity: str) -> str:
    if fidelity == "with-sources":
        return ("The original documents are included, so the collection can be "
                "re-chunked and re-embedded exactly after import.")
    return ("The original documents are not included. This is normal for a "
            "collection built before source retention existed. Import and query "
            "work; re-chunking does not.")


def _retrieve_section(has_script: bool, cfg: dict, collection: str) -> str:
    if not has_script:
        return ("`retrieve.py` is **not** included in this package, because the "
                "collection had no saved retrieval settings when it was exported. "
                "A script carrying stock defaults while claiming to carry tuned "
                "ones would be worse than no script. Query the API directly at "
                "`POST /api/query`, or save retrieval settings and export again.")
    usage = _resolve_includes("@@INCLUDE:retrieve_usage@@")
    return (f"`retrieve.py` queries this collection through a running rag-docker "
            f"API, using the settings it was tuned with.\n\n"
            f"{usage}\n\n"
            f"Baked-in defaults for this package: collection `{collection}`, mode "
            f"`{cfg['retrieval_mode']}`, top-k {cfg['top_k']}, alpha {cfg['alpha']}, "
            f"answer style `{cfg['response_format']}`.")


def build(
    collection: str,
    include_models: bool = False,
    progress: Callable[[int], None] | None = None,
) -> dict:
    """Write one package and return a summary. Blocking; run it in a thread.

    Assembly order matters (spec §6.2): content first, digests as we go, then
    the manifest from those digests, then README.md and retrieve.py which embed
    the manifest digest. Manifest last is what makes <id8> derivable.
    """
    created = datetime.now(timezone.utc)
    created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = exports_dir()
    warnings: list[str] = []

    # Staged inside exports_dir so the final rename is on the same filesystem.
    stage = Path(tempfile.mkdtemp(dir=out_dir, prefix=".build-"))
    tmp_archive: Path | None = None
    try:
        b = _Builder(stage)

        # 1. chunks.jsonl — streamed, one line at a time.
        chunk_count = 0
        dimensions: int | None = None
        first_props: dict | None = None
        unresolved = 0

        def write_chunks(path: Path) -> None:
            nonlocal chunk_count, dimensions, first_props, unresolved
            with path.open("w", encoding="utf-8") as fh:
                for record in read_chunks(collection):
                    if dimensions is None and record["vector"]:
                        dimensions = len(record["vector"])
                    if first_props is None:
                        first_props = record["properties"]
                    if record["source_sha256"] is None:
                        unresolved += 1
                    fh.write(json.dumps(record, default=_json_default))
                    fh.write("\n")
                    chunk_count += 1
                    if progress and chunk_count % 500 == 0:
                        progress(chunk_count)

        b.add("chunks.jsonl", write_chunks)
        if progress:
            progress(chunk_count)

        # 2. collection.json
        b.add_json("collection.json", wc._collection_config_sync(collection))

        # 3. configs
        ingest_cfg = _ingest_config(collection)
        if ingest_cfg is not None:
            b.add_json("ingest_config.json", ingest_cfg)

        retrieval_cfg, is_default = retrieval_config.resolve(collection)
        has_saved_retrieval = not is_default
        b.add_json("retrieval_config.json", retrieval_cfg)

        # 4. gold standard sessions
        for session in _goldstandard_sessions(collection):
            b.add_json(f"goldstandard/{session['session_id']}.json", session)

        # 5. sources, when they exist
        index = sources.load_index(collection)
        fidelity = "with-sources" if index["documents"] else "chunks-only"
        source_document_count = len(index["documents"])
        if fidelity == "with-sources":
            b.add_json("sources/index.json", index)
            src_dir = sources.collection_dir(collection)
            for digest in index["documents"]:
                blob = src_dir / digest
                if not blob.exists():
                    warnings.append(f"retained source {digest[:12]} is missing on disk")
                    continue
                b.add(f"sources/{digest}", lambda p, s=blob: shutil.copyfile(s, p))

        # 5b. bundled models, resolved through each model's manifest
        models_bundled = False
        if include_models:
            wanted = [settings.embed_model, settings.llm_model]
            if not model_bundle.store_available():
                warnings.append(
                    "models were requested but the Ollama model store is not mounted; "
                    "the package was built without them")
            else:
                for model in wanted:
                    try:
                        for rel, source in model_bundle.export_model(model, stage):
                            b.add(rel, lambda p, s=source: shutil.copyfile(s, p))
                    except (OSError, ValueError) as exc:
                        warnings.append(f"model '{model}' could not be bundled: {exc}")
                    else:
                        models_bundled = True

        if unresolved:
            warnings.append(
                f"{unresolved} chunk(s) could not be linked to a single source "
                "document; their source_sha256 is null"
            )

        # 6. manifest — written from the digests collected above
        chunking = None
        if ingest_cfg:
            chunking = {
                "strategy": ingest_cfg.get("chunking_strategy"),
                "chunk_size": ingest_cfg.get("chunk_size"),
                "chunk_overlap": ingest_cfg.get("chunk_overlap"),
            }
        elif first_props:
            # No saved config: report what the chunks themselves say.
            chunking = {
                "strategy": first_props.get("chunk_strategy"),
                "chunk_size": first_props.get("chunk_size"),
                "chunk_overlap": first_props.get("chunk_overlap"),
            }

        manifest = {
            "package_format": PACKAGE_FORMAT,
            "created_at": created_at,
            "produced_by": {
                "platform": "rag-docker",
                "weaviate": str(wc._meta_sync().get("version", "unknown")),
            },
            "collection": {
                "name": collection,
                "chunk_count": chunk_count,
                "source_document_count": source_document_count,
            },
            "embedding": {
                "model": settings.embed_model,
                "dimensions": dimensions,
            },
            "llm": {"model": settings.llm_model},
            "chunking": chunking,
            "fidelity": fidelity,
            # What the package actually carries, not what was asked for: a
            # request that could not be honoured is recorded in `warnings`.
            "models_bundled": models_bundled,
            "bundled_models": sorted(
                {settings.embed_model.split(":")[0], settings.llm_model.split(":")[0]}
            ) if models_bundled else [],
            # False means the collection had no saved retrieval settings, so a
            # script would have carried stock defaults while implying tuned ones.
            "retrieve_script": has_saved_retrieval,
            "files": dict(sorted(b.files.items())),
            "warnings": warnings,
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        id8 = sha256_file(manifest_path)[:8]

        # 7. the generated pair, which embed <id8>
        stem = package_stem(collection, created, id8)
        filename = f"{stem}.tar.gz"

        contents_extra = ""
        if models_bundled:
            contents_extra += ("models/                 Ollama model files, so the "
                               "target needs no registry\n")
        if fidelity == "with-sources":
            contents_extra = ("sources/                the original documents, "
                              "content-addressed\n")
        if any(k.startswith("goldstandard/") for k in manifest["files"]):
            contents_extra += "goldstandard/           evaluation sessions for this collection\n"
        if has_saved_retrieval:
            contents_extra += "retrieve.py             a standalone query script for this collection\n"

        if has_saved_retrieval:
            (stage / "retrieve.py").write_text(_render("retrieve.py.tmpl", {
                "COLLECTION_NAME": collection,
                "PACKAGE_FILENAME": filename,
                "ID8": id8,
                "CREATED_AT": created_at,
                "EMBED_MODEL": settings.embed_model,
                "EMBED_DIMENSIONS": dimensions if dimensions is not None else "unknown",
                "RETRIEVAL_MODE": retrieval_cfg["retrieval_mode"],
                "TOP_K": retrieval_cfg["top_k"],
                "ALPHA": retrieval_cfg["alpha"],
                "RESPONSE_FORMAT": retrieval_cfg["response_format"],
            }))
            (stage / "retrieve.py").chmod(0o755)

        (stage / "README.md").write_text(_render("package_readme.md.tmpl", {
            "COLLECTION_NAME": collection,
            "CHUNK_COUNT": chunk_count,
            "SOURCE_DOCUMENT_COUNT": source_document_count,
            "FIDELITY": fidelity,
            "FIDELITY_NOTE": _fidelity_note(fidelity),
            "EMBED_MODEL": settings.embed_model,
            "EMBED_DIMENSIONS": dimensions if dimensions is not None else "unknown",
            "CREATED_AT": created_at,
            "ID8": id8,
            "PACKAGE_FILENAME": filename,
            "RETRIEVE_SECTION": _retrieve_section(has_saved_retrieval, retrieval_cfg, collection),
            "CONTENTS_EXTRA": contents_extra,
        }))

        # 8. archive under a temporary name, then rename, so a partial file is
        #    never mistaken for a package.
        tmp_archive = out_dir / f".{stem}.tar.gz.partial"
        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(stage, arcname=stem)
        final = out_dir / filename
        tmp_archive.replace(final)
        tmp_archive = None

        return {
            "filename": filename,
            "size_bytes": final.stat().st_size,
            "chunk_count": chunk_count,
            "source_document_count": source_document_count,
            "fidelity": fidelity,
            "models_bundled": models_bundled,
            "retrieve_script": has_saved_retrieval,
            "id8": id8,
            "warnings": warnings,
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if tmp_archive is not None:
            tmp_archive.unlink(missing_ok=True)


# ── Reading a package back ────────────────────────────────────────────────────

class PackageError(Exception):
    """A package that cannot be used, carrying the spec §11 error code."""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def read_manifest(archive: Path) -> dict:
    """Read manifest.json out of a package without extracting the whole archive."""
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if Path(member.name).name == "manifest.json" and member.isfile():
                fh = tar.extractfile(member)
                if fh is None:
                    break
                return json.loads(fh.read().decode("utf-8"))
    raise ValueError("no manifest.json in package")


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing any member that would land outside dest.

    A package is a file someone was handed, so it is untrusted input. Python
    3.12 has `filter="data"` for this; doing it explicitly keeps the guarantee
    on 3.11 and makes the refusal auditable.
    """
    root = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise PackageError(
                "PACKAGE_UNREADABLE",
                f"Package contains a link ('{member.name}'), which is not allowed.",
                {"member": member.name})
        target = (dest / member.name).resolve()
        if target != root and root not in target.parents:
            raise PackageError(
                "PACKAGE_UNREADABLE",
                f"Package contains a path outside the archive ('{member.name}').",
                {"member": member.name})
    tar.extractall(dest)


def open_package(archive: Path, dest: Path) -> tuple[Path, dict]:
    """Checks 1 and 2 of spec §6.2: readable archive, understood format.

    Returns (package root directory, manifest).
    """
    if not archive.is_file():
        raise PackageError("PACKAGE_UNREADABLE",
                           f"No package named '{archive.name}' in the exports directory.")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, dest)
    except PackageError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise PackageError("PACKAGE_UNREADABLE",
                           f"'{archive.name}' is not a readable .tar.gz archive.",
                           {"reason": f"{type(exc).__name__}: {exc}"}) from exc

    roots = [p for p in dest.iterdir() if p.is_dir()]
    # §4.1 requires exactly one top-level directory.
    if len(roots) != 1:
        raise PackageError("PACKAGE_UNREADABLE",
                           f"'{archive.name}' does not expand to a single directory.",
                           {"found": sorted(p.name for p in roots)})
    pkg = roots[0]

    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                           f"'{archive.name}' has no manifest.json, so it is not a RAG package.")
    try:
        manifest = json.loads(manifest_path.read_text())
    except ValueError as exc:
        raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                           f"The manifest in '{archive.name}' is not valid JSON.",
                           {"reason": str(exc)}) from exc

    fmt = manifest.get("package_format")
    if not isinstance(fmt, int) or fmt > PACKAGE_FORMAT:
        raise PackageError(
            "PACKAGE_FORMAT_UNSUPPORTED",
            f"Package format {fmt!r} is newer than this instance understands "
            f"(supported: {PACKAGE_FORMAT}). Upgrade rag-docker to import it.",
            {"package_format": fmt, "supported": PACKAGE_FORMAT})
    return pkg, manifest


def verify_digests(pkg: Path, manifest: dict) -> None:
    """Check 3 of spec §6.2. Names the first file that fails."""
    for rel, expected in sorted(manifest.get("files", {}).items()):
        target = pkg / rel
        if not target.is_file():
            raise PackageError("PACKAGE_CORRUPT",
                               f"'{rel}' is listed in the manifest but missing from the package.",
                               {"file": rel})
        actual = "sha256:" + sha256_file(target)
        if actual != expected:
            raise PackageError(
                "PACKAGE_CORRUPT",
                f"'{rel}' does not match its manifest digest; the package is damaged "
                "or was modified after export.",
                {"file": rel, "expected": expected, "actual": actual})


def iter_chunks_file(pkg: Path) -> Iterator[dict]:
    """Stream chunks.jsonl back. Mirrors read_chunks() on the way in."""
    path = pkg / "chunks.jsonl"
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise PackageError("PACKAGE_CORRUPT",
                                   f"chunks.jsonl line {number} is not valid JSON.",
                                   {"file": "chunks.jsonl", "line": number}) from exc
```

### api/services/exporter.py

```python
"""The export job: lifecycle, progress and the one-at-a-time guard.

Format lives in `packager.py`. This module only decides when a build runs, what
its status looks like while it does, and that two builds of the same collection
never overlap — they would race on the staging directory and the temporary
archive.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid

from services import packager

_log = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
# collection name -> job_id of the export currently running for it
_active: dict[str, str] = {}
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def active_job_for(collection: str) -> str | None:
    with _lock:
        return _active.get(collection)


def _run(job_id: str, collection: str, include_models: bool) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    def progress(written: int) -> None:
        job["chunks_written"] = written

    try:
        result = packager.build(collection, include_models=include_models, progress=progress)
    except Exception as exc:                       # noqa: BLE001 - reported to the caller
        _log.exception("Export of %r failed", collection)
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
    else:
        job.update(
            status="completed",
            filename=result["filename"],
            size_bytes=result["size_bytes"],
            chunks_written=result["chunk_count"],
            source_document_count=result["source_document_count"],
            fidelity=result["fidelity"],
            models_bundled=result["models_bundled"],
            retrieve_script=result["retrieve_script"],
            warnings=result["warnings"],
        )
    finally:
        with _lock:
            # Only clear the slot if it is still ours.
            if _active.get(collection) == job_id:
                del _active[collection]


async def start_export_job(collection: str, include_models: bool = False) -> str:
    """Register the job and run the build off the event loop.

    Raises RuntimeError carrying the running job id if one is already in flight
    for this collection.
    """
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        running = _active.get(collection)
        if running is not None:
            raise RuntimeError(running)
        _active[collection] = job_id

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "collection": collection,
        "chunks_written": 0,
        "filename": None,
        "size_bytes": None,
        "source_document_count": None,
        "fidelity": None,
        "models_bundled": None,
        "retrieve_script": None,
        "warnings": [],
        "error": None,
    }

    # to_thread keeps the blocking Weaviate iteration off the event loop, so an
    # export does not stall ingest or query (spec §6.1).
    asyncio.create_task(asyncio.to_thread(_run, job_id, collection, include_models))
    return job_id
```

### api/services/importer.py

```python
"""The import job: validate a package, then build a collection from it.

Reads the package through `packager.py` so the format has exactly one
implementation. Validation order is spec §6.2 and stops at the first failure.

**Atomicity, and where it departs from the plan.** The plan said to build into a
temporary collection and rename it on success. Weaviate has no rename:
`client.collections` offers create/delete/exists/get/list_all and nothing else,
confirmed against 4.23.1. So the guarantee in spec §6.5 — a failed import leaves
no partial collection and never destroys the target — is met differently
depending on whether there is anything to protect:

* `abort` and `rename` produce a collection name that does not yet exist, so the
  build goes straight into it and is deleted on failure. Nothing pre-existing is
  at risk, and there is no second pass.
* `replace` builds into a temporary collection first, to prove the package
  inserts cleanly, and only then deletes the existing collection and builds the
  real one. That costs a second insert pass, which is the price of a
  non-destructive replace in a database that cannot rename. If the second pass
  fails, the temporary collection is *kept* and named in the error, so the data
  is recoverable rather than lost.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import logging
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from services import collection_writes, collection_recovery
from config import settings
from services import goldstandard
from services import model_bundle
from services import packager
from services import retrieval_config
from services import sources
from services import weaviate_client as wc
from services.packager import PackageError

_log = logging.getLogger(__name__)

ON_CONFLICT = ("abort", "rename", "replace")

_jobs: dict[str, dict] = {}
_active: set[str] = set()
_lock = threading.Lock()

# Weaviate capitalises the first character of a collection name and rejects
# anything outside [A-Za-z0-9_]. Both were confirmed against the live server.
_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Surviving a hard kill ─────────────────────────────────────────────────────
#
# `abort` and `rename` build straight into the target collection, so there is no
# staging name for the startup sweep to recognise. A SIGKILL during the insert
# would leave a half-filled collection that looks like a real one. A marker
# written before the build, and removed after it, lets the next start tell the
# two apart.

def _markers_dir() -> Path:
    d = Path(settings.upload_dir) / "imports_in_progress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(collection: str) -> Path:
    return _markers_dir() / f"{_safe_file(collection)}.json"


def _mark_started(collection: str, expected_chunks: int, job_id: str) -> None:
    _marker_path(collection).write_text(json.dumps({
        "collection": collection,
        "expected_chunks": expected_chunks,
        "job_id": job_id,
    }, indent=2))


def _mark_finished(collection: str) -> None:
    _marker_path(collection).unlink(missing_ok=True)


# Extraction workspaces created by an import or a re-chunk. Both remove their
# own directory in a `finally`, which a hard kill skips -- twelve of these were
# found holding 152 MB after the kill tests, and a with-models package would
# leave 2.3 GB behind each time.
_WORKDIR_PREFIXES = ("import-", "rechunk-")


def sweep_stale_workdirs() -> list[str]:
    """Remove extraction directories abandoned by a killed job."""
    root = Path(settings.upload_dir)
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith(_WORKDIR_PREFIXES):
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            _log.exception("Could not remove stale work directory %s", entry)
            continue
        removed.append(f"{entry.name} ({size // (1024 * 1024)} MB)")
    return removed


def sweep_interrupted_imports() -> list[str]:
    """Remove collections left half-built by a killed import.

    The expected chunk count is compared rather than trusting the marker alone:
    a marker that outlived a *successful* import — a failed unlink, a disk
    error — must not cost the user a complete collection.
    """
    removed: list[str] = []
    for marker in sorted(_markers_dir().glob("*.json")):
        try:
            data = json.loads(marker.read_text())
            collection = data["collection"]
            expected = int(data.get("expected_chunks", -1))
        except (OSError, ValueError, KeyError):
            marker.unlink(missing_ok=True)
            continue
        try:
            if wc._collection_exists_sync(collection):
                col = wc.get_client().collections.get(collection)
                actual = col.aggregate.over_all(total_count=True).total_count or 0
                if expected < 0 or actual != expected:
                    wc.get_client().collections.delete(collection)
                    removed.append(f"{collection} ({actual} of {expected} chunks)")
        except Exception:                             # noqa: BLE001
            _log.exception("Could not resolve interrupted import of %r", collection)
            continue
        marker.unlink(missing_ok=True)
    return removed


def canonical(name: str) -> str:
    """The name Weaviate will actually store, so collision checks are honest."""
    return name[:1].upper() + name[1:] if name else name


def _safe_file(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _rename_target(name: str, id8: str) -> str:
    """Spec §6.4's `rename`, adjusted to a name Weaviate accepts.

    The spec says `<name>-imported-<id8>`; Weaviate rejects hyphens with a 422,
    so the separator is an underscore. A numeric suffix is added only if that
    name is taken too, which happens when the same package is imported twice.
    """
    base = f"{canonical(name)}_imported_{id8}"
    if not wc._collection_exists_sync(base):
        return base
    for n in range(2, 100):
        candidate = f"{base}_{n}"
        if not wc._collection_exists_sync(candidate):
            return candidate
    raise PackageError("COLLECTION_EXISTS",
                       f"Could not find a free name based on '{base}'.")


# ── Validation (spec §6.2) ────────────────────────────────────────────────────

def _check_embedding(manifest: dict) -> None:
    """Check 4. A refusal, not a warning — see spec §6.2."""
    embedding = manifest.get("embedding") or {}
    pkg_model = embedding.get("model")
    pkg_dims = embedding.get("dimensions")
    our_model = settings.embed_model

    if pkg_model != our_model:
        fidelity = manifest.get("fidelity")
        remedy = ("This package is `with-sources`, so it can be re-embedded after "
                  "import once that is supported."
                  if fidelity == "with-sources" else
                  "This package is `chunks-only`, so it cannot be re-embedded from "
                  "the original documents. Use an instance running "
                  f"'{pkg_model}', or re-export from one.")
        raise PackageError(
            "EMBEDDING_MISMATCH",
            f"The package was embedded with '{pkg_model}' ({pkg_dims} dimensions) "
            f"but this instance uses '{our_model}'. Vectors from a different model "
            f"are meaningless here, not merely different, so the import is refused. "
            + remedy,
            {"package_model": pkg_model, "package_dimensions": pkg_dims,
             "instance_model": our_model})

    # Same model name but a different width means one side is not what it claims.
    if pkg_dims is not None:
        actual = _probe_dimensions()
        if actual is not None and actual != pkg_dims:
            raise PackageError(
                "EMBEDDING_MISMATCH",
                f"The package reports {pkg_dims}-dimension vectors from "
                f"'{pkg_model}', but this instance's '{our_model}' produces "
                f"{actual}. The models share a name but not a vector space.",
                {"package_model": pkg_model, "package_dimensions": pkg_dims,
                 "instance_model": our_model, "instance_dimensions": actual})


_probed_dimensions: int | None = None


def _probe_dimensions() -> int | None:
    """Embed a token once to learn this instance's real vector width."""
    global _probed_dimensions
    if _probed_dimensions is None:
        try:
            from services import ollama_client
            vector = asyncio.run(ollama_client.embed("dimension probe"))
            _probed_dimensions = len(vector)
        except Exception as exc:                      # noqa: BLE001
            _log.warning("Could not probe embedding dimensions: %s", exc)
            return None
    return _probed_dimensions


def _ensure_models(pkg: Path, manifest: dict) -> list[str]:
    """Spec §6.3. The embedding model is the one that decides the import.

    Present by name  -> skip; an existing model is assumed deliberate.
    Absent, bundled  -> install, then verify Ollama actually reports it.
    Absent, unbundled-> EMBEDDING_MODEL_MISSING.
    """
    notes: list[str] = []
    if not model_bundle.store_available():
        # Nothing can be installed or checked; say so rather than guess.
        notes.append("the Ollama model store is not mounted, so models were not checked")
        return notes

    embed_model = settings.embed_model
    llm_model = settings.llm_model
    bundled = model_bundle.bundled_models(pkg)

    for model, required in ((embed_model, True), (llm_model, False)):
        name = model_bundle.split_ref(model)[0]
        if model_bundle.is_installed(model):
            notes.append(f"model '{name}' already present; left untouched")
            continue
        if name not in bundled:
            if not required:
                notes.append(f"model '{name}' is absent and not bundled; "
                             "queries will fail until it is pulled")
                continue
            pkg_model = (manifest.get("embedding") or {}).get("model")
            raise PackageError(
                "EMBEDDING_MODEL_MISSING",
                f"This instance does not have the embedding model '{name}' and the "
                f"package does not bundle it. The vectors in this package were "
                f"produced by '{pkg_model}', so nothing can embed a query against "
                f"them. Pull it with `docker compose exec ollama ollama pull {name}`, "
                f"or import a package exported with include_models=true.",
                {"model": name, "package_model": pkg_model, "bundled": bundled})
        try:
            model_bundle.install_model(pkg, model)
        except (OSError, ValueError) as exc:
            raise PackageError(
                "EMBEDDING_MODEL_MISSING" if required else "IMPORT_FAILED",
                f"Could not install bundled model '{name}': {exc}",
                {"model": name}) from exc
        if not model_bundle.is_installed(model):
            raise PackageError(
                "EMBEDDING_MODEL_MISSING" if required else "IMPORT_FAILED",
                f"Installed '{name}' from the package but Ollama does not report it.",
                {"model": name})
        notes.append(f"model '{name}' installed from the package")
    return notes


# ── Building ──────────────────────────────────────────────────────────────────

def _create_from_package(name: str, pkg: Path) -> None:
    cfg_path = pkg / "collection.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    wc._create_collection_sync(
        name,
        cfg.get("index_type", "hnsw"),
        cfg.get("distance_metric", "cosine"),
        cfg.get("hnsw_config") or {},
    )


def _insert_chunks(name: str, pkg: Path, manifest: dict,
                   progress) -> int:
    """Insert every chunk with its original uuid and vector.

    The uuid is preserved deliberately: gold-standard sessions reference chunks
    by id, and that is the reason sessions are exportable at all.
    """
    client = wc.get_client()
    col = client.collections.get(name)
    expected_dims = (manifest.get("embedding") or {}).get("dimensions")
    written = 0
    with col.batch.dynamic() as batch:
        for record in packager.iter_chunks_file(pkg):
            vector = record.get("vector")
            if not isinstance(vector, list) or not vector:
                raise PackageError("PACKAGE_CORRUPT",
                                   f"Chunk {record.get('id')} has no vector.",
                                   {"file": "chunks.jsonl", "id": record.get("id")})
            if expected_dims and len(vector) != expected_dims:
                raise PackageError(
                    "PACKAGE_CORRUPT",
                    f"Chunk {record.get('id')} has {len(vector)} dimensions but the "
                    f"manifest declares {expected_dims}.",
                    {"file": "chunks.jsonl", "id": record.get("id")})
            batch.add_object(properties=record["properties"],
                             uuid=record["id"],
                             vector=vector)
            written += 1
            if progress and written % 500 == 0:
                progress(written)
        if batch.number_errors > 0:
            raise PackageError(
                "PACKAGE_CORRUPT",
                f"Weaviate rejected {batch.number_errors} object(s) while importing.",
                {"errors": batch.number_errors})
    if progress:
        progress(written)
    return written


def _restore_sidecars(target: str, pkg: Path, original: str) -> list[str]:
    """Sources, configs and gold-standard sessions. Returns notes for the job."""
    notes: list[str] = []

    src = pkg / "sources"
    if src.is_dir():
        dest = sources.collection_dir(target)
        dest.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.is_file():
                shutil.copyfile(item, dest / item.name)

    ingest_cfg = pkg / "ingest_config.json"
    if ingest_cfg.is_file():
        data = json.loads(ingest_cfg.read_text())
        data["collection"] = target
        out = Path(settings.upload_dir) / "ingest_configs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{_safe_file(target)}.json").write_text(json.dumps(data, indent=2, sort_keys=True))

    retrieval_cfg = pkg / "retrieval_config.json"
    if retrieval_cfg.is_file():
        data = json.loads(retrieval_cfg.read_text())
        data["collection"] = target
        try:
            retrieval_config.save(data)
        except Exception as exc:                      # noqa: BLE001
            notes.append(f"retrieval settings could not be restored: {exc}")

    gold = pkg / "goldstandard"
    if gold.is_dir():
        (Path(settings.upload_dir) / "goldstandard_sessions").mkdir(parents=True, exist_ok=True)
        restored = 0
        for session_file in sorted(gold.glob("*.json")):
            try:
                data = json.loads(session_file.read_text())
            except ValueError:
                notes.append(f"gold-standard session {session_file.name} was unreadable")
                continue
            # The session points at the collection by name; after a rename that
            # name is different, and a session pointing at nothing is worse than
            # no session.
            data["collection"] = target
            # A restored session is valid again for this collection, so any
            # orphan flag from the collection it replaced no longer applies.
            data.pop("orphaned", None)
            data.pop("orphaned_reason", None)
            data.pop("orphaned_at", None)
            # Write through the service: a direct file write leaves the
            # in-memory cache holding the old version, which the next flagging
            # pass would write straight back over this one.
            goldstandard.store_session(data)
            restored += 1
        if restored:
            notes.append(f"{restored} gold-standard session(s) restored")
            if target != original:
                notes.append(f"their collection was rewritten from '{original}' to '{target}'")
    return notes


@collection_writes.serialized("target")
def _build(target: str, pkg: Path, manifest: dict, progress) -> int:
    """Create and fill `target`. Removes it again if anything fails."""
    _create_from_package(target, pkg)
    try:
        return _insert_chunks(target, pkg, manifest, progress)
    except Exception:
        # Spec §6.5: a failure part-way leaves no partial collection.
        try:
            wc.get_client().collections.delete(target)
        except Exception:                             # noqa: BLE001
            _log.exception("Could not remove partial collection %r", target)
        raise


def _run(job_id: str, filename: str, on_conflict: str) -> None:
    job = _jobs[job_id]
    job["status"] = "running"
    work = Path(tempfile.mkdtemp(prefix="import-", dir=settings.upload_dir))
    temp_collection: str | None = None
    marked: str | None = None
    # True only while a *successfully built* staging collection is on disk.
    # _build deletes its own collection on failure, so temp_collection being
    # set is not by itself evidence that anything survived to recover.
    staged = False
    ownership: dict | None = None

    def progress(n: int) -> None:
        job["chunks_written"] = n

    try:
        archive = packager.exports_dir() / Path(filename).name
        pkg, manifest = packager.open_package(archive, work)        # checks 1, 2
        packager.verify_digests(pkg, manifest)                      # check 3
        _check_embedding(manifest)                                  # check 4

        original = manifest["collection"]["name"]
        id8 = packager.sha256_file(pkg / "manifest.json")[:8]
        job["collection"] = original
        job["fidelity"] = manifest.get("fidelity")

        replace_notes: list[str] = []
        target = canonical(original)
        if not _NAME_OK.match(target):
            raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                               f"'{original}' is not a usable collection name.",
                               {"name": original})

        model_notes = _ensure_models(pkg, manifest)                 # spec §6.3

        # The synchronous executor holds one target guard through conflict
        # check, both build passes, cutover and sidecar restoration.
        with (collection_writes.guard(target) if on_conflict == "replace" else nullcontext()):
            exists = wc._collection_exists_sync(target)                 # check 5
            if exists and on_conflict == "abort":
                raise PackageError(
                    "COLLECTION_EXISTS",
                    f"A collection named '{target}' already exists. Import with "
                    "on_conflict='rename' to keep both, or 'replace' to overwrite it.",
                    {"collection": target})

            if exists and on_conflict == "rename":
                target = _rename_target(original, id8)

            if exists and on_conflict == "replace":
                # Prove the package inserts cleanly before destroying anything.
                ownership = collection_recovery.begin(target, "import", wc.get_client())
                temp_collection = ownership["staging"]
                _build(temp_collection, pkg, manifest, progress)
                job["chunks_written"] = 0
                collection_recovery.retain(ownership, package=pkg)
                staged = True
                # Counted before the delete, because the delete is what orphans them.
                orphaned = len(goldstandard.sessions_for(target))
                wc._delete_collection_sync(target)   # also drops its sources + config
                if orphaned:
                    # Spec §8 rule 4: silently destroying evaluation work is worse
                    # than reporting it, and refusing the import would block a
                    # legitimate operation over data the user may not care about.
                    replace_notes.append(
                        f"{orphaned} gold-standard session(s) from the replaced "
                        "collection were kept and marked orphaned")

            expected = manifest.get("collection", {}).get("chunk_count", -1)
            _mark_started(target, expected, job_id)
            marked = target
            written = _build(target, pkg, manifest, progress)
            _mark_finished(target)
            marked = None
            notes = model_notes + replace_notes + _restore_sidecars(target, pkg, original)

            if staged and temp_collection:
                try:
                    collection_recovery.discard(ownership, wc.get_client())
                except Exception:                         # noqa: BLE001
                    _log.exception("Could not remove staging collection %r", temp_collection)
                staged = False

            job.update(status="completed", collection=target, original_collection=original,
                       chunks_written=written, renamed=(target != canonical(original)),
                       notes=notes)

    except PackageError as exc:
        job.update(status="failed", error_code=exc.code, error=exc.message,
                   error_detail=exc.detail)
        if staged and temp_collection:
            # The real build failed after the target was deleted. Keeping the
            # staging collection means the data is recoverable, not lost.
            job["error"] = (exc.message + f" The imported data is available as "
                            f"'{temp_collection}'.")
            job["error_detail"] = {**(exc.detail or {}), "recovered_as": temp_collection,
                                   "sidecar_snapshots": str(collection_recovery._root() / ownership["operation_id"])}
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Import of %r failed", filename)
        job.update(status="failed", error_code="IMPORT_FAILED",
                   error=f"{type(exc).__name__}: {exc}")
        if staged and temp_collection:
            job["error"] = (job["error"] + f" The imported data is available as "
                            f"'{temp_collection}'.")
            job["error_detail"] = {"recovered_as": temp_collection,
                                   "sidecar_snapshots": str(collection_recovery._root() / ownership["operation_id"])}
    finally:
        # A handled failure already removed the partial collection, so the
        # marker has nothing left to describe. Only a hard kill leaves one
        # behind, which is the case the startup sweep exists for.
        if marked:
            _mark_finished(marked)
        if ownership and ownership["state"] == "scratch":
            try:
                collection_recovery.discard(ownership, wc.get_client())
            except Exception:
                _log.exception("Could not remove owned import scratch %r", ownership["staging"])
        shutil.rmtree(work, ignore_errors=True)
        with _lock:
            _active.discard(filename)


async def start_import_job(filename: str, on_conflict: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        if filename in _active:
            raise RuntimeError(filename)
        _active.add(filename)

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "on_conflict": on_conflict,
        "collection": None,
        "original_collection": None,
        "chunks_written": 0,
        "fidelity": None,
        "renamed": False,
        "notes": [],
        "error": None,
        "error_code": None,
        "error_detail": None,
    }
    asyncio.create_task(asyncio.to_thread(_run, job_id, filename, on_conflict))
    return job_id
```

### api/services/model_bundle.py

```python
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
```

### api/services/tuning.py

```python
"""Re-chunking, re-embedding and re-indexing a collection in place.

Spec §7. Every operation here rebuilds the collection, because chunk identity
or vector width changes and Weaviate cannot alter either in place.

**Safety.** Each rebuild is staged: the new chunks are built into a temporary
collection first, and the live one is replaced only once that succeeds. Weaviate
has no rename (see `importer.py`), so the final step copies vectors out of the
staging collection rather than re-embedding — one embedding pass, not two. A
preparation failure leaves the original collection untouched. Replacement can
still fail after cutover; reindex then marks retained evaluation pairs stale.

**Gold standard.** Anything that changes chunk identity marks every session for
the collection `stale`, with a reason and a timestamp. Sessions are never
deleted and never remapped (spec §7.3).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import mimetypes
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from services import collection_writes, collection_recovery
from config import settings
from services import goldstandard
from services import sources
from services import weaviate_client as wc
from services.chunker import chunk as do_chunk
from services.ingest_pipeline import _parse_file
from services.packager import PackageError

_log = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_active: set[str] = set()
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Reading the collection's own chunks ───────────────────────────────────────

def _existing_chunks(collection: str) -> list[dict]:
    """Stored properties, without vectors. Used when re-embedding chunk text."""
    col = wc.get_client().collections.get(collection)
    return [dict(o.properties or {}) for o in col.iterator()]


def _existing_records(collection: str) -> list[dict]:
    """Read the supported single-vector corpus without regenerating identity."""
    return list(_iter_existing_records(collection))


def _iter_existing_records(collection: str):
    seen = set()
    col = wc.get_client().collections.get(collection)
    for obj in col.iterator(include_vector=True):
        identity = str(obj.uuid)
        vector = obj.vector
        if isinstance(vector, dict):
            if set(vector) != {"default"}:
                raise RuntimeError("Reindex requires the collection's single default vector")
            vector = vector["default"]
        if (not isinstance(vector, list) or not vector
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in vector)):
            raise RuntimeError(f"Reindex cannot preserve the stored vector for {identity}")
        if identity in seen:
            raise RuntimeError(f"Reindex received duplicate stored UUID {identity}")
        seen.add(identity)
        yield {"id": identity, "vector": copy.deepcopy(vector),
               "properties": copy.deepcopy(dict(obj.properties or {}))}


def _write_records(collection: str, records: list[dict]) -> None:
    """Supply exact records, drain the batch, then compare backend readback."""
    col = wc.get_client().collections.get(collection)
    with col.batch.dynamic() as batch:
        for record in records:
            batch.add_object(properties=copy.deepcopy(record["properties"]),
                             uuid=record["id"], vector=copy.deepcopy(record["vector"]))
    if batch.number_errors:
        raise RuntimeError(f"{batch.number_errors} error(s) copying reindex records")
    _verify_records(collection, records)


def _verify_records(collection: str, records: list[dict]) -> None:
    expected = {record["id"]: record for record in records}
    seen = set()
    for record in _iter_existing_records(collection):
        if record != expected.get(record["id"]):
            raise RuntimeError("Reindex backend readback changed UUIDs, properties or vectors")
        seen.add(record["id"])
    if seen != expected.keys():
        raise RuntimeError("Reindex backend readback changed UUIDs, properties or vectors")


def _chunks_from_sources(collection: str, strategy: str, chunk_size: int,
                         chunk_overlap: int, similarity_threshold: float,
                         min_chunk_size: int) -> list[dict]:
    """Re-parse and re-chunk every retained original.

    Everything is parsed before the collection is touched: parsing is the
    failure-prone step, and discovering a bad file after the rebuild has started
    would cost the collection.
    """
    index = sources.load_index(collection)
    documents = index.get("documents") or {}
    if not documents:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Collection '{collection}' has no retained source documents, so it "
            "cannot be re-chunked. Only collections ingested after source "
            "retention was added carry their originals; export shows this as "
            "fidelity 'chunks-only'.",
            {"collection": collection})

    src_dir = sources.collection_dir(collection)
    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    work = Path(tempfile.mkdtemp(prefix="rechunk-", dir=settings.upload_dir))
    try:
        for digest, entry in sorted(documents.items()):
            blob = src_dir / digest
            if not blob.is_file():
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Retained source {digest[:12]} is missing from disk, so "
                    f"'{collection}' cannot be rebuilt from its originals.",
                    {"collection": collection, "digest": digest})
            # Parsers dispatch on the file extension, so the original filename
            # has to be restored before parsing.
            filename = (entry.get("filenames") or [digest])[0]
            staged = work / Path(filename).name
            shutil.copyfile(blob, staged)

            text, elements = _parse_file(staged)
            chunks = do_chunk(
                text=text,
                strategy=strategy,
                chunk_size=chunk_size,
                chunk_overlap_size=chunk_overlap,
                similarity_threshold=similarity_threshold,
                min_chunk_size=min_chunk_size,
                elements=elements if strategy == "context_aware" else None,
            )
            source_type = staged.suffix.lower().lstrip(".")
            out.extend({
                "content": c,
                "source_file": staged.name,
                "source_type": source_type,
                "chunk_index": i,
                "chunk_strategy": strategy,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "created_at": now,
            } for i, c in enumerate(chunks))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not out:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Re-chunking '{collection}' produced no chunks; check the chunking "
            "parameters.", {"collection": collection})
    return out


# ── Rebuilding ────────────────────────────────────────────────────────────────

@collection_writes.serialized("collection")
def _rebuild(collection: str, properties: list[dict], index_type: str | None,
             distance_metric: str | None, progress, *, records: list[dict] | None = None, source_collection: str | None = None) -> int:
    """Stage the new chunks, then swap them into place.

    Re-chunk/re-embed use the embedding insert path. Reindex supplies original
    records and verifies them before replacement and after the final copy.
    """
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    if records is not None:
        wc._validate_reindex_vectorizer_sync(collection)
    config = wc._collection_config_sync(collection)
    new_index = index_type or config["index_type"]
    new_distance = distance_metric or config["distance_metric"]
    hnsw = config.get("hnsw_config") or {}

    client = wc.get_client()
    ownership = collection_recovery.begin(collection_writes.canonical(collection), "tune", client)
    staging = ownership["staging"]
    cutover_started = False
    completed = False
    original_intact = False
    try:
        wc._create_collection_sync(staging, new_index, new_distance, hnsw)
        if records is not None:
            _write_records(staging, records)
            # Application writers share the held guard. Also refuse a source
            # change from an independently connected backend writer.
            _verify_records(collection, records)
            collection_recovery.retain(ownership, source_collection=source_collection)
            cutover_started = True
            client.collections.delete(collection)
            wc._create_collection_sync(collection, new_index, new_distance, hnsw)
            _write_records(collection, records)
            if progress:
                progress(len(records))
            completed = True
            return len(records)
        wc._insert_chunks_sync(staging, properties)
        staged = [
            {"id": str(o.uuid),
             "vector": (o.vector or {}).get("default"),
             "properties": dict(o.properties or {})}
            for o in client.collections.get(staging).iterator(include_vector=True)
        ]
        if len(staged) != len(properties):
            raise RuntimeError(
                f"staged {len(staged)} chunks but expected {len(properties)}")
        if progress:
            progress(len(staged))

        collection_recovery.retain(ownership, source_collection=source_collection)
        cutover_started = True
        # Past this point the original is replaced. Everything that could fail
        # has already run against the staging collection.
        client.collections.delete(collection)
        wc._create_collection_sync(collection, new_index, new_distance, hnsw)
        target = client.collections.get(collection)
        with target.batch.dynamic() as batch:
            for record in staged:
                batch.add_object(properties=record["properties"],
                                 uuid=record["id"], vector=record["vector"])
            if batch.number_errors > 0:
                raise RuntimeError(
                    f"{batch.number_errors} error(s) writing the rebuilt collection")
        completed = True
        return len(staged)
    except Exception as exc:
        if records is not None and cutover_started:
            try:
                _verify_records(collection, records)
                original_intact = wc._collection_config_sync(collection) == config
            except Exception:
                original_intact = False
            if not original_intact:
                try:
                    goldstandard.mark_stale(source_collection, "reindex replacement failed after cutover began; exact record preservation was not verified")
                except Exception:
                    _log.exception("Could not mark evaluation historical after failed reindex")
                if ownership["state"] == "recovery":
                    raise PackageError("TUNE_FAILED", str(exc) + f" Verified data retained as '{staging}'.",
                                       {"recovered_as": staging, "sidecar_snapshots": str(collection_recovery._root() / ownership["operation_id"])}) from exc
        elif cutover_started and ownership["state"] == "recovery":
            raise PackageError("TUNE_FAILED", str(exc) + f" Verified data retained as '{staging}'.",
                               {"recovered_as": staging, "sidecar_snapshots": str(collection_recovery._root() / ownership["operation_id"])}) from exc
        raise
    finally:
        try:
            if ownership:
                if completed or original_intact or ownership["state"] == "scratch":
                    collection_recovery.discard(ownership, client)
            else:
                client.collections.delete(staging)
        except Exception:                             # noqa: BLE001
            _log.exception("Could not remove owned staging collection %r", staging)


@collection_writes.serialized("collection")
def _run(job_id: str, collection: str, operation: str, params: dict, *, source_collection: str | None = None) -> None:
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    job = _jobs[job_id]
    job["collection"] = collection
    job["status"] = "running"

    def progress(n: int) -> None:
        job["chunks_written"] = n

    try:
        has_sources = sources.has_sources(source_collection)
        records = None

        if operation == "rechunk":
            if not has_sources:
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Collection '{collection}' is chunks-only: its original "
                    "documents were not retained, so it cannot be re-chunked. "
                    "Re-embedding from the stored chunk text is available, but "
                    "chunk boundaries cannot change.",
                    {"collection": collection})
            properties = _chunks_from_sources(source_collection, **params["chunking"])
            reason = "the collection was re-chunked, so its chunks no longer match these pairs"

        elif operation == "reembed":
            if params.get("chunking") is not None:
                # Spec §7.2: refuse the combination rather than silently
                # dropping one half of what was asked for.
                if not has_sources:
                    raise PackageError(
                        "SOURCES_REQUIRED",
                        f"Collection '{collection}' is chunks-only. Re-embedding "
                        "regenerates vectors from the stored chunk text, so chunk "
                        "boundaries cannot change; this request also asked for new "
                        "chunking parameters. Send one or the other.",
                        {"collection": collection})
                properties = _chunks_from_sources(source_collection, **params["chunking"])
                reason = "the collection was re-chunked and re-embedded"
            elif has_sources:
                properties = _existing_chunks(collection)
                reason = "the collection was re-embedded, so its vectors changed"
            else:
                properties = _existing_chunks(collection)
                reason = ("the collection was re-embedded from stored chunk text, "
                          "so its vectors changed")

        elif operation == "reindex":
            records = _existing_records(collection)
            properties = [record["properties"] for record in records]
            reason = None            # exact UUID/vector/property copy is verified
        else:
            raise PackageError("TUNE_UNSUPPORTED", f"Unknown operation '{operation}'.")

        job["chunks_total"] = len(properties)
        rebuild_args = (collection, properties, params.get("index_type"),
                        params.get("distance_metric"), progress)
        written = (_rebuild(*rebuild_args, records=records, source_collection=source_collection) if records is not None
                   else _rebuild(*rebuild_args, source_collection=source_collection))

        notes = []
        if reason:
            stale = goldstandard.mark_stale(source_collection, reason)
            if stale:
                notes.append(f"{stale} gold-standard session(s) marked stale")
        else:
            notes.append("UUIDs, properties and vectors verified unchanged after reindex; "
                         "gold-standard sessions were left alone")

        job.update(status="completed", chunks_written=written, notes=notes)

    except PackageError as exc:
        job.update(status="failed", error_code=exc.code, error=exc.message,
                   error_detail=exc.detail)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Tuning %r on %r failed", operation, collection)
        job.update(status="failed", error_code="TUNE_FAILED",
                   error=f"{type(exc).__name__}: {exc}")
    finally:
        with _lock:
            _active.discard(collection)


async def start_tune_job(collection: str, operation: str, params: dict) -> str:
    source_collection = collection
    collection = collection_writes.canonical(collection)
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        if collection in _active:
            raise RuntimeError(collection)
        _active.add(collection)

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "collection": collection,
        "operation": operation,
        "chunks_total": 0,
        "chunks_written": 0,
        "notes": [],
        "error": None,
        "error_code": None,
        "error_detail": None,
    }
    asyncio.create_task(asyncio.to_thread(_run, job_id, collection, operation, params, source_collection=source_collection))
    return job_id
```

### api/routers/__init__.py

```python

```

### api/services/system_info.py

```python
"""Host/VM resource reporting for the health endpoint.

The numbers here describe the Docker VM the containers run inside, not the
macOS host. On Docker Desktop, /proc/meminfo inside a container reports the
VM's total memory, which is exactly the figure a user needs when deciding
whether Ollama has room for the LLM.
"""
from __future__ import annotations

# The guest always sees slightly less than the amount configured in Docker
# Desktop, because the VM reserves some for itself (8192 MiB configured was
# measured as 7.75 GiB visible, ~3% overhead). Comparing the visible figure
# directly against the recommendation would therefore report "below" even when
# the user has allocated exactly the recommended amount, so allow a margin.
_VM_OVERHEAD_TOLERANCE = 0.95


def _read_mem_total_gb() -> float | None:
    """Total memory of the Docker VM, in GiB, or None if unreadable."""
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def memory_info(recommended_gb: float) -> dict:
    """Report memory allocated to Docker against the recommended minimum.

    Never raises: a health endpoint must not fail because a resource probe did.
    """
    allocated = _read_mem_total_gb()

    if allocated is None:
        return {
            "status": "unknown",
            "allocated_gb": None,
            "recommended_minimum_gb": round(recommended_gb, 1),
            "note": "Could not read /proc/meminfo to determine allocated memory.",
        }

    meets = allocated >= recommended_gb * _VM_OVERHEAD_TOLERANCE
    info = {
        "status": "ok" if meets else "below_recommended",
        "allocated_gb": round(allocated, 2),
        "recommended_minimum_gb": round(recommended_gb, 1),
    }
    if not meets:
        info["note"] = (
            f"Docker is allocated {allocated:.2f} GB; {recommended_gb:.1f} GB is "
            "recommended. The LLM needs roughly 6 GB resident, so below this it is "
            "repeatedly evicted and reloaded, and queries time out. Raise it in "
            "Docker Desktop -> Settings -> Resources -> Memory."
        )
    return info
```

### api/routers/health.py

```python
from __future__ import annotations
import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from services import ollama_client as ollama
from services import system_info
from services import weaviate_client as wc

router = APIRouter()


@router.get("/health")
async def health_check():
    results = {}
    overall_ok = True

    # Check through the Weaviate client itself, not a bare HTTP readiness probe.
    # The probe cannot detect a client/server version mismatch -- the server
    # answers "ready" while every client call fails. Bounded by wait_for so a
    # hung connect cannot stall past the container healthcheck's 5s timeout.
    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(wc.check_health(), timeout=4.0)
    except Exception:
        ok = False
    results["weaviate"] = {
        "status": "ok" if ok else "error",
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }
    if not ok:
        overall_ok = False

    ollama_result = await ollama.check_health()
    results["ollama"] = ollama_result
    if any(v["status"] != "ok" for v in ollama_result.values()):
        overall_ok = False

    # Resource reporting is advisory and deliberately does NOT affect
    # overall_ok. Low memory degrades performance but the stack still serves
    # requests; failing the endpoint would mark the api container unhealthy and
    # take the whole stack down over a tuning warning.
    resources = {"memory": system_info.memory_info(settings.recommended_memory_gb)}

    status_code = 200 if overall_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if overall_ok else "degraded",
            "services": results,
            "resources": resources,
        },
    )
```

### api/routers/help.py

```python
"""In-app help, rendered from the same templates as a package's own README.

Spec §10 requires one shared source for both, so the page and the packages it
describes cannot drift apart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from models.schemas import HelpResponse
from services import ollama_client, packager
from utils import api_error

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/help")


async def _embedding_dimensions() -> int | str:
    """The real width, from an actual embedding call.

    Falls back to a word rather than a number: quoting a made-up dimension count
    in a page about why dimensions must match would be its own small lie.
    """
    try:
        return len(await ollama_client.embed("dimension probe"))
    except Exception as exc:                          # noqa: BLE001
        _log.warning("Could not probe embedding dimensions for help page: %s", exc)
        return "its own number of"


@router.get("/transfer", response_model=HelpResponse)
async def transfer_help():
    dimensions = await _embedding_dimensions()
    try:
        markdown = packager.render_help(dimensions)
    except RuntimeError as exc:
        return api_error(500, "HELP_RENDER_FAILED", str(exc))
    return HelpResponse(topic="transfer", markdown=markdown)
```

### api/routers/collections.py

```python
from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import (
    CollectionInfo,
    CollectionsResponse,
    CreateCollectionRequest,
)
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/collections")

_REGISTRY_FILE: Path | None = None
_registry_lock = asyncio.Lock()


def _registry_path() -> Path:
    global _REGISTRY_FILE
    if _REGISTRY_FILE is None:
        _REGISTRY_FILE = Path(settings.upload_dir) / "collection_registry.json"
    return _REGISTRY_FILE


def _load_registry() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_registry(reg: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2))


@router.get("", response_model=CollectionsResponse)
async def list_collections():
    raw = await wc.get_collections()
    registry = await asyncio.to_thread(_load_registry)
    items = [
        CollectionInfo(
            name=c["name"],
            object_count=c["object_count"],
            index_type=c["index_type"],
            distance_metric=c["distance_metric"],
            created_at=registry.get(c["name"]),
        )
        for c in raw
    ]
    return CollectionsResponse(collections=items)


@router.post("", status_code=201)
async def create_collection(body: CreateCollectionRequest):
    if await wc.collection_exists(body.name):
        return api_error(409, "COLLECTION_EXISTS", f"Collection '{body.name}' already exists.")

    try:
        await wc.create_collection(
            name=body.name,
            index_type=body.index_type,
            distance_metric=body.distance_metric,
            hnsw_config=body.hnsw_config.model_dump(),
        )
    except Exception as exc:
        return api_error(500, "CREATE_FAILED", "Failed to create collection.", str(exc))

    async with _registry_lock:
        registry = await asyncio.to_thread(_load_registry)
        registry[body.name] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_save_registry, registry)

    return JSONResponse(status_code=201, content={"name": body.name, "status": "created"})


@router.delete("/{name}", status_code=200)
async def delete_collection(name: str):
    if not await wc.collection_exists(name):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{name}' not found.")

    try:
        count = await wc.delete_collection(name)
    except Exception as exc:
        return api_error(500, "DELETE_FAILED", "Failed to delete collection.", str(exc))

    async with _registry_lock:
        registry = await asyncio.to_thread(_load_registry)
        registry.pop(name, None)
        await asyncio.to_thread(_save_registry, registry)

    return {"name": name, "objects_deleted": count}
```

### api/routers/ingest.py

```python
from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from fastapi import APIRouter, Form, UploadFile, File
from pydantic import BaseModel

from config import settings
from models.schemas import IngestConfigResponse, IngestUploadResponse, JobStatusResponse
from services import ingest_config
from services import ingest_pipeline
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/ingest")

class SaveIngestConfigBody(BaseModel):
    collection: str
    chunking_strategy: str = "overlap"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    similarity_threshold: float | None = None
    min_chunk_size: int = 100


@router.post("/upload", response_model=IngestUploadResponse, status_code=202)
async def ingest_upload(
    collection: str = Form(...),
    strategy: str = Form("overlap"),
    chunk_size: int = Form(1000),
    chunk_overlap: int = Form(200),
    similarity_threshold: float = Form(0.85),
    min_chunk_size: int = Form(100),
    files: list[UploadFile] = File(...),
):
    if not await wc.collection_exists(collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")

    try:
        job_id = await ingest_pipeline.start_ingest_job(
            files=files,
            collection=collection,
            strategy=strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            similarity_threshold=similarity_threshold,
            min_chunk_size=min_chunk_size,
        )
    except ValueError as exc:
        return api_error(400, "NO_SUPPORTED_FILES", str(exc))

    return IngestUploadResponse(
        job_id=job_id,
        status="queued",
        files_queued=len(files),
        collection=collection,
    )


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    job = ingest_pipeline.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return JobStatusResponse(**job)


@router.get("/config/{collection}", response_model=IngestConfigResponse)
async def get_ingest_config(collection: str):
    cfg, is_default = await asyncio.to_thread(ingest_config.resolve, collection)
    return IngestConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=IngestConfigResponse, status_code=201)
async def save_ingest_config(body: SaveIngestConfigBody):
    cfg = body.model_dump()
    await asyncio.to_thread(ingest_config.save, cfg)
    return IngestConfigResponse(is_default=False, **cfg)
```

### api/routers/query.py

```python
from __future__ import annotations

from fastapi import APIRouter

from models.schemas import QueryRequest, QueryResponse
from services import metrics
from services import rag_pipeline
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/query")


@router.post("", response_model=QueryResponse)
async def run_query(body: QueryRequest):
    if not await wc.collection_exists(body.collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")

    result = await rag_pipeline.run_query(
        question=body.question,
        collection=body.collection,
        retrieval_mode=body.retrieval_mode,
        top_k=body.top_k,
        alpha=body.alpha,
        include_citations=body.include_citations,
        response_format=body.response_format,
    )

    await metrics.record(
        collection=body.collection,
        retrieval_mode=body.retrieval_mode,
        retrieval_ms=result["retrieval_latency_ms"],
        llm_ms=result["llm_latency_ms"],
        chunks_retrieved=result["chunks_retrieved"],
    )

    return QueryResponse(**result)
```

### api/routers/retrieval_config.py

```python
"""Per-collection retrieval settings.

Mirrors the ingest-config endpoints, including the asymmetry: GET takes the
collection in the path, POST takes it in the body.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from models.schemas import RetrievalConfigResponse, SaveRetrievalConfigBody
from services import retrieval_config

router = APIRouter(prefix="/retrieval")


@router.get("/config/{collection}", response_model=RetrievalConfigResponse)
async def get_retrieval_config(collection: str):
    cfg, is_default = await asyncio.to_thread(retrieval_config.resolve, collection)
    return RetrievalConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=RetrievalConfigResponse, status_code=201)
async def save_retrieval_config(body: SaveRetrievalConfigBody):
    cfg = body.model_dump()
    await asyncio.to_thread(retrieval_config.save, cfg)
    return RetrievalConfigResponse(is_default=False, **cfg)
```

### api/routers/transfer.py

```python
"""Export and import endpoints.

Import arrives in Phase 5; this module carries the export half and the shared
job-polling shape so both live behind one prefix-free router.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter

from models.schemas import (
    ExportJobStatusResponse,
    ExportRequest,
    ExportStartResponse,
    ImportJobStatusResponse,
    ImportRequest,
    ImportStartResponse,
    PackageListResponse,
    PackageSummary,
)
from services import exporter, importer, packager
from services import weaviate_client as wc
from utils import api_error

router = APIRouter()


@router.post("/export", response_model=ExportStartResponse, status_code=202)
async def start_export(body: ExportRequest):
    if not await wc.collection_exists(body.collection):
        # Checked before a job exists, so a typo does not leave a failed job
        # lying around for the user to interpret.
        return api_error(404, "COLLECTION_NOT_FOUND",
                         f"Collection '{body.collection}' not found.")
    try:
        job_id = await exporter.start_export_job(body.collection, body.include_models)
    except RuntimeError as exc:
        # start_export_job puts the already-running job id in the exception.
        return api_error(
            409, "EXPORT_IN_PROGRESS",
            f"An export of '{body.collection}' is already running.",
            detail={"job_id": str(exc)},
        )
    return ExportStartResponse(job_id=job_id, status="queued", collection=body.collection)


@router.get("/export/job/{job_id}", response_model=ExportJobStatusResponse)
async def export_job_status(job_id: str):
    job = exporter.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return ExportJobStatusResponse(**job)


@router.post("/import", response_model=ImportStartResponse, status_code=202)
async def start_import(body: ImportRequest):
    # Everything else — unreadable archive, bad digest, embedding mismatch,
    # name collision — is reported through the job, because it is only knowable
    # after reading the package, which takes long enough to need a job.
    try:
        job_id = await importer.start_import_job(body.filename, body.on_conflict)
    except RuntimeError as exc:
        return api_error(409, "IMPORT_IN_PROGRESS",
                         f"An import of '{exc}' is already running.",
                         detail={"filename": str(exc)})
    return ImportStartResponse(job_id=job_id, status="queued", filename=body.filename)


@router.get("/import/job/{job_id}", response_model=ImportJobStatusResponse)
async def import_job_status(job_id: str):
    job = importer.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return ImportJobStatusResponse(**job)


def _list_packages_sync() -> list[dict]:
    out = []
    for p in sorted(packager.exports_dir().glob("ragpkg-*.tar.gz")):
        try:
            manifest = packager.read_manifest(p)
        except Exception:
            # A file that is not a readable package still belongs in the listing;
            # import will report precisely why it cannot be used.
            out.append({"filename": p.name, "size_bytes": p.stat().st_size,
                        "collection": None, "chunk_count": None,
                        "fidelity": None, "created_at": None, "readable": False})
            continue
        out.append({
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "collection": manifest.get("collection", {}).get("name"),
            "chunk_count": manifest.get("collection", {}).get("chunk_count"),
            "fidelity": manifest.get("fidelity"),
            "created_at": manifest.get("created_at"),
            "readable": True,
        })
    return out


@router.get("/packages", response_model=PackageListResponse)
async def list_packages():
    """Packages sitting in ./exports — both exported here and dropped in to import."""
    packages = await asyncio.to_thread(_list_packages_sync)
    return PackageListResponse(packages=[PackageSummary(**p) for p in packages])
```

### api/routers/tuning.py

```python
"""Tuning a collection after import: re-chunk, re-embed, re-index.

Spec §7. Each operation rebuilds the collection and is long-running, so all
three follow the job-and-poll pattern.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from models.schemas import (
    ReembedRequest,
    ReindexRequest,
    RechunkRequest,
    TuneJobStatusResponse,
    TuneOptionsResponse,
    TuneStartResponse,
)
from services import sources, tuning
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/tune")


async def _start(collection: str, operation: str, params: dict):
    if not await wc.collection_exists(collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    try:
        job_id = await tuning.start_tune_job(collection, operation, params)
    except RuntimeError as exc:
        return api_error(409, "TUNE_IN_PROGRESS",
                         f"'{exc}' is already being tuned.",
                         detail={"collection": str(exc)})
    return TuneStartResponse(job_id=job_id, status="queued",
                             collection=collection, operation=operation)


@router.post("/rechunk", response_model=TuneStartResponse, status_code=202)
async def rechunk(body: RechunkRequest):
    """Re-split the retained originals and rebuild. Needs `with-sources`."""
    return await _start(body.collection, "rechunk", {"chunking": body.chunking()})


@router.post("/reembed", response_model=TuneStartResponse, status_code=202)
async def reembed(body: ReembedRequest):
    """Regenerate vectors.

    With no chunking parameters this re-embeds what is stored, leaving chunk
    boundaries alone. With them, it is a re-chunk as well, which a chunks-only
    collection refuses rather than half-honouring (spec §7.2).
    """
    return await _start(body.collection, "reembed",
                        {"chunking": body.chunking() if body.has_chunking() else None})


@router.post("/reindex", response_model=TuneStartResponse, status_code=202)
async def reindex(body: ReindexRequest):
    """Change index type or distance metric, reusing the existing vectors."""
    return await _start(body.collection, "reindex",
                        {"index_type": body.index_type,
                         "distance_metric": body.distance_metric})


@router.get("/job/{job_id}", response_model=TuneJobStatusResponse)
async def tune_job_status(job_id: str):
    job = tuning.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return TuneJobStatusResponse(**job)


@router.get("/{collection}", response_model=TuneOptionsResponse)
async def tune_options(collection: str):
    """What this collection can be tuned with, given its fidelity."""
    if not await wc.collection_exists(collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    has_sources = await asyncio.to_thread(sources.has_sources, collection)
    stats = await asyncio.to_thread(sources.stats, collection)
    return TuneOptionsResponse(
        collection=collection,
        fidelity="with-sources" if has_sources else "chunks-only",
        source_document_count=stats["document_count"],
        can_rechunk=has_sources,
        can_reembed=True,
        can_reindex=True,
        note=("Every tuning operation is available." if has_sources else
              "No original documents were retained, so this collection cannot be "
              "re-chunked. Re-embedding works from the stored chunk text, which "
              "leaves chunk boundaries unchanged."),
    )
```

### api/routers/goldstandard.py

```python
from __future__ import annotations
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from config import settings
from models.schemas import (
    GenerateRequest,
    GenerateResponse,
    GoldPair,
    PatchPairRequest,
    RegenerateRequest,
    SaveRequest,
    SaveResponse,
    SessionResponse,
)
from services import goldstandard as gs
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/goldstandard")


@router.post("/generate", response_model=GenerateResponse, status_code=202)
async def generate(body: GenerateRequest):
    if not await wc.collection_exists(body.collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")

    result = await gs.start_generation(
        collection=body.collection,
        sample_size=body.sample_size,
        seed=body.seed,
    )
    return GenerateResponse(**result)


@router.get("/session/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    session = gs.get_session(session_id)
    if session is None:
        return api_error(404, "SESSION_NOT_FOUND", f"Session '{session_id}' not found.")
    pairs = []
    for p in session.get("pairs", []):
        try:
            pairs.append(GoldPair(**p))
        except Exception:
            pass
    return SessionResponse(
        session_id=session["session_id"],
        status=session["status"],
        pairs_total=session["pairs_total"],
        pairs_attempted=session.get("pairs_attempted", session["pairs_completed"]),
        pairs_completed=session["pairs_completed"],
        pairs_failed=session.get("pairs_failed", 0),
        pairs=pairs,
        collection=session.get("collection", ""),
        errors=session.get("errors", []),
    )


@router.patch("/session/{session_id}/pair/{pair_id}", response_model=GoldPair)
async def patch_pair(session_id: str, pair_id: str, body: PatchPairRequest):
    updates = body.model_dump(exclude_none=True)
    pair = await gs.update_pair(session_id, pair_id, updates)
    if pair is None:
        return api_error(404, "PAIR_NOT_FOUND", f"Pair '{pair_id}' not found in session '{session_id}'.")
    return GoldPair(**pair)


@router.post("/regenerate", response_model=GoldPair)
async def regenerate(body: RegenerateRequest):
    try:
        pair = await gs.regenerate_pair(body.session_id, body.pair_id)
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message)
    if pair is None:
        return api_error(404, "PAIR_NOT_FOUND", f"Pair '{body.pair_id}' not found in session '{body.session_id}'.")
    return GoldPair(**pair)


@router.post("/save", response_model=SaveResponse)
async def save(body: SaveRequest):
    result = await gs.save_session(body.session_id, body.filename)
    if result is None:
        return api_error(404, "SESSION_NOT_FOUND", f"Session '{body.session_id}' not found.")
    return SaveResponse(**result)


@router.get("/download/{filename}")
async def download(filename: str):
    base = Path(settings.upload_dir).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base) + "/"):
        return api_error(400, "INVALID_PATH", "Invalid filename.")
    if not target.exists():
        return api_error(404, "FILE_NOT_FOUND", f"File '{filename}' not found.")
    return FileResponse(
        path=str(target),
        media_type="application/json",
        filename=filename,
    )
```

### api/routers/metrics.py

```python
from __future__ import annotations

from fastapi import APIRouter, Query

from models.schemas import MetricsResponse
from services import metrics

router = APIRouter(prefix="/metrics")


@router.get("/latency", response_model=MetricsResponse)
async def latency_summary(
    collection: str | None = Query(default=None),
    retrieval_mode: str | None = Query(default=None),
    history_limit: int = Query(default=100, ge=0, le=500),
):
    return metrics.get_summary(
        collection=collection,
        retrieval_mode=retrieval_mode,
        history_limit=history_limit,
    )
```

### api/services/metrics.py

```python
from __future__ import annotations
import asyncio
import json
import statistics
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import settings

MAX_RECORDS = 500
_ring: deque[dict] = deque(maxlen=MAX_RECORDS)

_METRICS_FILE = None


def _metrics_path() -> Path:
    global _METRICS_FILE
    if _METRICS_FILE is None:
        p = Path(settings.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        _METRICS_FILE = p / "metrics.jsonl"
    return _METRICS_FILE


def load_from_disk() -> None:
    p = _metrics_path()
    if not p.exists():
        return
    lines = p.read_text().splitlines()
    for line in lines[-MAX_RECORDS:]:
        try:
            _ring.append(json.loads(line))
        except Exception:
            pass


def _append_sync(line: str) -> None:
    with open(_metrics_path(), "a") as f:
        f.write(line + "\n")


async def record(
    collection: str,
    retrieval_mode: str,
    retrieval_ms: int,
    llm_ms: int,
    chunks_retrieved: int,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
        "retrieval_mode": retrieval_mode,
        "retrieval_ms": retrieval_ms,
        "llm_ms": llm_ms,
        "total_ms": retrieval_ms + llm_ms,
        "chunks_retrieved": chunks_retrieved,
    }
    _ring.append(entry)
    await asyncio.to_thread(_append_sync, json.dumps(entry))


def _percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(data_sorted) - 1)
    frac = k - lo
    return round(data_sorted[lo] + frac * (data_sorted[hi] - data_sorted[lo]), 1)


def _stats(values: list[float]) -> dict:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "count": 0}
    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "mean": round(statistics.mean(values), 1),
        "count": len(values),
    }


def get_summary(
    collection: str | None = None,
    retrieval_mode: str | None = None,
    history_limit: int = 100,
) -> dict:
    records = list(_ring)
    if collection:
        records = [r for r in records if r.get("collection") == collection]
    if retrieval_mode:
        records = [r for r in records if r.get("retrieval_mode") == retrieval_mode]

    retrieval = [r["retrieval_ms"] for r in records]
    llm = [r["llm_ms"] for r in records]
    total = [r["total_ms"] for r in records]

    # Most recent `history_limit` records, kept in chronological order. The ring
    # buffer is already oldest-first, so the tail is the newest slice.
    recent = records[-history_limit:] if history_limit > 0 else []
    history = [
        {
            "timestamp": r.get("ts", ""),
            "collection": r.get("collection", ""),
            "retrieval_mode": r.get("retrieval_mode", ""),
            "retrieval_ms": r.get("retrieval_ms", 0),
            "llm_ms": r.get("llm_ms", 0),
            "total_ms": r.get("total_ms", 0),
        }
        for r in recent
    ]

    return {
        "total_records": len(records),
        "retrieval_latency": _stats(retrieval),
        "llm_latency": _stats(llm),
        "total_latency": _stats(total),
        "history": history,
    }
```

### api/templates/partials/encryption.md

```markdown
## ⚠ Packages are not encrypted

Nothing in a package is protected. `sources/` holds the original documents byte
for byte, and `chunks.jsonl` holds their text. If the corpus contains
confidential or personal material, the package does too. Move and store it
accordingly.
```

### api/templates/partials/fidelity_table.md

```markdown
| Fidelity | Means |
|---|---|
| `with-sources` | The original documents travel with the package. Every tuning operation is available after import, including re-chunking. |
| `chunks-only` | No original documents. The collection can be imported and queried, but it cannot be re-chunked, and re-embedding works from chunk text rather than the source, so it is approximate. |
```

### api/templates/partials/embedding_rule.md

```markdown
Vectors are only meaningful to the model that produced them. A vector from a
different embedding model is not merely different — it is meaningless in this
vector space, and a collection built from mismatched vectors answers every query
confidently and wrongly.

Import therefore **refuses** a package whose embedding model is not the one this
instance runs, rather than warning about it. This instance uses
`@@EMBED_MODEL@@` at @@EMBED_DIMENSIONS@@ dimensions.

Check any machine with `docker compose exec ollama ollama list`.
```

### api/templates/partials/naming.md

````markdown
```
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>.tar.gz
```

The collection name in the filename is lowercased and stripped of punctuation,
so it is lossy, and two different collections can produce the same label. `<id8>`
is the first eight hex characters of the manifest's own digest.

**`manifest.json` is authoritative.** Import reads the collection name from there
and never parses the filename. Renaming a package file changes nothing about what
it contains.
````

### api/templates/partials/contents.md

````markdown
```
manifest.json           what this package is; authoritative
collection.json         schema, index type, distance metric, HNSW parameters
chunks.jsonl            one JSON object per chunk, with its vector
ingest_config.json      chunking settings, if the collection had any saved
retrieval_config.json   the retrieval settings the collection was tuned with
README.md               generated from the manifest
retrieve.py             a standalone query script, when retrieval settings exist
goldstandard/           evaluation sessions for this collection
sources/                the original documents, at `with-sources` fidelity only
models/                 Ollama model files, when exported with include_models
```

Every file listed in the manifest's `files` map carries a SHA-256 digest, and
import verifies all of them before touching the database. `manifest.json`,
`README.md` and `retrieve.py` are not in that map: the first cannot contain its
own digest, and the other two are generated from it afterwards.
````

### api/templates/partials/retrieve_usage.md

````markdown
```bash
python3 retrieve.py "your question here"
python3 retrieve.py --top-k 10 --mode hybrid "your question here"
python3 retrieve.py --api-url http://localhost:9090/api "your question here"
python3 retrieve.py --timeout 900 "a question that needs a long answer"
```

It needs Python 3 and nothing else — no `pip install` — because the air-gapped
case is the one this project targets. It exits 0 on success, 2 on bad usage,
3 if the API is unreachable or times out, 4 if the collection is absent and 5 if
the API returns an error.

A package carries `retrieve.py` only when the collection had retrieval settings
saved. A script claiming tuned parameters while carrying stock defaults would be
worse than no script.
````

### api/templates/package_readme.md.tmpl

````markdown
# RAG package — @@COLLECTION_NAME@@

This archive is a portable copy of one RAG collection: its chunks and their
vectors, the settings it was tuned with, and — at `with-sources` fidelity — the
original documents it was built from.

- **Collection:** `@@COLLECTION_NAME@@`
- **Chunks:** @@CHUNK_COUNT@@
- **Source documents:** @@SOURCE_DOCUMENT_COUNT@@
- **Fidelity:** `@@FIDELITY@@`
- **Embedding model:** `@@EMBED_MODEL@@` (@@EMBED_DIMENSIONS@@ dimensions)
- **Created:** @@CREATED_AT@@
- **Package id:** `@@ID8@@`

@@INCLUDE:encryption@@

## What fidelity means

@@INCLUDE:fidelity_table@@

This package is **`@@FIDELITY@@`**. @@FIDELITY_NOTE@@

## Prerequisite: the embedding model must match

@@INCLUDE:embedding_rule@@

## Importing

1. Copy this `.tar.gz` into the target instance's `./exports/` directory.
2. Open the web UI and go to **Transfer → Import**, or call the API directly:

```bash
curl -X POST http://localhost:8080/api/import \
  -H 'Content-Type: application/json' \
  -d '{"filename": "@@PACKAGE_FILENAME@@", "on_conflict": "abort"}'
```

3. Poll the returned `job_id` at `GET /api/import/job/{job_id}`.

`on_conflict` is required and has no default. It is one of `abort` (fail if the
name is taken), `rename` (import under a free name) or `replace` (overwrite,
but only after this package has been proven to import cleanly).

## The filename is a label, not an input

This package is:

```
@@PACKAGE_FILENAME@@
```

@@INCLUDE:naming@@

## Querying it

@@RETRIEVE_SECTION@@

## Contents

@@INCLUDE:contents@@
````

### api/templates/retrieve.py.tmpl

```python
#!/usr/bin/env python3
"""Query the "@@COLLECTION_NAME@@" collection through a rag-docker API.

Generated with the RAG package @@PACKAGE_FILENAME@@
  package id : @@ID8@@
  created    : @@CREATED_AT@@
  embedding  : @@EMBED_MODEL@@ (@@EMBED_DIMENSIONS@@ dimensions)

The defaults below are the settings this collection was tuned with. Every one
can be overridden with a flag. Standard library only, on purpose: this has to
run on an air-gapped machine where `pip install` is not an option.

Exit codes: 0 ok, 2 bad usage, 3 API unreachable, 4 collection absent,
5 API returned an error.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

# ── Baked in at export from the collection's saved retrieval settings ─────────
COLLECTION = "@@COLLECTION_NAME@@"
DEFAULT_API_URL = "http://localhost:8080/api"
DEFAULT_MODE = "@@RETRIEVAL_MODE@@"
DEFAULT_TOP_K = @@TOP_K@@
DEFAULT_ALPHA = @@ALPHA@@
DEFAULT_RESPONSE_FORMAT = "@@RESPONSE_FORMAT@@"

MODES = ("hnsw", "flat", "hybrid", "semantic")
# Generous by default: the answer is generated by an LLM on CPU, which can take
# minutes on a long question. A run that exceeds this is reported as a timeout,
# not as an unreachable API.
DEFAULT_TIMEOUT_SECONDS = 600
# A reverse proxy sits in front of the API in the reference deployment, so when
# the API is down the proxy answers with a gateway error rather than refusing
# the connection. That is still "unreachable", not "the API rejected this".
GATEWAY_STATUSES = (502, 503, 504)


def build_parser():
    p = argparse.ArgumentParser(
        description='Ask a question of the "%s" collection.' % COLLECTION,
        epilog="Example: %(prog)s --top-k 10 --mode hybrid \"who approves overtime?\"",
    )
    p.add_argument("question", help="the question to ask")
    p.add_argument("--api-url", default=DEFAULT_API_URL,
                   help="base URL of the rag-docker API (default: %(default)s)")
    p.add_argument("--collection", default=COLLECTION,
                   help="collection to query (default: %(default)s)")
    p.add_argument("--mode", default=DEFAULT_MODE, choices=MODES,
                   help="retrieval mode (default: %(default)s)")
    p.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                   help="number of chunks to retrieve (default: %(default)s)")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA,
                   help="hybrid keyword/meaning balance, 0-1 (default: %(default)s)")
    p.add_argument("--format", dest="response_format", default=DEFAULT_RESPONSE_FORMAT,
                   choices=("end_user", "engineer"),
                   help="answer style (default: %(default)s)")
    p.add_argument("--citations", action="store_true",
                   help="also print the source chunks behind the answer")
    p.add_argument("--json", action="store_true",
                   help="print the raw API response instead of formatted text")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                   help="seconds to wait for an answer (default: %(default)s)")
    return p


def fail(code, message, hint):
    """One sentence and what to do about it. Never a traceback."""
    sys.stderr.write("error: %s\n       %s\n" % (message, hint))
    return code


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not args.question.strip():
        return fail(2, "The question is empty.",
                    'Pass it as one argument, e.g. retrieve.py "who approves overtime?"')
    if not 1 <= args.top_k <= 50:
        return fail(2, "--top-k must be between 1 and 50, got %d." % args.top_k,
                    "The collection was exported with --top-k %s." % DEFAULT_TOP_K)
    if not 0.0 <= args.alpha <= 1.0:
        return fail(2, "--alpha must be between 0.0 and 1.0, got %s." % args.alpha,
                    "0 is pure keyword matching, 1 is pure meaning.")
    if args.timeout <= 0:
        return fail(2, "--timeout must be greater than 0, got %s." % args.timeout,
                    "It is a number of seconds to wait for the answer.")

    url = args.api_url.rstrip("/") + "/query"
    payload = json.dumps({
        "question": args.question,
        "collection": args.collection,
        "retrieval_mode": args.mode,
        "top_k": args.top_k,
        "alpha": args.alpha,
        "include_citations": bool(args.citations),
        "response_format": args.response_format,
    }).encode("utf-8")

    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A rag-docker error carries {"error": {"code", "message", "detail"}};
        # anything else is some other server that happened to answer.
        envelope = False
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            error = detail.get("error")
            envelope = isinstance(error, dict)
            code = error.get("code", "") if envelope else ""
            message = (error.get("message") if envelope else None) or exc.reason
        except Exception:
            code, message = "", exc.reason
        if code == "COLLECTION_NOT_FOUND":
            return fail(4, 'The API has no collection named "%s".' % args.collection,
                        "Import the package first, or pass --collection with the "
                        "name it was imported under.")
        if exc.code == 404 and not envelope:
            # Matching a bare 404 to a missing collection would send someone
            # hunting for an import problem when the URL is simply wrong.
            return fail(5, "No rag-docker API answered at %s." % url,
                        "Something responded but it is not this API. Check "
                        "--api-url; it should end in /api.")
        if exc.code in GATEWAY_STATUSES:
            return fail(3, "The API is not responding behind %s (HTTP %s)."
                        % (args.api_url, exc.code),
                        "The proxy is up but the API is not. Check it with "
                        "`docker compose ps` and start it with `docker compose up -d`.")
        return fail(5, "The API rejected the query: %s" % message,
                    "HTTP %s from %s." % (exc.code, url))
    except urllib.error.URLError as exc:
        # URLError subclasses OSError and wraps socket errors, so a connect
        # timeout arrives here rather than in the TimeoutError branch below.
        if isinstance(exc.reason, TimeoutError):
            return fail(3, "The API did not answer within %s seconds." % args.timeout,
                        "Generation runs on CPU and can be slow; retry with a "
                        "longer --timeout, or a smaller --top-k.")
        return fail(3, "Cannot reach the API at %s (%s)." % (args.api_url, exc.reason),
                    "Start the stack with `docker compose up -d`, or pass "
                    "--api-url if it is served elsewhere.")
    except TimeoutError:
        return fail(3, "The API did not answer within %s seconds." % args.timeout,
                    "Generation runs on CPU and can be slow; retry with a longer "
                    "--timeout, or a smaller --top-k.")
    except OSError as exc:
        return fail(3, "Cannot reach the API at %s (%s)." % (args.api_url, exc),
                    "Start the stack with `docker compose up -d`, or pass "
                    "--api-url if it is served elsewhere.")
    except ValueError:
        return fail(5, "The API returned a response that is not JSON.",
                    "Check that %s is a rag-docker API and not something else." % args.api_url)

    if args.json:
        print(json.dumps(body, indent=2))
        return 0

    print(body.get("answer", "").strip())

    citations = body.get("citations") or []
    if args.citations and citations:
        print("\nSources:")
        for c in citations:
            print("  - %s (chunk %s, score %.3f)"
                  % (c.get("source_file", "?"), c.get("chunk_index", "?"),
                     c.get("score", 0.0)))
            excerpt = " ".join((c.get("excerpt") or "").split())
            if excerpt:
                print("      %s" % (excerpt[:200] + ("…" if len(excerpt) > 200 else "")))

    retrieval_ms = body.get("retrieval_latency_ms")
    llm_ms = body.get("llm_latency_ms")
    if retrieval_ms is not None and llm_ms is not None:
        sys.stderr.write("retrieved in %sms, generated in %sms\n" % (retrieval_ms, llm_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### api/templates/help_transfer.md.tmpl

```markdown
# Moving a RAG between machines

A collection can be exported as a single `.tar.gz` and imported somewhere else —
another laptop, a customer's machine, an air-gapped network. The package carries
the chunks and their vectors, the settings the collection was tuned with, a
standalone query script and, when the originals were retained, the source
documents themselves.

Everything below is rendered from the same templates that generate the
`README.md` inside each package, so the two cannot disagree.

## Where packages live

Both directions use `./exports/` in the project directory, which is a bind mount
rather than a Docker volume so you can pick files up and drop them in directly.

- **Exporting** writes the `.tar.gz` there.
- **Importing** reads from there: copy a package in first, then import by filename.

`GET /api/packages` lists what is currently in that directory, including files it
cannot read — those are shown rather than hidden, so nothing disappears silently.

Packages are never deleted automatically, and importing never modifies the file.

## Naming

@@INCLUDE:naming@@

## What a package contains

@@INCLUDE:contents@@

## Fidelity: with-sources or chunks-only

@@INCLUDE:fidelity_table@@

**Why some collections are `chunks-only`.** Retaining original documents was
added after the first version of this platform. A collection ingested before that
has its chunks and vectors but not the files they came from, so it exports as
`chunks-only`. Nothing is wrong with it — it imports and answers questions
normally. It simply cannot be re-split, because the text to re-split is gone.

Re-ingesting the original documents into a new collection is the way to get full
fidelity for an older corpus.

## The embedding model rule

@@INCLUDE:embedding_rule@@

If the target machine has no embedding model at all, export with
`include_models: true`. That bundles `@@EMBED_MODEL@@` and `@@LLM_MODEL@@` as
Ollama's own manifest and blob files, taking the package from kilobytes to
roughly 2.3 GB. On import a model already present is left alone; a missing one is
installed from the package and checked before the collection is built.

Without bundled models, importing into a machine that lacks the embedding model
fails with `EMBEDDING_MODEL_MISSING` — a different error from a mismatch, because
the remedy is different.

## Handling a name collision

`on_conflict` is required when importing and has no default, because the wrong
choice can delete a collection.

| Value | Behaviour |
|---|---|
| `abort` | Fail if a collection of that name already exists. |
| `rename` | Import alongside it as `<name>_imported_<id8>`. |
| `replace` | Overwrite it — but only after the incoming package has been proven to import cleanly, so a failed replace leaves the original intact. |

Replacing a collection keeps its gold-standard sessions and marks them orphaned,
and the import result reports how many. They are never deleted silently.

## Tuning after import

| Goal | Requires | How |
|---|---|---|
| Change mode, `top_k` or `alpha` | nothing | Retrieval page, or per request on `POST /query` |
| Change index type or distance metric | nothing | `POST /tune/reindex` — reuses the existing vectors |
| Change chunk size, overlap or strategy | `with-sources` | `POST /tune/rechunk` |
| Change the embedding model | `with-sources` preferred | `POST /tune/reembed` |
| Add documents | matching embedding model | normal ingest |

`GET /api/tune/<collection>` reports which of these a given collection can do.

Every rebuild is staged into a temporary collection and swapped in only once it
succeeds, so a failed tune leaves the collection as it was.

**Gold-standard sessions are flagged, never deleted or remapped.** Re-chunking or
re-embedding changes which chunks exist, so evaluation pairs built against the
old ones no longer describe what is stored; those sessions are marked `stale`
with a reason and a timestamp. Guessing which new chunk replaces an old one would
corrupt a baseline silently, which is worse than an honest flag. Changing only
the index or distance metric does not touch chunk identity, and leaves sessions
alone.

## Running a package's query script

@@INCLUDE:retrieve_usage@@

@@INCLUDE:encryption@@
```

### api/main.py

```python
from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    from services import goldstandard, metrics
    from services import weaviate_client as wc
    goldstandard.load_sessions_from_disk()
    metrics.load_from_disk()
    # Only durable ownership permits scratch cleanup; retain verified recovery.
    log = logging.getLogger(__name__)
    try:
        abandoned = await wc.sweep_staging()
        if abandoned:
            log.warning("Removed %d staging collection(s) left by a previous run: %s. "
                        "Re-import the package to try again; it is still in ./exports.",
                        len(abandoned), ", ".join(abandoned))
    except Exception:                                 # noqa: BLE001
        log.exception("Startup sweep of staging collections failed")
    try:
        import asyncio
        from services import importer
        partial = await asyncio.to_thread(importer.sweep_interrupted_imports)
        if partial:
            log.warning("Removed %d collection(s) left half-built by an interrupted "
                        "import: %s. Re-import the package to try again.",
                        len(partial), ", ".join(partial))
        stale_dirs = await asyncio.to_thread(importer.sweep_stale_workdirs)
        if stale_dirs:
            log.warning("Removed %d abandoned extraction directory(ies): %s",
                        len(stale_dirs), ", ".join(stale_dirs))
    except Exception:                                 # noqa: BLE001
        log.exception("Startup sweep of interrupted imports failed")
    yield
    from services import weaviate_client as wc
    wc.close_client()


app = FastAPI(title="RAG API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import help as help_router, health, collections, ingest, query, goldstandard, metrics, retrieval_config, transfer, tuning

app.include_router(health.router)
app.include_router(help_router.router)
app.include_router(collections.router)
app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(goldstandard.router)
app.include_router(metrics.router)
app.include_router(retrieval_config.router)
app.include_router(transfer.router)
app.include_router(tuning.router)
```

---

## 4. UI Frontend

### ui/Dockerfile

```dockerfile
FROM node:26-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:26-alpine
WORKDIR /app
RUN npm install -g serve
COPY --from=builder /app/dist ./dist
EXPOSE 3000
CMD ["serve", "-s", "dist", "-l", "3000"]
```

### ui/package.json

```json
{
  "name": "rag-ui",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "lucide-react": "^1.48.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "^9.0.1",
    "react-router-dom": "^7.18.4",
    "recharts": "^3.10.1",
    "remark-gfm": "^4.0.0"
  },
  "devDependencies": {
    "@tailwindcss/typography": "^0.5.15",
    "@types/react": "^18.3.5",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "autoprefixer": "^10.6.1",
    "postcss": "^8.4.45",
    "tailwindcss": "^3.4.11",
    "typescript": "^7.0.2",
    "vite": "^6.4.3"
  }
}
```

### ui/vite.config.ts

```typescript
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://api:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
```

### ui/tailwind.config.js

```javascript
/** @type {import('tailwindcss').Config} */
import typography from '@tailwindcss/typography'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {},
  },
  // `prose` is used by the Q&A answer pane and the transfer help page. Without
  // this plugin those classes compile to nothing and Tailwind's preflight has
  // already stripped heading and list styling, so markdown renders as one
  // undifferentiated block.
  plugins: [typography],
}
```

### ui/postcss.config.js

```javascript
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

### ui/tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
```

### ui/index.html

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>RAG Platform</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

### ui/src/main.tsx

```typescript
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
```

### ui/src/index.css

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

### ui/src/App.tsx

```typescript
import { BrowserRouter } from 'react-router-dom'
import { RoleProvider } from './context/RoleContext'
import { QueryConfigProvider } from './context/QueryConfigContext'
import AppRouter from './router'

export default function App() {
  return (
    <BrowserRouter>
      <RoleProvider>
        <QueryConfigProvider>
          <AppRouter />
        </QueryConfigProvider>
      </RoleProvider>
    </BrowserRouter>
  )
}
```

### ui/src/router.tsx

```typescript
import { Routes, Route, Navigate } from 'react-router-dom'
import { useRole } from './context/RoleContext'
import NavBar from './components/NavBar'
import LandingPage from './pages/LandingPage'
import QAPage from './pages/QAPage'
import ImportPage from './pages/ImportPage'
import ChunkingPage from './pages/ChunkingPage'
import RetrievalPage from './pages/RetrievalPage'
import GoldStandardPage from './pages/GoldStandardPage'
import CollectionsPage from './pages/CollectionsPage'
import HealthPage from './pages/HealthPage'
import TransferPage from './pages/TransferPage'
import HelpTransferPage from './pages/HelpTransferPage'

export default function AppRouter() {
  const { role } = useRole()

  if (!role) {
    return (
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    )
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <NavBar />
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/qa" replace />} />
          <Route path="/qa" element={<QAPage />} />
          {role !== 'end_user' && (
            <>
              <Route path="/import" element={<ImportPage />} />
              <Route path="/chunking" element={<ChunkingPage />} />
              <Route path="/retrieval" element={<RetrievalPage />} />
              <Route path="/goldstandard" element={<GoldStandardPage />} />
              <Route path="/transfer" element={<TransferPage />} />
              <Route path="/help/transfer" element={<HelpTransferPage />} />
            </>
          )}
          {role === 'engineer' && (
            <>
              <Route path="/collections" element={<CollectionsPage />} />
              <Route path="/health" element={<HealthPage />} />
            </>
          )}
          <Route path="*" element={<Navigate to="/qa" replace />} />
        </Routes>
      </main>
    </div>
  )
}
```

### ui/src/context/RoleContext.tsx

```typescript
import { createContext, useContext, useReducer, ReactNode } from 'react'

type Role = 'engineer' | 'developer' | 'end_user' | null

interface RoleState { role: Role }
type RoleAction = { type: 'SET_ROLE'; role: Role }

const RoleContext = createContext<{ role: Role; setRole: (r: Role) => void } | null>(null)

function reducer(_state: RoleState, action: RoleAction): RoleState {
  return { role: action.role }
}

function loadRole(): Role {
  try {
    const stored = sessionStorage.getItem('rag_role')
    if (!stored) return null
    const parsed = JSON.parse(stored)
    return parsed.role ?? null
  } catch {
    return null
  }
}

export function RoleProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, { role: loadRole() })

  function setRole(role: Role) {
    if (role) {
      sessionStorage.setItem('rag_role', JSON.stringify({ role }))
    } else {
      sessionStorage.removeItem('rag_role')
    }
    dispatch({ type: 'SET_ROLE', role })
  }

  return (
    <RoleContext.Provider value={{ role: state.role, setRole }}>
      {children}
    </RoleContext.Provider>
  )
}

export function useRole() {
  const ctx = useContext(RoleContext)
  if (!ctx) throw new Error('useRole must be used within RoleProvider')
  return ctx
}
```

### ui/src/context/QueryConfigContext.tsx

```typescript
import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from 'react'
import { api, RetrievalConfig } from '../api/client'

export interface QueryConfig {
  retrieval_mode: string
  top_k: number
  alpha: number
  ef: number | null
  response_format: string
}

// Mirrors api/services/retrieval_config.py DEFAULTS. Used before a collection
// is chosen and as the fallback when the API cannot be reached.
export const DEFAULT_CONFIG: QueryConfig = {
  retrieval_mode: 'hnsw',
  top_k: 5,
  alpha: 0.75,
  ef: null,
  response_format: 'end_user',
}

interface QueryConfigValue {
  collection: string
  setCollection: (name: string) => void
  config: QueryConfig
  /** True while the collection has no saved settings and is using DEFAULT_CONFIG. */
  isDefault: boolean
  loading: boolean
  error: string
  saveConfig: (config: QueryConfig) => Promise<void>
}

const QueryConfigContext = createContext<QueryConfigValue | null>(null)

function fromResponse(r: RetrievalConfig): QueryConfig {
  return {
    retrieval_mode: r.retrieval_mode,
    top_k: r.top_k,
    alpha: r.alpha,
    ef: r.ef,
    response_format: r.response_format,
  }
}

export function QueryConfigProvider({ children }: { children: ReactNode }) {
  const [collection, setCollection] = useState('')
  const [config, setConfigState] = useState<QueryConfig>(DEFAULT_CONFIG)
  const [isDefault, setIsDefault] = useState(true)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // Settings are fetched per collection, so a slow response for a collection
  // the user has already navigated away from must not overwrite the current
  // one. Every load carries a ticket; only the latest ticket may apply.
  const requestId = useRef(0)

  useEffect(() => {
    if (!collection) {
      setConfigState(DEFAULT_CONFIG)
      setIsDefault(true)
      setError('')
      setLoading(false)
      return
    }
    const ticket = ++requestId.current
    setLoading(true)
    setError('')
    api
      .getRetrievalConfig(collection)
      .then(r => {
        if (ticket !== requestId.current) return
        setConfigState(fromResponse(r))
        setIsDefault(r.is_default)
        setLoading(false)
      })
      .catch((e: unknown) => {
        if (ticket !== requestId.current) return
        // Falling back to defaults keeps Q&A answerable when the settings
        // endpoint is unavailable, rather than blocking the page.
        setConfigState(DEFAULT_CONFIG)
        setIsDefault(true)
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      })
  }, [collection])

  const saveConfig = useCallback(
    async (next: QueryConfig) => {
      if (!collection) throw new Error('Select a collection before saving retrieval settings.')
      const saved = await api.saveRetrievalConfig({ collection, ...next })
      // A completed save supersedes any load still in flight for this
      // collection, which would otherwise land afterwards with stale values.
      requestId.current++
      setConfigState(fromResponse(saved))
      setIsDefault(saved.is_default)
      setError('')
      setLoading(false)
    },
    [collection],
  )

  return (
    <QueryConfigContext.Provider
      value={{ collection, setCollection, config, isDefault, loading, error, saveConfig }}
    >
      {children}
    </QueryConfigContext.Provider>
  )
}

export function useQueryConfig() {
  const ctx = useContext(QueryConfigContext)
  if (!ctx) throw new Error('useQueryConfig must be used within QueryConfigProvider')
  return ctx
}
```

### ui/src/api/client.ts

```typescript
const BASE = '/api'

// The proxy's request-body limit: `client_max_body_size` in proxy/nginx.conf.
// It covers a whole request, so a multi-file upload counts every file. Kept
// here only so the Import page can warn before sending; nginx enforces it.
// Change both together.
export const MAX_UPLOAD_MB = 512
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

// Errors the proxy answers itself, before the API sees the request. Those
// arrive as nginx's HTML error pages, not the API's JSON error shape.
const PROXY_ERRORS: Record<number, string> = {
  413: `The upload is larger than the ${MAX_UPLOAD_MB} MB limit. Split it into smaller batches.`,
  502: 'The API is not responding. It may still be starting; try again in a minute.',
  504: 'The API took too long to respond.',
}

async function request<T>(method: string, path: string, body?: unknown, isFormData = false): Promise<T> {
  const headers: Record<string, string> = isFormData ? {} : { 'Content-Type': 'application/json' }
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: isFormData ? (body as FormData) : body !== undefined ? JSON.stringify(body) : undefined,
  })
  // Read as text first: calling res.json() on an nginx error page threw a
  // JSON syntax error, which is what the user saw instead of the real problem.
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { /* not JSON; handled below */ }
  if (!res.ok) {
    throw new Error(data?.error?.message ?? PROXY_ERRORS[res.status] ?? `HTTP ${res.status}`)
  }
  if (data === null && text) throw new Error('The server sent a response the UI could not read.')
  return data as T
}

export const api = {
  getCollections: () => request<{ collections: CollectionInfo[] }>('GET', '/collections'),
  createCollection: (body: CreateCollectionBody) => request('POST', '/collections', body),
  deleteCollection: (name: string) => request('DELETE', `/collections/${name}?confirm=true`),

  // Ingest — form field is "strategy"; job status URL is /ingest/job/{id}; config POST is /ingest/config
  uploadFiles: (form: FormData) => request<{ job_id: string; status: string; files_queued: number; collection: string }>('POST', '/ingest/upload', form, true),
  getJobStatus: (jobId: string) => request<JobStatus>('GET', `/ingest/job/${jobId}`),
  getIngestConfig: (collection: string) => request<IngestConfig>('GET', `/ingest/config/${collection}`),
  saveIngestConfig: (body: SaveIngestConfigBody) => request<IngestConfig>('POST', '/ingest/config', body),

  // Retrieval settings are stored per collection by the API, not in the browser.
  getRetrievalConfig: (collection: string) => request<RetrievalConfig>('GET', `/retrieval/config/${collection}`),
  saveRetrievalConfig: (body: SaveRetrievalConfigBody) => request<RetrievalConfig>('POST', '/retrieval/config', body),

  query: (body: QueryBody) => request<QueryResult>('POST', '/query', body),

  generateGoldStandard: (body: GenerateBody) => request<GenerateResult>('POST', '/goldstandard/generate', body),
  getSession: (sessionId: string) => request<Session>('GET', `/goldstandard/session/${sessionId}`),
  patchPair: (sessionId: string, pairId: string, body: PatchPairBody) => request<GoldPair>('PATCH', `/goldstandard/session/${sessionId}/pair/${pairId}`, body),
  regeneratePair: (body: { session_id: string; pair_id: string }) => request<GoldPair>('POST', '/goldstandard/regenerate', body),
  saveSession: (body: { session_id: string; filename?: string }) => request<SaveResult>('POST', '/goldstandard/save', body),
  downloadUrl: (filename: string) => `${BASE}/goldstandard/download/${filename}`,

  // Transfer
  startExport: (body: ExportBody) => request<ExportStart>('POST', '/export', body),
  getExportJob: (jobId: string) => request<ExportJob>('GET', `/export/job/${jobId}`),
  startImport: (body: ImportBody) => request<ImportStart>('POST', '/import', body),
  getImportJob: (jobId: string) => request<ImportJob>('GET', `/import/job/${jobId}`),
  getPackages: () => request<{ packages: PackageSummary[] }>('GET', '/packages'),
  getTuneOptions: (collection: string) => request<TuneOptions>('GET', `/tune/${collection}`),
  getTransferHelp: () => request<{ topic: string; markdown: string }>('GET', '/help/transfer'),

  getMetrics: () => request<MetricsResult>('GET', '/metrics/latency'),
  getHealth: () => request<HealthResult>('GET', '/health'),
}

// Types
export interface CollectionInfo {
  name: string; object_count: number; index_type: string; distance_metric: string; created_at: string | null
}
export interface CreateCollectionBody {
  name: string; index_type: string; distance_metric: string; hnsw_config: { efConstruction: number; maxConnections: number; ef: number }
}
export interface JobStatus {
  job_id: string; status: string; files_total: number; files_completed: number; files_failed: number; chunks_stored: number; errors: string[]
  // Files that could not be parsed at all — never counted in files_total.
  skipped?: string[]
}
export interface IngestConfig {
  collection: string; chunking_strategy: string; chunk_size: number; chunk_overlap: number; similarity_threshold: number | null; min_chunk_size: number; is_default: boolean
}
export interface SaveIngestConfigBody {
  collection: string; chunking_strategy: string; chunk_size: number; chunk_overlap: number; similarity_threshold: number | null; min_chunk_size: number
}
export interface QueryBody {
  question: string; collection: string; retrieval_mode: string; top_k: number; alpha?: number; include_citations: boolean; response_format: string
}
export interface RetrievalConfig {
  collection: string; retrieval_mode: string; top_k: number; alpha: number; ef: number | null; response_format: string; is_default: boolean
}
export interface SaveRetrievalConfigBody {
  collection: string; retrieval_mode: string; top_k: number; alpha: number; ef: number | null; response_format: string
}
export interface Citation {
  source_file: string; chunk_index: number; score: number; excerpt: string
}
export interface QueryResult {
  answer: string; citations: Citation[] | null; retrieval_latency_ms: number; llm_latency_ms: number; chunks_retrieved: number
}
export interface GenerateBody { collection: string; sample_size: number; seed?: number }
export interface GenerateResult { session_id: string; status: string; pairs_total: number; pairs_completed: number }
// pairs_attempted reaches pairs_total even when a pair fails; pairs_completed is how many exist.
export interface GoldPair {
  pair_id: string; question: string; answer: string; contexts: string[]; ground_truth: string; source_file: string; chunk_index: number; status: string
}
export interface Session {
  session_id: string; status: string; pairs_total: number; pairs_attempted?: number
  pairs_completed: number; pairs_failed?: number; pairs: GoldPair[]; collection: string; errors?: string[]
}
export interface PatchPairBody { status: string; question?: string; answer?: string; ground_truth?: string }
export interface SaveResult { filename: string; pairs_saved: number; pairs_excluded: number; download_url: string }
export interface LatencyStats { p50: number; p95: number; p99: number }
export interface LatencyRecord {
  timestamp: string
  collection: string
  retrieval_mode: string
  retrieval_ms: number
  llm_ms: number
  total_ms: number
}
export interface MetricsResult {
  total_records: number
  retrieval_latency: LatencyStats
  llm_latency: LatencyStats
  total_latency: LatencyStats
  history: LatencyRecord[]
}
export interface ExportBody { collection: string; include_models: boolean }
export interface ExportStart { job_id: string; status: string; collection: string }
export interface ExportJob {
  job_id: string; status: string; collection: string; chunks_written: number
  filename: string | null; size_bytes: number | null; source_document_count: number | null
  fidelity: string | null; models_bundled: boolean | null; retrieve_script: boolean | null
  warnings: string[]; error: string | null
}
export interface ImportBody { filename: string; on_conflict: string }
export interface ImportStart { job_id: string; status: string; filename: string }
export interface ImportJob {
  job_id: string; status: string; filename: string; on_conflict: string
  collection: string | null; original_collection: string | null; chunks_written: number
  fidelity: string | null; renamed: boolean; notes: string[]
  error: string | null; error_code: string | null; error_detail: Record<string, unknown> | null
}
export interface PackageSummary {
  filename: string; size_bytes: number; collection: string | null
  chunk_count: number | null; fidelity: string | null; created_at: string | null; readable: boolean
}
export interface TuneOptions {
  collection: string; fidelity: string; source_document_count: number
  can_rechunk: boolean; can_reembed: boolean; can_reindex: boolean; note: string
}
export interface HealthResult {
  status: string
  services: {
    weaviate: { status: string; latency_ms: number }
    // The API nests these under `ollama`; they are not flat `ollama_llm` /
    // `ollama_embed` keys. Reading the flat names yielded undefined and threw
    // during render, which unmounted the whole app.
    ollama: {
      llm: { status: string; latency_ms: number; model: string }
      embed: { status: string; latency_ms: number; model: string }
    }
  }
  resources?: {
    memory: {
      status: string
      allocated_gb: number | null
      recommended_minimum_gb: number
      note?: string
    }
  }
}
```

### ui/src/components/NavBar.tsx

```typescript
import { Link, useNavigate } from 'react-router-dom'
import { useRole } from '../context/RoleContext'

export default function NavBar() {
  const { role, setRole } = useRole()
  const navigate = useNavigate()

  function switchRole() {
    setRole(null)
    navigate('/')
  }

  const roleLabel = role === 'engineer' ? 'AI Engineer' : role === 'developer' ? 'Developer' : 'End User'

  return (
    <nav className="bg-white border-b border-gray-200 px-4 py-3 flex items-center gap-6">
      <span className="font-bold text-blue-700 text-lg">RAG Platform</span>
      <Link to="/qa" className="text-sm text-gray-700 hover:text-blue-600">Q&amp;A</Link>
      {role !== 'end_user' && (
        <>
          <Link to="/import" className="text-sm text-gray-700 hover:text-blue-600">Import</Link>
          <Link to="/chunking" className="text-sm text-gray-700 hover:text-blue-600">Chunking</Link>
          <Link to="/retrieval" className="text-sm text-gray-700 hover:text-blue-600">Retrieval</Link>
          <Link to="/goldstandard" className="text-sm text-gray-700 hover:text-blue-600">Gold Standard</Link>
          <Link to="/transfer" className="text-sm text-gray-700 hover:text-blue-600">Transfer</Link>
        </>
      )}
      {role === 'engineer' && (
        <>
          <Link to="/collections" className="text-sm text-gray-700 hover:text-blue-600">Collections</Link>
          <Link to="/health" className="text-sm text-gray-700 hover:text-blue-600">Health</Link>
        </>
      )}
      <div className="ml-auto flex items-center gap-3">
        <span className="text-xs bg-blue-100 text-blue-800 px-2 py-1 rounded">{roleLabel}</span>
        <button onClick={switchRole} className="text-sm text-gray-500 hover:text-blue-600">Switch Role</button>
      </div>
    </nav>
  )
}
```

### ui/src/components/RoleGate.tsx

```typescript
import { ReactNode } from 'react'
import { useRole } from '../context/RoleContext'

interface Props {
  roles: string[]
  children: ReactNode
}

export default function RoleGate({ roles, children }: Props) {
  const { role } = useRole()
  if (!role || !roles.includes(role)) return null
  return <>{children}</>
}
```

### ui/src/components/CitationsPanel.tsx

```typescript
import { Citation } from '../api/client'

export default function CitationsPanel({ citations }: { citations: Citation[] }) {
  return (
    <div className="mt-4 border-t pt-4">
      <h3 className="text-sm font-semibold text-gray-600 mb-2">Sources</h3>
      <div className="space-y-2">
        {citations.map((c, i) => (
          <div key={i} className="text-xs bg-gray-50 border rounded p-2">
            <div className="flex justify-between mb-1">
              <span className="font-medium">{c.source_file}</span>
              <span className="text-gray-500">chunk {c.chunk_index} · score {c.score.toFixed(3)}</span>
            </div>
            <p className="text-gray-700 italic">{c.excerpt}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
```

### ui/src/components/ProgressPanel.tsx

```typescript
import { JobStatus } from '../api/client'

export default function ProgressPanel({ job }: { job: JobStatus }) {
  const pct = job.files_total > 0 ? Math.round((job.files_completed / job.files_total) * 100) : 0
  return (
    <div className="mt-4 p-4 border rounded bg-gray-50">
      <div className="flex justify-between text-sm mb-1">
        <span>Files: {job.files_completed}/{job.files_total}</span>
        <span>Chunks stored: {job.chunks_stored}</span>
        <span className={job.status === 'completed' ? 'text-green-600' : job.status === 'failed' ? 'text-red-600' : 'text-blue-600'}>
          {job.status}
        </span>
      </div>
      <div className="w-full bg-gray-200 rounded h-2">
        <div className="bg-blue-500 h-2 rounded transition-all" style={{ width: `${pct}%` }} />
      </div>
      {job.errors.length > 0 && (
        <ul className="mt-2 text-xs text-red-600 space-y-1">
          {job.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
      {(job.skipped?.length ?? 0) > 0 && (
        // Skipped files are never counted in files_total, so without this the
        // count simply reads lower than what the user uploaded.
        <div className="mt-2 text-xs text-amber-700">
          <p className="font-medium">
            {job.skipped!.length} file{job.skipped!.length === 1 ? '' : 's'} skipped — not a supported type:
          </p>
          <ul className="space-y-1 mt-1">
            {job.skipped!.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}
```

### ui/src/components/StrategyExplainer.tsx

```typescript
const EXPLANATIONS: Record<string, string> = {
  fixed: 'Splits your document into equal-sized pieces by character count. Simple and fast, but may cut sentences in the middle. Best for structured data like CSV or JSON.',
  overlap: 'Like Fixed Size, but each piece shares some text with the next one. This helps the system find answers that fall near a boundary. A good general-purpose choice.',
  language: 'Splits at natural sentence and paragraph breaks before falling back to character count. Keeps sentences intact. Recommended for most narrative documents.',
  context_aware: "Uses the document's own structure — headings, paragraphs, tables — to define boundaries. Best for structured reports, policies, or manuals with clear section headings.",
  semantic: 'Groups sentences that are about the same topic together, regardless of their position. Produces the most meaningful chunks but is the slowest option. Best for long, dense documents.',
}

export default function StrategyExplainer({ strategy }: { strategy: string }) {
  const text = EXPLANATIONS[strategy]
  if (!text) return null
  return (
    <div className="mt-2 p-3 bg-blue-50 border border-blue-200 rounded text-sm text-blue-800">
      {text}
    </div>
  )
}
```

### ui/src/components/LatencyCharts.tsx

```typescript
import { LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { MetricsResult } from '../api/client'

export default function LatencyCharts({ data }: { data: MetricsResult }) {
  const chartData = data.history.map(r => ({
    time: new Date(r.timestamp).toLocaleTimeString(),
    Retrieval: r.retrieval_ms,
    LLM: r.llm_ms,
    Total: r.total_ms,
  }))

  return (
    <div>
      <div className="grid grid-cols-3 gap-4 mb-6">
        {([
          ['RETRIEVAL', data.retrieval_latency],
          ['LLM', data.llm_latency],
          ['TOTAL', data.total_latency],
        ] as const).map(([label, stats]) => (
          <div key={label} className="border rounded p-3 bg-white">
            <div className="text-xs text-gray-500 mb-1">{label}</div>
            <div className="text-sm">
              <span className="text-gray-600">P50:</span> {Math.round(stats.p50)}ms ·{' '}
              <span className="text-gray-600">P95:</span> {Math.round(stats.p95)}ms ·{' '}
              <span className="text-gray-600">P99:</span> {Math.round(stats.p99)}ms
            </div>
          </div>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData}>
          <XAxis dataKey="time" tick={{ fontSize: 11 }} />
          <YAxis unit="ms" tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend />
          <Line type="monotone" dataKey="Retrieval" stroke="#3b82f6" dot={false} />
          <Line type="monotone" dataKey="LLM" stroke="#f59e0b" dot={false} />
          <Line type="monotone" dataKey="Total" stroke="#10b981" dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
```

### ui/src/pages/LandingPage.tsx

```typescript
import { useNavigate } from 'react-router-dom'
import { useRole } from '../context/RoleContext'

const ROLES = [
  {
    id: 'engineer' as const,
    label: 'AI Engineer',
    description: 'Full access: ingest, chunking, retrieval, gold standard, collection management, health dashboard.',
  },
  {
    id: 'developer' as const,
    label: 'Developer',
    description: 'Ingest documents, configure chunking and retrieval, run Q&A, generate gold standard.',
  },
  {
    id: 'end_user' as const,
    label: 'End User',
    description: 'Ask questions and get answers from the knowledge base.',
  },
]

export default function LandingPage() {
  const { setRole } = useRole()
  const navigate = useNavigate()

  function select(role: 'engineer' | 'developer' | 'end_user') {
    setRole(role)
    navigate('/qa')
  }

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold text-gray-800 mb-2">RAG Platform</h1>
      <p className="text-gray-500 mb-10">Select your role to continue</p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 w-full max-w-3xl">
        {ROLES.map(r => (
          <button
            key={r.id}
            onClick={() => select(r.id)}
            className="bg-white border-2 border-gray-200 hover:border-blue-500 rounded-xl p-6 text-left transition-all shadow-sm hover:shadow-md"
          >
            <h2 className="text-lg font-semibold text-gray-800 mb-2">{r.label}</h2>
            <p className="text-sm text-gray-500">{r.description}</p>
          </button>
        ))}
      </div>
    </div>
  )
}
```

### ui/src/pages/QAPage.tsx

```typescript
import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, CollectionInfo, Citation } from '../api/client'
import { useRole } from '../context/RoleContext'
import { useQueryConfig } from '../context/QueryConfigContext'
import CitationsPanel from '../components/CitationsPanel'

export default function QAPage() {
  const { role } = useRole()
  // The selected collection lives in the context because the retrieval
  // settings are stored per collection and must follow the selection.
  const { collection, setCollection, config } = useQueryConfig()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [answer, setAnswer] = useState('')
  const [citations, setCitations] = useState<Citation[] | null>(null)
  const [showCitations, setShowCitations] = useState(false)
  const [latency, setLatency] = useState<{ ret: number; llm: number } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (!collection && r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
    // Runs once; a collection already chosen on the Retrieval page is kept.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function submit() {
    if (!question.trim() || !collection) return
    setLoading(true)
    setError('')
    setAnswer('')
    setCitations(null)
    setLatency(null)
    try {
      const result = await api.query({
        question,
        collection,
        retrieval_mode: config.retrieval_mode,
        top_k: config.top_k,
        alpha: config.alpha,
        include_citations: showCitations,
        response_format: role === 'end_user' ? 'end_user' : 'engineer',
      })
      setAnswer(result.answer)
      setCitations(result.citations)
      setLatency({ ret: result.retrieval_latency_ms, llm: result.llm_latency_ms })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && e.ctrlKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="max-w-3xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Ask a Question</h1>
      <div className="flex gap-3 mb-3">
        <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm flex-1">
          {collections.map(c => <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>)}
        </select>
        {role !== 'end_user' && (
          <select value={config.retrieval_mode} disabled className="border rounded px-3 py-2 text-sm bg-gray-50 text-gray-500">
            <option value={config.retrieval_mode}>{config.retrieval_mode}</option>
          </select>
        )}
      </div>
      <textarea
        value={question}
        onChange={e => setQuestion(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Type your question… (Ctrl+Enter to submit)"
        rows={3}
        className="w-full border rounded px-3 py-2 text-sm mb-3 resize-none focus:outline-none focus:ring-2 focus:ring-blue-300"
      />
      <div className="flex items-center gap-4 mb-4">
        <button
          onClick={submit}
          disabled={loading || !question.trim()}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700"
        >
          {loading ? 'Thinking…' : 'Ask'}
        </button>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input type="checkbox" checked={showCitations} onChange={e => setShowCitations(e.target.checked)} />
          Show source citations
        </label>
      </div>
      {error && <p className="text-red-600 text-sm mb-3">{error}</p>}
      {answer && (
        <div className="bg-white border rounded p-4">
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{answer}</ReactMarkdown>
          </div>
          {latency && (
            <p className="text-xs text-gray-400 mt-3">
              Retrieved in {latency.ret}ms · Generated in {latency.llm}ms
            </p>
          )}
          {showCitations && citations && <CitationsPanel citations={citations} />}
        </div>
      )}
    </div>
  )
}
```

### ui/src/pages/ImportPage.tsx

```typescript
import { useState, useEffect } from 'react'
import { api, CollectionInfo, JobStatus, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from '../api/client'
import { useRole } from '../context/RoleContext'
import StrategyExplainer from '../components/StrategyExplainer'
import ProgressPanel from '../components/ProgressPanel'

const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']

export default function ImportPage() {
  const { role } = useRole()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [strategy, setStrategy] = useState('overlap')
  const [chunkSize, setChunkSize] = useState(1000)
  const [chunkOverlap, setChunkOverlap] = useState(200)
  const [minChunkSize, setMinChunkSize] = useState(100)
  const [similarityThreshold, setSimilarityThreshold] = useState(0.85)
  const [jobId, setJobId] = useState('')
  const [job, setJob] = useState<JobStatus | null>(null)
  const [error, setError] = useState('')
  const [showNewColModal, setShowNewColModal] = useState(false)
  const [newColName, setNewColName] = useState('')
  const [newColIndexType, setNewColIndexType] = useState('hnsw')

  const showOverlap = strategy !== 'semantic' && strategy !== 'context_aware'
  const showSimilarity = strategy === 'semantic' && role === 'engineer'

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!jobId) return
    const interval = setInterval(async () => {
      try {
        const j = await api.getJobStatus(jobId)
        setJob(j)
        if (j.status === 'completed' || j.status === 'failed' || j.status === 'partial') clearInterval(interval)
      } catch { /* ignore */ }
    }, 3000)
    return () => clearInterval(interval)
  }, [jobId])

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setFiles(prev => [...prev, ...Array.from(e.dataTransfer.files)])
  }

  async function createCollection() {
    if (!newColName.trim()) return
    try {
      await api.createCollection({
        name: newColName,
        index_type: newColIndexType,
        distance_metric: 'cosine',
        hnsw_config: { efConstruction: 128, maxConnections: 64, ef: 64 },
      })
      const r = await api.getCollections()
      setCollections(r.collections)
      setCollection(newColName)
      setShowNewColModal(false)
      setNewColName('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function startIngest() {
    if (!files.length || !collection) return
    setError('')
    // Refuse before sending: the proxy would reject it anyway, but only after
    // the browser had started pushing hundreds of MB, and a connection the
    // proxy closes mid-upload can surface as a bare network error.
    const total = files.reduce((n, f) => n + f.size, 0)
    if (total > MAX_UPLOAD_BYTES) {
      setError(`These files total ${(total / 1024 / 1024).toFixed(0)} MB; one upload can be at most ${MAX_UPLOAD_MB} MB. Split them into smaller batches.`)
      return
    }
    const form = new FormData()
    files.forEach(f => form.append('files', f))
    form.append('collection', collection)
    // API form field is "strategy" (not "chunking_strategy")
    form.append('strategy', strategy)
    form.append('chunk_size', String(chunkSize))
    form.append('chunk_overlap', String(chunkOverlap))
    form.append('similarity_threshold', String(similarityThreshold))
    form.append('min_chunk_size', String(minChunkSize))
    try {
      const res = await api.uploadFiles(form)
      setJobId(res.job_id)
      setJob(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Import Documents</h1>

      <div
        onDrop={handleDrop}
        onDragOver={e => e.preventDefault()}
        className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center mb-4 cursor-pointer hover:border-blue-400"
        onClick={() => document.getElementById('file-input')?.click()}
      >
        <p className="text-gray-500">Drop files here or click to browse</p>
        <p className="text-xs text-gray-400 mt-1">PDF, DOCX, TXT, MD, CSV, JSON, ZIP · up to {MAX_UPLOAD_MB} MB per upload</p>
        <input id="file-input" type="file" multiple className="hidden" accept=".pdf,.docx,.txt,.md,.csv,.json,.zip"
          onChange={e => setFiles(prev => [...prev, ...Array.from(e.target.files || [])])} />
      </div>

      {files.length > 0 && (
        <ul className="mb-4 space-y-1">
          {files.map((f, i) => (
            <li key={i} className="flex justify-between text-sm bg-gray-50 border rounded px-3 py-1">
              <span>{f.name}</span>
              <button onClick={() => setFiles(files.filter((_, j) => j !== i))} className="text-red-400 hover:text-red-600">✕</button>
            </li>
          ))}
        </ul>
      )}

      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Collection</label>
        <div className="flex gap-2">
          <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm flex-1">
            {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
          <button onClick={() => setShowNewColModal(true)} className="text-sm border rounded px-3 py-2 hover:bg-gray-50">+ New</button>
        </div>
      </div>

      <div className="mb-2">
        <label className="block text-sm font-medium mb-1">Chunking Strategy</label>
        <select value={strategy} onChange={e => setStrategy(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {STRATEGIES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
        </select>
        <StrategyExplainer strategy={strategy} />
      </div>

      <div className="grid grid-cols-2 gap-4 mt-4 mb-4">
        <div>
          <label className="block text-xs text-gray-600 mb-1">Chunk Size: {chunkSize}</label>
          <input type="range" min={200} max={16000} step={100} value={chunkSize} onChange={e => setChunkSize(+e.target.value)} className="w-full" />
        </div>
        {showOverlap && (
          <div>
            <label className="block text-xs text-gray-600 mb-1">Overlap: {chunkOverlap}</label>
            <input type="range" min={0} max={2000} step={50} value={chunkOverlap} onChange={e => setChunkOverlap(+e.target.value)} className="w-full" />
          </div>
        )}
        <div>
          <label className="block text-xs text-gray-600 mb-1">Min Chunk Size: {minChunkSize}</label>
          <input type="range" min={40} max={2000} step={10} value={minChunkSize} onChange={e => setMinChunkSize(+e.target.value)} className="w-full" />
        </div>
        {showSimilarity && (
          <div>
            <label className="block text-xs text-gray-600 mb-1">Similarity Threshold: {similarityThreshold}</label>
            <input type="range" min={0} max={1} step={0.05} value={similarityThreshold} onChange={e => setSimilarityThreshold(+e.target.value)} className="w-full" />
          </div>
        )}
      </div>

      <button onClick={startIngest} disabled={!files.length} className="bg-blue-600 text-white px-6 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">
        Start Ingest
      </button>
      {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
      {job && <ProgressPanel job={job} />}

      {showNewColModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-80 shadow-xl">
            <h2 className="font-semibold mb-4">New Collection</h2>
            <input value={newColName} onChange={e => setNewColName(e.target.value)} placeholder="Collection name" className="border rounded px-3 py-2 text-sm w-full mb-3" />
            <select value={newColIndexType} onChange={e => setNewColIndexType(e.target.value)} className="border rounded px-3 py-2 text-sm w-full mb-4">
              <option value="hnsw">HNSW (Approximate)</option>
              <option value="flat">Flat (Exact KNN)</option>
            </select>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowNewColModal(false)} className="text-sm text-gray-500 hover:text-gray-700">Cancel</button>
              <button onClick={createCollection} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
```

### ui/src/pages/ChunkingPage.tsx

```typescript
import { useState, useEffect } from 'react'
import { api, CollectionInfo, IngestConfig } from '../api/client'
import StrategyExplainer from '../components/StrategyExplainer'
import { useRole } from '../context/RoleContext'

const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']

export default function ChunkingPage() {
  const { role } = useRole()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [config, setConfig] = useState<IngestConfig | null>(null)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!collection) return
    api.getIngestConfig(collection).then(setConfig).catch(() => {})
  }, [collection])

  async function save() {
    if (!config) return
    setError('')
    try {
      await api.saveIngestConfig({
        collection,
        chunking_strategy: config.chunking_strategy,
        chunk_size: config.chunk_size,
        chunk_overlap: config.chunk_overlap,
        similarity_threshold: config.similarity_threshold,
        min_chunk_size: config.min_chunk_size,
      })
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
      api.getIngestConfig(collection).then(setConfig)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const showOverlap = config && config.chunking_strategy !== 'semantic' && config.chunking_strategy !== 'context_aware'
  const showSimilarity = config && config.chunking_strategy === 'semantic' && role === 'engineer'

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Chunking Configuration</h1>
      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Collection</label>
        <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
      </div>
      {config && (
        <>
          {config.is_default && <p className="text-xs text-amber-600 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-4">Using system defaults. Save to set a custom configuration for this collection.</p>}
          <div className="mb-4">
            <label className="block text-sm font-medium mb-1">Strategy</label>
            <select value={config.chunking_strategy} onChange={e => setConfig({ ...config, chunking_strategy: e.target.value })} className="border rounded px-3 py-2 text-sm w-full">
              {STRATEGIES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
            </select>
            <StrategyExplainer strategy={config.chunking_strategy} />
          </div>
          <div className="grid grid-cols-2 gap-4 mb-4">
            <div>
              <label className="block text-xs text-gray-600 mb-1">Chunk Size: {config.chunk_size}</label>
              <input type="range" min={200} max={16000} step={100} value={config.chunk_size} onChange={e => setConfig({ ...config, chunk_size: +e.target.value })} className="w-full" />
            </div>
            {showOverlap && (
              <div>
                <label className="block text-xs text-gray-600 mb-1">Overlap: {config.chunk_overlap}</label>
                <input type="range" min={0} max={2000} step={50} value={config.chunk_overlap} onChange={e => setConfig({ ...config, chunk_overlap: +e.target.value })} className="w-full" />
              </div>
            )}
            <div>
              <label className="block text-xs text-gray-600 mb-1">Min Chunk Size: {config.min_chunk_size}</label>
              <input type="range" min={40} max={2000} step={10} value={config.min_chunk_size} onChange={e => setConfig({ ...config, min_chunk_size: +e.target.value })} className="w-full" />
            </div>
            {showSimilarity && (
              <div>
                <label className="block text-xs text-gray-600 mb-1">Similarity Threshold: {config.similarity_threshold ?? 0.85}</label>
                <input type="range" min={0} max={1} step={0.05} value={config.similarity_threshold ?? 0.85} onChange={e => setConfig({ ...config, similarity_threshold: +e.target.value })} className="w-full" />
              </div>
            )}
          </div>
          <button onClick={save} className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700">Save as Default</button>
          {saved && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
          {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
        </>
      )}
    </div>
  )
}
```

### ui/src/pages/RetrievalPage.tsx

```typescript
import { useEffect, useState } from 'react'
import { api, CollectionInfo } from '../api/client'
import { useRole } from '../context/RoleContext'
import { useQueryConfig, QueryConfig } from '../context/QueryConfigContext'

const MODES = [
  { id: 'hnsw', label: 'HNSW — Approximate (default)', description: 'The fastest option. Uses a smart graph to find the closest matches quickly. May very rarely miss the single best result, but works well for almost all use cases.' },
  { id: 'flat', label: 'Flat — Exact', description: 'Checks every stored chunk to find the mathematically perfect match. More accurate but slower as your collection grows. Best for collections under 10,000 chunks.' },
  { id: 'hybrid', label: 'Hybrid', description: 'Combines keyword search with meaning-based search. Best when your questions include specific terms, names, or codes. Adjust the slider to balance between the two modes.' },
  { id: 'semantic', label: 'Semantic', description: 'Pure meaning-based search. Best for conceptual questions where the exact words are less important than the idea.' },
]

export default function RetrievalPage() {
  const { role } = useRole()
  const { collection, setCollection, config, isDefault, loading, error, saveConfig } = useQueryConfig()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [mode, setMode] = useState(config.retrieval_mode)
  const [topK, setTopK] = useState(config.top_k)
  const [alpha, setAlpha] = useState(config.alpha)
  const [ef, setEf] = useState(config.ef ?? 64)
  const [efConstruction, setEfConstruction] = useState(128)
  const [maxConnections, setMaxConnections] = useState(64)
  const [applied, setApplied] = useState(false)
  const [saveError, setSaveError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (!collection && r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
    // Runs once; picking a default collection must not fight the user's choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Saved settings arrive asynchronously and change whenever another
  // collection is picked, so the form mirrors the context rather than owning
  // the values. `applied` is deliberately not reset here: a save replaces
  // `config`, which would otherwise clear the confirmation immediately.
  useEffect(() => {
    setMode(config.retrieval_mode)
    setTopK(config.top_k)
    setAlpha(config.alpha)
    setEf(config.ef ?? 64)
  }, [config])

  useEffect(() => {
    setApplied(false)
    setSaveError('')
  }, [collection])

  async function apply() {
    const next: QueryConfig = {
      retrieval_mode: mode,
      top_k: topK,
      alpha,
      ef: mode === 'hnsw' ? ef : null,
      // The role toggle drives the answer style live; persisting it here is
      // what makes the stored config usable by an exported retrieval script.
      response_format: role === 'end_user' ? 'end_user' : 'engineer',
    }
    setSaveError('')
    try {
      await saveConfig(next)
      setApplied(true)
      setTimeout(() => setApplied(false), 3000)
    } catch (e: unknown) {
      setSaveError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="max-w-xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">Retrieval Configuration</h1>
      <p className="text-sm text-gray-500 mb-6">
        Settings are saved per collection on the server, so they survive a restart and travel with an export.
      </p>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Collection</label>
        <select
          value={collection}
          onChange={e => setCollection(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm"
        >
          {collections.length === 0 && <option value="">No collections yet</option>}
          {collections.map(c => (
            <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>
          ))}
        </select>
        <p className="text-xs text-gray-500 mt-1">
          {loading
            ? 'Loading saved settings…'
            : collection
              ? isDefault
                ? 'No settings saved for this collection yet — showing defaults.'
                : 'Showing the settings saved for this collection.'
              : 'Create a collection to configure retrieval.'}
        </p>
        {error && <p className="text-xs text-amber-600 mt-1">Could not load saved settings ({error}). Showing defaults.</p>}
      </div>

      <div className="mb-4">
        <label className="block text-sm font-medium mb-2">Retrieval Mode</label>
        <div className="space-y-2">
          {MODES.map(m => (
            <label key={m.id} className={`flex gap-3 p-3 border rounded cursor-pointer ${mode === m.id ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
              <input type="radio" name="mode" value={m.id} checked={mode === m.id} onChange={() => setMode(m.id)} className="mt-1" />
              <div>
                <div className="text-sm font-medium">{m.label}</div>
                <div className="text-xs text-gray-500">{m.description}</div>
              </div>
            </label>
          ))}
        </div>
      </div>

      <div className="mb-4">
        <label className="block text-xs text-gray-600 mb-1">Top-K Results: {topK}</label>
        <input type="range" min={1} max={20} value={topK} onChange={e => setTopK(+e.target.value)} className="w-full" />
      </div>

      {mode === 'hybrid' && (
        <div className="mb-4">
          <label className="block text-xs text-gray-600 mb-1">
            Keyword ← Balance → Meaning: {alpha}
          </label>
          <input type="range" min={0} max={1} step={0.05} value={alpha} onChange={e => setAlpha(+e.target.value)} className="w-full" />
        </div>
      )}

      {mode === 'hnsw' && role === 'engineer' && (
        <details className="mb-4 border rounded">
          <summary className="px-3 py-2 text-sm cursor-pointer font-medium">Advanced HNSW Parameters</summary>
          <div className="p-3 space-y-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">ef (query accuracy): {ef}</label>
              <input type="range" min={16} max={512} step={8} value={ef} onChange={e => setEf(+e.target.value)} className="w-full" />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">efConstruction (build accuracy): {efConstruction}</label>
              <input type="range" min={64} max={512} step={8} value={efConstruction} onChange={e => setEfConstruction(+e.target.value)} className="w-full" />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">maxConnections (graph density): {maxConnections}</label>
              <input type="range" min={16} max={128} step={4} value={maxConnections} onChange={e => setMaxConnections(+e.target.value)} className="w-full" />
            </div>
          </div>
        </details>
      )}

      <button
        onClick={apply}
        disabled={!collection || loading}
        className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
      >
        Save for this collection
      </button>
      {applied && <span className="ml-3 text-green-600 text-sm">Saved!</span>}
      {saveError && <p className="text-red-600 text-sm mt-3">{saveError}</p>}
    </div>
  )
}
```

### ui/src/pages/GoldStandardPage.tsx

```typescript
import { useState, useEffect, useRef } from 'react'
import { api, CollectionInfo, Session, GoldPair } from '../api/client'

export default function GoldStandardPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [sampleSize, setSampleSize] = useState(20)
  const [session, setSession] = useState<Session | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [filename, setFilename] = useState('')
  const [saveResult, setSaveResult] = useState('')
  const [editingPair, setEditingPair] = useState<GoldPair | null>(null)
  const [editQuestion, setEditQuestion] = useState('')
  const [editAnswer, setEditAnswer] = useState('')
  const [editGroundTruth, setEditGroundTruth] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!sessionId) return
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.getSession(sessionId)
        setSession(s)
        if (s.status !== 'generating') {
          clearInterval(pollRef.current!)
        }
      } catch { /* ignore */ }
    }, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [sessionId])

  async function generate() {
    if (session && !confirm('This will start a new session and replace the current one. Any unsaved pairs will be lost. Continue?')) return
    setError('')
    setLoading(true)
    setSaveResult('')
    try {
      const res = await api.generateGoldStandard({ collection, sample_size: sampleSize })
      setSessionId(res.session_id)
      setSession(null)
      const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15)
      setFilename(`${collection}_${ts}.json`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function patchPair(pairId: string, status: string, updates?: { question?: string; answer?: string; ground_truth?: string }) {
    if (!sessionId) return
    const body = { status, ...updates }
    try {
      const updated = await api.patchPair(sessionId, pairId, body)
      setSession(prev => prev ? { ...prev, pairs: prev.pairs.map(p => p.pair_id === pairId ? updated : p) } : prev)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function regenerate(pairId: string) {
    if (!sessionId) return
    try {
      const updated = await api.regeneratePair({ session_id: sessionId, pair_id: pairId })
      setSession(prev => prev ? { ...prev, pairs: prev.pairs.map(p => p.pair_id === pairId ? updated : p) } : prev)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function saveExport() {
    if (!sessionId) return
    try {
      const res = await api.saveSession({ session_id: sessionId, filename: filename || undefined })
      setSaveResult(`${res.pairs_saved} pairs exported, ${res.pairs_excluded} excluded.`)
      window.open(api.downloadUrl(res.filename), '_blank')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  function openEdit(pair: GoldPair) {
    setEditingPair(pair)
    setEditQuestion(pair.question)
    setEditAnswer(pair.answer)
    setEditGroundTruth(pair.ground_truth)
  }

  async function submitEdit() {
    if (!editingPair) return
    await patchPair(editingPair.pair_id, 'edited', {
      question: editQuestion,
      answer: editAnswer,
      ground_truth: editGroundTruth,
    })
    setEditingPair(null)
  }

  const approved = session?.pairs.filter(p => p.status === 'approved').length ?? 0
  const edited = session?.pairs.filter(p => p.status === 'edited').length ?? 0
  const rejected = session?.pairs.filter(p => p.status === 'rejected').length ?? 0
  const pending = session?.pairs.filter(p => p.status === 'pending').length ?? 0
  const canExport = approved + edited > 0

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Gold Standard Generator</h1>

      <div className="bg-white border rounded p-4 mb-6">
        <h2 className="font-semibold mb-3">Phase 1 — Generate</h2>
        <div className="flex gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Collection</label>
            <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm">
              {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Sample Size (1–100)</label>
            <input type="number" min={1} max={100} value={sampleSize} onChange={e => setSampleSize(+e.target.value)} className="border rounded px-3 py-2 text-sm w-24" />
          </div>
          <button onClick={generate} disabled={loading} className="bg-blue-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">
            Generate Pairs
          </button>
        </div>

        {session && session.status === 'generating' && (
          <div className="mt-3">
            <p className="text-sm text-blue-600">Generating… {session.pairs_attempted ?? session.pairs_completed}/{session.pairs_total} pairs</p>
            <div className="w-full bg-gray-200 rounded h-2 mt-1">
              <div className="bg-blue-500 h-2 rounded transition-all" style={{ width: `${session.pairs_total > 0 ? ((session.pairs_attempted ?? session.pairs_completed) / session.pairs_total) * 100 : 0}%` }} />
            </div>
          </div>
        )}
      </div>

      {error && <p className="text-red-600 text-sm mb-4">{error}</p>}

      {session && session.pairs.length > 0 && (
        <div className="bg-white border rounded p-4 mb-6">
          <h2 className="font-semibold mb-2">Phase 2 — Review</h2>
          <p className="text-xs text-gray-500 mb-3">
            {approved} approved · {edited} edited · {rejected} rejected · {pending} pending
          </p>
          <div className="space-y-3">
            {session.pairs.map(pair => (
              <details key={pair.pair_id} className={`border rounded ${pair.status === 'approved' ? 'border-green-300 bg-green-50' : pair.status === 'rejected' ? 'border-red-200 bg-red-50' : pair.status === 'edited' ? 'border-yellow-300 bg-yellow-50' : ''}`}>
                <summary className="px-3 py-2 cursor-pointer flex items-center gap-2 text-sm">
                  <span className="flex-1 font-medium">{pair.question}</span>
                  <span className="text-xs text-gray-400">{pair.source_file}</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${
                    pair.status === 'approved' ? 'bg-green-200 text-green-800' :
                    pair.status === 'rejected' ? 'bg-red-200 text-red-800' :
                    pair.status === 'edited' ? 'bg-yellow-200 text-yellow-800' :
                    'bg-gray-200 text-gray-600'
                  }`}>{pair.status}</span>
                  <button onClick={e => { e.preventDefault(); patchPair(pair.pair_id, 'approved') }} className="text-green-600 hover:text-green-800 text-lg leading-none" title="Approve">✓</button>
                  <button onClick={e => { e.preventDefault(); openEdit(pair) }} className="text-blue-500 hover:text-blue-700 text-sm" title="Edit">✎</button>
                  <button onClick={e => { e.preventDefault(); patchPair(pair.pair_id, 'rejected') }} className="text-red-500 hover:text-red-700 text-sm" title="Reject">✕</button>
                  <button onClick={e => { e.preventDefault(); regenerate(pair.pair_id) }} className="text-gray-400 hover:text-gray-600 text-sm" title="Regenerate">↺</button>
                </summary>
                <div className="px-3 pb-3 space-y-2 text-xs text-gray-600">
                  <div><strong>Answer:</strong> {pair.answer}</div>
                  <div><strong>Ground Truth:</strong> {pair.ground_truth}</div>
                  <div><strong>Context:</strong> <span className="text-gray-500">{pair.contexts[0]?.slice(0, 300)}…</span></div>
                </div>
              </details>
            ))}
          </div>
        </div>
      )}

      {session && session.pairs.length > 0 && (
        <div className="bg-white border rounded p-4">
          <h2 className="font-semibold mb-3">Phase 3 — Export</h2>
          <div className="flex gap-3 items-center">
            <input value={filename} onChange={e => setFilename(e.target.value)} placeholder="filename.json" className="border rounded px-3 py-2 text-sm flex-1" />
            <button onClick={saveExport} disabled={!canExport} className="bg-green-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-green-700">
              Export Approved
            </button>
          </div>
          {saveResult && <p className="text-sm text-green-600 mt-2">{saveResult}</p>}
        </div>
      )}

      {editingPair && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg shadow-xl">
            <h2 className="font-semibold mb-4">Edit Pair</h2>
            <label className="block text-xs text-gray-600 mb-1">Question</label>
            <textarea value={editQuestion} onChange={e => setEditQuestion(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-3" />
            <label className="block text-xs text-gray-600 mb-1">Answer</label>
            <textarea value={editAnswer} onChange={e => setEditAnswer(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-3" />
            <label className="block text-xs text-gray-600 mb-1">Ground Truth</label>
            <textarea value={editGroundTruth} onChange={e => setEditGroundTruth(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-4" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setEditingPair(null)} className="text-sm text-gray-500">Cancel</button>
              <button onClick={submitEdit} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
```

### ui/src/pages/CollectionsPage.tsx

```typescript
import { useState, useEffect } from 'react'
import { api, CollectionInfo } from '../api/client'

export default function CollectionsPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [error, setError] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [name, setName] = useState('')
  const [indexType, setIndexType] = useState('hnsw')
  const [distanceMetric, setDistanceMetric] = useState('cosine')
  const [efConstruction, setEfConstruction] = useState(128)
  const [maxConnections, setMaxConnections] = useState(64)
  const [ef, setEf] = useState(64)
  const [deleteTarget, setDeleteTarget] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState('')

  async function load() {
    api.getCollections().then(r => setCollections(r.collections)).catch(() => {})
  }

  useEffect(() => { load() }, [])

  async function create() {
    try {
      await api.createCollection({ name, index_type: indexType, distance_metric: distanceMetric, hnsw_config: { efConstruction, maxConnections, ef } })
      setShowModal(false)
      setName('')
      load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function deleteCollection() {
    if (deleteConfirm !== deleteTarget) return
    try {
      await api.deleteCollection(deleteTarget)
      setDeleteTarget('')
      setDeleteConfirm('')
      load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Collections</h1>
        <button onClick={() => setShowModal(true)} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">+ New Collection</button>
      </div>
      {error && <p className="text-red-600 text-sm mb-4">{error}</p>}
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-2">Name</th>
            <th className="py-2">Objects</th>
            <th className="py-2">Index</th>
            <th className="py-2">Distance</th>
            <th className="py-2">Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {collections.map(c => (
            <tr key={c.name} className="border-b hover:bg-gray-50">
              <td className="py-2 font-medium">{c.name}</td>
              <td className="py-2">{c.object_count}</td>
              <td className="py-2">{c.index_type}</td>
              <td className="py-2">{c.distance_metric}</td>
              <td className="py-2 text-gray-500">{c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}</td>
              <td className="py-2">
                <button onClick={() => setDeleteTarget(c.name)} className="text-red-400 hover:text-red-600 text-xs">Delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {showModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold mb-4">New Collection</h2>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="Name" className="border rounded px-3 py-2 text-sm w-full mb-3" />
            <div className="grid grid-cols-2 gap-3 mb-3">
              <div>
                <label className="text-xs text-gray-500 block mb-1">Index Type</label>
                <select value={indexType} onChange={e => setIndexType(e.target.value)} className="border rounded px-2 py-1 text-sm w-full">
                  <option value="hnsw">HNSW</option>
                  <option value="flat">Flat (KNN)</option>
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 block mb-1">Distance</label>
                <select value={distanceMetric} onChange={e => setDistanceMetric(e.target.value)} className="border rounded px-2 py-1 text-sm w-full">
                  <option value="cosine">Cosine</option>
                  <option value="dot">Dot Product</option>
                  <option value="l2-squared">L2 Squared</option>
                </select>
              </div>
            </div>
            {indexType === 'hnsw' && (
              <div className="space-y-2 mb-4">
                <div>
                  <label className="text-xs text-gray-500">efConstruction: {efConstruction}</label>
                  <input type="range" min={64} max={512} step={8} value={efConstruction} onChange={e => setEfConstruction(+e.target.value)} className="w-full" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">maxConnections: {maxConnections}</label>
                  <input type="range" min={16} max={128} step={4} value={maxConnections} onChange={e => setMaxConnections(+e.target.value)} className="w-full" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">ef: {ef}</label>
                  <input type="range" min={16} max={512} step={8} value={ef} onChange={e => setEf(+e.target.value)} className="w-full" />
                </div>
              </div>
            )}
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowModal(false)} className="text-sm text-gray-500">Cancel</button>
              <button onClick={create} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create</button>
            </div>
          </div>
        </div>
      )}

      {deleteTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-96 shadow-xl">
            <h2 className="font-semibold text-red-600 mb-3">Delete Collection</h2>
            <p className="text-sm text-gray-600 mb-3">Type <strong>{deleteTarget}</strong> to confirm deletion of all objects.</p>
            <input value={deleteConfirm} onChange={e => setDeleteConfirm(e.target.value)} placeholder={deleteTarget} className="border rounded px-3 py-2 text-sm w-full mb-4" />
            <div className="flex justify-end gap-2">
              <button onClick={() => { setDeleteTarget(''); setDeleteConfirm('') }} className="text-sm text-gray-500">Cancel</button>
              <button onClick={deleteCollection} disabled={deleteConfirm !== deleteTarget} className="bg-red-600 text-white px-4 py-2 rounded text-sm disabled:opacity-40 hover:bg-red-700">Delete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
```

### ui/src/pages/TransferPage.tsx

```typescript
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, CollectionInfo, ExportJob, ImportJob, PackageSummary, TuneOptions,
} from '../api/client'

function sizeLabel(bytes: number | null): string {
  if (bytes === null) return ''
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(0)} KB`
  return `${bytes} B`
}

const CONFLICT = [
  { id: 'abort', label: 'Abort', description: 'Fail if a collection of that name already exists. Nothing is changed.' },
  { id: 'rename', label: 'Rename', description: 'Import alongside the existing one, under a new name the API reports back.' },
  { id: 'replace', label: 'Replace', description: 'Overwrite the existing collection — but only after this package is proven to import cleanly, so a failure leaves it intact.' },
]

export default function TransferPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [tune, setTune] = useState<TuneOptions | null>(null)
  const [includeModels, setIncludeModels] = useState(false)
  const [exportJob, setExportJob] = useState<ExportJob | null>(null)
  const [exportError, setExportError] = useState('')

  const [packages, setPackages] = useState<PackageSummary[]>([])
  const [filename, setFilename] = useState('')
  const [onConflict, setOnConflict] = useState('abort')
  const [importJob, setImportJob] = useState<ImportJob | null>(null)
  const [importError, setImportError] = useState('')

  // Polling handles are kept so a job that finishes, or a page that unmounts,
  // does not leave a timer running against a job nobody is watching.
  const exportTimer = useRef<number | null>(null)
  const importTimer = useRef<number | null>(null)

  const loadPackages = useCallback(() => {
    api.getPackages().then(r => setPackages(r.packages)).catch(() => {})
  }, [])

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(c => c || r.collections[0].name)
    }).catch(() => {})
    loadPackages()
    return () => {
      if (exportTimer.current) window.clearTimeout(exportTimer.current)
      if (importTimer.current) window.clearTimeout(importTimer.current)
    }
  }, [loadPackages])

  // Fidelity is shown before the export runs, so the choice is informed rather
  // than discovered afterwards in the manifest.
  useEffect(() => {
    if (!collection) { setTune(null); return }
    api.getTuneOptions(collection).then(setTune).catch(() => setTune(null))
  }, [collection])

  function pollExport(jobId: string) {
    api.getExportJob(jobId).then(job => {
      setExportJob(job)
      if (job.status === 'completed' || job.status === 'failed') {
        loadPackages()
      } else {
        exportTimer.current = window.setTimeout(() => pollExport(jobId), 2000)
      }
    }).catch((e: unknown) => setExportError(e instanceof Error ? e.message : String(e)))
  }

  function pollImport(jobId: string) {
    api.getImportJob(jobId).then(job => {
      setImportJob(job)
      if (job.status !== 'completed' && job.status !== 'failed') {
        importTimer.current = window.setTimeout(() => pollImport(jobId), 2000)
      } else if (job.status === 'completed') {
        api.getCollections().then(r => setCollections(r.collections)).catch(() => {})
      }
    }).catch((e: unknown) => setImportError(e instanceof Error ? e.message : String(e)))
  }

  async function startExport() {
    setExportError(''); setExportJob(null)
    try {
      const r = await api.startExport({ collection, include_models: includeModels })
      pollExport(r.job_id)
    } catch (e: unknown) {
      setExportError(e instanceof Error ? e.message : String(e))
    }
  }

  async function startImport() {
    setImportError(''); setImportJob(null)
    try {
      const r = await api.startImport({ filename, on_conflict: onConflict })
      pollImport(r.job_id)
    } catch (e: unknown) {
      setImportError(e instanceof Error ? e.message : String(e))
    }
  }

  const exportBusy = exportJob !== null && exportJob.status !== 'completed' && exportJob.status !== 'failed'
  const importBusy = importJob !== null && importJob.status !== 'completed' && importJob.status !== 'failed'

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-baseline justify-between mb-2">
        <h1 className="text-2xl font-bold">Transfer</h1>
        <Link to="/help/transfer" className="text-sm text-blue-600 hover:underline">
          How export and import work →
        </Link>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        Packages are written to and read from <code>./exports</code> in the project directory.
      </p>

      {/* ── Export ─────────────────────────────────────────────────────── */}
      <section className="bg-white border rounded p-4 mb-6">
        <h2 className="font-medium mb-3">Export a collection</h2>

        <label className="block text-sm font-medium mb-1">Collection</label>
        <select
          value={collection}
          onChange={e => setCollection(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm mb-2"
        >
          {collections.length === 0 && <option value="">No collections yet</option>}
          {collections.map(c => (
            <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>
          ))}
        </select>

        {tune && (
          <p className="text-xs mb-3">
            <span className={tune.fidelity === 'with-sources' ? 'text-green-700' : 'text-amber-700'}>
              Fidelity: <strong>{tune.fidelity}</strong>
            </span>
            {' — '}
            {tune.fidelity === 'with-sources'
              ? `${tune.source_document_count} original document(s) will travel with the package.`
              : 'No original documents were retained, so the package cannot be re-chunked after import.'}
          </p>
        )}

        <label className="flex items-start gap-2 text-sm mb-3">
          <input type="checkbox" checked={includeModels} onChange={e => setIncludeModels(e.target.checked)} className="mt-1" />
          <span>
            Include the models
            <span className="block text-xs text-gray-500">
              Bundles the embedding model and the LLM, taking the package to roughly 2.3 GB.
              Needed only when the target machine has never pulled them.
            </span>
          </span>
        </label>

        <button
          onClick={startExport}
          disabled={!collection || exportBusy}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {exportBusy ? 'Exporting…' : 'Export'}
        </button>

        {exportError && <p className="text-red-600 text-sm mt-3">{exportError}</p>}
        {exportJob && (
          <div className="mt-3 text-sm">
            <p className="text-gray-600">
              {exportJob.status === 'completed' ? 'Done.' :
               exportJob.status === 'failed' ? 'Failed.' :
               `Writing… ${exportJob.chunks_written} chunks so far.`}
            </p>
            {exportJob.status === 'completed' && exportJob.filename && (
              <p className="mt-1">
                <code className="text-xs break-all">{exportJob.filename}</code>
                <span className="text-gray-500 text-xs">
                  {' '}({sizeLabel(exportJob.size_bytes)}, {exportJob.fidelity}
                  {exportJob.models_bundled ? ', models included' : ''}
                  {exportJob.retrieve_script ? ', with retrieve.py' : ', no retrieve.py'})
                </span>
              </p>
            )}
            {exportJob.status === 'failed' && <p className="text-red-600">{exportJob.error}</p>}
            {exportJob.warnings.map((w, i) => (
              <p key={i} className="text-amber-700 text-xs mt-1">{w}</p>
            ))}
          </div>
        )}
      </section>

      {/* ── Import ─────────────────────────────────────────────────────── */}
      <section className="bg-white border rounded p-4">
        <h2 className="font-medium mb-3">Import a package</h2>

        <div className="flex items-center justify-between mb-1">
          <label className="block text-sm font-medium">Package in ./exports</label>
          <button onClick={loadPackages} className="text-xs text-blue-600 hover:underline">Refresh</button>
        </div>
        <select
          value={filename}
          onChange={e => setFilename(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm mb-3"
        >
          <option value="">Select a package…</option>
          {packages.map(p => (
            <option key={p.filename} value={p.filename}>
              {p.filename}
              {p.readable
                ? ` — ${p.collection}, ${p.chunk_count} chunks, ${p.fidelity}, ${sizeLabel(p.size_bytes)}`
                : ' — unreadable'}
            </option>
          ))}
        </select>
        {packages.length === 0 && (
          <p className="text-xs text-gray-500 mb-3">
            Nothing in <code>./exports</code> yet. Copy a package there and press Refresh.
          </p>
        )}

        <label className="block text-sm font-medium mb-2">If the collection already exists</label>
        <div className="space-y-2 mb-3">
          {CONFLICT.map(c => (
            <label key={c.id} className={`flex gap-3 p-3 border rounded cursor-pointer ${onConflict === c.id ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
              <input type="radio" name="conflict" value={c.id} checked={onConflict === c.id}
                     onChange={() => setOnConflict(c.id)} className="mt-1" />
              <div>
                <div className="text-sm font-medium">{c.label}</div>
                <div className="text-xs text-gray-500">{c.description}</div>
              </div>
            </label>
          ))}
        </div>

        <button
          onClick={startImport}
          disabled={!filename || importBusy}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {importBusy ? 'Importing…' : 'Import'}
        </button>

        {importError && <p className="text-red-600 text-sm mt-3">{importError}</p>}
        {importJob && (
          <div className="mt-3 text-sm">
            <p className="text-gray-600">
              {importJob.status === 'completed' ? 'Done.' :
               importJob.status === 'failed' ? 'Failed.' :
               `Importing… ${importJob.chunks_written} chunks so far.`}
            </p>
            {importJob.status === 'completed' && (
              <p className="mt-1">
                Imported as <strong>{importJob.collection}</strong>
                {importJob.renamed && <span className="text-gray-500"> (renamed from {importJob.original_collection})</span>}
                <span className="text-gray-500"> — {importJob.chunks_written} chunks, {importJob.fidelity}</span>
              </p>
            )}
            {importJob.status === 'failed' && (
              <p className="text-red-600 mt-1">
                {importJob.error_code && <code className="text-xs mr-2">{importJob.error_code}</code>}
                {importJob.error}
              </p>
            )}
            {importJob.notes.map((n, i) => (
              <p key={i} className="text-gray-500 text-xs mt-1">{n}</p>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
```

### ui/src/pages/HelpTransferPage.tsx

```typescript
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '../api/client'

export default function HelpTransferPage() {
  const [markdown, setMarkdown] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.getTransferHelp()
      .then(r => setMarkdown(r.markdown))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-baseline justify-between mb-4">
        <h1 className="text-2xl font-bold">Export and Import</h1>
        <Link to="/transfer" className="text-sm text-blue-600 hover:underline">Go to Transfer →</Link>
      </div>
      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {error && (
        <p className="text-sm text-red-600">
          Could not load the help content ({error}). The API may be starting up.
        </p>
      )}
      {markdown && (
        // Served by the API, rendered from the same templates as the README
        // inside every package, so this page cannot drift from what ships.
        <article className="prose prose-sm max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
        </article>
      )}
    </div>
  )
}
```

### ui/src/pages/HealthPage.tsx

```typescript
import { useState, useEffect } from 'react'
import { api, HealthResult, MetricsResult } from '../api/client'
import LatencyCharts from '../components/LatencyCharts'

export default function HealthPage() {
  const [health, setHealth] = useState<HealthResult | null>(null)
  const [metrics, setMetrics] = useState<MetricsResult | null>(null)

  async function load() {
    try {
      const [h, m] = await Promise.all([api.getHealth(), api.getMetrics()])
      setHealth(h)
      setMetrics(m)
    } catch { /* ignore */ }
  }

  useEffect(() => {
    load()
    const interval = setInterval(load, 30000)
    return () => clearInterval(interval)
  }, [])

  function StatusBadge({ status }: { status: string }) {
    return (
      <span className={`inline-block w-2 h-2 rounded-full mr-2 ${status === 'ok' ? 'bg-green-500' : 'bg-red-500'}`} />
    )
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Health Dashboard</h1>
      {health && (
        <div className="grid grid-cols-3 gap-4 mb-8">
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.weaviate.status} />
              Weaviate
            </div>
            <div className="text-xs text-gray-500">{health.services.weaviate.latency_ms}ms</div>
          </div>
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.ollama.llm.status} />
              LLM ({health.services.ollama.llm.model})
            </div>
            <div className="text-xs text-gray-500">{health.services.ollama.llm.latency_ms}ms</div>
          </div>
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.ollama.embed.status} />
              Embed ({health.services.ollama.embed.model})
            </div>
            <div className="text-xs text-gray-500">{health.services.ollama.embed.latency_ms}ms</div>
          </div>
        </div>
      )}
      {metrics && metrics.total_records > 0 && (
        <div className="bg-white border rounded p-4">
          <h2 className="font-semibold mb-4">Latency Trends ({metrics.total_records} queries in ring buffer)</h2>
          <LatencyCharts data={metrics} />
        </div>
      )}
      {metrics && metrics.total_records === 0 && (
        <p className="text-gray-400 text-sm">No query data yet. Run some Q&A queries to populate charts.</p>
      )}
    </div>
  )
}
```

---

*End of Implementation — Version 1.0*

---

## 5. Verification Suite

Integration tests for the acceptance criteria in Section 10 of
`SPECIFICATIONS.md` and Section 13 of `RAG_EXPORT_SPECIFICATIONS.md`.
They require a running stack; `scripts/verify/README.md` explains why unit
tests would not have caught the defects this project actually produced.

### scripts/verify/README.md

````markdown
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
````

### scripts/verify/all.sh

```bash
#!/usr/bin/env bash
#
# Run every verification suite against a running stack.
#
#   bash scripts/verify/all.sh              # everything (~20 min, LLM-bound)
#   RAG_SKIP_SLOW=1 bash scripts/verify/all.sh   # skip LLM work (~3 min)
#   RAG_ALLOW_RESTART=1 bash scripts/verify/all.sh  # also restart the stack
#   bash scripts/verify/all.sh 02 04        # only the named suites
#
# Exits non-zero if any check fails, so it can gate a commit or a release.
#
# These are integration tests: they need the stack up, because the defects this
# project actually produced -- a missing parser dependency, a silent 200 where a
# 422 belonged, vectors that survive a vectorizer -- are all invisible to unit
# tests of the same code.
set -uo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"

API="${RAG_API:-http://localhost:8080/api}"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"

code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$API/health" 2>/dev/null)
if [ "$code" != "200" ]; then
  printf '\nNo healthy API at %s (HTTP %s).\n' "$API" "$code"
  printf 'Start the stack first:  docker compose up -d\n\n'
  exit 2
fi

printf '\nBuilding fixtures in %s\n' "$FIX"
rm -rf "$FIX"; python3 ./fixtures.py "$FIX" >/dev/null
export RAG_FIXTURES="$FIX"

ALL=(01_infrastructure 02_ingest 03_query 04_goldstandard 05_transfer 06_ui)
if [ "$#" -gt 0 ]; then
  SUITES=()
  for want in "$@"; do
    for s in "${ALL[@]}"; do
      case "$s" in "$want"*) SUITES+=("$s") ;; esac
    done
  done
else
  SUITES=("${ALL[@]}")
fi
[ "${#SUITES[@]}" -gt 0 ] || { printf 'No suite matched: %s\n' "$*"; exit 2; }

started=$(python3 -c "import time;print(time.time())")
declare -a RESULTS
overall=0
for suite in "${SUITES[@]}"; do
  printf '\n──────────────────────────────────────────────────────────────\n'
  printf '  %s\n' "$suite"
  printf '──────────────────────────────────────────────────────────────\n'
  if bash "./$suite.sh"; then
    RESULTS+=("  ok    $suite")
  else
    RESULTS+=("  FAIL  $suite")
    overall=1
  fi
done
elapsed=$(python3 -c "import time;print(int(time.time()-$started))")

printf '\n══════════════════════════════════════════════════════════════\n'
printf '  Summary  (%dm%02ds)\n' "$((elapsed/60))" "$((elapsed%60))"
printf '══════════════════════════════════════════════════════════════\n'
printf '%s\n' "${RESULTS[@]}"
if [ "$overall" -eq 0 ]; then
  printf '\n  All suites passed.\n\n'
else
  printf '\n  At least one suite failed.\n\n'
fi
exit "$overall"
```

### scripts/verify/lib.sh

```bash
#!/usr/bin/env bash
# Shared helpers for the verification suites.
#
# Sourced, never executed. Every suite expects a running stack and leaves the
# instance as it found it.
#
# A note on JSON and the shell: never round-trip an API response through `echo`.
# zsh and bash interpret backslash escapes differently, and an LLM-generated
# answer containing \n will be silently corrupted into invalid JSON. Pipe curl
# straight into python3, or capture to a file. This cost real debugging time
# more than once.

set -uo pipefail

API="${RAG_API:-http://localhost:8080/api}"
# Collections and packages created by the suites all carry this prefix so
# cleanup can find them without guessing.
PREFIX="${RAG_TEST_PREFIX:-Vfy}"
# Suites that need an LLM call are slow (20-60s each). Set RAG_SKIP_SLOW=1 for
# a structural-only run.
SKIP_SLOW="${RAG_SKIP_SLOW:-0}"

PASS=0; FAIL=0; SKIP=0
FAILED_NAMES=()

_c_pass=$'\033[32m'; _c_fail=$'\033[31m'; _c_skip=$'\033[33m'; _c_off=$'\033[0m'
[ -t 1 ] || { _c_pass=""; _c_fail=""; _c_skip=""; _c_off=""; }

section() { printf '\n  %s\n' "$1"; }

# check <name> <condition-exit-status> [detail]
check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS+1)); printf '    %sPASS%s  %s\n' "$_c_pass" "$_c_off" "$name"
  else
    FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    printf '    %sFAIL%s  %s%s\n' "$_c_fail" "$_c_off" "$name" "${detail:+  — $detail}"
  fi
}

# check_eq <name> <actual> <expected>
check_eq() {
  local name="$1" actual="$2" expected="$3"
  [ "$actual" = "$expected" ]
  check "$name" $? "got '$actual', wanted '$expected'"
}

skip() {
  SKIP=$((SKIP+1)); printf '    %sSKIP%s  %s%s\n' "$_c_skip" "$_c_off" "$1" "${2:+  — $2}"
}

# jq-free JSON field read: api_get <path> | jfield <expr>
# `expr` is python indexing against the parsed document, e.g. ['status']
jfield() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

api_get()  { curl -s -m 120 "$API$1"; }
api_code() { curl -s -o /dev/null -m 120 -w '%{http_code}' "$@"; }
api_post() { curl -s -m 600 -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }
api_post_code() { curl -s -o /dev/null -m 600 -w '%{http_code}' -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }

require_stack() {
  local code
  code=$(api_code "$API/health")
  if [ "$code" != "200" ]; then
    printf '\n  Cannot reach a healthy API at %s (HTTP %s).\n' "$API" "$code"
    printf '  Start the stack first:  docker compose up -d\n\n'
    exit 2
  fi
}

# wait_for_job <url-path> [timeout-seconds] — polls until status is terminal.
# Echoes the final status.
wait_for_job() {
  local path="$1" limit="${2:-600}" waited=0 status=""
  while [ "$waited" -lt "$limit" ]; do
    status=$(api_get "$path" | jfield "['status']")
    case "$status" in
      completed|failed|partial|cancelled) printf '%s' "$status"; return 0 ;;
    esac
    sleep 2; waited=$((waited+2))
  done
  printf 'timeout'; return 1
}

make_collection() {
  api_post "/collections" "{\"name\":\"$1\",\"index_type\":\"${2:-hnsw}\",\"distance_metric\":\"${3:-cosine}\",\"hnsw_config\":{\"efConstruction\":128,\"maxConnections\":64,\"ef\":64}}" >/dev/null
}

drop_collection() { curl -s -o /dev/null -m 120 -X DELETE "$API/collections/$1?confirm=true"; }

# Remove every collection and package this run created.
cleanup_prefixed() {
  local names
  names=$(api_get "/collections" | python3 -c "
import json,sys
for c in json.load(sys.stdin)['collections']:
    if c['name'].startswith('$PREFIX'): print(c['name'])" 2>/dev/null)
  for n in $names; do drop_collection "$n"; done
  rm -f "${REPO_ROOT:-.}"/exports/ragpkg-"$(echo "$PREFIX" | tr '[:upper:]' '[:lower:]')"*.tar.gz 2>/dev/null || true
}

summary() {
  printf '\n  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
  if [ "$FAIL" -gt 0 ]; then
    printf '  failed:\n'
    printf '    - %s\n' "${FAILED_NAMES[@]}"
    return 1
  fi
  return 0
}
```

### scripts/verify/fixtures.py

```python
#!/usr/bin/env python3
"""Write the test corpus used by the verification suites.

    python3 fixtures.py <output-dir>

Everything is generated from the standard library so the suites have no
dependencies of their own — the PDF and DOCX are written by hand rather than
pulled from a document library.
"""
from __future__ import annotations

import json
import pathlib
import random
import sys
import zipfile

PARAGRAPHS = [
    "Overtime Approval. Requests relating to overtime must be submitted in writing "
    "to the responsible manager, who reviews them within five working days.",
    "Approval for overtime is granted by the department head, except where the "
    "amount exceeds the delegated limit, in which case the finance director approves.",
    "Expense Reimbursement. Employees submit receipts within thirty days of the "
    "expense being incurred. Claims without receipts are refused.",
    "Remote Work. Staff may work remotely up to three days each week with written "
    "agreement from their line manager and the people team.",
    "Records of every decision are retained for seven years in the central archive, "
    "and are available to auditors on request.",
    "Travel Booking. Flights must be booked at least fourteen days in advance. "
    "Rail travel is preferred for journeys under four hours.",
]
ONE_LINER = PARAGRAPHS[0]


def _pdf(paragraphs: list[str], padding: int = 0) -> bytes:
    """A minimal single-page PDF. Hand-built to avoid a writer dependency.

    `padding` adds an unreferenced stream object of that many bytes. No page
    points at it, so parsers skip it: the file is large on the wire but carries
    only the text above, which keeps an upload-size test fast to ingest.
    """
    lines: list[str] = []
    for para in paragraphs:
        cur = ""
        for word in para.split():
            if len(cur) + len(word) + 1 > 82:
                lines.append(cur)
                cur = word
            else:
                cur = (cur + " " + word).strip()
        lines.extend([cur, ""])

    stream = b"BT /F1 11 Tf 54 740 Td 14 TL\n"
    for line in lines:
        safe = (line.encode("ascii", "replace")
                    .replace(b"(", b"").replace(b")", b"").replace(b"\\", b""))
        stream += b"(" + safe + b") Tj T*\n"
    stream += b"ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if padding:
        # Seeded so the fixture is byte-identical on every run. Random bytes
        # rather than zeros so nothing on the path can compress it away.
        filler = random.Random(21).randbytes(padding)
        objects.append(b"<< /Length " + str(padding).encode() + b" >>\nstream\n"
                       + filler + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


def _docx(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def write(target: pathlib.Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(PARAGRAPHS) + "\n"

    # One of each supported type. `.md` is here because it silently failed to
    # ingest until the `markdown` dependency was added.
    (target / "policies.txt").write_text(body)
    (target / "policies.md").write_text("# Policies\n\n" + body)
    (target / "policies.csv").write_text(
        "policy,detail\n" + "".join(f'p{i},"{p}"\n' for i, p in enumerate(PARAGRAPHS)))
    (target / "policies.json").write_text(json.dumps(
        [{"policy": f"p{i}", "detail": p} for i, p in enumerate(PARAGRAPHS)], indent=2))
    (target / "policies.pdf").write_bytes(_pdf(PARAGRAPHS))
    (target / "policies.docx").write_bytes(_docx(ONE_LINER))

    # Edge cases.
    # Two fragments under any sensible min_chunk_size, for the merge rule.
    (target / "tiny.txt").write_text("Short.\n\nAlso short.\n\n" + ONE_LINER + "\n")
    # Right extension, unparseable content — one bad file must not fail a batch.
    (target / "broken.pdf").write_bytes(b"%PDF-1.4\nnot a real pdf body\n%%EOF\n")
    # Over nginx's 1 MB default request-body limit, which once rejected every
    # real-world PDF with a 413 before the API saw it. See issue #21.
    (target / "large.pdf").write_bytes(_pdf(PARAGRAPHS, padding=3 * 1024 * 1024))
    # Unsupported types, which must be reported rather than dropped in silence.
    (target / "notes.xyz").write_text("unsupported\n")
    (target / "notes.rtf").write_text("also unsupported\n")

    # A ZIP mixing supported and unsupported members.
    with zipfile.ZipFile(target / "batch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("policies.txt", "policies.md", "policies.pdf",
                     "notes.xyz", "notes.rtf"):
            z.write(target / name, arcname=name)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: fixtures.py <output-dir>")
    out = pathlib.Path(sys.argv[1])
    write(out)
    for path in sorted(out.iterdir()):
        print(f"  {path.name:16s} {path.stat().st_size:7d} bytes")
```

### scripts/verify/validate_package.py

```python
"""Validate one export package against RAG_EXPORT_SPECIFICATIONS.md §4.

    python3 validate_package.py <path-to-ragpkg-*.tar.gz>

Exits non-zero if any clause fails. Checks the filename convention, that <id8>
really is the manifest digest, every listed digest, that nothing in the archive
is unlisted, the chunks.jsonl record shape, source content-addressing, and the
generated README and retrieve.py.
"""
import ast, hashlib, json, re, sys, tarfile, tempfile
from pathlib import Path

archive = Path(sys.argv[1])
fails = []
def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok: fails.append(name)

# E5 — filename (§4.1)
NAME_RE = re.compile(r"^ragpkg-([a-z0-9]+(?:-[a-z0-9]+)*)-(\d{8}T\d{6}Z)-([0-9a-f]{8})\.tar\.gz$")
m = NAME_RE.match(archive.name)
check("E5 filename matches the §4.1 pattern", m is not None, archive.name)
if not m:
    sys.exit(1)
slug, ts, id8 = m.groups()

with tempfile.TemporaryDirectory() as td:
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        tar.extractall(td)
    root = Path(td)
    tops = {n.split("/")[0] for n in names}
    check("archive expands to a single directory", len(tops) == 1, str(tops))
    check("that directory is the filename minus .tar.gz",
          tops == {archive.name[:-len(".tar.gz")]}, str(tops))
    pkg = root / archive.name[:-len(".tar.gz")]

    manifest = json.loads((pkg / "manifest.json").read_text())

    # §4.1 — id8 is the manifest digest prefix
    digest = hashlib.sha256((pkg / "manifest.json").read_bytes()).hexdigest()
    check("<id8> is the first 8 hex of the manifest digest",
          digest[:8] == id8, f"manifest={digest[:8]} filename={id8}")

    # §4.1 — slug is derived from the authoritative name
    name = manifest["collection"]["name"]
    expect = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    check("slug derives from manifest collection name", slug == expect,
          f"{name!r} -> {expect!r}, filename has {slug!r}")

    # E6 — every listed digest matches
    bad = []
    for rel, want in manifest["files"].items():
        f = pkg / rel
        if not f.is_file():
            bad.append(f"{rel}: listed but missing"); continue
        got = "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
        if got != want: bad.append(f"{rel}: digest mismatch")
    check(f"E6 all {len(manifest['files'])} listed digests verify", not bad, "; ".join(bad[:3]))

    # No file in the package is silently unlisted
    UNDIGESTED = {"manifest.json", "README.md", "retrieve.py"}
    on_disk = {str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.is_file()}
    unlisted = on_disk - set(manifest["files"]) - UNDIGESTED
    check("no file is unlisted and undigested", not unlisted, str(sorted(unlisted)))
    overlap = UNDIGESTED & set(manifest["files"])
    check("generated files are not in the digest map (would be circular)",
          not overlap, str(sorted(overlap)))

    # §4.2 — required layout
    for req in ("manifest.json", "collection.json", "chunks.jsonl",
                "retrieval_config.json", "README.md"):
        check(f"§4.2 contains {req}", (pkg / req).is_file())

    # §4.4 — manifest shape
    for key in ("package_format", "created_at", "produced_by", "collection",
                "embedding", "llm", "chunking", "fidelity", "models_bundled", "files"):
        check(f"§4.4 manifest has {key}", key in manifest)
    check("package_format is 1", manifest.get("package_format") == 1)
    check("created_at is UTC ISO-8601 with Z",
          bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", manifest["created_at"])),
          manifest["created_at"])
    check("embedding.dimensions is an int", isinstance(manifest["embedding"]["dimensions"], int),
          str(manifest["embedding"]))

    # §4.3 — fidelity
    fid = manifest["fidelity"]
    check("fidelity is one of the two spec values", fid in ("with-sources", "chunks-only"), fid)
    has_sources_dir = (pkg / "sources").is_dir()
    check("sources/ present iff fidelity is with-sources",
          has_sources_dir == (fid == "with-sources"), f"{fid}, dir={has_sources_dir}")

    # §4.5 — chunks.jsonl
    lines = (pkg / "chunks.jsonl").read_text().strip().split("\n")
    lines = [l for l in lines if l]
    check("chunk_count matches chunks.jsonl lines",
          len(lines) == manifest["collection"]["chunk_count"],
          f"{len(lines)} lines vs {manifest['collection']['chunk_count']}")
    EIGHT = {"content","source_file","source_type","chunk_index",
             "chunk_strategy","chunk_size","chunk_overlap","created_at"}
    probs = []
    for i, line in enumerate(lines):
        rec = json.loads(line)
        if set(rec) != {"id","vector","properties","source_sha256"}:
            probs.append(f"line {i+1}: keys {sorted(rec)}")
        elif set(rec["properties"]) != EIGHT:
            probs.append(f"line {i+1}: properties {sorted(rec['properties'])}")
        elif len(rec["vector"]) != manifest["embedding"]["dimensions"]:
            probs.append(f"line {i+1}: vector len {len(rec['vector'])}")
        elif not all(isinstance(v,(int,float)) for v in rec["vector"]):
            probs.append(f"line {i+1}: vector not all numbers")
    check("§4.5 every chunk has the 4 fields, 8 properties and a full vector",
          not probs, "; ".join(probs[:3]))

    if fid == "with-sources":
        idx = json.loads((pkg / "sources" / "index.json").read_text())
        stored = {p.name for p in (pkg / "sources").iterdir() if p.name != "index.json"}
        check("every indexed source document is present",
              set(idx["documents"]) == stored,
              f"index={len(idx['documents'])} files={len(stored)}")
        bad = [d for d in stored if hashlib.sha256((pkg/"sources"/d).read_bytes()).hexdigest() != d]
        check("source files are content-addressed correctly", not bad, str(bad[:2]))

    # Generated files
    readme = (pkg / "README.md").read_text()
    check("README has no unsubstituted placeholders",
          not re.search(r"@@[A-Z_0-9]+@@", readme),
          str(set(re.findall(r"@@[A-Z_0-9]+@@", readme))))
    check("E19 README states the collection name", name in readme)
    check("E19 README states the fidelity", fid in readme)
    check("E19 README carries the encryption warning", "not encrypted" in readme.lower())

    if manifest.get("retrieve_script"):
        rp = pkg / "retrieve.py"
        check("retrieve.py present when retrieve_script is true", rp.is_file())
        src = rp.read_text()
        check("retrieve.py has no unsubstituted placeholders",
              not re.search(r"@@[A-Z_0-9]+@@", src),
              str(set(re.findall(r"@@[A-Z_0-9]+@@", src))))
        try:
            ast.parse(src); ok = True; err = ""
        except SyntaxError as e:
            ok = False; err = str(e)
        check("retrieve.py is valid Python", ok, err)
        check("retrieve.py records its provenance (id8 + created_at)",
              id8 in src and manifest["created_at"] in src)
        imports = {n.split(".")[0] for node in ast.walk(ast.parse(src))
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for n in ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ""])}
        STDLIB = {"argparse","json","sys","urllib","os","re"}
        check("retrieve.py imports stdlib only", imports <= STDLIB, str(sorted(imports)))
    else:
        check("retrieve.py absent when retrieve_script is false",
              not (pkg / "retrieve.py").exists())

print(f"\n{'PACKAGE VALID' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
sys.exit(1 if fails else 0)
```

### scripts/verify/01_infrastructure.sh

```bash
#!/usr/bin/env bash
# SPECIFICATIONS.md §10.5 — Infrastructure
#
# The restart and first-run timing checks are disruptive, so they are opt-in.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
C="${PREFIX}Infra"

section "§10.5 Infrastructure"

# ── exactly one host-published port ──────────────────────────────────────────
bindings=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Ports}}') \
  | grep -o '0\.0\.0\.0:[0-9]*->[0-9]*/tcp' | sort -u)
count=$(printf '%s\n' "$bindings" | grep -c . )
check_eq "only one port is published to the host" "$count" "1"
printf '%s' "$bindings" | grep -q -- '->80/tcp'
check "that port maps to the proxy's port 80" $? "$bindings"

for svc in api weaviate; do
  published=$( (cd "$REPO_ROOT" && docker compose ps --format "{{.Service}}|{{.Ports}}") \
    | grep "^$svc|" | grep -c '0\.0\.0\.0' || true)
  check_eq "$svc publishes nothing to the host" "$published" "0"
done

# ── five services, all reporting healthy where a healthcheck exists ──────────
running=$( (cd "$REPO_ROOT" && docker compose ps --services --filter status=running) | grep -c .)
check_eq "five services are running" "$running" "5"
unhealthy=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Status}}') | grep -c 'unhealthy' || true)
check_eq "no service reports unhealthy" "$unhealthy" "0"

# ── health endpoint reports each dependency ──────────────────────────────────
api_get "/health" > /tmp/vfy_health.json
check_eq "health status is ok" "$(jfield "['status']" < /tmp/vfy_health.json)" "ok"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_health.json'))
s=d['services']
ok = (s['weaviate']['status']=='ok' and s['ollama']['llm']['status']=='ok'
      and s['ollama']['embed']['status']=='ok'
      and all(v['latency_ms'] >= 0 for v in (s['weaviate'], s['ollama']['llm'], s['ollama']['embed'])))
sys.exit(0 if ok else 1)"
check "per-service status and latency are reported" $?

# ── ingest config defaults ───────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
check_eq "a fresh collection reports is_default true" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":800,\"chunk_overlap\":150,\"similarity_threshold\":0.9,\"min_chunk_size\":80}" >/dev/null
api_get "/ingest/config/$C" > /tmp/vfy_cfg.json
check_eq "after saving, is_default is false" "$(jfield "['is_default']" < /tmp/vfy_cfg.json)" "False"
check_eq "the saved strategy is returned" "$(jfield "['chunking_strategy']" < /tmp/vfy_cfg.json)" "semantic"

# ── deleting a collection must take its configs with it ──────────────────────
# Spec §8 rule 1. This was not happening for the ingest config, so a recreated
# collection silently inherited chunking settings the user never chose.
drop_collection "$C"
make_collection "$C"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":900,\"chunk_overlap\":100,\"similarity_threshold\":0.9,\"min_chunk_size\":70}" >/dev/null
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":9,\"alpha\":0.4,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
drop_collection "$C"
make_collection "$C"
check_eq "a recreated collection does not inherit the ingest config" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
check_eq "a recreated collection does not inherit the retrieval config" \
  "$(api_get "/retrieval/config/$C" | jfield "['is_default']")" "True"

# ── persistence across a restart (opt-in: it stops the stack) ────────────────
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  api_get "/collections" > /tmp/vfy_before.json
  started=$(python3 -c "import time;print(time.time())")
  (cd "$REPO_ROOT" && docker compose down >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1)
  for _ in $(seq 1 120); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
  elapsed=$(python3 -c "import time;print(int(time.time()-$started))")
  [ "$elapsed" -le 120 ]
  check "restart reaches healthy within 120s" $? "took ${elapsed}s"
  api_get "/collections" > /tmp/vfy_after.json
  python3 -c "
import json,sys
b={c['name']:c for c in json.load(open('/tmp/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open('/tmp/vfy_after.json'))['collections']}
ok = set(b)==set(a) and all(b[k]['object_count']==a[k]['object_count'] for k in b)
sys.exit(0 if ok else 1)"
  check "Weaviate data survives down/up" $?
  python3 -c "
import json,sys
b={c['name']:c for c in json.load(open('/tmp/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open('/tmp/vfy_after.json'))['collections']}
sys.exit(0 if all(b[k]['created_at']==a[k]['created_at'] for k in b if k in a) else 1)"
  check "created_at is preserved across restart" $?
  check_eq "saved ingest config survives restart" \
    "$(api_get "/ingest/config/$C" | jfield "['chunking_strategy']")" "semantic"
else
  skip "restart, persistence and timing" "set RAG_ALLOW_RESTART=1 to include them"
fi

# ── startup sweeps leave a clean instance alone ──────────────────────────────
leftover=$( (cd "$REPO_ROOT" && docker compose exec -T api sh -c \
  'ls -d /app/uploads/import-* /app/uploads/rechunk-* 2>/dev/null | wc -l') | tr -d ' ')
check_eq "no abandoned extraction directories" "${leftover:-0}" "0"
staging=$(api_get "/collections" | python3 -c "
import json,sys
print(sum(1 for c in json.load(sys.stdin)['collections']
          if '__importing_' in c['name'] or '__tuning_' in c['name']))")
check_eq "no abandoned staging collections" "$staging" "0"

drop_collection "$C"
cleanup_prefixed
summary
```

### scripts/verify/02_ingest.sh

```bash
#!/usr/bin/env bash
# SPECIFICATIONS.md §10.1 — Ingest
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
# Regenerate when the newest fixture is missing, so a directory left by an
# older run does not hide a check behind a missing file.
[ -f "$FIX/large.pdf" ] || python3 ./fixtures.py "$FIX" >/dev/null

require_stack
C="${PREFIX}Ingest"

# Uploads a set of files and echoes the finished job document to a file.
ingest() {
  local collection="$1" strategy="$2" size="$3" minsize="$4"; shift 4
  local args=() f
  for f in "$@"; do args+=(-F "files=@$f"); done
  curl -s -m 600 -X POST "$API/ingest/upload" \
    -F "collection=$collection" -F "strategy=$strategy" -F "chunk_size=$size" \
    -F "chunk_overlap=60" -F "min_chunk_size=$minsize" "${args[@]}" > /tmp/vfy_job.json
  local job; job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_job.json'))['job_id'])" 2>/dev/null)
  [ -n "$job" ] || { printf '{}' > /tmp/vfy_job.json; return 1; }
  wait_for_job "/ingest/job/$job" 900 >/dev/null
  api_get "/ingest/job/$job" > /tmp/vfy_job.json
}

section "§10.1 Ingest"

# ── all six supported types ──────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 200 40 \
  "$FIX/policies.txt" "$FIX/policies.md" "$FIX/policies.csv" \
  "$FIX/policies.json" "$FIX/policies.pdf" "$FIX/policies.docx"
read -r status completed chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['chunks_stored'])")"
[ "$status" = completed ] && [ "$completed" = 6 ] && [ "$chunks" -gt 0 ]
check "all six file types ingest" $? "status=$status completed=$completed chunks=$chunks"

# Confirm the chunks actually landed, not merely that the job reported success.
stored=$(api_get "/collections" | python3 -c "
import json,sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
[ "$stored" -ge 6 ]
check "every type produced at least one chunk" $? "collection holds $stored chunks"

# ── ZIP batch, with unsupported members reported ─────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 200 40 "$FIX/batch.zip" "$FIX/notes.xyz"
read -r status completed skipped_n <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], len(d.get('skipped',[])))")"
[ "$status" = completed ] && [ "$completed" = 3 ]
check "ZIP extracts and processes supported members" $? "status=$status completed=$completed (want 3)"
[ "$skipped_n" -ge 3 ]
check "unsupported files are reported, not dropped silently" $? "$skipped_n skipped"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if all('unsupported type' in s for s in d.get('skipped',[])) else 1)"
check "each skip names the unsupported extension" $?

# ── upload size limit at the proxy ───────────────────────────────────────────
# nginx refuses request bodies over `client_max_body_size` (default 1 MB) with
# a 413 before the API sees them, so every real-world PDF failed through the
# UI while the few-KB fixtures here all passed. These go through $API, which is
# the proxy, on purpose. See issue #21.
size=$(python3 -c "import os;print(os.path.getsize('$FIX/large.pdf'))")
[ "$size" -gt 1048576 ]
check "the large fixture is over nginx's 1 MB default" $? "$size bytes"

drop_collection "$C"; make_collection "$C"
if ingest "$C" fixed 300 50 "$FIX/large.pdf"; then
  read -r status completed chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['chunks_stored'])")"
else
  status="rejected"; completed=0; chunks=0
fi
[ "$status" = completed ] && [ "$completed" = 1 ] && [ "$chunks" -gt 0 ]
check "an upload over 1 MB is accepted through the proxy and ingests" $? \
  "status=$status completed=$completed chunks=$chunks"

# Just over the 512 MB limit. A sparse file, so nothing is written to disk, and
# nginx answers from the Content-Length header without reading the body.
big_dir=$(mktemp -d)
python3 -c "open('$big_dir/oversize.txt','wb').truncate(513*1024*1024)"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X POST "$API/ingest/upload" \
  -F "collection=$C" -F "strategy=fixed" -F "files=@$big_dir/oversize.txt")
rm -rf "$big_dir"
check_eq "an upload over the 512 MB limit is refused with 413" "$code" "413"

# ── every chunking strategy against a PDF ────────────────────────────────────
for strategy in fixed overlap language context_aware semantic; do
  if [ "$SKIP_SLOW" = "1" ] && [ "$strategy" = "semantic" ]; then
    skip "strategy '$strategy' (loads a sentence-transformer)"; continue
  fi
  SC="${C}$(printf '%s' "$strategy" | tr -d '_')"
  drop_collection "$SC"; make_collection "$SC"
  ingest "$SC" "$strategy" 300 50 "$FIX/policies.pdf"
  read -r status chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json')); print(d['status'], d['chunks_stored'])")"
  [ "$status" = completed ] && [ "$chunks" -gt 0 ]
  check "strategy '$strategy' produces chunks from a PDF" $? "status=$status chunks=$chunks"
  drop_collection "$SC"
done

# ── min_chunk_size merging ───────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 60 100 "$FIX/tiny.txt"
under=$(curl -s -m 60 -X POST "$API/query" -H 'Content-Type: application/json' \
  -d "{\"question\":\"short\",\"collection\":\"$C\",\"retrieval_mode\":\"flat\",\"top_k\":20,\"include_citations\":true,\"response_format\":\"end_user\"}" \
  2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('?'); raise SystemExit
print(sum(1 for c in (d.get('citations') or []) if len(c['excerpt'].strip()) < 60))" 2>/dev/null)
python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json')); import sys
sys.exit(0 if d['chunks_stored'] == 1 else 1)"
check "chunks below min_chunk_size are merged" $? "stored $(python3 -c "import json;print(json.load(open('/tmp/vfy_job.json'))['chunks_stored'])") chunk(s), wanted 1"

# ── one bad file must not fail the batch ─────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 300 50 "$FIX/broken.pdf" "$FIX/policies.txt" "$FIX/tiny.txt"
read -r status completed failed errs <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['files_failed'], len(d['errors']))")"
[ "$status" = partial ] && [ "$completed" = 2 ] && [ "$failed" = 1 ] && [ "$errs" -ge 1 ]
check "a parser failure does not stop the other files" $? \
  "status=$status completed=$completed failed=$failed errors=$errs"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if any('broken.pdf' in e for e in d['errors']) else 1)"
check "the failure names the offending file" $?

# ── job status transitions ───────────────────────────────────────────────────
# `queued` is only observable when the executor is saturated, so this asserts
# the terminal transition and that the POST reports the initial state.
drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=300" -F "min_chunk_size=50" -F "files=@$FIX/policies.txt" > /tmp/vfy_start.json
initial=$(python3 -c "import json;print(json.load(open('/tmp/vfy_start.json'))['status'])")
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_start.json'))['job_id'])")
check_eq "a new job starts as 'queued'" "$initial" "queued"
final=$(wait_for_job "/ingest/job/$job" 900)
check_eq "job reaches 'completed'" "$final" "completed"

drop_collection "$C"
cleanup_prefixed
summary
```

### scripts/verify/03_query.sh

```bash
#!/usr/bin/env bash
# SPECIFICATIONS.md §10.2 — Query
cd "$(dirname "$0")" && . ./lib.sh
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}Query"

section "§10.2 Query"

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_q.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_q.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null

ask() {  # ask <mode> <format> <citations> -> writes /tmp/vfy_ans.json
  api_post "/query" "{\"question\":\"who approves overtime?\",\"collection\":\"$C\",\"retrieval_mode\":\"$1\",\"top_k\":3,\"alpha\":0.5,\"include_citations\":$3,\"response_format\":\"$2\"}" > /tmp/vfy_ans.json
}

if [ "$SKIP_SLOW" = "1" ]; then
  skip "§10.2 entirely" "every check needs an LLM call"
  summary; exit $?
fi

# ── a question returns an answer, with usable latencies ──────────────────────
ask hnsw end_user true
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if d.get('answer','').strip() else 1)"
check "a question returns a non-empty answer" $?
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if d.get('retrieval_latency_ms',0) > 0 and d.get('llm_latency_ms',0) > 0 else 1)"
check "latency fields present and non-zero" $? \
  "$(python3 -c "import json;d=json.load(open('/tmp/vfy_ans.json'));print(d.get('retrieval_latency_ms'),d.get('llm_latency_ms'))")"

# ── citations are shaped correctly ───────────────────────────────────────────
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
c = d.get('citations') or []
ok = bool(c) and all(
    isinstance(x.get('source_file'), str) and x['source_file']
    and isinstance(x.get('score'), (int, float))
    and isinstance(x.get('chunk_index'), int)
    and isinstance(x.get('excerpt'), str) for x in c)
sys.exit(0 if ok else 1)"
check "citations carry source_file, chunk_index, score and excerpt" $?

ask hnsw end_user false
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if not d.get('citations') else 1)"
check "citations are withheld when not requested" $?

# ── all four retrieval modes ─────────────────────────────────────────────────
for mode in hnsw flat hybrid semantic; do
  ask "$mode" end_user true
  python3 -c "
import json,sys
try: d=json.load(open('/tmp/vfy_ans.json'))
except Exception: sys.exit(1)
sys.exit(0 if 'error' not in d and d.get('chunks_retrieved',0) > 0 and d.get('answer','').strip() else 1)"
  check "retrieval mode '$mode' returns results" $?
done

# ── end_user is shorter than engineer ────────────────────────────────────────
# The prompts instruct plain language but never brevity, so a single pair is
# noise. Compare across trials and require a clear majority.
trials="${RAG_FORMAT_TRIALS:-3}"
eu_total=0; en_total=0; eu_wins=0
for _ in $(seq 1 "$trials"); do
  ask flat end_user false;  eu=$(python3 -c "import json;print(len(json.load(open('/tmp/vfy_ans.json'))['answer']))")
  ask flat engineer false;  en=$(python3 -c "import json;print(len(json.load(open('/tmp/vfy_ans.json'))['answer']))")
  eu_total=$((eu_total+eu)); en_total=$((en_total+en))
  [ "$eu" -lt "$en" ] && eu_wins=$((eu_wins+1))
done
[ "$eu_total" -lt "$en_total" ]
check "end_user answers are shorter than engineer on average" $? \
  "mean end_user=$((eu_total/trials)) engineer=$((en_total/trials)); end_user shorter in $eu_wins/$trials trials"

drop_collection "$C"
cleanup_prefixed
summary
```

### scripts/verify/04_goldstandard.sh

```bash
#!/usr/bin/env bash
# SPECIFICATIONS.md §10.3 — Gold Standard
#
# Every check here corresponds to a defect that was live in the codebase:
# lost pairs, counters that reported attempts as successes, a 500 where a 409
# belonged, and a PATCH that silently rewrote approved content.
cd "$(dirname "$0")" && . ./lib.sh
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}Gold"

section "§10.3 Gold Standard"

if [ "$SKIP_SLOW" = "1" ]; then
  skip "§10.3 entirely" "every check needs LLM generation"
  summary; exit $?
fi

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_g.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_g.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null

SAMPLE="${RAG_GS_SAMPLE:-3}"
api_post "/goldstandard/generate" "{\"collection\":\"$C\",\"sample_size\":$SAMPLE}" > /tmp/vfy_gen.json
SID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_gen.json'))['session_id'])")

# ── 409 while still generating ───────────────────────────────────────────────
# Regenerating mid-flight raced with the generation loop and returned 500.
overlapped=0
for _ in $(seq 1 60); do
  read -r st n <<<"$(api_get "/goldstandard/session/$SID" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d['status'], len(d['pairs']))")"
  if [ "$st" = generating ] && [ "$n" -ge 1 ]; then
    pid=$(api_get "/goldstandard/session/$SID" | jfield "['pairs'][0]['pair_id']")
    code=$(api_post_code "/goldstandard/regenerate" "{\"session_id\":\"$SID\",\"pair_id\":\"$pid\"}")
    check_eq "regenerate during generation returns 409" "$code" "409"
    overlapped=1; break
  fi
  [ "$st" != generating ] && break
  sleep 1
done
[ "$overlapped" = 1 ] || skip "409-while-generating" "generation finished before a regenerate could overlap"

wait_for_job "/goldstandard/session/$SID" 1800 >/dev/null
api_get "/goldstandard/session/$SID" > /tmp/vfy_sess.json

# ── every requested pair exists, and the counters agree ──────────────────────
read -r total attempted completed failed actual <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_sess.json'))
print(d['pairs_total'], d.get('pairs_attempted','?'), d['pairs_completed'],
      d.get('pairs_failed','?'), len(d['pairs']))")"
[ "$completed" = "$total" ] && [ "$actual" = "$total" ]
check "generate returns sample_size pairs" $? \
  "total=$total completed=$completed actual=$actual failed=$failed"
[ "$completed" = "$actual" ]
check "pairs_completed matches the pairs that exist" $? \
  "completed=$completed actual=$actual (this counted attempts before)"
[ "$attempted" = "$total" ]
check "pairs_attempted reaches the total so progress can finish" $? "attempted=$attempted/$total"

python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_sess.json'))
need = ('question','answer','ground_truth','contexts')
sys.exit(0 if d['pairs'] and all(all(p.get(f) for f in need) for p in d['pairs']) else 1)"
check "every pair has question, answer, ground_truth and contexts" $?

# ── the PATCH audit rule ─────────────────────────────────────────────────────
PID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][0]['pair_id'])")
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"question":"rewritten without declaring an edit"}')
check_eq "editing content without status='edited' is refused" "$code" "422"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"edited","question":"a properly declared edit"}')
check_eq "editing content with status='edited' is accepted" "$code" "200"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"approved"}')
check_eq "a status-only change is accepted" "$code" "200"

# ── regenerate replaces exactly one pair ─────────────────────────────────────
if [ "$actual" -ge 2 ]; then
  TARGET=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][1]['pair_id'])")
  api_post "/goldstandard/regenerate" "{\"session_id\":\"$SID\",\"pair_id\":\"$TARGET\"}" > /tmp/vfy_regen.json
  api_get "/goldstandard/session/$SID" > /tmp/vfy_after.json
  python3 - "$TARGET" <<'ENDPY'
import json, sys
target = sys.argv[1]
before = {p['pair_id']: p for p in json.load(open('/tmp/vfy_sess.json'))['pairs']}
after = {p['pair_id']: p for p in json.load(open('/tmp/vfy_after.json'))['pairs']}
changed = [k for k in before if k in after and before[k] != after[k]]
# The first pair was edited above, so it is expected to differ too.
unexpected = [k for k in changed if k != target and k != list(before)[0]]
sys.exit(0 if set(before) == set(after) and target in changed and not unexpected else 1)
ENDPY
  check "regenerate replaces only the targeted pair, ids stable" $?
else
  skip "regenerate-replaces-one" "needs at least two pairs"
fi

# ── export filtering, schema and filename ────────────────────────────────────
python3 - "$SID" "$API" <<'ENDPY'
import json, sys, urllib.request
sid, api = sys.argv[1], sys.argv[2]
pairs = json.load(urllib.request.urlopen(f"{api}/goldstandard/session/{sid}"))['pairs']
plan = ["approved", "edited", "rejected", "pending", "approved"]
for pair, status in zip(pairs, plan):
    body = {"status": status}
    if status == "edited":
        body["question"] = "edited for export"
    req = urllib.request.Request(
        f"{api}/goldstandard/session/{sid}/pair/{pair['pair_id']}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)
ENDPY
api_post "/goldstandard/save" "{\"session_id\":\"$SID\"}" > /tmp/vfy_save.json
SAVE_INFO=$(python3 - "$C" <<'ENDPY'
import json, re, sys
collection = sys.argv[1]
d = json.load(open('/tmp/vfy_save.json'))
kept = json.load(open('/tmp/vfy_sess.json'))['pairs']
expected = sum(1 for p, s in zip(kept, ["approved","edited","rejected","pending","approved"])
               if s in ("approved", "edited"))
print(d['pairs_saved'], d['pairs_excluded'], expected, d['filename'],
      1 if re.match(rf'^{collection}_\d{{8}}_\d{{6}}\.json$', d['filename']) else 0)
ENDPY
)
read -r saved excluded expected fname name_ok <<<"$SAVE_INFO"
[ "$saved" = "$expected" ]
check "export keeps only approved and edited pairs" $? "saved=$saved expected=$expected excluded=$excluded"
[ "$((saved + excluded))" = "$actual" ]
check "saved + excluded accounts for every pair" $? "$saved + $excluded vs $actual"
check_eq "default filename is {collection}_{YYYYMMDD_HHMMSS}.json" "$name_ok" "1"

curl -s -m 120 "$API/goldstandard/download/$fname" -o /tmp/vfy_export.json
python3 -c "
import json,sys
d = json.load(open('/tmp/vfy_export.json'))
RAGAS = {'question','answer','contexts','ground_truth'}
ok = isinstance(d, list) and d and all(
    RAGAS <= set(r) and isinstance(r['contexts'], list) and r['contexts']
    and all(isinstance(x,str) and x for x in r['contexts'])
    and all(isinstance(r[k],str) and r[k].strip() for k in ('question','answer','ground_truth'))
    for r in d)
sys.exit(0 if ok else 1)"
check "exported file is valid JSON in the RAGAS schema" $?

code=$(api_code "$API/goldstandard/download/definitely_not_here.json")
check_eq "download of an unknown filename returns 404" "$code" "404"

# ── sessions survive a restart ───────────────────────────────────────────────
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  (cd "$(git rev-parse --show-toplevel 2>/dev/null || echo ../..)" && docker compose restart api >/dev/null 2>&1)
  for _ in $(seq 1 60); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 3; done
  api_get "/goldstandard/session/$SID" > /tmp/vfy_post.json
  python3 -c "
import json,sys
a=json.load(open('/tmp/vfy_after.json')); b=json.load(open('/tmp/vfy_post.json'))
sys.exit(0 if [p['pair_id'] for p in a['pairs']] == [p['pair_id'] for p in b['pairs']] else 1)"
  check "sessions survive an API restart" $?
else
  skip "session survives a restart" "set RAG_ALLOW_RESTART=1 to include it"
fi

drop_collection "$C"
cleanup_prefixed
summary
```

### scripts/verify/05_transfer.sh

```bash
#!/usr/bin/env bash
# RAG_EXPORT_SPECIFICATIONS.md §13 — export, import and tuning (E5-E20)
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
bash ./14_reindex.sh
check "exact-record reindex acceptance suite" $?
C="${PREFIX}Transfer"
EXPORTS="$REPO_ROOT/exports"

section "Export, import and tuning"

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_t.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_t.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null
chunks_before=$(api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")

# Retrieval settings must exist for the package to carry retrieve.py.
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null

# ── export ───────────────────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_exp.json
ejob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp.json'))['job_id'])")
estatus=$(wait_for_job "/export/job/$ejob" 1800)
check_eq "export completes" "$estatus" "completed"
api_get "/export/job/$ejob" > /tmp/vfy_expjob.json
read -r PKG fidelity script <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_expjob.json'))
print(d['filename'], d['fidelity'], d['retrieve_script'])")"
check_eq "a collection with retained sources exports with-sources" "$fidelity" "with-sources"
check_eq "a tuned collection ships retrieve.py" "$script" "True"

python3 ./validate_package.py "$EXPORTS/$PKG" > /tmp/vfy_val.txt 2>&1
check "package satisfies every §4 clause" $? "$(tail -2 /tmp/vfy_val.txt | head -1)"

# ── corruption is detected ───────────────────────────────────────────────────
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, shutil, subprocess, sys, tarfile, tempfile
src = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work)
    root = next(p for p in work.iterdir() if p.is_dir())
    chunks = root / "chunks.jsonl"
    chunks.write_bytes(chunks.read_bytes()[: len(chunks.read_bytes()) // 2])
    out = src.parent / (src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=root.name)
ENDPY
CORRUPT=$(python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, sys
src = pathlib.Path(sys.argv[1])
print(src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
ENDPY
)
api_post "/import" "{\"filename\":\"$CORRUPT\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "a truncated package is refused as PACKAGE_CORRUPT" "$code" "PACKAGE_CORRUPT"
api_get "/import/job/$ijob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if 'chunks.jsonl' in (d.get('error') or '') else 1)"
check "the corruption error names the offending file" $?
rm -f "$EXPORTS/$CORRUPT"

# ── conflict handling ────────────────────────────────────────────────────────
api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "abort refuses an existing collection" "$code" "COLLECTION_EXISTS"

api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"rename\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
istatus=$(wait_for_job "/import/job/$ijob" 1800)
api_get "/import/job/$ijob" > /tmp/vfy_impjob.json
read -r istat iname irenamed iwritten <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_impjob.json'))
print(d['status'], d['collection'], d['renamed'], d['chunks_written'])")"
check_eq "rename imports alongside the original" "$istat" "completed"
[ "$irenamed" = "True" ] && [ "$iname" != "$C" ]
check "the renamed collection has a new name" $? "imported as $iname"
check_eq "every chunk is imported" "$iwritten" "$chunks_before"

# ── import is lossless ───────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$iname\"}" > /tmp/vfy_exp2.json
ejob2=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp2.json'))['job_id'])")
wait_for_job "/export/job/$ejob2" 1800 >/dev/null
PKG2=$(api_get "/export/job/$ejob2" | jfield "['filename']")
python3 - "$EXPORTS/$PKG" "$EXPORTS/$PKG2" <<'ENDPY'
import json, sys, tarfile, tempfile, pathlib
def chunks(path):
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(path) as t:
            t.extractall(td)
        root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
        return {r["id"]: r for r in
                (json.loads(l) for l in (root / "chunks.jsonl").read_text().splitlines() if l.strip())}
a, b = chunks(sys.argv[1]), chunks(sys.argv[2])
same = set(a) == set(b) and all(a[k]["vector"] == b[k]["vector"]
                                and a[k]["properties"] == b[k]["properties"] for k in a)
sys.exit(0 if same else 1)
ENDPY
check "re-export after import is byte-identical (uuids, vectors, properties)" $?
rm -f "$EXPORTS/$PKG2"
drop_collection "$iname"

# ── tuning ───────────────────────────────────────────────────────────────────
api_get "/tune/$C" > /tmp/vfy_tune.json
check_eq "tune options report with-sources" "$(jfield "['fidelity']" < /tmp/vfy_tune.json)" "with-sources"
check_eq "re-chunking is offered" "$(jfield "['can_rechunk']" < /tmp/vfy_tune.json)" "True"

api_post "/tune/reindex" "{\"collection\":\"$C\",\"index_type\":\"flat\",\"distance_metric\":\"cosine\"}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-index completes" "$tstatus" "completed"
api_get "/tune/job/$tjob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if any('unchanged' in n for n in d['notes']) else 1)"
check "re-index leaves gold-standard sessions alone" $?
after_index=$(api_get "/collections" | python3 -c "
import json,sys; print([c['index_type'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
check_eq "the index type actually changed" "$after_index" "flat"

api_post "/tune/rechunk" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-chunk completes" "$tstatus" "completed"
after_chunks=$(api_get "/tune/job/$tjob" | jfield "['chunks_written']")
[ "$after_chunks" -gt "$chunks_before" ]
check "re-chunking with a smaller size yields more chunks" $? "$chunks_before -> $after_chunks"

# ── chunks-only refuses re-chunking ──────────────────────────────────────────
SRCLESS="${PREFIX}Chunksonly"
drop_collection "$SRCLESS"; make_collection "$SRCLESS"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$SRCLESS" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_s.json
sjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_s.json'))['job_id'])")
wait_for_job "/ingest/job/$sjob" 900 >/dev/null
(cd "$REPO_ROOT" && docker compose exec -T api sh -c "rm -rf /app/sources/$SRCLESS") >/dev/null 2>&1
check_eq "a source-less collection reports chunks-only" \
  "$(api_get "/tune/$SRCLESS" | jfield "['fidelity']")" "chunks-only"
api_post "/tune/rechunk" "{\"collection\":\"$SRCLESS\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-chunking a chunks-only collection is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
api_post "/tune/reembed" "{\"collection\":\"$SRCLESS\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-embedding a chunks-only collection with new chunking is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
drop_collection "$SRCLESS"

# ── the help page and the package README share a source ──────────────────────
api_get "/help/transfer" > /tmp/vfy_help.json
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import json, pathlib, sys, tarfile, tempfile
help_md = json.load(open('/tmp/vfy_help.json'))['markdown']
partials = pathlib.Path('../../api/templates/partials')
with tempfile.TemporaryDirectory() as td:
    with tarfile.open(sys.argv[1]) as t:
        t.extractall(td)
    root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
    readme = (root / "README.md").read_text()
missing = []
for part in sorted(partials.glob("*.md")):
    probe = max(part.read_text().split("\n"), key=len).strip()
    if "@@" in probe:
        probe = probe.split("@@")[0].strip()
    if not probe:
        continue
    if probe not in help_md:
        missing.append(f"{part.stem}: absent from help page")
    # retrieve_usage only appears in a README when that package ships a script
    elif probe not in readme and part.stem != "retrieve_usage":
        missing.append(f"{part.stem}: absent from package README")
sys.exit(0 if not missing else 1)
ENDPY
check "help page and package README render from the same partials" $?
python3 -c "
import json,re,sys
m=json.load(open('/tmp/vfy_help.json'))['markdown']
sys.exit(0 if not re.search(r'@@[A-Z_0-9]+@@', m) else 1)"
check "the help page has no unsubstituted placeholders" $?

rm -f "$EXPORTS/$PKG"
drop_collection "$C"
cleanup_prefixed
summary
```

### scripts/verify/06_ui.sh

```bash
#!/usr/bin/env bash
# SPECIFICATIONS.md §10.4 — Web UI, driven by a real headless browser.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

IMAGE="rag-verify-browser:latest"
NETWORK="${RAG_NETWORK:-}"
if [ -z "$NETWORK" ]; then
  NETWORK=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Name}}' | head -1) )
  NETWORK=$(docker inspect "$NETWORK" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null)
fi
if [ -z "$NETWORK" ]; then
  printf '    could not determine the compose network; set RAG_NETWORK\n'
  exit 2
fi

# Build once; it is cached thereafter.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  printf '    building the browser image (first run only)...\n'
  docker build -q -t "$IMAGE" ./browser >/dev/null || { printf '    image build failed\n'; exit 2; }
fi

# A collection must exist for the delete-confirmation check to have a target.
C="${PREFIX}Ui"
drop_collection "$C"; make_collection "$C"

docker run --rm --network "$NETWORK" \
  -e RAG_UI_BASE="${RAG_UI_BASE:-http://proxy}" \
  -e RAG_SKIP_SLOW="$SKIP_SLOW" \
  -v "$PWD/browser":/w:ro -w /w "$IMAGE" node ui_criteria.js
rc=$?

drop_collection "$C"
cleanup_prefixed
exit $rc
```

### scripts/verify/browser/Dockerfile

```dockerfile
# Headless Chromium for the UI checks.
#
# jsdom cannot run this UI: Vite emits <script type="module">, which jsdom will
# not execute, so the page renders as an empty root and every assertion passes
# vacuously. A real browser is the only way these checks mean anything.
FROM node:20-alpine
RUN apk add --no-cache chromium
# Baked in rather than bind-mounted: a scratch directory can be reaped between
# runs, which has already broken this image's dependencies once.
RUN npm install -g puppeteer-core@24
ENV NODE_PATH=/usr/local/lib/node_modules
WORKDIR /w
```

### scripts/verify/browser/lib.js

```javascript
// Shared browser-test helpers.
const puppeteer = require('puppeteer-core');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeReporter() {
  let pass = 0, fail = 0, skip = 0;
  const failed = [];
  return {
    check(name, ok, detail = '') {
      if (ok) { pass++; console.log(`    PASS  ${name}`); }
      else { fail++; failed.push(name); console.log(`    FAIL  ${name}${detail ? '  — ' + detail : ''}`); }
    },
    skip(name, why = '') { skip++; console.log(`    SKIP  ${name}${why ? '  — ' + why : ''}`); },
    section(t) { console.log(`\n  ${t}`); },
    summary() {
      console.log(`\n  ${pass} passed, ${fail} failed, ${skip} skipped`);
      if (fail) { console.log('  failed:'); failed.forEach(f => console.log(`    - ${f}`)); }
      return fail === 0;
    },
  };
}

async function launch() {
  return puppeteer.launch({
    executablePath: '/usr/bin/chromium-browser', headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
}

// A fresh browser context per role, with console errors and API calls recorded.
// Console errors matter: a React component that throws unmounts the whole app,
// which once turned every page blank after visiting /health.
async function session(browser, base, role) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [], api = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });
  page.on('request', r => {
    const u = r.url();
    if (u.includes('/api/')) api.push({ method: r.method(), url: u.replace(/^https?:\/\/[^/]+/, ''), at: Date.now() });
  });
  await page.goto(base + '/', { waitUntil: 'networkidle2' });
  if (role) await page.evaluate(r => sessionStorage.setItem('rag_role', JSON.stringify({ role: r })), role);
  return { ctx, page, errors, api };
}

const bodyText = page => page.evaluate(
  () => (document.querySelector('#root')?.innerText || '').replace(/\s+/g, ' ').trim());

// React tracks input state internally, so assigning .value is ignored. Use the
// native setter and dispatch the event React listens for.
const setValue = (page, selectorFn, value) => page.evaluate((fn, v) => {
  const el = eval(fn)();
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype
              : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}, selectorFn, value);

const clickByText = (page, text) => page.evaluate(t => {
  const el = [...document.querySelectorAll('a,button')].find(e => (e.textContent || '').trim() === t);
  if (!el) return false; el.click(); return true;
}, text);

module.exports = { sleep, makeReporter, launch, session, bodyText, setValue, clickByText };
```

### scripts/verify/browser/ui_criteria.js

```javascript
// SPECIFICATIONS.md §10.4 — Web UI, plus the Transfer and help pages.
//
// Selectors here are deliberately precise. Loose ones have produced false
// results in both directions: a collections dropdown once matched as the
// chunking-strategy selector, `input[type=text]` missed an input with no type
// attribute, and a row's delete link matched a class test meant for the modal's
// confirm button.
const { sleep, makeReporter, launch, session, bodyText, clickByText } = require('./lib');

const BASE = process.env.RAG_UI_BASE || 'http://proxy';
const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic'];

(async () => {
  const browser = await launch();
  const r = makeReporter();

  // ── role persistence ───────────────────────────────────────────────────────
  r.section('§10.4 role selection');
  {
    const s = await session(browser, BASE, null);
    await s.page.goto(BASE + '/', { waitUntil: 'networkidle2' });
    await sleep(1200);
    const picked = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => /Engineer/i.test(x.textContent));
      if (!b) return false; b.click(); return true;
    });
    r.check('a role can be chosen on the landing page', picked);
    const stored = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    r.check('the choice is persisted', !!stored, String(stored));
    for (const p of ['/qa', '/collections', '/health', '/qa']) {
      await s.page.goto(BASE + p, { waitUntil: 'networkidle2' }); await sleep(700);
    }
    const after = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    const navPresent = await s.page.evaluate(() => document.querySelectorAll('nav a').length > 0);
    r.check('the role survives navigation', after === stored && navPresent);
    await s.ctx.close();
  }

  // ── role gating ────────────────────────────────────────────────────────────
  r.section('§10.4 role gating');
  {
    const s = await session(browser, BASE, 'end_user');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    r.check('End User sees only Q&A in the nav', links.length === 1 && /Q&A/.test(links[0]), JSON.stringify(links));
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(1200);
    const landed = await s.page.evaluate(() => location.pathname);
    r.check('End User cannot reach a gated route directly', landed === '/qa', `landed on ${landed}`);
    await s.ctx.close();
  }
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    for (const want of ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer', 'Collections', 'Health']) {
      r.check(`Engineer nav includes ${want}`, links.includes(want), JSON.stringify(links));
    }
    await s.ctx.close();
  }

  // ── chunking explainer ─────────────────────────────────────────────────────
  r.section('§10.4 chunking explainer');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/chunking', { waitUntil: 'networkidle2' }); await sleep(2200);
    let reloads = 0; s.page.on('framenavigated', () => reloads++);
    const options = await s.page.evaluate(K => {
      const sel = [...document.querySelectorAll('select')]
        .find(x => { const v = [...x.options].map(o => o.value); return K.every(k => v.includes(k)); });
      return sel ? [...sel.options].map(o => o.value) : [];
    }, STRATEGIES);
    r.check('the strategy selector offers every strategy', options.length === STRATEGIES.length, JSON.stringify(options));
    const seen = [];
    for (const v of options) {
      await s.page.evaluate(val => {
        const sel = [...document.querySelectorAll('select')].find(x => [...x.options].some(o => o.value === val));
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, val);
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }, v);
      await sleep(500);
      seen.push(await bodyText(s.page));
    }
    r.check('each strategy renders a distinct explanation',
            options.length > 1 && new Set(seen).size === options.length, `${new Set(seen).size} distinct`);
    r.check('no page reload occurs', reloads === 0, `${reloads} navigations`);
    await s.ctx.close();
  }

  // ── delete confirmation ────────────────────────────────────────────────────
  r.section('§10.4 delete confirmation');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(2200);
    const opened = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Delete');
      if (!b) return false; b.click(); return true;
    });
    if (!opened) {
      r.skip('delete confirmation', 'no collection present to delete');
    } else {
      await sleep(700);
      const state = await s.page.evaluate(() => {
        const inputs = [...document.querySelectorAll('input')].filter(i => !i.type || i.type === 'text');
        // the modal's confirm button is the solid red one; the row link is not
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        return { inputs: inputs.length, disabled: btn ? btn.disabled : null };
      });
      r.check('the modal asks for the name to be typed', state.inputs > 0, JSON.stringify(state));
      r.check('confirm is disabled until it matches', state.disabled === true, JSON.stringify(state));
      const before = s.api.filter(x => x.method === 'DELETE').length;
      await s.page.evaluate(() => {
        const el = [...document.querySelectorAll('input')].find(i => !i.type || i.type === 'text');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, 'not-the-name');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        if (btn && !btn.disabled) btn.click();
      });
      await sleep(900);
      r.check('a wrong name sends no DELETE',
              s.api.filter(x => x.method === 'DELETE').length === before);
    }
    await s.ctx.close();
  }

  // ── upload size limit ──────────────────────────────────────────────────────
  // The Import page refuses a selection over the proxy's limit before sending.
  // The file is sparse: it reports 513 MB but occupies no disk, and the check
  // must stop it before the browser ever reads it. See issue #21.
  r.section('upload size limit');
  {
    const fs = require('fs');
    const big = '/tmp/vfy-oversize-upload.txt';
    fs.closeSync(fs.openSync(big, 'w'));
    fs.truncateSync(big, 513 * 1024 * 1024);
    const s = await session(browser, BASE, 'developer');
    await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
    const hint = await bodyText(s.page);
    r.check('the drop zone states the upload limit', hint.includes('up to 512 MB per upload'));
    const input = await s.page.$('#file-input');
    await input.uploadFile(big);
    await sleep(500);
    const posts = () => s.api.filter(x => x.method === 'POST' && x.url.includes('/ingest/upload')).length;
    const before = posts();
    const clicked = await clickByText(s.page, 'Start Ingest');
    await sleep(900);
    const text = await bodyText(s.page);
    r.check('an oversize selection is refused with the limit named',
            clicked && text.includes('one upload can be at most 512 MB'),
            clicked ? text.slice(0, 160) : 'Start Ingest button not found');
    r.check('an oversize selection sends no upload', posts() === before);
    r.check('no console errors on the import page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    fs.unlinkSync(big);
    await s.ctx.close();
  }

  // ── health dashboard ───────────────────────────────────────────────────────
  r.section('§10.4 health dashboard');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/health', { waitUntil: 'networkidle2' }); await sleep(2500);
    const body = await bodyText(s.page);
    const latencies = body.match(/\d+\s*ms/g) || [];
    r.check('per-service latency is shown', latencies.length >= 3, latencies.slice(0, 5).join(' '));
    // Services are labelled by role and model, not by the word "Ollama".
    for (const want of ['Weaviate', 'LLM', 'Embed']) {
      r.check(`the dashboard names ${want}`, new RegExp(want, 'i').test(body));
    }
    if (process.env.RAG_SKIP_SLOW === '1') {
      r.skip('30s auto-refresh', 'needs a 70s observation window');
    } else {
      const t0 = Date.now(); s.api.length = 0;
      await sleep(70000);
      const hits = s.api.filter(x => x.url.includes('/health')).map(x => Math.round((x.at - t0) / 1000));
      const gaps = hits.slice(1).map((v, i) => v - hits[i]);
      r.check('the dashboard refreshes on its own', hits.length >= 2, `polled at t+${hits.join('s, t+')}s`);
      r.check('the interval is about 30s', gaps.length > 0 && gaps.every(g => g >= 25 && g <= 35), `gaps: ${gaps.join(', ')}s`);
    }
    r.check('no console errors on the health page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  // ── transfer help page ─────────────────────────────────────────────────────
  r.section('transfer help page');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/help/transfer', { waitUntil: 'networkidle2' }); await sleep(2500);
    const info = await s.page.evaluate(() => {
      const h1 = document.querySelector('h1');
      const p = document.querySelector('article p');
      return {
        chars: (document.querySelector('#root')?.innerText || '').length,
        headings: [...document.querySelectorAll('h2')].map(e => e.textContent.trim()),
        tables: document.querySelectorAll('table').length,
        h1Size: h1 ? parseFloat(getComputedStyle(h1).fontSize) : 0,
        pSize: p ? parseFloat(getComputedStyle(p).fontSize) : 0,
      };
    });
    r.check('the help page renders substantive content', info.chars > 3000, `${info.chars} chars`);
    r.check('markdown tables render', info.tables >= 3, `${info.tables} tables`);
    r.check('headings are styled (typography plugin present)', info.h1Size > info.pSize,
            `h1=${info.h1Size}px p=${info.pSize}px`);
    for (const want of ['Where packages live', 'Naming', 'What a package contains',
                        'Fidelity', 'embedding model rule', 'name collision', 'Tuning after import']) {
      r.check(`§10 topic covered: ${want}`,
              info.headings.some(h => h.toLowerCase().includes(want.toLowerCase())),
              info.headings.join(' | ').slice(0, 80));
    }
    r.check('no console errors on the help page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  await browser.close();
  process.exit(r.summary() ? 0 : 1);
})().catch(e => { console.log('  HARNESS FAILURE: ' + e.message); process.exit(2); });
```

## 6. Packaging and Offline Distribution

Specified in `SPECIFICATIONS.md` §11. The files below are the implementation.

### api/.dockerignore

```
# Build context exclusions for the api image.
#
# The Dockerfile ends with `COPY . .`, so anything left in this directory is
# baked into the image unless excluded here.

# Python bytecode and tool caches. Platform-specific and never useful in the
# image; they also bust the `COPY . .` layer cache on every local test run.
__pycache__/
**/__pycache__/
*.py[cod]
*$py.class
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# A local virtualenv would shadow the image's site-packages.
.venv/
venv/
env/
ENV/

# Runtime data. UPLOAD_DIR is the named volume ingest_uploads mounted at
# /app/uploads, so anything here would be baked in and then hidden by the mount.
uploads/

# OS and editor noise
.DS_Store
**/.DS_Store
Thumbs.db
*.swp
*~

# Local environment files.
#
# NOTE: config.py sets `env_file = ".env"`, so a .env placed in this directory
# WOULD be read at runtime if it were copied in. It is excluded deliberately --
# secrets do not belong in an image layer. docker-compose.yml supplies every
# setting the API needs as explicit environment variables. If you need local
# overrides, add them to the `api` service's `environment:` block instead.
.env
.env.*

# Version control
.git
.gitignore

# Read directly by the builder; no need to ship them inside the image.
Dockerfile
.dockerignore
```

### ui/.dockerignore

```
# Build context exclusions for the ui image.
#
# The Dockerfile runs `COPY package*.json ./` -> `npm ci` -> `COPY . .`, so the
# second COPY lands on top of an already-installed tree. Without this file, a
# host node_modules/ would overwrite the container's freshly installed packages
# with ones built for the host platform (darwin/arm64 binaries inside a linux
# image), producing a broken or silently wrong build.

# Host install and build output
node_modules
dist
.vite
.cache

# OS and editor noise. These bust the `COPY . .` layer cache on every Finder
# or editor touch, forcing a needless rebuild of `npm run build`.
.DS_Store
**/.DS_Store
Thumbs.db
*.swp
*~

# Local environment files are never baked into an image.
# (Nothing in ui/src reads import.meta.env today, so the build does not need them.)
.env
.env.*

# Logs and caches
*.log
npm-debug.log*
yarn-error.log*
.npm
.eslintcache

# Version control
.git
.gitignore

# Read directly by the builder; no need to ship them inside the image.
Dockerfile
.dockerignore
```

### package.sh

Source-only archive (~150 KB). The target machine rebuilds the images and
re-downloads the models, so it needs internet.

```bash
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
```

### package-offline.sh

Self-contained bundle (~4.6 GB) for an air-gapped target: source plus
`docker save` of all five images plus the Ollama model weights.

```bash
#!/usr/bin/env bash
#
# Build a fully self-contained OFFLINE bundle of this project.
#
#   bash package-offline.sh [output.tar]
#
# Unlike package.sh (source only, ~150 KB, needs the internet on first run),
# this bundle contains everything required to run with NO network access:
#
#   * the source tree
#   * all five Docker images, pre-built (docker save)
#   * the Ollama model weights (phi3.5 + nomic-embed-text)
#
# Measured at 4.6 GB (2.5 GB images + 2.2 GB model weights) with both models
# present. Build it on a machine where the stack already works,
# then move the file to the target Mac and run install-offline.sh there.

set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-rag-docker-offline.tar}"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"   # absolute

# The MCP server is parked (see docker-compose.yml). Its image is deliberately
# NOT bundled: nothing in the stack uses it, and requiring it here would make
# packaging fail on a machine that has never built it. Re-add
# rag-docker-mcp:latest when the MCP server is brought back.
IMAGES=(
  rag-docker-api:latest
  rag-docker-ui:latest
  semitechnologies/weaviate:1.39.4
  ollama/ollama:0.3.14
  nginx:1.27-alpine
)

echo "==> Checking prerequisites"
for img in "${IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || { echo "ERROR: image '$img' not found locally. Run 'docker compose build' and 'docker compose up -d' first." >&2; exit 1; }
done

# Resolve the compose project so we read the right volume.
PROJECT="$(docker compose config --format json | tr -d ' \n' | sed -n 's/^{"name":"\([^"]*\)".*/\1/p')"
[ -n "$PROJECT" ] || PROJECT="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
MODELVOL="${PROJECT}_ollama_models"
docker volume inspect "$MODELVOL" >/dev/null 2>&1 \
  || { echo "ERROR: volume '$MODELVOL' not found. Start the stack once so the models download." >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/rag-docker/offline"

echo "==> Copying source"
tar cf - \
  --exclude='./.DS_Store' --exclude='*/.DS_Store' \
  --exclude='*/node_modules/*' --exclude='*/__pycache__/*' \
  --exclude='*.pyc' --exclude='*/dist/*' \
  --exclude='*/.venv/*' --exclude='*/venv/*' \
  --exclude='./.env' --exclude='*/.env' --exclude='*/.env.*' \
  --exclude='./.git/*' --exclude='*.tar' --exclude='*.zip' \
  --exclude='./offline/*' \
  --exclude='./ingest-inbox/*' \
  --exclude='./exports/*' \
  . | ( cd "$STAGE/rag-docker" && tar xf - )

# Both zip and tar drop a directory once all its contents are excluded, so the
# mount target is recreated explicitly. Without it Docker creates ./ingest-inbox
# as root on the target machine and the user cannot drop files into it.
mkdir -p "$STAGE/rag-docker/ingest-inbox"
touch "$STAGE/rag-docker/ingest-inbox/.gitkeep"
mkdir -p "$STAGE/rag-docker/exports"
touch "$STAGE/rag-docker/exports/.gitkeep"

echo "==> Saving images (this is the slow part)"
docker save "${IMAGES[@]}" | gzip > "$STAGE/rag-docker/offline/images.tar.gz"

echo "==> Exporting model weights from volume '$MODELVOL'"
# Uses the ollama image itself as the tar helper: it ships /usr/bin/tar and is
# already part of the bundle, so no extra image is needed here or on the target.
docker run --rm --entrypoint sh \
  -v "$MODELVOL":/models:ro \
  -v "$STAGE/rag-docker/offline":/out \
  ollama/ollama:0.3.14 \
  -c 'tar czf /out/ollama_models.tar.gz -C /models .'

echo "==> Assembling bundle"
rm -f "$OUT"
tar cf "$OUT" -C "$STAGE" rag-docker

echo
echo "Created $OUT ($(du -h "$OUT" | cut -f1))"
echo "  images:       $(du -h "$STAGE/rag-docker/offline/images.tar.gz" | cut -f1)"
echo "  model weights:$(du -h "$STAGE/rag-docker/offline/ollama_models.tar.gz" | cut -f1)"
echo
echo "On the target Mac (no internet required):"
echo "  tar xf $(basename "$OUT") && cd rag-docker"
echo "  bash install-offline.sh"
```

### install-offline.sh

Runs on the target. Loads the images, restores the weights into the project's
volume, and starts the stack without building or pulling.

```bash
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
```

### scripts/verify/14_reindex.sh

```bash
#!/usr/bin/env bash
# Exact-record reindex and unavailable-embedding acceptance.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$bindings" || exit 2
require_stack
section "Exact-record reindex"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_collection_writes.py)
check "collection writer barrier regressions" $?
# Transport only these three controlled sources into a temporary API directory.
# The tests exercise the actual helper and parent cleanup function with mocks.
(cd ../.. && python3 -c 'import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode="w|"); [t.add("scripts/verify/"+name,arcname=name) for name in ("reindex.py","lib.sh","reindex_verifier_cases.py")]; t.close()' | docker compose exec -T api python -c 'import os,sys,tarfile,tempfile,subprocess; task=tempfile.TemporaryDirectory(prefix="owned-reindex-tests-"); tarfile.open(fileobj=sys.stdin.buffer,mode="r|*").extractall(task.name,filter="data"); result=subprocess.run([sys.executable,task.name+"/reindex_verifier_cases.py"],env={**os.environ,"RAG_TEST_API_DIR":"/app","RAG_REINDEX_VERIFIER_SOURCE":task.name+"/reindex.py","RAG_VERIFIER_LIB":task.name+"/lib.sh"}); task.cleanup(); sys.exit(result.returncode)')
check "verifier async ownership and parent cleanup regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/verify/reindex_cases.py)
check "owned reindex preservation and failure regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/reindex.py)
check "real reindex with embedding endpoint unavailable" $?
summary
```

### scripts/verify/reindex_cases.py

```python
"""Controlled reindex preservation/failure cases; registered by14_reindex.sh."""
import copy, math, os, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app'))
from services import tuning
validate_vectorizer = tuning.wc._validate_reindex_vectorizer_sync


def records():
    return [{'id': '49000000-0000-4000-8000-00000000000'+str(i),
             'vector': [0.125, float(i), -0.25],
             'properties': {'content': 'Owned inert '+str(i), 'chunk_index': i, 'source_file': 'owned.txt'}} for i in range(2)]


class Batch:
    def __init__(self, owner, name):
        self.owner, self.name, self.number_errors, self.pending = owner, name, 0, []
    def __enter__(self): return self
    def add_object(self, properties, uuid=None, vector=None):
        self.pending.append({'id': str(uuid), 'vector': copy.deepcopy(vector), 'properties': copy.deepcopy(properties)})
        if self.owner.mutate_arguments:
            properties['content'] = 'SDK argument mutation'; vector[0] = 999
    def __exit__(self, *exc):
        if self.owner.fail(self.name): self.number_errors = 1
        else: self.owner.data[self.name] += self.pending
        self.owner.closed.append(self.name)
        if self.owner.corrupt(self.name) and self.owner.data[self.name]:
            self.owner.data[self.name][0]['properties']['content'] = 'Owned readback corruption'
        if self.owner.change_source and self.name != 'OwnedReindex':
            self.owner.data['OwnedReindex'][0]['properties']['content'] = 'Newer independent write'


class Collections:
    def __init__(self, initial):
        self.data = {'OwnedReindex': copy.deepcopy(initial)}; self.deleted = []; self.created = []; self.closed = []
        self.fail = lambda name: False; self.corrupt = lambda name: False
        self.change_source = self.mutate_arguments = False
    def get(self, name):
        def iterator(include_vector=False):
            for record in self.data[name]:
                yield SimpleNamespace(uuid=record['id'], properties=copy.deepcopy(record['properties']), vector={'default':copy.deepcopy(record['vector'])} if include_vector else None)
        return SimpleNamespace(iterator=iterator, batch=SimpleNamespace(dynamic=lambda: Batch(self, name)))
    def delete(self, name): self.deleted.append(name); del self.data[name]
    def create(self, name, index, distance, hnsw):
        self.created.append((name,index,distance,copy.deepcopy(hnsw))); self.data[name] = []


class ReindexTests(unittest.TestCase):
    def setUp(self):
        self.original = records(); self.backend = Collections(self.original)
        self.config = {'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{'ef':64,'efConstruction':128,'maxConnections':64}}
        self.embedding = Mock(side_effect=AssertionError('Reindex contacted embedding insertion'))
        self.stale = Mock(return_value=1)
        patches = [patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=self.backend)),
                   patch.object(tuning.wc,'_create_collection_sync',side_effect=self.backend.create),
                   patch.object(tuning.wc,'_collection_config_sync',return_value=self.config),
                   patch.object(tuning.wc,'_validate_reindex_vectorizer_sync'),
                   patch.object(tuning.wc,'_insert_chunks_sync',self.embedding),
                   patch.object(tuning.sources,'has_sources',return_value=False),
                   patch.object(tuning.goldstandard,'mark_stale',self.stale),
                   patch.object(tuning,'_jobs',{}),patch.object(tuning,'_active',{'OwnedReindex'})]
        for change in patches: change.start(); self.addCleanup(change.stop)
        import tempfile
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        def begin(collection,operation,client):return {'staging':collection+'__tuning_owned','state':'scratch','operation_id':'owned'}
        def retain(owner, **kwargs):owner['state']='recovery'
        def discard(owner,client):client.collections.delete(owner['staging'])
        for change in [patch.object(tuning.collection_recovery,'begin',side_effect=begin),patch.object(tuning.collection_recovery,'retain',side_effect=retain),patch.object(tuning.collection_recovery,'discard',side_effect=discard),patch.object(tuning.collection_recovery,'_root',return_value=Path(self.temp.name))]:
            change.start();self.addCleanup(change.stop)
    def run_job(self, operation='reindex'):
        tuning._jobs['owned'] = {'status':'queued','chunks_written':0,'notes':[]}
        tuning._run('owned','OwnedReindex',operation,{'index_type':'flat','distance_metric':'dot',**({'chunking':{}} if operation=='rechunk' else {})})
        return tuning._jobs['owned']
    def test_reindex_changes_physical_config_and_preserves_every_record_without_embedding(self):
        job=self.run_job(); self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],2)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.embedding.assert_not_called(); self.stale.assert_not_called()
        self.assertTrue(all(entry[1:3]==('flat','dot') for entry in self.backend.created)); self.assertIn('verified unchanged',job['notes'][0])
        self.assertNotIn('OwnedReindex',tuning._active)
    def test_deferred_batch_failure_is_seen_before_original_deletion(self):
        self.backend.fail=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.assertEqual(job['chunks_written'],0); self.stale.assert_not_called()
    def test_staging_readback_mismatch_preserves_original(self):
        self.backend.corrupt=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted); self.stale.assert_not_called()
    def test_observed_source_change_during_staging_is_not_overwritten(self):
        self.backend.change_source=True; job=self.run_job(); self.assertEqual(job['status'],'failed')
        self.assertNotIn('OwnedReindex',self.backend.deleted); self.assertEqual(self.backend.data['OwnedReindex'][0]['properties']['content'],'Newer independent write')
    def test_final_deferred_failure_has_no_completion_claim_and_marks_retained_pairs(self):
        self.backend.fail=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.assertEqual(job['notes'],[])
        self.stale.assert_called_once(); self.assertIn('cutover',self.stale.call_args.args[1])
    def test_final_readback_mismatch_is_not_completed(self):
        self.backend.corrupt=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.stale.assert_called_once()
    def test_missing_or_nonfinite_vectors_fail_before_any_creation(self):
        for vector in [[],None,[math.nan],[math.inf],[True]]:
            with self.subTest(vector=vector):
                self.backend.data['OwnedReindex'][0]['vector']=vector; job=self.run_job()
                self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[]); self.assertEqual(self.backend.deleted,[])
    def test_unsupported_named_vector_is_refused_without_guessing(self):
        col=SimpleNamespace(iterator=lambda **kw:iter([SimpleNamespace(uuid=self.original[0]['id'],properties={},vector={'other':[1.0]})]))
        with patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=SimpleNamespace(get=lambda name:col))):
            with self.assertRaisesRegex(RuntimeError,'single default vector'): tuning._existing_records('OwnedReindex')
    def test_duplicate_readback_id_is_refused(self):
        self.backend.data['OwnedReindex'].append(copy.deepcopy(self.original[0])); job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[])
    def test_sdk_argument_mutation_does_not_change_expected_snapshot(self):
        self.backend.mutate_arguments=True; snapshot=tuning._existing_records('OwnedReindex'); before=copy.deepcopy(snapshot)
        self.backend.create('OwnedCopy','flat','dot',{}); tuning._write_records('OwnedCopy',snapshot)
        self.assertEqual(snapshot,before); self.assertEqual(self.backend.data['OwnedCopy'],self.original)
    def test_empty_collection_reindexes_without_embedding(self):
        self.backend.data['OwnedReindex']=[]; job=self.run_job()
        self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],0); self.embedding.assert_not_called(); self.stale.assert_not_called()
    def test_ingest_started_after_source_check_waits_for_final_reindex_copy(self):
        import tempfile,threading,uuid
        from concurrent.futures import ThreadPoolExecutor
        from services import ingest_pipeline as ingest
        entered=threading.Event();release=threading.Event();parsed=threading.Event();attempted=threading.Event()
        verify=tuning._verify_records;paused=False
        def paused_verify(name,records):
            nonlocal paused
            verify(name,records)
            if name=='OwnedReindex' and not paused:
                paused=True;entered.set();assert release.wait(3)
        def parse(path):parsed.set();return 'Owned later upload',[]
        def insert(name,chunks):self.backend.data[name].append({'id':str(uuid.uuid4()),'vector':[.125,0.,-.25],'properties':chunks[0]})
        job={'status':'queued','chunks_stored':0,'files_completed':0,'files_failed':0,'files_total':1,'errors':[]}
        with tempfile.TemporaryDirectory() as temp,patch.object(tuning,'_verify_records',side_effect=paused_verify),patch.object(ingest,'_jobs',{'owned_ingest':job}),patch.object(ingest,'_parse_file',side_effect=parse),patch.object(ingest,'do_chunk',return_value=['Owned later upload']),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert),patch.object(ingest.sources,'store'),ThreadPoolExecutor() as pool:
            path=Path(temp)/'owned.txt';path.write_text('Owned later upload')
            reindex=pool.submit(self.run_job);self.assertTrue(entered.wait(2))
            def start_ingest():
                attempted.set();ingest._process_job_sync('owned_ingest',[path],Path(temp),'OwnedReindex','fixed',150,0,.5,40)
            upload=pool.submit(start_ingest);self.assertTrue(attempted.wait(2));self.assertFalse(parsed.wait(.05));release.set()
            result=reindex.result(timeout=3);upload.result(timeout=3)
        self.assertEqual(result['status'],'completed');self.assertEqual(job['status'],'completed')
        self.assertEqual(self.backend.data['OwnedReindex'][:2],self.original);self.assertEqual(len(self.backend.data['OwnedReindex']),3)
    def test_reindex_snapshot_waits_for_already_running_ingest(self):
        import tempfile,threading,uuid
        from concurrent.futures import ThreadPoolExecutor
        from services import ingest_pipeline as ingest
        parsed=threading.Event();release=threading.Event();snapshot=threading.Event();attempted=threading.Event()
        original_read=tuning._existing_records
        def read(name):snapshot.set();return original_read(name)
        def parse(path):parsed.set();assert release.wait(3);return 'Owned active upload',[]
        def insert(name,chunks):self.backend.data[name].append({'id':str(uuid.uuid4()),'vector':[.125,0.,-.25],'properties':chunks[0]})
        job={'status':'queued','chunks_stored':0,'files_completed':0,'files_failed':0,'files_total':1,'errors':[]}
        with tempfile.TemporaryDirectory() as temp,patch.object(tuning,'_existing_records',side_effect=read),patch.object(ingest,'_jobs',{'owned_ingest':job}),patch.object(ingest,'_parse_file',side_effect=parse),patch.object(ingest,'do_chunk',return_value=['Owned active upload']),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert),patch.object(ingest.sources,'store'),ThreadPoolExecutor() as pool:
            path=Path(temp)/'owned.txt';path.write_text('Owned active upload')
            upload=pool.submit(ingest._process_job_sync,'owned_ingest',[path],Path(temp),'OwnedReindex','fixed',150,0,.5,40)
            self.assertTrue(parsed.wait(2))
            def start_reindex():attempted.set();return self.run_job()
            reindex=pool.submit(start_reindex);self.assertTrue(attempted.wait(2));self.assertFalse(snapshot.wait(.05));release.set()
            upload.result(timeout=3);result=reindex.result(timeout=3)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['chunks_written'],3)
        self.assertEqual(self.backend.data['OwnedReindex'][:2],self.original);self.assertEqual(len(self.backend.data['OwnedReindex']),3)
    def test_foreign_vectorizer_refuses_before_staging_or_delete(self):
        tuning.wc._validate_reindex_vectorizer_sync.side_effect=ValueError('Owned incompatible model')
        job=self.run_job();self.assertEqual(job['status'],'failed')
        self.assertEqual(self.backend.created,[]);self.assertEqual(self.backend.deleted,[])
        self.assertEqual(self.backend.data['OwnedReindex'],self.original);self.stale.assert_not_called()
    def test_vectorizer_validation_checks_model_endpoint_type_and_named_vectors(self):
        from config import settings
        cfg=SimpleNamespace(vectorizer_config=SimpleNamespace(vectorizer='text2vec-ollama',model={'model':settings.embed_model,'apiEndpoint':f'http://{settings.ollama_host}:{settings.ollama_port}'},vectorize_collection_name=False),vector_config=None,properties=[SimpleNamespace(name=p.name,data_type=p._to_dict()["dataType"][0],vectorizer='text2vec-ollama',vectorizer_config=SimpleNamespace(skip=False,vectorize_property_name=True),vectorizer_configs=None,nested_properties=None) for p in tuning.wc.COLLECTION_PROPERTIES])
        client=SimpleNamespace(collections=SimpleNamespace(get=lambda name:SimpleNamespace(config=SimpleNamespace(get=lambda:cfg))))
        with patch.object(tuning.wc,'get_client',return_value=client):
            validate_vectorizer('Owned')
            for attribute,value in [('model','foreign'),('apiEndpoint','http://foreign:1')]:
                original=cfg.vectorizer_config.model[attribute];cfg.vectorizer_config.model[attribute]=value
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                cfg.vectorizer_config.model[attribute]=original
            cfg.vectorizer_config.vectorizer='unsupported'
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
            cfg.vectorizer_config.vectorizer='text2vec-ollama';cfg.vector_config={'foreign':object()}
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
    def test_vectorizer_refuses_extra_module_options_and_changed_property_inputs(self):
        from config import settings
        props=[SimpleNamespace(name=p.name,data_type=p._to_dict()["dataType"][0],vectorizer='text2vec-ollama',vectorizer_config=SimpleNamespace(skip=False,vectorize_property_name=True),vectorizer_configs=None,nested_properties=None) for p in tuning.wc.COLLECTION_PROPERTIES]
        cfg=SimpleNamespace(vectorizer_config=SimpleNamespace(vectorizer='text2vec-ollama',model={'model':settings.embed_model,'apiEndpoint':f'http://{settings.ollama_host}:{settings.ollama_port}'},vectorize_collection_name=False),vector_config=None,properties=props)
        client=SimpleNamespace(collections=SimpleNamespace(get=lambda name:SimpleNamespace(config=SimpleNamespace(get=lambda:cfg))))
        with patch.object(tuning.wc,'get_client',return_value=client):
            validate_vectorizer('Owned')
            cfg.vectorizer_config.model['source_properties']=['content']
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
            cfg.vectorizer_config.model.pop('source_properties')
            for field,value in [('skip',True),('vectorize_property_name',False)]:
                rules=props[0].vectorizer_config;old=getattr(rules,field);setattr(rules,field,value)
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                setattr(rules,field,old)
            for field,value in [('name','custom_text'),('data_type','int'),('vectorizer','foreign'),('vectorizer_configs',{'default':object()})]:
                old=getattr(props[0],field);setattr(props[0],field,value)
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                setattr(props[0],field,old)
            props[1]=props[0]
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
    def test_staging_creation_failures_cleanup_owned_scratch_without_touching_original(self):
        create=self.backend.create
        for created_before_error in (False,True):
            with self.subTest(created_before_error=created_before_error):
                def fail(name,*args):
                    if created_before_error:create(name,*args)
                    raise RuntimeError('Owned staging create acknowledgement failure')
                def discard(owner,client):
                    if owner['staging'] in client.collections.data:client.collections.delete(owner['staging'])
                with patch.object(tuning.wc,'_create_collection_sync',side_effect=fail),patch.object(tuning.collection_recovery,'discard',side_effect=discard) as cleanup:
                    job=self.run_job()
                self.assertEqual(job['status'],'failed');cleanup.assert_called_once()
                self.assertEqual(set(self.backend.data),{'OwnedReindex'});self.assertEqual(self.backend.data['OwnedReindex'],self.original)
                self.stale.assert_not_called()
    def test_delete_refusal_with_intact_original_preserves_evaluation_validity(self):
        delete=self.backend.delete
        def refuse(name):
            if name=='OwnedReindex':raise RuntimeError('Owned delete refusal')
            delete(name)
        with patch.object(self.backend,'delete',side_effect=refuse):job=self.run_job()
        self.assertEqual(job['status'],'failed');self.assertEqual(self.backend.data['OwnedReindex'],self.original)
        self.stale.assert_not_called();self.assertEqual(set(self.backend.data),{'OwnedReindex'})
    def test_uncertain_delete_retains_recovery_and_marks_historical(self):
        delete=self.backend.delete
        def uncertain(name):
            delete(name)
            if name=='OwnedReindex':raise RuntimeError('Owned lost delete acknowledgement')
        with patch.object(self.backend,'delete',side_effect=uncertain):job=self.run_job()
        self.assertEqual(job['status'],'failed');self.stale.assert_called_once()
        retained=job['error_detail']['recovered_as'];self.assertEqual(self.backend.data[retained],self.original)
        self.assertEqual(job['chunks_written'],0)
    def test_lowercase_alias_uses_canonical_job_and_cutover_identity(self):
        tuning._jobs['owned']={'status':'queued','chunks_written':0,'notes':[]}
        tuning._run('owned','ownedReindex','reindex',{'index_type':'flat','distance_metric':'dot'})
        job=tuning._jobs['owned'];self.assertEqual(job['status'],'completed');self.assertEqual(job['collection'],'OwnedReindex')
        self.assertEqual(self.backend.data['OwnedReindex'],self.original)
        self.assertTrue(all(name.startswith('OwnedReindex') for name,_,_,_ in self.backend.created))
    def test_alias_jobs_share_the_same_active_identity(self):
        import asyncio
        from unittest.mock import AsyncMock
        async def check():
            with patch.object(tuning,'_active',set()),patch.object(tuning.asyncio,'to_thread',new=AsyncMock(return_value=None)):
                identity=await tuning.start_tune_job('ownedReindex','reindex',{})
                self.assertEqual(tuning._jobs[identity]['collection'],'OwnedReindex')
                with self.assertRaises(RuntimeError):await tuning.start_tune_job('OwnedReindex','reindex',{})
                await asyncio.sleep(0)
        asyncio.run(check())
    def test_rechunk_and_reembed_preserve_caller_spelled_source_identity(self):
        for operation in ('rechunk','reembed'):
            with self.subTest(operation=operation),patch.object(tuning.sources,'has_sources',return_value=True) as has_sources,patch.object(tuning,'_chunks_from_sources',return_value=[{'content':'Owned caller sources'}]) as read_sources,patch.object(tuning,'_existing_chunks',return_value=[{'content':'Owned caller chunks'}]),patch.object(tuning,'_rebuild',return_value=1):
                tuning._jobs['owned']={'status':'queued','chunks_written':0,'notes':[]}
                params={'chunking':{'strategy':'fixed','chunk_size':150,'chunk_overlap':0,'similarity_threshold':.5,'min_chunk_size':40}}
                tuning._run('owned','ownedReindex',operation,params)
                self.assertEqual(tuning._jobs['owned']['status'],'completed');has_sources.assert_called_once_with('ownedReindex');self.assertEqual(read_sources.call_args.args[0],'ownedReindex')
    def test_every_tuning_operation_registers_owned_staging(self):
        for operation in ('reindex','reembed','rechunk'):
            with self.subTest(operation=operation),patch.object(tuning.collection_recovery,'begin',wraps=tuning.collection_recovery.begin) as begin:
                # Existing controlled rebuild fixture executes the real rebuild;
                # rechunk uses inert parsed properties without changing traversal.
                with patch.object(tuning.sources,'has_sources',return_value=True),patch.object(tuning,'_chunks_from_sources',return_value=[r['properties'] for r in self.original]),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=lambda name,props:self.backend.data.__setitem__(name,__import__('copy').deepcopy(self.original))):
                    job=self.run_job(operation)
                begin.assert_called_once();self.assertEqual(begin.call_args.args[:2],('OwnedReindex','tune'))
                self.assertEqual(set(self.backend.data),{'OwnedReindex'})
    def test_reembed_retains_its_explicit_regeneration_path(self):
        def embed(name,props):
            self.backend.data[name]=[{'id':'49000000-0000-4000-8000-000000000100','vector':[4.,5.,6.],'properties':copy.deepcopy(props[0])},
                                     {'id':'49000000-0000-4000-8000-000000000101','vector':[4.,5.,6.],'properties':copy.deepcopy(props[1])}]
        self.embedding.side_effect=embed; job=self.run_job('reembed')
        self.assertEqual(job['status'],'completed'); self.embedding.assert_called_once(); self.stale.assert_called_once()
        self.assertNotEqual(self.backend.data['OwnedReindex'],self.original)

if __name__=='__main__': unittest.main()
```

### scripts/verify/reindex.py

```python
"""Real Weaviate reindex with an unreachable embedding endpoint; owned fixtures only."""
import asyncio, json, os, subprocess, sys, tempfile, threading, uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import goldstandard as gs, tuning, ingest_pipeline as ingest
from services import weaviate_client as wc


async def completed(client,path,timeout=300,expected_status="completed"):
    deadline=asyncio.get_running_loop().time()+timeout;last='not observed'
    while True:
        remaining=deadline-asyncio.get_running_loop().time()
        if remaining<=0:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}')
        try:response=await asyncio.wait_for(client.get(path),remaining)
        except asyncio.TimeoutError as exc:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}') from exc
        assert response.status_code==200,response.text
        job=response.json();last=job.get('status','missing')
        if last not in ('queued','running'):
            assert last==expected_status,job
            return job
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def settle_owned_jobs(jobs,timeout=30):
    deadline=asyncio.get_running_loop().time()+timeout
    while True:
        pending=[(module.__name__,jobid,(module.get_job(jobid) or {}).get('status','missing')) for module,jobid in jobs if (module.get_job(jobid) or {}).get('status') not in ('completed','failed')]
        if not pending or asyncio.get_running_loop().time()>=deadline:return pending
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def bounded_poll_cases():
    from types import SimpleNamespace
    class StuckClient:
        async def get(self,path):return SimpleNamespace(status_code=200,text='',json=lambda:{'status':'running'})
    try:await completed(StuckClient(),'/owned/stuck-job',timeout=.01)
    except TimeoutError as exc:assert '/owned/stuck-job' in str(exc) and 'running' in str(exc)
    else:raise AssertionError('Stuck owned job did not meet its deadline')
    pending=await settle_owned_jobs([(SimpleNamespace(__name__='owned',get_job=lambda identity:{'status':'queued'}),'owned-cleanup-job')],timeout=.01)
    assert pending==[('owned','owned-cleanup-job','queued')]
    print('PASS two controlled job-poll and cleanup deadline cases',flush=True)

def owned_name(prefix,token):
    # Parent suites may sweep their prefix. This standalone owner must remain
    # outside that namespace when preserving a failed or interrupted fixture.
    if not prefix:raise ValueError('A nonempty parent fixture prefix is required')
    first = 'A' if not prefix.startswith('A') else 'B'
    return first+'OwnedReindex'+token

def preserve_receipt(directory,created,pending):
    path=Path(directory)/'owned-fixtures.json'
    with path.open('w') as output:
        json.dump({'created_collections':list(created),'pending_jobs':pending,'fixture_directory':directory,'cleanup':'Inspect terminal writer state and delete only these exact owned names; do not prefix-sweep.'},output,indent=2)
        output.flush();os.fsync(output.fileno())
    return str(path)


async def queued_ingest_checks(api,client,collection,jobs,check):
    before=await asyncio.to_thread(tuning._existing_records,collection)
    paused=threading.Event();release=threading.Event();original_verify=tuning._verify_records;once=False
    def verify(target,records):
        nonlocal once
        original_verify(target,records)
        if target==collection and not once:
            once=True;paused.set()
            if not release.wait(30):raise TimeoutError('Owned source-check pause expired')
    # Only the embedding fixture is replaced; the HTTP upload, parser, chunker,
    # ingest worker, source retention and actual backend writes remain real.
    def supplied_vectors(target,chunks):
        col=client.collections.get(target)
        for chunk in chunks:col.data.insert(properties=chunk,uuid=str(uuid.uuid4()),vector=[.25]*len(before[0]['vector']))
    with patch.object(tuning,'_verify_records',side_effect=verify),patch.object(wc,'_insert_chunks_sync',side_effect=supplied_vectors):
        try:
            response=await api.post('/tune/reindex',json={'collection':collection,'index_type':'hnsw','distance_metric':'cosine'})
            assert response.status_code==202,response.text;reindex=response.json()['job_id'];jobs.append((tuning,reindex))
            check(await asyncio.to_thread(paused.wait,10),'real reindex reaches protected source check before cutover')
            uploaded=await api.post('/ingest/upload',data={'collection':collection,'strategy':'fixed','chunk_size':'150','chunk_overlap':'0','min_chunk_size':'100'},files={'files':('owned-concurrent.txt',b'Owned concurrency upload with inert public text. '*40,'text/plain')})
            assert uploaded.status_code==202,uploaded.text;upload=uploaded.json()['job_id'];jobs.append((ingest,upload))
            status=await api.get('/ingest/job/'+upload)
            check(status.status_code==200 and status.json()['status']=='queued','actual ingest HTTP job remains queued while reindex holds the source guard')
            release.set()
            reindexed=await completed(api,'/tune/job/'+reindex)
            ingested=await completed(api,'/ingest/job/'+upload)
            check(reindexed['chunks_written']==len(before) and ingested['chunks_stored']>0,'reindex verifies its copy before the waiting real ingest completes')
        finally:release.set()
    after=await asyncio.to_thread(tuning._existing_records,collection);observed={record['id']:record for record in after}
    check(all(observed.get(record['id'])==record for record in before) and len(after)==len(before)+ingested['chunks_stored'],'real backend retains original exact records and the post-cutover upload')


async def retained_cutover_checks(api,client,name,temp,record_create,jobs,check):
    collection=name+'CutoverFail';sid='gs_'+uuid.uuid4().hex[:8]
    await asyncio.to_thread(wc._create_collection_sync,collection,'hnsw','cosine',{})
    col=client.collections.get(collection)
    await asyncio.to_thread(col.data.insert,properties={'content':'Owned inert recovery','source_file':'owned.txt','chunk_index':0},uuid=str(uuid.uuid4()),vector=[.125]*768)
    before=await asyncio.to_thread(tuning._existing_records,collection)
    session={'session_id':sid,'collection':collection[:1].lower()+collection[1:],'status':'completed','pairs_total':1,'pairs_completed':1,'pairs_attempted':1,'pairs_failed':0,'pairs':[{'pair_id':'p_owned','question':'Owned','answer':'Owned','contexts':['Owned inert recovery'],'ground_truth':'Owned','source_file':'owned.txt','chunk_index':0,'status':'approved'}]}
    await asyncio.to_thread(gs.store_session,session);session_bytes=await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes())
    def create(target,*args,**kwargs):
        if target==collection:raise RuntimeError('Owned injected final-create failure after cutover')
        record_create(target,*args,**kwargs)
    with patch.object(wc,'_create_collection_sync',side_effect=create):
        response=await api.post('/tune/reindex',json={'collection':collection[:1].lower()+collection[1:],'index_type':'flat','distance_metric':'dot'})
        assert response.status_code==202,response.text;job=response.json()['job_id'];jobs.append((tuning,job))
        result=await completed(api,'/tune/job/'+job,expected_status='failed')
    check(result['chunks_written']==0,'post-cutover failure is not published as completion')
    stage=result['error_detail']['recovered_as'];check(stage.startswith(collection+'__tuning_'),'failure identifies the exact operation-owned retained copy')
    check(await asyncio.to_thread(tuning._existing_records,stage)==before,'retained backend copy preserves exact UUID/properties/vector after final create fails')
    paths=await asyncio.to_thread(lambda:list(tuning.collection_recovery._root().glob('*.json')));assert len(paths)==1
    owner=await asyncio.to_thread(lambda:json.loads(paths[0].read_text()))
    check(owner['state']=='recovery' and owner['target']==collection and owner['staging']==stage,'durable recovery ownership binds the original and retained copy')
    snapshot=Path(result['error_detail']['sidecar_snapshots'])/'goldstandard'/(sid+'.json')
    check(await asyncio.to_thread(snapshot.read_bytes)==session_bytes,'pre-cutover evaluation snapshot is retained byte-identically')
    check(await asyncio.to_thread(lambda:gs.get_session(sid).get('stale')),'failed cutover marks its retained evaluation historical')
    await asyncio.to_thread(wc._sweep_staging_sync)
    check(await asyncio.to_thread(client.collections.exists,stage) and await asyncio.to_thread(paths[0].is_file),'actual startup sweep preserves retained recovery and its durable record')
    child="""import asyncio,json,sys
from services import weaviate_client as wc,goldstandard as gs,tuning
from main import app,lifespan
expected=json.load(sys.stdin)
async def proof():
    async with lifespan(app):
        assert wc.get_client().collections.exists(sys.argv[1])
        assert tuning._existing_records(sys.argv[1])==expected
        assert gs.get_session(sys.argv[2])['stale']
asyncio.run(proof())
print('PASS independent API lifespan restores recovery, exact records and historical session')
"""
    env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources')}
    restarted=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child,stage,sid],input=json.dumps(before),text=True,capture_output=True,env=env,timeout=30)
    if restarted.returncode:print(restarted.stderr,flush=True)
    check(restarted.returncode==0,'fresh API process restores owned recovery and exact records: '+restarted.stderr[-200:])
    deletion=await api.delete('/collections/'+stage[:1].lower()+stage[1:])
    check(deletion.status_code==200 and deletion.json()['objects_deleted']==len(before),'explicit lowercase-alias DELETE removes the retained backend copy')
    check(not await asyncio.to_thread(paths[0].exists) and not await asyncio.to_thread(snapshot.parent.parent.exists),'explicit recovery deletion retires exactly its ownership journal and metadata snapshots')


async def additional_vectorizer_and_import_checks(api,client,name,temp,created,jobs,check):
    custom=name+'Custom'
    def create_custom():
        properties=[wc.Property(name=p.name,data_type=p.dataType,skip_vectorization=(p.name=='content')) for p in wc.COLLECTION_PROPERTIES]
        client.collections.create(name=custom,vectorizer_config=wc.Configure.Vectorizer.text2vec_ollama(api_endpoint='http://127.0.0.1:1',model=settings.embed_model,vectorize_collection_name=False),properties=properties)
        created.append(custom)
        client.collections.get(custom).data.insert(properties={'content':'Owned custom vectorizer input'},vector=[.125]*768)
    await asyncio.to_thread(create_custom)
    before=await asyncio.to_thread(tuning._existing_records,custom)
    request=await api.post('/tune/reindex',json={'collection':custom,'index_type':'flat','distance_metric':'dot'})
    assert request.status_code==202,request.text;identity=request.json()['job_id'];jobs.append((tuning,identity))
    refusal=await completed(api,'/tune/job/'+identity,expected_status='failed')
    check('vectorizer configuration' in refusal['error'] and refusal['chunks_written']==0,'actual custom property vectorization is refused before staging')
    check(await asyncio.to_thread(tuning._existing_records,custom)==before,'custom property refusal preserves real UUID/property/vector data')
    check(await asyncio.to_thread(lambda:not list(tuning.collection_recovery._root().glob('*.json'))),'custom property refusal creates no ownership or staging')
    # A distinct process registers actual import scratch, creates it, then exits
    # without Python finally. Startup ownership sweep must remove exactly it.
    parent=name+'ImportParent'
    code="from services import collection_recovery as r,weaviate_client as w; import os; o=r.begin("+repr(parent)+",'import',w.get_client()); w._create_collection_sync(o['staging'],'hnsw','cosine',{}); print(o['staging'],flush=True); os._exit(17)"
    env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources')}
    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code],env=env,text=True,capture_output=True,timeout=30)
    def records():return [json.loads(p.read_text()) for p in tuning.collection_recovery._root().glob('*.json') if json.loads(p.read_text()).get('target')==parent]
    ownership=await asyncio.to_thread(records)
    created.extend(o['staging'] for o in ownership)
    check(result.returncode==17 and len(ownership)==1 and ownership[0]['state']=='scratch','independent import scratch writer hard-exits with durable positive ownership')
    staging=ownership[0]['staging']
    check(await asyncio.to_thread(client.collections.exists,staging),'hard exit leaves the exact owned import staging collection')
    removed=await asyncio.to_thread(wc._sweep_staging_sync)
    check(staging in removed and not await asyncio.to_thread(client.collections.exists,staging),'startup removes the exact positively owned interrupted import scratch')
    check(await asyncio.to_thread(lambda:not records()),'startup completes and removes the owned import scratch journal')

async def caller_sidecar_and_legacy_tuning_checks(api,client,name,temp,created,check):
    from services import sources,ingest_config,retrieval_config
    caller=(name+'CallerDelete');caller=caller[:1].lower()+caller[1:];sid='gs_'+uuid.uuid4().hex[:8]
    await asyncio.to_thread(wc._create_collection_sync,caller,'hnsw','cosine',{})
    await asyncio.to_thread(client.collections.get(caller).data.insert,properties={'content':'Owned alias deletion'},vector=[.125]*768)
    await asyncio.to_thread(sources.store,caller,'owned.txt',b'Owned caller original')
    await asyncio.to_thread(ingest_config.save,{'collection':caller});await asyncio.to_thread(retrieval_config.save,{'collection':caller})
    await asyncio.to_thread(gs.store_session,{'session_id':sid,'collection':caller,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
    check(await asyncio.to_thread(lambda:sources.collection_dir(caller).is_dir() and (Path(temp)/'ingest_configs'/(caller+'.json')).is_file()),'actual caller-spelled source/config sidecars exist before deletion')
    response=await api.delete('/collections/'+caller)
    check(response.status_code==200 and not await asyncio.to_thread(client.collections.exists,caller),'caller-spelled HTTP deletion removes the canonical backend collection')
    check(await asyncio.to_thread(lambda:not sources.collection_dir(caller).exists() and not (Path(temp)/'ingest_configs'/(caller+'.json')).exists() and not (Path(temp)/'retrieval_configs'/(caller+'.json')).exists() and gs.get_session(sid)['orphaned']),'caller source/config paths are cleaned and matching evaluation is orphaned')
    before=await asyncio.to_thread(tuning._existing_records,name)
    child="from services import tuning,weaviate_client as w; import os; original=w._create_collection_sync; w._create_collection_sync=lambda *a,**k:(original(*a,**k),os._exit(17)); tuning._jobs['owned']={'status':'queued','chunks_written':0}; tuning._run('owned',"+repr(name)+",'reembed',{'index_type':'hnsw','distance_metric':'cosine'})"
    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child],env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources')},text=True,capture_output=True,timeout=30)
    def owners():return [json.loads(p.read_text()) for p in tuning.collection_recovery._root().glob('*.json') if json.loads(p.read_text()).get('target')==name]
    ownership=await asyncio.to_thread(owners);created.extend(o['staging'] for o in ownership)
    check(result.returncode==17 and len(ownership)==1 and ownership[0]['state']=='scratch','actual reembed worker hard-exits with positive staging ownership')
    stage=ownership[0]['staging'];check(await asyncio.to_thread(client.collections.exists,stage),'hard exit leaves exact positively owned legacy tuning staging')
    removed=await asyncio.to_thread(wc._sweep_staging_sync)
    check(stage in removed and not await asyncio.to_thread(client.collections.exists,stage) and await asyncio.to_thread(tuning._existing_records,name)==before and not await asyncio.to_thread(owners),'startup removes exact legacy tuning scratch while preserving original records')

async def main():
    token=uuid.uuid4().hex[:8]; name=owned_name(os.environ.get('RAG_TEST_PREFIX','Vfy49'),token)
    probe=name+'Probe'; sid='gs_'+token; job=None; jobs=[]; checks=0
    await bounded_poll_cases()
    for prefix in ('Vfy49','A','B'):
        if prefix:assert not owned_name(prefix,'owned').startswith(prefix)
    print('PASS parent cleanup namespace excludes verifier-owned names',flush=True)
    client=None;created=[]
    original_create=wc._create_collection_sync
    def record_create(collection,*args,**kwargs):
        original_create(collection,*args,**kwargs);created.append(collection)
    temporary=await asyncio.to_thread(tempfile.TemporaryDirectory,prefix='owned-reindex-')
    temp=temporary.name
    try:
        client=await asyncio.to_thread(wc.get_client)
        with patch.object(settings,'upload_dir',temp), patch.object(settings,'sources_dir',str(Path(temp)/'sources')), \
             patch.object(settings,'ollama_host','127.0.0.1'), patch.object(settings,'ollama_port',1), \
             patch.object(gs,'_sessions',{}),patch.object(wc,'_create_collection_sync',side_effect=record_create):
            def check(condition,label):
                nonlocal checks
                assert condition,label; checks+=1; print('PASS '+label,flush=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned') as api:
                try:
                    await asyncio.to_thread(wc._create_collection_sync,probe,'hnsw','cosine',{})
                    try:
                        await asyncio.to_thread(client.collections.get(probe).data.insert,properties={'content':'Owned endpoint refusal probe'})
                    except Exception as exc:
                        message=str(exc).lower()
                        check('connect' in message and ('127.0.0.1:1' in message or 'connection refused' in message),'actual vectorization fails against the closed embedding endpoint')
                    else: raise AssertionError('Owned embedding endpoint unexpectedly served a vector')
                    check(await asyncio.to_thread(lambda:client.collections.get(probe).aggregate.over_all(total_count=True).total_count)==0,'failed embedding probe stores no object')
                    alias=name[:1].lower()+name[1:]
                    creation=await api.post('/collections',json={'name':alias,'index_type':'hnsw','distance_metric':'cosine'})
                    check(creation.status_code==201 and creation.json()['name']==alias,'collection HTTP creation echoes a supported lowercase alias')
                    col=client.collections.get(name)
                    source=[]
                    for i in range(2):
                        identity=str(uuid.uuid4()); vector=[(i+1)/8.0]*768
                        props={'content':'Owned inert reindex '+str(i),'source_file':'owned-inert.txt','source_type':'txt','chunk_index':i,'chunk_strategy':'fixed','chunk_size':150,'chunk_overlap':0,'created_at':'2026-09-28T00:00:00Z'}
                        await asyncio.to_thread(col.data.insert,properties=props,uuid=identity,vector=vector)
                        source.append(identity)
                    before=await asyncio.to_thread(tuning._existing_records,name)
                    check({r['id'] for r in before}==set(source),'stored explicit vectors and original UUIDs are readable with embeddings unavailable')
                    session={'session_id':sid,'collection':name,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs_attempted':1,'pairs_failed':0,'pairs':[{'pair_id':'p_'+token,'question':'Owned question','answer':'Owned answer','ground_truth':'Owned truth','contexts':['Owned inert reindex'],'source_file':'owned-inert.txt','chunk_index':0,'status':'approved'}]}
                    await asyncio.to_thread(gs.store_session,session); session_bytes=await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes())
                    request=await api.post('/tune/reindex',json={'collection':alias,'index_type':'flat','distance_metric':'dot'})
                    check(request.status_code==202,'real reindex HTTP handler queues the job with closed embedding configuration'); job=request.json()['job_id'];jobs.append((tuning,job))
                    result=await completed(api,'/tune/job/'+job)
                    check(result['status']=='completed' and result['collection']==name and result['chunks_written']==len(before), 'job completes only after final backend verification: '+str(result))
                    after=await asyncio.to_thread(tuning._existing_records,name)
                    check({r['id']:r for r in after}=={r['id']:r for r in before},'UUIDs, every property and all stored vector values match exactly after reindex')
                    config=await asyncio.to_thread(wc._collection_config_sync,name)
                    check(config['index_type']=='flat' and config['distance_metric']=='dot','physical index and distance change to flat/dot')
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes and not gs.get_session(sid).get('stale')),'retained evaluation identity/content/validity are unchanged')
                    check(any('verified unchanged' in note for note in result['notes']),'completion notes truthfully report verified identity and vector preservation')
                    with patch.object(settings,'embed_model','owned-incompatible-model'):
                        request=await api.post('/tune/reindex',json={'collection':name,'index_type':'hnsw','distance_metric':'cosine'})
                        assert request.status_code==202,request.text;job=request.json()['job_id'];jobs.append((tuning,job))
                        refused=await completed(api,'/tune/job/'+job,expected_status='failed')
                    check('vectorizer configuration' in refused['error'] and refused['chunks_written']==0,'foreign deployed embedding model is refused before replacement')
                    unchanged=await asyncio.to_thread(tuning._existing_records,name)
                    config_after_refusal=await asyncio.to_thread(wc._collection_config_sync,name)
                    check(unchanged==after and config_after_refusal==config,'refused model mismatch leaves real records and physical index unchanged')
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes),'refused model mismatch preserves evaluation bytes')
                    check(await asyncio.to_thread(lambda:not list(tuning.collection_recovery._root().glob('*.json'))),'successful reindex removes its exact durable ownership; refusal creates none')
                    check(all(not item.startswith(os.environ.get('RAG_TEST_PREFIX','Vfy49')) for item in created),'all real verifier-created names remain outside the parent prefix sweep')
                    await additional_vectorizer_and_import_checks(api,client,name,temp,created,jobs,check)
                    await caller_sidecar_and_legacy_tuning_checks(api,client,name,temp,created,check)
                    await queued_ingest_checks(api,client,name,jobs,check)
                    await retained_cutover_checks(api,client,name,temp,record_create,jobs,check)
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes),'failed secondary cutover leaves the unrelated primary evaluation unchanged')
                finally:
                    if jobs:
                        pending=await settle_owned_jobs(jobs)
                        if pending:
                            receipt=await asyncio.to_thread(preserve_receipt,temp,created,pending)
                            print(f'FAIL owned jobs still active: {pending}; preserving fixtures {created} and {receipt}',flush=True)
                            # Standalone verifier: preserve active fixtures and avoid executor join.
                            os._exit(2)
                    for owned in reversed(created):
                        if await asyncio.to_thread(client.collections.exists,owned): await asyncio.to_thread(client.collections.delete,owned)
                    check(not any([await asyncio.to_thread(client.collections.exists,item) for item in created]),'cleanup removes all exact recorded verifier-owned collections')
                    gs._sessions.pop(sid,None)
    finally:
        try:
            await asyncio.to_thread(temporary.cleanup)
        finally:
            if client is not None:await asyncio.to_thread(client.close)
    print(str(checks)+' real reindex checks passed',flush=True)

asyncio.run(main())
```

### scripts/verify/compose_target.py

```python
"""Refuse an in-container verifier when RAG_API selects another deployment."""
import sys
from urllib.parse import urlsplit

def matches_local_proxy(api, bindings):
    try:
        url=urlsplit(api)
        if url.scheme!='http' or url.hostname not in ('localhost','127.0.0.1','::1') or url.username or url.password or url.path.rstrip('/')!='/api' or url.query or url.fragment:
            return False
        port=url.port or 80
        for binding in bindings.splitlines():
            host, published=binding.rsplit(':',1)
            host=host.strip('[]')
            if int(published)!=port:continue
            if host in ('0.0.0.0','::') or host==url.hostname or (host=='127.0.0.1' and url.hostname=='localhost'):
                return True
        return False
    except (ValueError,TypeError):return False

if __name__=='__main__':
    if len(sys.argv)!=3 or not matches_local_proxy(sys.argv[1],sys.argv[2]):
        print('This in-container check requires RAG_API to select this Compose proxy on a published loopback port. Remote or mismatched targets are unsupported; no backend check ran.',file=sys.stderr)
        sys.exit(2)
```

### api/services/collection_writes.py

```python
"""Serialize collection mutations within one API process.

The registry counts both owners and waiting writers. A guard spans synchronous
worker work; callers must enter it in their executor, never across an async wait.
"""
from contextlib import contextmanager
from functools import wraps
from inspect import signature
import threading

_registry_lock = threading.Lock()
_registry = {}


def canonical(collection):
    """The same first-character alias normalization used by the backend SDK."""
    return collection[:1].upper() + collection[1:]


@contextmanager
def guard(collection):
    collection = canonical(collection)
    with _registry_lock:
        entry = _registry.setdefault(collection, [threading.RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _registry_lock:
            entry[1] -= 1
            if not entry[1]:
                del _registry[collection]


def serialized(argument):
    def decorate(function):
        parameters = signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            collection = parameters.bind(*args, **kwargs).arguments[argument]
            with guard(collection):
                return function(*args, **kwargs)
        return run
    return decorate
```

### api/services/collection_recovery.py

```python
"""Durable ownership of scratch collections and retained recovery copies.

Names are never cleanup authority. Only a valid record created by this service
allows startup to remove scratch; recovery records survive until explicit
collection deletion or successful completion of their owning operation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from config import settings
from services import sources

log = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _root() -> Path:
    root = Path(settings.upload_dir) / "collection_operations"
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if created:
        _sync_dir(root.parent)
    return root


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as output:
        json.dump(record, output, indent=2, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(path)
    _sync_dir(path.parent)


def _write(record: dict) -> None:
    atomic_json(_root() / f"{record['operation_id']}.json", record)


def begin(target: str, operation: str, client) -> dict:
    if not _NAME.fullmatch(target) or operation not in ("import", "tune"):
        raise ValueError("Invalid collection operation")
    token = uuid.uuid4().hex
    marker = "__importing_" if operation == "import" else "__tuning_"
    staging = f"{target}{marker}{token}"
    if client.collections.exists(staging):
        raise RuntimeError(f"Recovery name '{staging}' is already in use")
    record = dict(version=1, operation_id=token, operation=operation,
                  target=target, staging=staging, state="scratch")
    _write(record)  # Ownership is persisted before collection creation.
    return record


def _copy(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def retain(record: dict, *, package: Path | None = None, source_collection: str | None = None) -> None:
    """Snapshot sidecars and mark recovery durably BEFORE deleting the target."""
    metadata = _root() / record["operation_id"]
    metadata.mkdir()
    target, staging = source_collection or record["target"], record["staging"]
    upload = Path(settings.upload_dir)
    _copy(package / "sources" if package else sources.collection_dir(target),
          sources.collection_dir(staging))
    for kind in ("ingest", "retrieval"):
        origin = package / f"{kind}_config.json" if package else upload / f"{kind}_configs" / f"{target}.json"
        _copy(origin, metadata / f"{kind}_config.json")
        if origin.is_file():
            config = json.loads(origin.read_text())
            config["collection"] = staging
            out = upload / f"{kind}_configs" / f"{staging}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(config, indent=2, sort_keys=True))
    if package:
        _copy(package / "goldstandard", metadata / "goldstandard")
        _copy(package / "collection.json", metadata / "collection.json")
        _copy(package / "manifest.json", metadata / "manifest.json")
    else:
        sessions = upload / "goldstandard_sessions"
        for path in sessions.glob("*.json"):
            # Preserve unreadable state rather than guessing that it is unrelated.
            try:
                belongs = json.loads(path.read_text()).get("collection") == target
            except (ValueError, OSError, AttributeError):
                belongs = True
            if belongs:
                _copy(path, metadata / "goldstandard" / path.name)
    # Sources live on a separate named volume. Flush both snapshots before the
    # state transition; a crash before the transition leaves the original safe.
    paths = [metadata, sources.collection_dir(staging)]
    paths += [upload / f"{kind}_configs" / f"{staging}.json" for kind in ("ingest", "retrieval")]
    for path in paths:
        files = list(path.rglob("*")) if path.is_dir() else [path]
        for item in files:
            if item.is_file():
                with item.open("rb") as data:
                    os.fsync(data.fileno())
        if path.is_dir():
            for directory in sorted((p for p in path.rglob("*") if p.is_dir()), reverse=True):
                _sync_dir(directory)
            _sync_dir(path)
        if path.exists():
            _sync_dir(path.parent)
    _sync_dir(upload)
    updated = {**record, "state": "recovery"}
    _write(updated)
    record.update(updated)


def discard(record: dict, client) -> None:
    """Delete an owned copy after success, or scratch while the target is safe."""
    # Persist intent before the first deletion. Startup can finish this exact
    # authorized cleanup even if backend or filesystem cleanup is interrupted.
    if record["state"] != "cleanup":
        updated = {**record, "state": "cleanup"}
        _write(updated)
        record.update(updated)
    name = record["staging"]
    if client.collections.exists(name):
        client.collections.delete(name)
    sources.delete(name)
    if sources.collection_dir(name).exists():
        raise OSError(f"Could not remove recovery sources for {name}")
    for kind in ("ingest", "retrieval"):
        (Path(settings.upload_dir) / f"{kind}_configs" / f"{name}.json").unlink(missing_ok=True)
    metadata = _root() / record["operation_id"]
    if metadata.exists():
        shutil.rmtree(metadata)
    (_root() / f"{record['operation_id']}.json").unlink(missing_ok=True)
    _sync_dir(_root())



def _read_owned_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Ownership must be a regular metadata file of at most 4096 bytes")
    record = json.loads(path.read_text())
    token = record["operation_id"]
    operation = record["operation"]
    marker = "__importing_" if operation == "import" else "__tuning_"
    if (type(record.get("version")) is not int or record["version"] != 1 or operation not in ("import", "tune")
            or not re.fullmatch(r"[0-9a-f]{32}", token)
            or path.name != f"{token}.json" or not _NAME.fullmatch(record["target"])
            or record["staging"] != f"{record['target']}{marker}{token}"
            or record["state"] not in ("scratch", "recovery", "cleanup")):
        raise ValueError("Invalid collection ownership record")
    return record


def retire_deleted(name: str, client) -> None:
    """Retire exact recovery ownership only after explicit backend deletion.

    A missing backend copy at startup does not itself authorize losing retained
    snapshots. Invalid or unrelated journals never grant cleanup authority.
    """
    for path in sorted(_root().glob("*.json")):
        try:
            record = _read_owned_record(path)
        except Exception:
            log.exception("Unreadable collection ownership %s; preserved", path)
            continue
        if record["staging"] == name and record["state"] in ("recovery", "cleanup"):
            discard(record, client)


def sweep(client) -> list[str]:
    removed = []
    for path in sorted(_root().glob("*.json")):
        try:
            record = _read_owned_record(path)
            if record["state"] == "recovery":
                log.warning("Retained recovery collection %r; sidecar snapshots: %s",
                            record["staging"], _root() / record["operation_id"])
                continue
            discard(record, client)
            removed.append(record["staging"])
        except Exception:  # Unreadable ownership never grants deletion authority.
            log.exception("Could not resolve collection ownership %s; preserved", path)
    return removed
```

### scripts/tests/test_collection_writes.py

```python
"""Writer barriers cover complete workers, nesting and independent collections."""
import os,sys,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api') if __file__!='<stdin>' else '/app'))
from services import collection_writes as writes

class WriterTests(unittest.TestCase):
    def test_same_collection_waits_until_complete_guard_releases(self):
        attempted=threading.Event();entered=threading.Event()
        def worker():
            attempted.set()
            with writes.guard('Owned'):entered.set()
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                task=pool.submit(worker);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05))
            task.result(timeout=2);self.assertTrue(entered.is_set())
        self.assertNotIn('Owned',writes._registry)
    def test_reentrant_worker_and_primitive_share_one_guard(self):
        @writes.serialized('collection')
        def primitive(collection):
            with writes.guard(collection):return writes._registry[collection][1]
        with writes.guard('Owned'):self.assertEqual(primitive(collection='Owned'),3)
        self.assertNotIn('Owned',writes._registry)
    def test_other_collections_do_not_wait(self):
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                def independent():
                    with writes.guard('Other'):return True
                self.assertTrue(pool.submit(independent).result(timeout=2))
    def test_failed_worker_releases_guard(self):
        with self.assertRaises(RuntimeError):
            with writes.guard('Owned'):raise RuntimeError('Owned failure')
        self.assertNotIn('Owned',writes._registry)
    def test_backend_case_aliases_share_the_same_guard(self):
        entered=threading.Event()
        def worker():
            with writes.guard('owned'):entered.set()
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                task=pool.submit(worker);self.assertFalse(entered.wait(.05))
            task.result(timeout=2);self.assertTrue(entered.is_set())
    def test_actual_import_and_backend_mutators_wait_before_entering_their_body(self):
        from unittest.mock import patch
        from services import importer,weaviate_client as wc
        operations=[(wc._create_collection_sync,('owned','hnsw','cosine',{}),wc,'get_client'),
                    (wc._insert_chunks_sync,('owned',[]),wc,'get_client'),
                    (wc._delete_collection_sync,('owned',),wc,'get_client'),
                    (importer._build,('owned',Path('/inert'),{},None),importer,'_create_from_package')]
        for operation,args,module,boundary in operations:
            with self.subTest(operation=operation.__name__):
                attempted=threading.Event();entered=threading.Event()
                def boundary_call(*args,**kwargs):
                    entered.set();raise RuntimeError('Owned stopped backend boundary')
                def worker():attempted.set();return operation(*args)
                with patch.object(module,boundary,side_effect=boundary_call),ThreadPoolExecutor() as pool:
                    with writes.guard('Owned'):
                        task=pool.submit(worker);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05))
                    with self.assertRaisesRegex(RuntimeError,'Owned stopped'):task.result(timeout=2)
                    self.assertTrue(entered.is_set())
                self.assertNotIn('Owned',writes._registry)

class ImportCutoverTests(unittest.TestCase):
    def execute(self,build_hook=None,restore_hook=None,delete_hook=None):
        import tempfile,json
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from services import importer,collection_recovery as recovery
        from config import settings
        task=tempfile.TemporaryDirectory();self.addCleanup(task.cleanup);root=Path(task.name)
        pkg=root/'pkg';pkg.mkdir();(pkg/'manifest.json').write_text('{}')
        backend={'OwnedImport'};deleted=[];observed=[]
        class Collections:
            def exists(self,name):return name in backend
            def delete(self,name):deleted.append(name);backend.discard(name)
        client=SimpleNamespace(collections=Collections())
        job={'status':'queued','chunks_written':0};manifest={'collection':{'name':'OwnedImport','chunk_count':1}}
        def build(name,*args):
            backend.add(name)
            if name!='OwnedImport':
                records=[json.loads(p.read_text()) for p in recovery._root().glob('*.json')]
                self.assertEqual(len(records),1);self.assertEqual(records[0]['staging'],name);self.assertEqual(records[0]['state'],'scratch');observed.append(name)
            if build_hook:build_hook(name,backend)
            return 1
        def delete(name):
            client.collections.delete(name)
            if delete_hook:delete_hook(name)
        stack=ExitStack();self.addCleanup(stack.close)
        for change in [patch.object(settings,'upload_dir',str(root)),patch.object(settings,'sources_dir',str(root/'sources')),patch.object(importer,'_jobs',{'owned':job}),patch.object(importer,'_active',{'owned.zip'}),patch.object(importer.packager,'exports_dir',return_value=root),patch.object(importer.packager,'open_package',return_value=(pkg,manifest)),patch.object(importer.packager,'verify_digests'),patch.object(importer,'_check_embedding'),patch.object(importer,'_ensure_models',return_value=[]),patch.object(importer.wc,'get_client',return_value=client),patch.object(importer.wc,'_collection_exists_sync',side_effect=lambda name:name in backend),patch.object(importer.wc,'_delete_collection_sync',side_effect=delete),patch.object(importer,'_build',side_effect=build),patch.object(importer.goldstandard,'sessions_for',return_value=[]),patch.object(importer,'_restore_sidecars',side_effect=restore_hook or (lambda *args:[]))]:stack.enter_context(change)
        return importer,job,backend,deleted,observed,root
    def test_replace_guard_spans_deleted_target_and_sidecar_restoration(self):
        from concurrent.futures import ThreadPoolExecutor
        deleted_event=threading.Event();restore_event=threading.Event();resume_delete=threading.Event();resume_restore=threading.Event();entered=threading.Event();attempted=threading.Event()
        def delete(name):deleted_event.set();assert resume_delete.wait(3)
        def restore(*args):restore_event.set();assert resume_restore.wait(3);return []
        module,job,backend,deleted,observed,root=self.execute(delete_hook=delete,restore_hook=restore)
        def writer():
            attempted.set()
            with writes.guard('ownedImport'):entered.set();self.assertIn('OwnedImport',backend)
        with ThreadPoolExecutor() as pool:
            task=pool.submit(module._run,'owned','owned.zip','replace');self.assertTrue(deleted_event.wait(2))
            waiting=pool.submit(writer);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05));resume_delete.set()
            self.assertTrue(restore_event.wait(2));self.assertFalse(entered.wait(.05));resume_restore.set();task.result(timeout=3);waiting.result(timeout=3)
        self.assertEqual(job['status'],'completed');self.assertEqual(backend,{'OwnedImport'});self.assertEqual(len(observed),1)
        self.assertEqual(list((root/'collection_operations').glob('*.json')),[])
    def test_import_staging_failure_cleans_owned_journal_and_collection(self):
        def fail(name,backend):
            if name!='OwnedImport':raise RuntimeError('Owned staging insertion failed')
        module,job,backend,deleted,observed,root=self.execute(build_hook=fail)
        module._run('owned','owned.zip','replace');self.assertEqual(job['status'],'failed');self.assertEqual(backend,{'OwnedImport'})
        self.assertNotIn('OwnedImport',deleted);self.assertEqual(list((root/'collection_operations').glob('*.json')),[])
    def test_replace_failure_retains_positive_recovery_and_startup_preserves_it(self):
        from services import collection_recovery as recovery
        def fail(name,backend):
            if name=='OwnedImport':backend.remove(name);raise RuntimeError('Owned final insertion failed')
        module,job,backend,deleted,observed,root=self.execute(build_hook=fail)
        module._run('owned','owned.zip','replace');self.assertEqual(job['status'],'failed');retained=job['error_detail']['recovered_as']
        self.assertIn(retained,backend);self.assertTrue(Path(job['error_detail']['sidecar_snapshots']).is_dir())
        self.assertEqual(recovery.sweep(module.wc.get_client()),[]);self.assertIn(retained,backend)


class DeletedRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from config import settings
        from services import collection_recovery as recovery,weaviate_client as wc,goldstandard as gs
        self.recovery,self.wc=recovery,wc
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name);stack=ExitStack();self.addCleanup(stack.close)
        for change in [patch.object(settings,'upload_dir',temp.name),patch.object(settings,'sources_dir',str(self.root/'sources')),patch.object(gs,'_sessions',{})]:stack.enter_context(change)
        self.backend={'OwnedRecovery'}
        class Collections:
            def exists(inner,name):return writes.canonical(name) in self.backend
            def delete(inner,name):self.backend.remove(writes.canonical(name))
            def get(inner,name):return SimpleNamespace(aggregate=SimpleNamespace(over_all=lambda **kw:SimpleNamespace(total_count=3)))
        self.client=SimpleNamespace(collections=Collections());stack.enter_context(patch.object(wc,'get_client',return_value=self.client))
        self.owner=recovery.begin('OwnedRecovery','tune',self.client);self.backend.add(self.owner['staging']);recovery.retain(self.owner)
    def test_explicit_alias_delete_retires_only_matching_recovery_snapshots(self):
        import json
        other=self.recovery.begin('OwnedRecovery','import',self.client);self.backend.add(other['staging']);self.recovery.retain(other)
        corrupt=self.recovery._root()/'invalid.json';corrupt.write_text(json.dumps({**self.owner,'operation_id':'invalid'}))
        name=self.owner['staging'];self.assertEqual(self.wc._delete_collection_sync(name[:1].lower()+name[1:]),3)
        self.assertNotIn(name,self.backend);self.assertFalse((self.recovery._root()/self.owner['operation_id']).exists());self.assertFalse((self.recovery._root()/(self.owner['operation_id']+'.json')).exists())
        self.assertIn(other['staging'],self.backend);self.assertTrue((self.recovery._root()/other['operation_id']).is_dir());self.assertTrue(corrupt.is_file())
    def test_caller_spelled_sidecars_and_sessions_are_cleaned_on_normal_delete(self):
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        caller='ownedRecovery';sources.store(caller,'inert.txt',b'Owned inert original')
        ingest_config.save({'collection':caller});retrieval_config.save({'collection':caller})
        session={'session_id':'gs_490abcde','collection':caller,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]};gs.store_session(session)
        self.wc._delete_collection_sync(caller)
        self.assertFalse(sources.collection_dir(caller).exists());self.assertFalse((self.root/'ingest_configs'/(caller+'.json')).exists());self.assertFalse((self.root/'retrieval_configs'/(caller+'.json')).exists())
        self.assertTrue(gs.get_session(session['session_id'])['orphaned']);self.assertIn(self.owner['staging'],self.backend)
    def test_deleting_original_preserves_distinct_retained_recovery(self):
        self.wc._delete_collection_sync('OwnedRecovery');self.assertIn(self.owner['staging'],self.backend)
        self.assertTrue((self.recovery._root()/self.owner['operation_id']).is_dir())
    def test_missing_backend_at_startup_does_not_authorize_snapshot_loss(self):
        self.backend.remove(self.owner['staging']);self.assertEqual(self.recovery.sweep(self.client),[])
        self.assertTrue((self.recovery._root()/self.owner['operation_id']).is_dir())
    def test_explicit_cleanup_failure_is_resumed_from_durable_intent(self):
        import json
        from unittest.mock import patch
        with patch.object(self.recovery.shutil,'rmtree',side_effect=OSError('Owned cleanup failure')):
            with self.assertRaises(OSError):self.wc._delete_collection_sync(self.owner['staging'])
        journal=self.recovery._root()/(self.owner['operation_id']+'.json');self.assertEqual(json.loads(journal.read_text())['state'],'cleanup')
        self.assertEqual(self.recovery.sweep(self.client),[self.owner['staging']]);self.assertFalse(journal.exists());self.assertFalse((self.recovery._root()/self.owner['operation_id']).exists())



if __name__=='__main__':unittest.main()
```

### scripts/verify/reindex_verifier_cases.py

```python
"""Owned async lifecycle and parent-cleanup regressions; no backend/model calls."""
import ast,asyncio,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
# Loaded from the host script on stdin with its helper passed alongside it.
source=Path(os.environ.get('RAG_REINDEX_VERIFIER_SOURCE',str(Path(__file__).with_name('reindex.py'))))
tree=ast.parse(source.read_text());assert isinstance(tree.body[-1],ast.Expr);tree.body.pop()
ns={'__file__':str(source),'__name__':'owned_verifier'};exec(compile(tree,str(source),'exec'),ns)

class LifecycleTests(unittest.TestCase):
    def test_failure_cleans_exact_collections_and_temp_off_loop_and_restores_cache(self):
        loop_thread=threading.get_ident();calls=[];created={};closed=[];temps=[]
        original_temp=tempfile.TemporaryDirectory
        class OwnedTemp(original_temp):
            def __init__(self,*args,**kwargs):calls.append(('temp_create',threading.get_ident()));super().__init__(*args,**kwargs);temps.append(self.name)
            def cleanup(self):calls.append(('temp_cleanup',threading.get_ident()));super().cleanup()
        def create(name,*args,**kwargs):name=ns['wc'].collection_writes.canonical(name);calls.append(('create',threading.get_ident()));created[name]=[]
        def delete(name):name=ns['wc'].collection_writes.canonical(name);calls.append(('delete',threading.get_ident()));del created[name]
        def collection(name):
            name=ns['wc'].collection_writes.canonical(name)
            def insert(properties,uuid=None,vector=None):
                if uuid is None:raise RuntimeError('connection refused 127.0.0.1:1')
                created[name].append(dict(id=uuid,properties=properties,vector=vector))
            return SimpleNamespace(data=SimpleNamespace(insert=insert),aggregate=SimpleNamespace(over_all=lambda **kw:SimpleNamespace(total_count=len(created[name]))))
        client=SimpleNamespace(collections=SimpleNamespace(get=collection,exists=lambda name:ns["wc"].collection_writes.canonical(name) in created,delete=delete),close=lambda:closed.append(threading.get_ident()))
        protected={'protected':{'session_id':'gs_11111111'}}
        with patch.object(ns['tempfile'],'TemporaryDirectory',OwnedTemp),patch.object(ns['wc'],'get_client',return_value=client),patch.object(ns['wc'],'_create_collection_sync',side_effect=create),patch.object(ns['tuning'],'_existing_records',side_effect=lambda name:list(created[ns["wc"].collection_writes.canonical(name)])),patch.object(ns['gs'],'_sessions',protected),patch.object(ns['gs'],'store_session',side_effect=OSError('Owned session failure')):
            with self.assertRaisesRegex(OSError,'Owned session failure'):asyncio.run(ns['main']())
            self.assertIs(ns['gs']._sessions,protected)
        self.assertFalse(created);self.assertEqual(len(closed),1);self.assertTrue(all(identity!=loop_thread for _,identity in calls));self.assertNotEqual(closed[0],loop_thread)
        self.assertTrue(all(not Path(directory).exists() for directory in temps))
    def test_client_failure_still_cleans_temporary_directory(self):
        original_temp=tempfile.TemporaryDirectory;temps=[]
        def create(*args,**kwargs):result=original_temp(*args,**kwargs);temps.append(result.name);return result
        with patch.object(ns['tempfile'],'TemporaryDirectory',side_effect=create),patch.object(ns['wc'],'get_client',side_effect=OSError('Owned client failure')):
            with self.assertRaisesRegex(OSError,'Owned client failure'):asyncio.run(ns['main']())
        self.assertTrue(temps);self.assertTrue(all(not Path(directory).exists() for directory in temps))
    def test_temp_creation_failure_does_not_open_client(self):
        with patch.object(ns['tempfile'],'TemporaryDirectory',side_effect=OSError('Owned temp failure')),patch.object(ns['wc'],'get_client') as client:
            with self.assertRaisesRegex(OSError,'Owned temp failure'):asyncio.run(ns['main']())
            client.assert_not_called()
    def test_parent_cleanup_preserves_exact_owned_namespace_and_receipt(self):
        prefix='VfyParent';owned=ns['owned_name'](prefix,'49000000');probe=owned+'Probe';parent=prefix+'Transfer'
        with tempfile.TemporaryDirectory(prefix='owned-parent-cleanup-') as directory:
            root=Path(directory);fixture=root/'collections.json';fixture.write_text(json.dumps({'collections':[{'name':name} for name in [owned,probe,parent]]}))
            receipt=ns['preserve_receipt'](directory,[owned,probe],[('tuning','owned-job','running')]);data=json.loads(Path(receipt).read_text())
            self.assertEqual(data['created_collections'],[owned,probe]);self.assertEqual(data['pending_jobs'],[['tuning','owned-job','running']])
            lib=Path(os.environ.get('RAG_VERIFIER_LIB',str(source.with_name('lib.sh'))))
            code='source "$1"; PREFIX="$2"; REPO_ROOT="$3"; api_get(){ cat "$4"; }; drop_collection(){ printf "%s\\n" "$1"; }; cleanup_prefixed'
            # api_get's function arguments differ from the script's, so retain
            # the controlled input path in a distinct variable before defining it.
            code=code.replace('api_get(){ cat "$4"; }','owned_fixture="$4"; api_get(){ cat "$owned_fixture"; }')
            run=subprocess.run(['bash','-c',code,'owned',str(lib),prefix,directory,str(fixture)],text=True,capture_output=True,cwd=directory)
            self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(run.stdout.splitlines(),[parent]);self.assertTrue(Path(receipt).exists())
        with self.assertRaises(ValueError):ns['owned_name']('','owned')

if __name__=='__main__':unittest.main()
```
