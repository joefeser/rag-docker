# RAG Docker — Export / Import Package Analysis

**Status:** analysis, pending specification
**Scope:** exporting a built RAG corpus so it can be moved, shared and re-tuned in another environment, and importing one back
**Related:** `ANALYSIS.md` (platform), `SPECIFICATIONS.md` §11 (offline distribution)

---

## 1. Executive Summary

The platform can already move the **system** — `package-offline.sh` ships images
and model weights so the stack runs anywhere with no network. What it cannot move
is the **content**: a collection someone has actually built, with its chunks,
vectors, ingest settings and evaluation data. This analysis covers that gap in
both directions, plus the tuning that makes an import worth doing.

Two findings dominate, and both are prerequisites rather than parts of the
feature.

The first: **uploaded source documents are deleted
after ingest** — `api/services/ingest_pipeline.py:108` does
`shutil.rmtree(tmp_dir)` in a `finally` block. Only the chunked text survives,
inside Weaviate. Of the four required tuning capabilities, one becomes impossible
(re-chunking), one can only be approximated (re-embedding works from chunk text
but cannot move chunk boundaries), and one carries a coherence caveat (documents
added later would be chunked by whatever settings are current, not the ones the
corpus was built with). Only adjusting retrieval is unaffected. That makes
**source-document retention a prerequisite, not an enhancement**: it must land
before or alongside export, or packages will permanently under-serve the workflow
they exist for.

The second: **retrieval settings are never persisted** — which matters here
because the package is meant to ship a runnable `retrieve.py` carrying "the
correct parameters", and there is currently nowhere to read them from. Mode, `top_k`, `alpha`
and `ef` live only in the browser's `sessionStorage`, so there is nothing on the
server to export, and a user's tuning does not survive closing the tab. A corpus
exported without them arrives as vectors the recipient must re-tune from scratch.

A third point is structural rather than a gap: **vectors are only meaningful to
the model that produced them**. An export therefore carries an embedding-model
identity, and import is a compatibility negotiation rather than a file copy.

---

## 2. Goals and Non-Goals

### Goals

- Export one collection as a single self-describing file that can be copied to
  another machine.
- Include a runnable Python retrieval script in the package, carrying the
  parameters the corpus was tuned for, so the recipient can query it immediately.
- Import such a package into another instance, with a clear outcome when the
  collection already exists.
- Support tuning after import: retrieval settings, re-chunking, re-embedding, and
  continued ingestion.
- Name packages predictably, and ship a `README.md` **inside every export** that
  explains the conventions and the import procedure.
- Provide an in-app help page covering both directions.
- Work offline, in keeping with the rest of the project.

### Non-Goals (Current Phase)

- **Cross-platform vector portability guarantees.** Packages target this stack.
- **Incremental or differential export.** Every export is a full snapshot.
- **Encryption or signing.** Packages are plain archives; corpora may be
  sensitive, and that is the operator's call (Section 10).
- **Merging two corpora into one collection.** Import creates or replaces; it
  does not reconcile.
- **Migrating between vector stores.** Weaviate only.

---

## 3. Confirmed Decisions

| Decision | Choice |
|---|---|
| Source documents | **Retain them** — add storage so exports can carry originals |
| Package scope | **One collection per package** |
| Models in package | **Optional, off by default**; identity always recorded |
| Tuning after import | **All four**: retrieval settings, re-chunk, re-embed, continue ingesting |

---

## 4. What the System Persists Today

| Volume | Size (measured) | Contents |
|---|---|---|
| `weaviate_data` | 652 KB | Vector index, chunks, collection schema |
| `ollama_models` | 2.3 GB | phi3.5 and nomic-embed-text weights |
| `ingest_uploads` | 28 KB | `collection_registry.json`, `ingest_configs/`, `goldstandard_sessions/`, `metrics.jsonl` |

A stored chunk carries: `content`, `source_file`, `source_type`, `chunk_index`,
`chunk_strategy`, `chunk_size`, `chunk_overlap`, `created_at` — plus its vector.

**What is absent:** the uploaded documents themselves. The pipeline writes them
to a temp directory under `UPLOAD_DIR`, parses them, and deletes the directory.
`source_file` survives only as a string on each chunk.

---

## 5. Two Gaps That Block the Feature

### 5.1 Source documents are not retained

#### What it costs, capability by capability

| Tuning capability | Needs | Possible today? |
|---|---|---|
| Adjust retrieval settings | existing vectors | **Yes** |
| Re-chunk | original documents | **No** |
| Re-embed with another model | chunk text (minimum) or originals (correct) | Partial — see below |
| Add documents to an imported collection | the embedding model, not originals | Yes, but new chunks must match the original chunking to be coherent |

Re-embedding from stored chunk text looks workable and is a trap. With the
default `overlap` strategy, adjacent chunks share `chunk_overlap` characters, so
concatenating them reproduces duplicated text; and chunk boundaries are frozen at
whatever the first ingest chose. Re-embedding chunk-by-chunk is legitimate —
vectors change, boundaries do not — but it cannot be combined with re-chunking,
and the distinction must be explicit to the user rather than implied.

#### What retention requires

A volume (`rag_sources`, or a subtree of `ingest_uploads`) holding the original
bytes keyed by collection, written during ingest instead of deleted. Consequences
the specification must settle:

- **Disk.** Storage becomes proportional to the corpus, not to the index. A
  500 MB document set currently leaves a few MB of chunks; after retention it
  costs 500 MB plus the index. The Docker disk guidance in `README.md` assumes
  the former.
- **Deletion semantics.** `DELETE /collections/{name}` must also remove retained
  sources, or storage leaks silently.
- **Duplicate uploads.** The same file ingested twice: stored twice, or
  content-addressed and stored once?
- **Existing collections.** Anything ingested before retention ships has no
  sources. Exports of those collections are chunk-only and must say so in the
  manifest rather than appearing complete.

That last point matters more than it looks: it means packages have **two fidelity
levels** from day one, and import must handle both.

### 5.2 Retrieval settings are not persisted at all

The brief asks to export "the RAG, LLM, retrieval scripts" — the script being
runnable Python that queries the corpus with the correct parameters (Section 6).
Generating such a script requires knowing what those parameters are, and that is
where the problem lies.

`ui/src/context/QueryConfigContext.tsx:40` writes `rag_query_config` to the
browser's `sessionStorage`, and that is the only place retrieval mode, `top_k`,
`alpha` and `ef` exist. There is no server-side store and no endpoint: the API
surface has `/ingest/config` for chunking and nothing equivalent for retrieval.

Two consequences:

- **There is currently nothing on the server to export.** A package can record
  the chunking configuration, because `ingest_config.json` is persisted per
  collection; it cannot record how the corpus is meant to be queried.
- **The settings do not even survive the browser.** `sessionStorage` is scoped to
  one tab, so a user's tuning is lost when they close it — independently of
  export. Someone who tunes a corpus carefully today has nothing durable to show
  for it.

So a `retrieve.py` generated today could only carry the API's defaults —
`hnsw`, `top_k=5`, `alpha=0.75` — regardless of what the corpus was actually
tuned to. It would run, return plausible results, and quietly not be the
configuration anyone chose. That is worse than shipping no script at all, because
nothing signals the discrepancy.

**Persisting retrieval settings per collection is therefore the second
prerequisite.** It is smaller than retention: a JSON file beside
`ingest_config.json`, a GET and a POST, and the UI reading from it instead of
`sessionStorage`. It also fixes a defect that exists independently of export —
today a user's tuning dies with the browser tab.

---

## 6. What a Package Contains

```
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>/        # see Section 7
├── README.md              # conventions + import procedure (ships in every export)
├── manifest.json          # machine-readable identity, provenance, integrity
├── collection.json        # schema, index type, distance metric, HNSW params
├── chunks.jsonl           # one JSON object per chunk, vector included
├── ingest_config.json     # saved chunking defaults, if any
├── retrieval_config.json  # mode, top_k, alpha, ef — requires the §5.2 work
├── retrieve.py            # runnable client: queries this collection with those parameters
├── goldstandard/          # evaluation sessions for this collection
├── sources/               # original documents — present only in full-fidelity packages
└── models/                # embedding/LLM weights — present only when explicitly included
```

### The retrieval script

The package ships a working `retrieve.py` so the receiving environment can query
the corpus immediately, with the parameters this corpus was tuned for, without
reading documentation or reconstructing settings by hand. It is generated at
export time from `manifest.json` and `retrieval_config.json`, so it cannot
disagree with them.

Three design questions, none of which have obvious answers:

**What does it talk to?** Two options: the RAG API (`POST /query`), or Weaviate
and Ollama directly. The API is the right target — it is the interface the
platform already supports, and going direct would mean reimplementing the
reformulate → embed → retrieve → synthesise pipeline in the script, which would
then drift from `rag_pipeline.py`. The consequence is that `retrieve.py`
**requires the stack to be running**; it is not a standalone RAG.

**What may it depend on?** A script that needs `pip install` is a script that
fails in an air-gapped environment, which is the one this project targets. It
should use **only the standard library** — `urllib.request` and `json` are
sufficient for a JSON POST. Reaching for `requests` or `httpx` would be more
pleasant to write and strictly worse for the recipient.

**How are parameters supplied?** Baking them in makes the script authoritative
but unchangeable; taking them all as flags makes it generic but pointless. The
useful shape is **defaults baked in from the export, overridable by flag** — so
running it bare reproduces the tuned behaviour, and a flag lets the recipient
experiment without editing code.

```
python retrieve.py "who approves overtime?"
python retrieve.py --top-k 10 --mode hybrid "who approves overtime?"
python retrieve.py --api-url http://localhost:9090/api "..."
```

It must also fail legibly, because it will be the first thing a recipient runs:
a stack that is not up, a collection that was never imported, and an embedding
model that does not match the manifest are all foreseeable, and each deserves a
sentence rather than a traceback.

Whether the script should also expose an importable function — so it can be
dropped into someone's own code rather than only run from a shell — is left to
the specification.

### Why JSONL rather than a Weaviate volume tarball

Copying `weaviate_data` would be simpler and exact. It is the wrong choice here:

| | Volume tarball | `chunks.jsonl` |
|---|---|---|
| Fidelity | byte-exact | exact for content and vectors |
| Weaviate version | **locked to 1.39.4** | survives upgrades |
| Granularity | whole instance | one collection |
| Re-chunk / re-embed | not addressable | straightforward |
| Inspectable | opaque | greppable, diffable |

The decision to support tuning is what settles this. A tarball can be restored but
not reasoned about; the entire point of importing is to change something.

The cost is import time: objects are inserted through the client in batches
rather than dropped in as an index. For corpora of this scale that is acceptable,
and it is the price of the portability that makes the feature useful.

### Size arithmetic (measured inputs)

`nomic-embed-text` produces **768 dimensions** (measured against the running
stack). As JSON floats a vector costs roughly 7–9 KB; as float32 it would be
3 KB. Per 10,000 chunks:

Measured on synthetic records of that shape (768-dim vectors, ~1 KB of text,
full metadata), per 10,000 chunks:

| Component | Measured |
|---|---|
| Vectors as JSON text | 76 MB |
| Chunk text and metadata | 12 MB |
| **Chunk-only package, raw** | **88 MB** |
| **Chunk-only package, gzipped** | **31 MB** |
| Source documents | as large as the corpus |
| Models, if included | +2.5 GB |

Vectors dominate — about 85% of an export before compression.

**A float32 sidecar is not worth it.** The obvious size lever is storing vectors
as packed float32 rather than JSON text, and uncompressed it looks decisive:
29 MB against 76 MB. After gzip the advantage nearly vanishes — 27 MB against
31 MB, about 12% — because JSON floats are digit text and compress 2.5×, while
packed float32 is close to random bytes and compresses only 1.08×. Twelve percent
does not justify a binary sidecar, a second format to version, and vectors that
can no longer be inspected with `grep`. The specification should keep JSONL and
record this measurement so the question is not reopened on intuition.

---

## 7. Naming Conventions

Every package is one file named:

```
ragpkg-<collection>-<YYYYMMDDTHHMMSSZ>-<id8>.tar.gz
```

| Part | Rule | Purpose |
|---|---|---|
| `ragpkg-` | fixed prefix | distinguishes content packages from `rag-docker-offline.tar`, which ships the system |
| `<collection>` | stored collection name, lowercased, non-alphanumerics → `-` | human identification |
| `<YYYYMMDDTHHMMSSZ>` | UTC, basic ISO 8601 | sorts chronologically as text; no colons, which are awkward in filenames |
| `<id8>` | first 8 hex of the manifest digest | distinguishes two exports made in the same second, and ties the filename to its contents |

The archive expands to a directory of the same name, so extracting several does
not collide.

**Collection names are not filename-safe and not round-trippable.** Weaviate
capitalises the first letter (`myDocs` is stored as `MyDocs`), and the slug
lowercases it. The filename is therefore a label; `manifest.json` carries the
authoritative name, and import uses the manifest, never the filename. Anything
else reintroduces the case-normalisation trap already documented in
`README.md`'s troubleshooting.

### `manifest.json`

Identity and provenance the import must check:

- package format version; created-at; exporting platform version
- collection name (authoritative), chunk count, source-document count
- **embedding model name and dimensions**; LLM name
- Weaviate version; chunking strategy and parameters
- fidelity: `chunks-only` or `with-sources`
- whether models are bundled
- SHA-256 per file, so import can detect truncation or tampering

---

## 8. Import: Compatibility and Collisions

Import is a negotiation, not a copy. Three checks, in order:

| Check | If it fails |
|---|---|
| Package integrity (checksums, format version) | refuse; the package is damaged or from a newer format |
| **Embedding model and dimension match** | refuse by default — vectors from another model are silently meaningless, not merely different. Offer re-embedding when the package has sources or chunk text |
| Collection name already exists | ask: import under a new name, replace, or abort |

Silent acceptance of a dimension mismatch is the worst available outcome: it
produces a collection that answers every query confidently and wrongly. This is
the same class of failure as the Weaviate client/server mismatch already seen on
this project, where `/health` reported green while every call failed — and it
argues for refusing loudly rather than coercing.

### Tuning paths after import

| Goal | Requires | Mechanism |
|---|---|---|
| Different retrieval mode / top_k / alpha | nothing | pass them per request on `POST /query`, which already accepts all three. Making a choice *stick* for the collection is the §5.2 work |
| Different index type or distance metric | re-create collection, re-insert vectors | vectors reused, no model needed |
| Different chunk size / overlap / strategy | `sources/` | re-ingest from retained originals |
| Different embedding model | `sources/` (preferred) or chunk text | re-embed; dimension changes, so the collection is rebuilt |
| More documents | matching embedding model | normal ingest into the imported collection |

---

## 9. Surfaces

Export and import need a trigger, and the choice affects the specification more
than it first appears.

- **API endpoints** (`POST /export/{collection}`, `POST /import`) fit the existing
  architecture and are reachable from the UI and any client.
- **Large payloads.** A package can be hundreds of megabytes or larger. Streaming
  a download and a multipart upload is workable, but the ingest pipeline's
  experience suggests a **job-and-poll** pattern (as `/ingest/upload` already
  uses) rather than a blocking request.
- **Where packages live.** A mounted directory (`./exports`) lets large files
  bypass HTTP entirely, and makes it obvious where to find them. The project
  already has the pattern in `./ingest-inbox`, the read-only mount left in place
  for the parked MCP server. The trade-off is another bind mount to document.

### Help page

A new route under the AI Engineer and Developer roles, alongside Import and
Collections. It must cover: what a package contains and what it deliberately
omits; the naming convention; the two fidelity levels and why an older collection
exports without sources; the compatibility rules, especially the embedding-model
one; the tuning paths table; and **how to run `retrieve.py`**, since that is the
first thing most recipients will do and the fastest way to confirm an import
worked. The in-export `README.md` and this page should
be generated from one source, or they will diverge. This project has already
found **five** places where documentation and implementation had drifted apart —
two response shapes and three URL paths — and three of those were affecting the
running UI, two of them silently.

---

## 10. Constraints and Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | Retention lands late | Exports permanently chunk-only: re-chunking impossible, re-embedding only approximate, added documents incoherent with the original chunking | Sequence retention first (§5.1) |
| 1b | Retrieval settings never persisted | Packages carry vectors but not how to query them; user tuning lost on tab close | Persist per collection before export ships (§5.2) |
| 2 | Dimension mismatch accepted silently | Confidently wrong answers | Refuse by default; require explicit re-embed |
| 3 | Disk growth after retention | Machines sized on current guidance fill up | Re-measure and update the 20/32 GB guidance |
| 4 | Sources retained but not deleted with the collection | Silent storage leak | Deletion must cascade |
| 5 | Package README and help page drift | Users follow stale instructions | Generate both from one source |
| 6 | Vectors dominate package size (~85% pre-compression) | Unwieldy packages for large corpora | Gzip gives 2.8x; a float32 sidecar was measured at only ~12% further and rejected |
| 7 | Filename treated as authoritative | Case-normalisation bugs return | Manifest is authoritative; filename is a label |
| 8 | Unencrypted corpora | Sensitive documents travel in the clear | State it plainly in the export README |
| 9 | Import partially applied then fails | Half a collection, no error surfaced | Import into a temporary name, promote on success |
| 10 | `retrieve.py` generated before §5.2 lands | Script runs and returns plausible results using API defaults, not the tuned configuration, with nothing signalling the difference | Do not ship the script until retrieval settings are persisted; until then omit it rather than generate a misleading one |
| 11 | Collection re-tuned after export | The shipped script no longer reflects the corpus it came from | Script records the export timestamp and manifest id it was generated from, so a mismatch is visible |

---

## 11. Open Questions for the Specification

1. **Where do sources live** — a new `rag_sources` volume, or a subtree of
   `ingest_uploads`? Affects backup and the offline bundle.
2. **Duplicate documents** — content-address and store once, or store per ingest?
3. **Package transport** — mounted `./exports` directory, HTTP streaming, or both?
4. **Retention retrofit** — is there any value in a one-off tool that rebuilds
   approximate sources from stored chunks for pre-retention collections, or is
   `chunks-only` simply their permanent fidelity?
5. **Gold-standard portability** — sessions reference chunk ids; do they survive a
   re-chunk, or are they invalidated and flagged?
6. **Gold-standard sessions of a replaced collection** — when import replaces an
   existing collection, are that collection's local sessions discarded, kept and
   orphaned, or refused until the user resolves them?

*(Compression was an open question and is now settled: gzip throughout. The
measurement in Section 6 shows 2.8x on the package and only ~12% further from a
float32 sidecar, which is not worth a second format.)*

---

## 12. Conclusion

The export half is mostly mechanical: read a collection with
`include_vector=True`, write JSONL, add a manifest. The import half carries the
real design weight, because it must refuse confidently wrong outcomes rather than
produce them.

The work is gated on two things outside the feature itself, and neither is
visible from the brief.

**Retaining source documents** is a small change to the ingest pipeline with a
large consequence for disk, deletion and the existing sizing guidance — and
without it, re-chunking is impossible, re-embedding is only approximate, and
documents added later will not match how the corpus was built. Every collection
ingested before it exists will carry a permanently lower fidelity that packages
must declare honestly.

**Persisting retrieval settings** is smaller and easier to overlook, because
nothing today reveals that they only exist in a browser tab. Until they are
stored per collection, an export can describe how a corpus was built but not how
it is meant to be queried, and the tuning a user does is lost the moment they
close the tab.

Both should be specified and landed before export ships. The export mechanism
itself is straightforward; what makes it worth building is having something
complete to put in the package.
