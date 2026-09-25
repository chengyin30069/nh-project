# nh-project Library Assistant — implementation baseline and V2/V3 design

> Updated: 2026-09-26  
> Status: V1 implemented; V2/V3 specified but not implemented  
> Repository: `chengyin30069/nh-project`  
> Deployment snapshot: 15,376 local metadata embeddings; this is an observation, not a fixed catalog size  
> Runtime: existing Python server + SQLite + NumPy + NVIDIA hosted NIM; no local GPU  
> User interface: dedicated `/AI_assistant` page, under the configured deployment base path

## 0. Revision summary

### 0.1 Decisions superseding the initial specification

1. The assistant is a **dedicated page**, not a sidebar. Library/proxy/reader pages provide a navigation link only.
2. **Every accepted V1 recommendation returns HTTP 202**. The browser polls a short-lived in-memory request, so model inference does not hold a reverse-proxy HTTP connection open.
3. The default online pipeline is parser → query embedding → bounded local fusion/ranking → authoritative verification. **Quality-model reranking is optional and disabled by default.**
4. Interactive NIM waits are capped at **20 seconds per call and 40 seconds per running recommendation**, including remote-scheduler queue waits. These are not end-to-end service-level guarantees: recommendation admission queue time, local SQL/vector/I/O work and browser polling are additional.
5. The parser has a 600-token output cap. Optional reranking has a 250-token cap and returns IDs only. For configured Nemotron 3/3.5 chat models, the provider requests `enable_thinking=false`.
6. Only **400 candidate metadata records plus final selected-record verification** are materialized per query. SQL may still read all eligible IDs, and vector search still evaluates the matrix; the implementation does not stat/read all 15k CBZ records per query.
7. Durable metadata jobs and ephemeral recommendation requests are different systems. V3 must add durable **parent requests and dependencies**, not keep a V1 request worker occupied for minutes/hours.
8. V2 ordinary searches use **precomputed visual summaries only**. Missing summaries do not trigger hidden image uploads or block a recommendation.
9. V3 new page analysis is an **explicit, budgeted user action**. A parser-produced narrative concept is not upload authorization.
10. API-key presence, connection verification, initialization failure and disabled configuration are reported separately.

### 0.2 Reading this document

- **Current / V1** describes the current working-tree implementation, primarily `server/assistant/`, `server/library_db.py`, `server/nh_server.py` and the shared UI assets.
- **Planned / V2 / V3** describes required future work. Proposed settings, routes and tables do not exist merely because they appear here.
- Current config validation rejects unknown keys. Do not paste the proposed V2/V3 settings into today's config.
- Historical provider references are listed in section 43. This revision aligns local implementation and design; it does not newly certify hosted model availability, free quotas or multimodal capabilities.

### 0.3 Measured progress and limits

The implementation session compared the same local-only `english` query against the approximately 15k catalog: **3.111 s before, 0.153 s after**, with five results in each run. This was one sequential measurement, not a cold-cache benchmark, percentile distribution, relevance evaluation or hosted-NIM end-to-end measurement.

Deterministic Python and four browser suites covered the implementation. Deployment checks confirmed HTTP 200 for the dedicated page and retention of the metadata index. Real hosted latency after the optimization has **not** been remeasured. Host I/O contention was also observed during deployment; asynchronous HTTP alone does not eliminate disk or provider delays.

## 1. Existing architecture to preserve

### 1.1 Integration boundary

Keep `ThreadingHTTPServer`, `LocalLibrary`, `LibraryDatabase`, server-generated HTML, `local-ui.js/css`, current Docker deployment and same-origin `/_nh-local/api/` endpoints. Do not add a separate FastAPI/React application, Redis, Celery or external vector service.

- Port 8765: existing download/extension API, unchanged.
- Port 8766: library/proxy/reader plus `/AI_assistant` and assistant APIs.
- Apply the configured base path to **page, assets, API polling and gallery links**. For example `/nh/AI_assistant` when `base_path=/nh`; `/AI_assistant` when no prefix is configured.
- Source metadata and downloaded CBZs remain authoritative. AI-derived data never overwrites source taxonomies.

### 1.2 Current execution model

```text
Browser: /AI_assistant
  POST /_nh-local/api/assistant/recommend -> 202 + job_id
  GET  /_nh-local/api/assistant/jobs/{job_id} -> progress/result
                |
        ThreadingHTTPServer
                |
        AssistantService
          |-- RequestQueue: one worker, <=2 unfinished requests, ephemeral
          |     `-- Recommender -> LibraryDatabase + VectorIndex
          |                        `-- provider interface -> NIM scheduler
          |-- AssistantIndexer: durable metadata jobs + scan checkpoints
          |     `-- same NIM scheduler
          `-- AssistantDatabase: assistant.sqlite3
```

The request worker is distinct from the remote scheduler worker. SQL work and candidate verification run locally. An HTTP handler enqueues or reads a snapshot; it does not run recommendation inference inline.

### 1.3 Isolation and failures

Normal browsing, downloading and reading must remain usable if the assistant is disabled, lacks a key, cannot initialize, or encounters provider/model errors. No NIM call occurs merely to probe models during server startup. A diagnostic connection check is user-triggered.

## 2. Source tree and dependencies

### 2.1 Current modules

| Module | Responsibility |
|---|---|
| `server/assistant/settings.py` | defaults and strict assistant config validation |
| `schema.py` | validated request and QueryPlan contracts |
| `provider.py` | provider-neutral DTOs/Protocol, `FakeModelProvider` |
| `nim_client.py` | HTTP/auth, priority scheduling, retries, circuit breaker, interactive budgets |
| `diagnostics.py` | sanitized initialization/provider errors |
| `db.py` | sidecar schema, durable embedding jobs, generation and scan state |
| `documents.py` | deterministic metadata text and compact candidate packets |
| `vector_index.py` | immutable NumPy metadata snapshots |
| `indexer.py` | incremental/full metadata indexing and lease renewal |
| `recommender.py` | validated plans, hard filters, bounded fusion, verification |
| `requests.py` | bounded ephemeral recommendation queue and progress snapshots |
| `service.py` | lifecycle, health/check, indexing and recommendation facade |
| `server/nh_server.py` | routing, server-rendered page shell, lifecycle wiring |
| `server/library_db.py` | authoritative helper queries and update/delete callbacks |
| `server/static/local-ui.js/css` | dedicated workspace and shared navigation link |

### 2.2 Dependencies and planned additions

V1 uses standard-library HTTP/JSON, PyYAML and NumPy. Docker installs `py3-yaml` and `py3-numpy`. No OpenAI SDK is required.

V2 adds Pillow and `server/assistant/images.py`. Add a focused durable-request coordinator module for V3 if necessary; extend the existing provider and database rather than introducing a second service. Add image/schema/job tests alongside existing tests.

## 3. Configuration and deployment

### 3.1 Current supported defaults

This block must match `server/assistant/settings.py`; it intentionally defaults to disabled:

```yaml
assistant:
  enabled: false
  provider: nvidia_nim
  api_base: https://integrate.api.nvidia.com/v1
  api_key_env: NVIDIA_API_KEY
  parser_model: nvidia/nemotron-3.5-lightning-30b-a3b
  quality_model: nvidia/nemotron-3-super-120b-a12b
  embedding_model: nvidia/nemotron-3-embed-1b
  visual_model: z-ai/glm-5-3-flash
  result_limit: 5
  dense_candidate_count: 80
  rerank_candidate_count: 12
  rerank_enabled: false
  request_timeout_seconds: 75
  interactive_timeout_seconds: 20
  interactive_budget_seconds: 40
  max_retries_interactive: 0
  max_retries_background: 6
  background_enabled: true
  max_remote_concurrency: 1
  min_request_interval_ms: 1600
  remote_image_analysis_enabled: false
  max_remote_image_edge: 896
  max_remote_image_bytes: 524288
```

The visual settings are accepted placeholders in V1; **V1 sends no page images even if the image flag is true**. Setting `visual_model` does not mean a visual index is implemented or that this model has passed an image smoke test.

Current validation includes typed values, HTTPS API base without URL credentials/query/fragment, result limit <=5, dense count <=1000, rerank count <=100, interactive retries <=3, interactive per-call timeout <=60 s and total budget <=120 s. Recommendation materialization still caps at 400 even if dense count is increased.

### 3.2 Key and configuration lifecycle

The real key is read only from the named process environment variable. For Compose, `.env` can supply `NVIDIA_API_KEY`; `.env.example` is a template. `.env` is ignored by Git and excluded from Docker builds. Direct Python execution does not automatically load Compose's `.env`.

Configuration/environment are startup snapshots. YAML is mounted read-only, not baked into the image. After editing YAML or `.env`, recreate the container:

```sh
docker compose up -d --build --force-recreate nh-server
```

Restart alone does not reload Compose environment values. Editors that replace a file by rename may also leave a running single-file bind mount pointing at the previous inode. Recreating refreshes the mount. Preserve the deployment's actual storage mount and UID/GID when doing so.

Never expose key values or raw provider exception bodies in HTML, JS, API responses or logs. The environment-variable **name**, loaded/not-loaded boolean and selected config filename may be shown for diagnosis.

### 3.3 Planned V2/V3 settings — not accepted by V1

Add validation and examples only when the corresponding phase is implemented. Proposed initial defaults:

| Setting | Initial value | Meaning |
|---|---:|---|
| `visual_index_on_new_gallery` | false | opt-in indexing of subsequent downloads |
| `visual_lazy_index_enabled` | false | optional background policy; never a fast-query dependency |
| `visual_candidate_count` | 80 | top visual-summary results before bounded fusion |
| `max_remote_images_per_request` | 6 | must also respect actual selected model capability |
| `max_remote_request_bytes` | 5242880 | total serialized payload, including base64 |
| `visual_request_timeout_seconds` | 30 | one VLM attempt, to bound head-of-line blocking |
| `deep_enabled` | false | explicit deep-analysis feature gate |
| `max_deep_books` | 4 | initial books per durable request; validated maximum 8 |
| `max_new_windows_per_book_per_request` | 3 | default additional windows, not whole-book completion |
| `max_deep_remote_attempts` | 24 | shared attempt budget including retries/repairs/embeddings |
| `deep_max_active_seconds` | 900 | execution/queue/cooldown budget after parent activation; excludes explicit pause |
| `deep_request_retention_days` | 7 | terminal parent/request payload retention; independent of reusable documents |

The image flag and feature-specific authorization are both required. Turning a flag on must not automatically submit a full rebuild. Increasing timeouts/concurrency is not the default fix for slow hosted endpoints.

## 4. Provider and scheduling

### 4.1 Provider contract

`ModelProvider` exposes:

```python
chat(*, model, messages, max_tokens, temperature, purpose) -> ChatResult
embed_texts(*, model, texts, input_type, purpose) -> list[list[float]]
```

`ChatResult` contains text and usage. `ProviderError` contains a sanitized code, optional HTTP status, retry delay and transient flag. Application logic does not use NVIDIA credentials or endpoint-specific payload handling.

The NIM adapter additionally provides `interactive_budget()`; `AssistantService` uses that context when available. V2/V3 must extend provider-neutral capability/error handling, not add NVIDIA logic inside the recommender.

### 4.2 HTTP and payload rules

- Chat: `POST {api_base}/chat/completions`; embedding: `POST {api_base}/embeddings`.
- Auth: server-side bearer header. Redirects are rejected rather than forwarding credentials.
- Embedding uses `input_type=passage` for documents and `query` for searches, `encoding_format=float`, `truncate=END`.
- Validate returned count, ordered indexes, dimensions and finite values; store actual dimensions, not a fixed constant.
- For supported Nemotron 3/3.5 model-name prefixes the adapter adds `chat_template_kwargs: {enable_thinking: false}`. This is provider-specific, not part of the generic prompt schema.
- V2 requires a small opt-in image compatibility check for the configured visual model before a bulk job: multi-image format, page-order interpretation, image count, payload limits, output JSON and refusal behavior. A configured model ID alone is insufficient evidence of support.

### 4.3 Priority and deadline semantics

Current scheduler priorities:

| Purpose | Priority |
|---|---:|
| query plan (`parse`) | 0 |
| query embedding (`query`) | 1 |
| optional interactive rerank (`rerank`) | 2 |
| metadata embedding (`embed_metadata`) | 10 |

There is **one** remote worker, even if the configured concurrency ceiling is greater than one. This stays within the ceiling; increasing the setting does not currently create more workers. The queue is selected by priority among due tasks. Minimum request interval and provider-wide Retry-After cooldown apply before dispatch.

For a running recommendation, each call receives the smaller of the per-call timeout and remaining interactive budget. The budget includes waiting behind background work, interval delays and retries. Expired queued calls are removed. An already-sent HTTP request is not forcibly cancelled; its caller may stop waiting and return fallback. Late completion must not publish results into an abandoned future or crash the scheduler.

HTTP retries default to zero for interactive calls. A JSON repair is a separate application-level request, still subject to the same total budget. Background retries are durable and owned by the indexer; they must not hold the scheduler sleeping through backoff.

**Planned:** add explicit purpose mappings for `deep_chunk` (3), deep aggregation/rerank (4), visual work (20) and maintenance (30). Unknown purposes currently fall back to 10, so merely naming a new job does not implement those priorities. Each chunk is separately schedulable; a whole book must never monopolize the remote worker. A sent visual call may still delay an interactive call until the shorter caller budget expires; document this limitation rather than promising preemption of in-flight HTTP.

### 4.4 Failures and circuit breaker

429/502/503/504 and network timeouts are transient; auth/invalid payload/model failures are not blindly retried. Honor numeric or HTTP-date Retry-After, use exponential backoff plus jitter for durable jobs, and make refusals distinct from outages.

After five consecutive transient failures, the provider is degraded for 60 seconds. During that period calls can fall back immediately. A later successful call closes the circuit; there is no perpetual background model probe.

### 4.5 Logging and timing

The adapter emits sanitized INFO-level records with purpose, model, latency, status, retry count, input bytes, image count and numeric token usage. Configure logging to collect those records; do not assume every deployment already retains them. Never log keys, full prompts/model outputs or image bytes by default.

Current responses include `elapsed_seconds` and `timings` for parse, filters, semantic, local search, rerank and verification. `elapsed_seconds` starts when the request worker executes, not when the browser submitted it. V2/V3 should add admission wait, remote wait/HTTP, decode, snapshot refresh and durable retry metrics without exposing user text.

## 5. Sidecar database and migration boundary

### 5.1 Current tables (`PRAGMA user_version=1`)

Path: `<storage>/.nh-local/assistant.sqlite3`. Connections use short transactions; network and file decoding occur outside them. There are no cross-database foreign keys.

| Table | Current keys / data |
|---|---|
| `assistant_documents` | PK `(gallery_id, kind, page_start, page_end, producer_version)`; text, content hash, producer, coverage, timestamps |
| `assistant_embeddings` | PK `(document_key, model_id)`; gallery/kind, dim, `f32le`, normalized flag, vector BLOB, content hash, timestamp |
| `assistant_jobs` | `job_id` PK; unique dedupe key, job type/gallery, JSON payload, priority/status, attempts, retry time, lease, sanitized errors, timestamps |
| `assistant_model_state` | role PK; model ID, config hash, updated time |
| `assistant_library_state` | key/value; embedding generation and metadata scan checkpoints |

**Important correction to the original SQL:** `page_start` and `page_end` are `INTEGER NOT NULL DEFAULT 0`, not nullable. Metadata documents use `(0,0)`. This avoids SQLite's nullable-composite-key duplicate behavior. Future actual page ranges use positive, inclusive, 1-based page numbers; whole-book documents also use `(0,0)` with their own kind.

Current kinds/jobs are operationally metadata-only. Although schema fields are generic, several methods hard-code metadata, version and active embedding model. Current `config_hash` records a hash of the model ID, not all prompt/sampler/config dependencies.

### 5.2 Current durability and deletion

`assistant_jobs` states:

```text
queued -> running -> succeeded
             |-> retry_wait -> running
             |-> failed
             `-> cancelled
```

The indexer claims batches with 600-second leases and renews them every 30 seconds, including during scheduler waits. Expired running leases are recovered. Commits require matching job attempt/status and document hash, preventing a late embedding from resurrecting a deleted/superseded document.

Full metadata scans persist `metadata_scan_pending` and `metadata_scan_after`; queued work and unfinished scans resume after restart. The in-memory V1 `RequestQueue` is **not** in these tables.

Catalog callbacks rebuild only changed documents, remove stale embeddings and delete derived state for removed galleries. Callback handling reconciles current catalog authority rather than assuming callback delivery order. Successful embedding writes/deletions increment `embedding_generation`.

A missing sidecar is created. Recognized SQLite corruption is quarantined for rebuilding; arbitrary permission/locking errors must be reported, not treated as permission to rename/delete a database. Never quarantine `library.sqlite3` as an assistant recovery action.

### 5.3 Required V2 migration work

Before adding visual jobs:

1. Implement ordered transactional migrations. Do not unconditionally reset `user_version=1`; preserve existing V1 documents/vectors and reject unsupported newer schemas safely.
2. Generalize document upsert, dedupe, claim, retry, completion and status by **job type, kind, source identity and active producer namespace**. Current per-gallery cancellation must not cancel unrelated visual/narrative jobs when metadata changes.
3. Separate `visual_summary` from `embed_visual` so a successful remote summary survives an embedding failure. Re-embedding stored text must not re-upload pages.
4. Persist source/sampler/model/prompt dependencies and normalized structured evidence alongside canonical retrieval text.
5. Return state counts by kind and active namespace; stale/refused/queued is not the same as indexed.
6. Make deletion clean new tables/dependencies and invalidate the applicable snapshots.

Proposed table for structured provenance, introduced by migration (not present in V1):

```sql
CREATE TABLE assistant_document_sources (
  document_key TEXT PRIMARY KEY,
  gallery_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  producer_namespace TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_assistant_sources_gallery_kind
  ON assistant_document_sources(gallery_id, kind);
```

Use the same stable document-key construction in documents, embeddings and provenance. Namespace includes producer model, prompt/schema version and sampler/window version. Store selected page numbers/member identifiers/hashes and observations in `evidence_json`; exclude upload bytes and filesystem paths. File size/mtime is a cheap invalidation token, not a cryptographic guarantee. Explicit refresh must handle externally replaced archives even when timestamps were preserved.

### 5.4 Required V3 durable parents

Introduce parent requests separately from reusable child jobs:

```sql
CREATE TABLE assistant_requests (
  request_id TEXT PRIMARY KEY,
  request_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  candidates_json TEXT NOT NULL,
  budget_json TEXT NOT NULL,
  progress_json TEXT NOT NULL,
  preliminary_result_json TEXT,
  result_json TEXT,
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  expires_at REAL
);
CREATE TABLE assistant_request_jobs (
  request_id TEXT NOT NULL REFERENCES assistant_requests(request_id) ON DELETE CASCADE,
  job_id TEXT NOT NULL REFERENCES assistant_jobs(job_id) ON DELETE CASCADE,
  PRIMARY KEY (request_id, job_id)
);
```

These are same-sidecar foreign keys; enable foreign-key enforcement on every connection after migration. Version every stored JSON contract. Persist budget reservations/consumption transactionally; restart must not reset spent attempts or active time. Shared child jobs can serve multiple parents. Gallery removal must invalidate affected parent candidates/results as well as child data.

## 6. Authoritative metadata access

### 6.1 Canonical V1 documents

One deterministic document per downloaded gallery: gallery ID; English/Japanese/Pretty titles; pages; parody, character, artist, group, tag, language and category names. Names are sorted/deduplicated, whitespace and missing fields normalized. No generated prose, private paths, cookies or server addresses.

Current canonical text has a **6000 UTF-8-byte cap** with safe truncation and SHA256 of the resulting text; this is not a claim of 6000 tokens. Provider truncation is an additional protection. Version is `metadata-v1`; the document key is `{gallery_id}:metadata:metadata-v1`.

### 6.2 Current library helpers

- `assistant_gallery(id)` and `assistant_records(ids)`: authoritative titles/pages/taxonomies, only existing downloaded archives; batch size <=500.
- `assistant_catalog_batch(after_id, limit=250)`: bounded explicit-index scan.
- `assistant_count()`: current catalog count.
- `assistant_resolve(term)`: canonical taxonomy resolution.
- `assistant_filter_candidates(...)`: required/excluded SQL terms, hard page bounds, paged ID scan (<=5000), optional shorter/longer ordering for bounded seeds.
- `assistant_backfill_pages(id)`: explicit-index backfill from local archive members for old records with unknown page counts.

`galleries.num_pages` was added to the authoritative catalog. Unknown page counts do not satisfy hard page bounds. The assistant uses these helpers rather than reaching into private SQL implementation. V2/V3 image code must use validated gallery IDs and the library's safe archive/page ordering, not arbitrary user/model paths.

## 7. Local vector search

### 7.1 Current snapshot behavior

Read active-model metadata embeddings from the sidecar, check a uniform dimension, decode finite float32 vectors, normalize and publish a contiguous immutable NumPy matrix plus gallery IDs. Search is matrix multiplication with the allowed-ID mask and top-k selection. Query vectors and stored vectors from different models/dimensions are never mixed.

`embedding_generation` invalidates the snapshot. Refresh occurs on use and after successful background embedding commits; replacement is atomic under a lock. Initial refresh may run in the caller and is part of local latency. There is no independent always-running refresh service today.

### 7.2 V2/V3 extension

Key snapshots by `(kind, embedding_model, producer_namespace)` with parallel document keys and page ranges for narrative chunks. Current loading filters `kind=metadata`; implement new branches explicitly. Avoid accidentally loading all kinds into one matrix.

Prefer per-namespace generations and coalesced refreshes for background batches. Measure memory before retaining all chunk embeddings: V1's 15k-book size does not bound V3's chunk count. Initially load only narrative chunks for bounded selected gallery IDs, then expand to a global chunk index only with a measured memory budget. A change of chat model does not force metadata re-embedding.

## 8. QueryPlan schema

### 8.1 Current validated plan

```json
{
  "required": [{"kind": "parody", "value": "blue archive"}],
  "preferred": [{"kind": "mood", "value": "heartwarming", "weight": 0.8}],
  "excluded": [{"kind": "tag", "value": "horror"}],
  "page_range": {"min": null, "max": 40, "hard": false},
  "semantic_query": "Blue Archive heartwarming short stories",
  "visual_query": null,
  "narrative_query": null,
  "mode": "fast",
  "requested_count": 5,
  "excluded_gallery_ids": []
}
```

Required/excluded kinds: `tag, artist, character, parody, group, language, category`. Preferred may also use `theme, mood, visual_style, scene, narrative`; these are semantic preferences, not authoritative taxonomy IDs.

Current validation rejects unknown keys and invalid types/enums; permits <=30 terms per group, term length <=200, finite weight 0–5, query text <=4000, page bounds 1–10000, and <=100 digit-string gallery exclusions. Counts are clamped to configured result limit and 1–5. Missing optional/default fields are normalized by the server.

### 8.2 Parsing and local resolution

Strip JSON fences, parse one object and validate. On invalid output, allow one bounded repair call; on provider failure or failed repair, use the user's text with the previous validated hard conditions and a warning. A repair shares the interactive budget.

Resolve metadata terms through existing normalization, transitive explicit aliases, exact normalized name/slug match, then bounded nearest Latin spelling fallback. Use only a unique resolution as a hard filter; report ambiguous/unresolved terms. Do not silently relax resolved conditions when the SQL universe is empty.

### 8.3 Mode is not upload consent

Current V1 accepts `auto|fast|deep` at the API but executes metadata-only fast behavior; deep/visual/narrative requests receive a limitation warning. V2 `auto` may search stored visual evidence. V3 parsing may suggest deeper analysis, but must not itself enqueue page uploads. An explicit deep-request action and the image-analysis setting are required.

## 9. Current HTTP contracts and planned extensions

All API paths below are relative to `/_nh-local/api/assistant`. Apply the deployment base path. Allowed networks and same-origin checks apply; JSON responses are `Cache-Control: no-store`.

### 9.1 Health and diagnostic check

`GET /health` and `GET /index/status` return enabled/available/key-loaded booleans, provider state, sanitized error, connection-check state, config source/environment-variable name, metadata index counts/model/jobs, visual placeholder, scanning and background-enabled flags.

Key presence is not a validity check. `POST /check` with `{}` sends a short embedding test and returns `missing_key`, `verified` or `failed`, with a sanitized explanation/status. It tests the configured embedding endpoint, not every model. Unlike recommendation submission this diagnostic currently waits for its single interactive call (bounded by the per-call timeout).

An enabled but failed initialization must remain `enabled=true, available=false`; it must not be mislabeled disabled. V1 visual health is always disabled/zero.

### 9.2 Recommendation submission

```http
POST /recommend
Content-Type: application/json
```

```json
{"message":"想看輕鬆的短篇","mode":"fast","previous_plan":null,"limit":5}
```

Body max 32 KiB; nonempty trimmed message 1–4000 Unicode characters; mode enum; previous plan validated using the same schema; result count clamped to 1–5. Unknown request fields are rejected.

**Current success response is 202, never a synchronous recommendation 200:**

```json
{
  "job_id": "opaque-request-id",
  "request_id": "opaque-request-id",
  "status": "queued",
  "stage": "queued",
  "created_at": 1790000000.0,
  "updated_at": 1790000000.0
}
```

Current errors: invalid input or full two-request queue → 400; forbidden origin/network → 403; unavailable assistant → 503. A future change to 429 for admission pressure must be deliberate and covered by clients/tests; do not describe it as today's behavior.

### 9.3 Polling a V1 request

`GET /jobs/{job_id}` returns 200 with `queued|processing|ready|failed` and stage:

```text
queued -> parse -> filters -> semantic -> local_search -> rerank -> verify -> ready
```

Some stages may be skipped; the `rerank` stage does not imply a quality call when reranking is disabled. Empty hard-filter results terminate early. A ready envelope contains a nested `result`:

```json
{
  "job_id": "opaque-request-id",
  "request_id": "opaque-request-id",
  "status": "ready",
  "stage": "ready",
  "created_at": 1790000000.0,
  "updated_at": 1790000001.0,
  "result": {
    "request_id": "recommendation-result-id",
    "status": "ready",
    "assistant_text": "Recommendations based on local metadata.",
    "plan": {"semantic_query": "short books", "mode": "fast", "requested_count": 5},
    "unresolved_terms": [],
    "results": [{
      "id": "123456",
      "title": "Example title",
      "pages": 28,
      "detail_url": "/g/123456/",
      "cover_url": "/catalog-thumbnail/123456",
      "reasons": ["28 pages", "language: english"],
      "evidence": [{"type": "metadata", "label": "language: english"}],
      "match_sources": ["metadata", "semantic"]
    }],
    "warnings": [],
    "elapsed_seconds": 1.0,
    "timings": {"parse": 0.2, "filters": 0.1, "semantic": 0.4, "local_search": 0.2, "rerank": 0.0, "verify": 0.1}
  }
}
```

The example plan is abbreviated; the actual response includes normalized defaults. Current inner result ID can differ from the outer job ID; poll by the outer `job_id`.

One request worker; at most two unfinished jobs; at most 64 retained entries. Terminal entries expire 900 seconds after their last update and can be evicted earlier under the entry cap. Restart loses all these jobs. Missing/expired jobs return 404 with a re-submit message. Browser refresh in the same tab can resume a still-existing job; server-restart recovery is not guaranteed.

The browser polls approximately every second while visible, pauses in hidden tabs, and retries poll-network failures up to three attempts with 2-second delays. It saves `job_id` and the pending message in sessionStorage. Health refresh is separate, every 10 seconds on the visible assistant page.

### 9.4 Current index routes

```text
POST /index/metadata        {}  -> 202, scanning flag
POST /index/gallery/{id}    {}  -> 202, queued flag
POST /index/retry-failed    {}  -> 202, queued count for active metadata model
```

`POST /index/visual` is **not implemented** and currently rejects the action. No current job-cancel or deep-analysis route exists.

### 9.5 Planned V2/V3 routes

| Route | Version | Contract |
|---|---|---|
| `POST /index/visual` | V2 | explicit `{scope: "all"}` or `{scope: "ids", gallery_ids: [...]}`; <=100 IDs; returns durable operation ID, scan progress/counts |
| `GET /operations/{id}` | V2 | durable visual scan/job progress; distinguishes summary-ready from searchable/embedded |
| `POST /operations/{id}/pause`, `/resume`, `/cancel` | V2 | persist intent; do not abort an already-sent HTTP call |
| `POST /index/retry-failed` | V2 | optional validated kind/namespace; `{}` preserves current metadata behavior; refusals need explicit per-gallery reanalysis |
| `POST /deep` | V3 | validated complete plan, explicit downloaded gallery IDs, idempotency key and within-server-cap budget; returns 202 and durable request ID |
| `GET /deep/{request_id}` | V3 | persistent parent status/progress, preliminary/final results, evidence coverage and consumed budget |
| `POST /deep/{id}/pause`, `/resume`, `/cancel` | V3 | durable parent controls; child reuse/reference rules in section 31 |

Keep `/jobs/{id}` for ephemeral ordinary searches. Do not silently change its TTL/status contract when durable analysis is added. New actions revalidate all client-supplied plans/IDs, even if originally returned by the server. A V2 visual scan operation can use the durable request table introduced earlier than V3; do not invent a second unrelated job store.

# V1 — Implemented metadata-first assistant

## 10. Scope

V1 supports source series/characters/artists/groups/languages/categories/tags, semantic preferences insofar as metadata supports them, page preferences, hard exclusions and structured follow-ups. It does not know page content or story arcs. Model ranking may not create gallery IDs or override hard conditions.

## 11. Metadata indexing

Full indexing is explicit; first setup uses the dedicated page's **Connection & metadata index → Build / resume index**. Ordinary restart resumes unfinished work but does not start a new full scan for an unchanged embedding model. New/changed authoritative records enqueue incremental work. An embedding-model change queues a namespace rebuild while preserving compatible old data until superseded.

- Build/store canonical document before remote work.
- Dedupe key: SHA256 of `embed_metadata:{gallery_id}:{content_hash}:{embedding_model}`.
- Initial batch size 32; on payload-limit/validation errors (400/413/422), halve batch size down to one and keep the safe size for the process lifetime.
- Commit each completed batch independently. One bad document does not undo prior successes.
- Persist scan cursor and pending flag; recover jobs by leases, not by resending every document.
- `background_enabled=false` pauses remote worker execution after restart; explicit scans can still create queued local work.
- Only re-embed changed canonical text. Do not rebuild 15k embeddings just to change parser, quality model or UI.

## 12. Online recommendation pipeline

### 12.1 Parse

The parser sees the latest message, optional previous validated plan and schema instructions, not all catalog taxonomies. Temperature 0.1, output cap 600 tokens. It emits concise constraints and semantic text; omit default/empty fields, which validation fills in. At most one repair, within the same budget.

### 12.2 SQL hard-filter universe

Resolve unique canonical required/excluded terms; enforce all resolved conditions and hard page bounds in SQL; remove excluded gallery IDs. Collect eligible IDs in bounded batches. If this universe is empty, return zero results without silent relaxation or further remote calls.

### 12.3 Semantic branch

Refresh/load the active metadata snapshot. When it is nonempty and the provider is configured, embed the semantic query in query mode and retrieve up to `dense_candidate_count` (default 80), masked to the universe. No query embedding is needed for an empty metadata matrix. Provider/query-vector failures use lexical/local fallback with warnings.

### 12.4 Bounded local candidates

Union dense hits, bounded existing local-search hits, exact preferred taxonomy seeds and required/page-order seeds. Seed branch limit is `min(dense_candidate_count, 100)`. Deduplicate and intersect with hard-allowed IDs; prioritize dense/lexical candidates and materialize **at most 400** records.

Score normalized title/name matches, preferred taxonomy matches and soft page preference. Current constants are title/lexical bonus 3.0, preferred weight factor 2.0 and page factor 0.5. These are implementation choices to evaluate, not universal relevance truths. Resolved exclusions are mandatory removals, never score penalties.

Fuse metadata and dense rank using RRF (`K=60`). Keep at most `rerank_candidate_count` (default 12) for final selection. Final output is at most `result_limit` (default 5). Avoid the former whole-library metadata/CBZ-stat loop.

### 12.5 Optional quality pass

When `rerank_enabled=true`, send only the compact top candidates, plan and count to `quality_model`. Temperature 0.1, output cap 250 tokens; output shape is `{"results":[{"id":"123"}]}`. No generated prose is needed because displayed V1 reasons are deterministic source-metadata statements.

Repair once if unusable, within the same interactive budget; then use deterministic local ordering. The default path never invokes the 120B quality model. Do not restore a mandatory quality call in V2 ordinary searches.

### 12.6 Verification and response

Drop unknown/duplicate IDs, reload selected records, require existing downloaded archives, and re-check current required/excluded/page conditions. Generate reasons and metadata evidence locally. Build gallery/thumbnail URLs from validated IDs. A gallery deleted or changed while inference runs cannot survive verification merely because the model selected it.

Return results, normalized plan, unresolved terms, warnings, total execution time and stage timings through the ready polling envelope. Late model completion after deadline is advisory and cannot replace a published fallback response.

## 13. Follow-up state

Browser sessionStorage is scoped by deployment base path and stores previous validated plan, last result IDs, pending message/job ID and up to five displayed conversation turns. **New search** clears it. The full conversation is not sent to NIM on each request and is not stored in a persistent chatbot database.

**Exclude from next search** explicitly adds a gallery ID to the plan. Do not claim the parser can always resolve “exclude the first previous book” from text: current requests do not supply a positional mapping of the previous result list to the parser. UI exclusions and explicit gallery IDs provide the implemented path.

An offline follow-up preserves previous hard constraints but cannot reliably interpret a new natural-language modification; return a warning rather than claiming otherwise.

## 14. Dedicated UI

`LocalLibrary.assistant_html()` supplies a standalone page shell. `local-ui.js` mounts a full-width workspace only on `body.nh-assistant-page`; ordinary pages mount one navigation link and perform no assistant health/poll requests.

The workspace includes library navigation, New search, a clear request composer, progress, conversation turns and responsive cards (three/two/one columns by viewport). Diagnostics/index controls live in an expandable section rather than dominating the conversation. Cards contain cover, title, ID/pages, verified reasons, local gallery link and exclusion action.

A newly completed turn scrolls into view. Polling resumes after tab reload when possible. New search/submission is disabled while the current search is pending, preventing accidental overlapping submissions. Screen-reader status regions, keyboard focus, text-only rendering and no horizontal mobile overflow are required.

Reader fit/original controls stay independent; the assistant link navigates away to the full page instead of opening an overlay.

## 15. V1 acceptance and remaining limits

Implemented and covered by deterministic tests:

- [x] standalone route and base-path-aware links; no assistant sidebar;
- [x] 202 submission, bounded request queue, polling/progress and same-tab reload resume;
- [x] durable deduped metadata indexing, scan checkpoints, leases and model namespaces;
- [x] bounded candidate reads, SQL hard filters, final gallery existence checks;
- [x] optional quality model, compact prompts and interactive deadlines;
- [x] missing/invalid key and initialization diagnostics, provider fallback;
- [x] XSS-safe cards, mobile layout, follow-ups and reader/library regression coverage.

Not established by those checks:

- [ ] hosted-NIM end-to-end latency/relevance distribution after optimization;
- [ ] persisted recovery of V1 interactive searches across server restart (intentionally absent);
- [ ] visual/narrative understanding or image uploads;
- [ ] a total 40-second user-visible latency guarantee.

Before choosing a different parser model, compare a fixed multilingual intent/constraint set and measured hosted latency. Keep embedding model unchanged unless an explicit migration/reindex is intended. Smaller parameter count alone does not prove lower shared-endpoint latency.

# V2 — Planned precomputed visual evidence

## 16. Scope and fast-path boundary

V2 adds style/scene retrieval from **stored sampled visual summaries**:

```text
explicit durable index operation
  -> safely sample local CBZ pages
  -> hosted VLM observation JSON
  -> validated, persisted visual document
  -> batch passage embedding
  -> local visual snapshot

ordinary recommendation
  -> V1 plan and filters
  -> metadata + existing visual vectors + lexical seeds
  -> bounded local fusion and verification
  -> ready result; no image analysis in this request
```

No local GPU or dedicated multimodal-embedding endpoint is required. A future multimodal embedding adapter is optional and must not change source-authority or fast-response semantics.

The existing `visual_model` default is a candidate for a pilot, not a proven production choice. Verify its current hosted multi-image contract before implementing provider payloads. Select a faster supported visual model if measurements justify it; source/sampler/model namespaces make this possible without deleting metadata vectors.

## 17. Visual document and provenance

Generate one strict structured observation per gallery/sample version. Example model-owned observation fields:

```json
{
  "style": ["black-and-white manga", "thin linework"],
  "setting": ["classroom", "outdoor walkway"],
  "visible_characters": ["two people; identity uncertain"],
  "activities": ["conversation", "walking"],
  "tone": ["casual"],
  "composition": ["dialogue-heavy panels"],
  "warnings": ["sampled pages only"]
}
```

Validate allowed keys, list/string limits, bounded total text and enums where applicable. Initial limits: <=8 items per list, <=200 characters per item, <=6000 UTF-8 bytes of canonical retrieval text; VLM output cap 800 tokens. On invalid JSON permit at most one repair counted against the operation budget. Do not store raw unchecked prose as authoritative evidence.

The server, not the model, supplies:

- downloaded gallery ID;
- actual selected 1-based page numbers and image-to-page mapping;
- archive change token, selected-member hashes and sampler version;
- visual model, prompt/schema version and producer namespace;
- `coverage=sampled`, analyzed page count and known total page count;
- normalized observation JSON and canonical text hash.

Store a `kind=visual` document at `(0,0)` plus structured provenance. A sampled document must retain the actual disjoint page list, not imply that every page between its first and last sample was examined. VLM guesses do not update source `character`, `parody`, `artist` or other taxonomies.

## 18. Deterministic bounded image sampling

### 18.1 Sampling plan

Count/order actual valid reader image members; do not trust a model or stale metadata count to choose pages.

| Actual pages | Initial interior sample |
|---|---:|
| 0 | no upload; `no_readable_pages` |
| 1 | one page |
| 2–8 | up to four unique representative pages |
| 9–40 | four pages |
| 41–150 | five pages |
| 151+ | six pages |

Use deterministic stratified positions, deduplicate rounding collisions and record a sampler version. A cover, if included, is labeled separately and consumes one of the same maximum six slots; it is not a seventh image. Do not infer endings or plot from this sparse sample.

### 18.2 Safe local decode

Add `images.py` using the existing gallery/archive and reader page ordering rules:

1. resolve an authorized existing downloaded gallery by numeric ID;
2. inspect archive members; reject unsafe paths, encrypted/unsupported members and unreasonable expansion;
3. read selected members only, without extracting the whole library;
4. reject a selected member larger than 64 MiB uncompressed and an excessive compression ratio (initial ceiling 200:1);
5. decode one image at a time with Pillow; enforce <=16 million decoded pixels, fail decompression-bomb warnings/errors, and reject corrupt data;
6. apply EXIF orientation, use a single frame for animated formats, convert to RGB;
7. resize to <=896 px edge, encode bounded JPEG, strip metadata;
8. construct the provider image content with explicit page labels/order;
9. discard upload buffers promptly after the request.

Initial local decode concurrency is one, separate from the HTTP and recommendation workers. Bound buffered serialized images, not merely thumbnail dimensions. Do not persist base64/page bytes in job JSON or logs. A retry re-reads selected members and validates the expected source identity.

### 18.3 Serialized request limits

Use the lower of configured limits and verified provider limits:

- <=6 images per call;
- <=512 KiB encoded bytes per image;
- <=5 MiB for the entire serialized JSON request, including base64 expansion, labels and prompt;
- one bounded quality/edge reduction on an explicit payload-size error;
- no automatic crop/encoding loops after content refusal;
- no whole-CBZ upload or filesystem path in a remote prompt.

Label size adaptation separately from refusal and authentication errors. If a single page still cannot fit safely, record it as unavailable and either use a clearly marked partial sample or fail that document; do not pretend all intended pages were analyzed.

## 19. Durable visual indexing policy

### 19.1 Explicit operation and feature gates

Initial deployment defaults: `remote_image_analysis_enabled=false`, automatic-on-new=false, lazy indexing=false. Available V2 triggers:

- explicit selected-ID pilot, <=100 validated IDs per API call;
- explicit full scan, creating work incrementally with a durable cursor;
- optional on-new-download policy after the administrator enables it;
- optional bounded lazy background policy after explicit opt-in.

A normal recommendation never waits for these jobs. If lazy policy is later enabled, limit newly scheduled galleries per query and per day, dedupe them and expose the queue; it must not quietly launch analysis of every unindexed candidate.

Check the upload gate **again at dispatch**. If disabled, hold queued image work as blocked/paused without incrementing attempts. A previously sent request cannot be unsent. Existing compatible summaries may still be searched while uploads are disabled.

### 19.2 Two-stage durable dependency

```text
visual_summary -> persisted validated visual document -> embed_visual -> searchable
```

A summary succeeds independently of its embedding. Batch up to 32 text documents for passage embedding; retry only the failed stage. Summary-ready/embedding-pending coverage is not counted as searchable coverage.

Dedupe identities include:

```text
summary: gallery + source_fingerprint + sampler + visual_model + prompt/schema
embedding: document_key + canonical_text_hash + embedding_model
```

Changing embedding model re-embeds stored text. Changing visual model/prompt/sampler marks the old producer namespace inactive for the new view and exposes rebuild-needed status, but does not silently upload the full library on restart. Keep valid old namespaces until replacement/explicit cleanup; do not mix incompatible evidence as if identical.

### 19.3 Progress, pause and retry

Persist scan cursor, discovered/completed/failed/blocked counts and pause/cancel intent. Full scan submission returns an operation ID promptly; it must not synchronously create 15k jobs inside an HTTP request.

429/503/timeouts use durable retry dates and Retry-After. Record bounded attempts; honor auth/model failures without hammering every queued gallery. An auth failure should pause the affected provider/namespace pending intervention. Generic retry-failed excludes deterministic refusals unless the operator explicitly requests reanalysis after a relevant provider/version change.

## 20. V2 recommendation pipeline

Keep the existing 202 submission and ephemeral request queue for ordinary searches.

1. Parse once and resolve source hard filters exactly as V1.
2. Determine which **active, nonempty** vector branches are relevant from `semantic_query`, `visual_query` and soft style/scene preferences.
3. Batch distinct query texts into one embedding request when they share the same embedding model; reuse a vector if the canonical query texts are identical. Do not mix `query` and `passage` items in a batch.
4. Search metadata and visual matrices separately, both masked by authoritative allowed IDs.
5. Fuse metadata, visual and lexical ranks with RRF. Limit each retrieval branch (initial 80), then preserve balanced branch contributions before the shared **400-record materialization ceiling**; do not truncate away all visual-only hits just because metadata was appended first.
6. Rank locally and verify selected source metadata/archive existence again. Include only active visual documents whose source identity still matches the gallery.
7. Generate reasons from source metadata and stored sampled observations. Optional quality ranking remains off by default and must fit the existing interactive budget.

Normal query target remains **one parser call + one batched query-embedding call**, not a parser plus embedding plus VLM plus 120B call. If no usable visual index exists, skip that branch instead of embedding a query for an empty matrix.

## 21. Coverage and missing-index semantics

An unindexed/refused/stale gallery remains eligible through metadata. Absence of visual evidence is not evidence that a style/scene is absent.

Planned response additions (inside ordinary `result`, preserving current fields):

```json
{
  "coverage": {
    "visual": {"eligible": 1000, "searchable": 320, "summary_pending_embedding": 12, "refused": 8, "stale": 20}
  },
  "analysis_suggestions": [{"type": "visual_index", "gallery_ids": ["123456"]}]
}
```

Define `eligible` as the current hard-filter universe of downloaded galleries, with unknown/stale source records excluded from searchable counts. Global health coverage and per-query coverage are different metrics. If source freshness is awaiting reconciliation, report that uncertainty rather than presenting a precise verified fraction.

New visual evidence item:

```json
{
  "type": "visual",
  "document_key": "opaque-versioned-document-key",
  "label": "Sampled pages show thin linework",
  "pages": [2, 10, 19, 28],
  "coverage": "sampled"
}
```

Server creates page links from verified page numbers. `analysis_suggestions` only invites a separate operation; rendering the suggestion does not upload anything.

## 22. Refusal, source change and model failure

Distinguish `provider_rejected`, `payload_too_large`, `invalid_analysis_json`, `image_decode_failed`, `source_changed`, `auth_failed`, `model_unavailable` and transient transport failures.

- A deterministic refusal terminates that analysis version without deleting metadata or retrying forever.
- If the archive changes while a request is in flight, discard the stale write and queue a current-version job only under the authorized operation/policy.
- A model removal or key failure degrades the visual operation, not the whole library.
- Rejected/failed visual evidence cannot become a negative hard filter or a reason to remove a V1 recommendation.
- Pausing/cancelling an operation does not delete already verified reusable documents.

## 23. V2 UI, pilot and acceptance

Extend `/AI_assistant`; do not reintroduce an overlay. Connection/index details show active namespace and metadata/visual coverage separately. A selected-ID or full-build action explains that sampled pages are sent remotely, reports progress, and exposes pause/resume/cancel.

Cards distinguish **Metadata match** from **Sampled visual match**, display analyzed page ranges/count and uncertainty, and open existing reader routes. A visual-style request with low coverage returns promptly with a clear note and an explicit index action.

Release sequence: opt-in 20–50-gallery pilot → verify payloads/refusals/latency/retrieval usefulness → selected-ID indexing → explicitly triggered full scan. Do not infer usefulness from successfully receiving JSON alone.

V2 acceptance:

- [ ] versioned DB migration preserves existing metadata index;
- [ ] safe bounded decode and verified configured VLM payload contract;
- [ ] no page upload from ordinary search, disabled config, or a parser decision alone;
- [ ] durable summary and embedding stages resume independently;
- [ ] active kind/model namespaces and source invalidation work;
- [ ] low coverage/refusal leaves metadata results usable;
- [ ] visual-only candidates can enter bounded fusion without violating hard filters;
- [ ] responsive page, polling, restart/pause/cancel and evidence links are tested;
- [ ] pilot measures quality and latency before a full build is offered as routine.

# V3 — Planned explicit, durable narrative analysis

## 24. Scope

V3 can investigate temporal/relationship requests such as a disagreement followed by reconciliation, or a tonal shift later in a book. Sparse visual samples cannot establish those claims.

Separate two experiences:

- **Search stored evidence:** ordinary fast recommendation may use already indexed narrative observations within the existing budget.
- **Analyze selected books:** a separate explicit deep operation may upload new windows and outlive an ordinary search or server restart.

V3 still needs no local GPU, and does not deep-index the entire library automatically.

## 25. Trigger, admission and budgets

### 25.1 Explicit trigger

The page first returns useful V1/V2 results. The user chooses **Analyze selected books** (or the equivalent explicit deep action), sees selected books and bounded work/coverage, and submits `POST /deep`. A plan with `narrative_query` or the ordinary `mode=deep` field may suggest this action but cannot create new page uploads by itself.

Validate source IDs, current downloads, plan, server caps, upload/deep feature gates and idempotency key. Client-supplied candidate IDs/budgets are not trusted just because the UI generated them. Preliminary/final recommendations always re-check metadata conditions.

### 25.2 Initial conservative budget

| Resource | Default per durable request |
|---|---:|
| selected books | 4 (server validation maximum 8) |
| new windows per selected book | 3 |
| pages per window | up to 6 |
| total remote attempts | 24, including every retry/repair and downstream text call |
| active wall-clock budget after activation | 900 s, including scheduler waits/cooldowns; explicit pauses excluded |
| simultaneously active deep parents | 1; bounded admission queue of 2 |

Existing compatible chunks do not consume a new-upload/window slot. They still require bounded local retrieval. No initial design assumes 8 books × 6 windows can be analyzed within one ordinary query.

Reserve calls for required downstream work before expanding more windows. Charge an attempt durably at dispatch, before sending HTTP; after an uncertain crash, conservatively keep it charged. Unsent cancelled reservations may be released transactionally. Account for shared work in every dependent parent's budget using a documented conservative rule (each dispatched shared attempt counts against each attached parent); cache hits completed before attachment cost zero new attempts.

If a retry delay would exceed the remaining active budget, or no remaining calls can complete a useful dependency chain, finish **partial** with known evidence and a continuation action. Do not let a free endpoint's long Retry-After create an indefinitely spinning request.

## 26. Narrative windows

Use the same safe decoder and exact reader ordering as V2. Initial chunking is six adjacent pages with one-page overlap for sequential expansion. Windows are inclusive, 1-based and capped by actual page count.

With three initial windows, select deterministic beginning/middle/ending regions where appropriate; remove duplicate/overlapping-equivalent windows for short books. Distinguish this sparse selection from continuous book coverage. If promising evidence is found, later explicit continuation can fill adjacent gaps under a new budget.

Each child job stores expected source fingerprint, page range, ordered member mapping, window/sampler version and model/prompt/schema namespace. Changed or deleted sources invalidate pending work and prevent stale completion from publishing. Do not regenerate unchanged windows merely because a different query asks about the same book.

## 27. Narrative chunk schema and evidence

Model-owned output is limited to observations in the supplied window:

```json
{
  "setting": ["outdoor path"],
  "characters_observed": ["two people; identities uncertain"],
  "actions": ["one person leaves during a conversation"],
  "dialogue_summary": "The visible dialogue may indicate disagreement.",
  "relationship_change": "possible disagreement; no resolution visible",
  "confidence": "medium",
  "warnings": ["small text not fully readable"]
}
```

The server owns gallery ID, actual page range, source fingerprint and evidence references. Reject unknown fields, oversized lists/prose and invalid confidence values. Initial output cap 900 tokens, bounded canonical text <=6000 bytes. One repair at most, charged to the parent budget.

Identity hinted by source metadata must remain labeled as metadata rather than visual identification. Do not assert events on unsupplied pages or infer an entire causal arc from disconnected samples. Store structured evidence and canonical `kind=narrative_chunk` text, then enqueue `embed_narrative_chunk` independently.

## 28. Dialogue/OCR policy

A general VLM's dialogue reading is uncertain evidence, not authoritative OCR. Summarize readable dialogue with uncertainty flags; do not manufacture verbatim quotes. Missing/unreadable text must remain missing.

No local GPU OCR is required. A future `HostedOcrProvider` needs its own explicit capability, privacy, timeout and budget rules; store OCR as separate evidence with page provenance. It must not silently multiply remote calls in the first V3 release.

## 29. Book aggregation

After bounded chunks are persisted, aggregate **compact ordered observations**, not original pages. Start with deterministic coverage accounting and an optional lightweight text summarizer using `parser_model`; a quality model is an explicit later option, not the default for every book.

Proposed summary schema:

```json
{
  "summary": "Observed windows suggest a disagreement and a later friendly interaction; intervening events were not analyzed.",
  "themes": ["friendship"],
  "relationship_arc": "possible reconciliation; incomplete evidence",
  "ending_known": false,
  "coverage": "partial",
  "evidence_refs": ["chunk-key-a", "chunk-key-b"]
}
```

Server verifies every evidence reference and computes coverage from the union of actual analyzed page ranges. Use `partial` or `full` for narrative coverage; do not use ambiguous `near_full` as a substitute for known missing pages. Overlap counts once. `ending_known=true` requires analyzed ending pages and sufficiently readable evidence, not merely a window selected near the end.

Initial aggregation packet: at most 12 relevant compact chunks per book and an 800-token output cap. If more chunks exist, select deterministically for the query and expose omitted ranges; never make an unbounded accumulated prompt. Summary dependencies include ordered child hashes, source identity, aggregation model and prompt/schema version. New chunks invalidate only the dependent book summary, not unchanged chunks.

If summarization fails or the call budget runs out, use stored chunk observations and deterministic coverage as partial evidence; do not discard completed image work. Embed `kind=narrative_book` separately when a valid summary exists.

## 30. Retrieval and optional final ranking

Ordinary cached-evidence search stays in the ephemeral queue and shared 40-second remote budget. Batch metadata/visual/narrative query strings when compatible; search each kind separately and fuse ranks. Before gallery-level RRF, aggregate chunk hits per gallery with a per-book contribution cap so a long book cannot dominate by having more chunks.

For a deep parent's final result:

- use at most eight selected, still-valid books;
- include metadata, optional visual summary, <=3 relevant chunk references per book, optional narrative summary and exact coverage;
- default to local rank/evidence selection;
- optional final quality call returns only candidate IDs and provided evidence references, with an initial 512-token cap and a reserved attempt;
- server verifies IDs, metadata filters, source identity, reference ownership and page bounds again.

Visible reasons come from validated stored observations with uncertainty/coverage labels. A model choosing a reference does not prove semantic entailment: do not automatically turn “possibly disagrees” into “definitely reconciles.” Keep prose as uncertain as the underlying evidence. Evidence links use existing local `/g/{id}/{page}/` routes.

## 31. Durable parent lifecycle

### 31.1 Parent versus child states

Keep the generic durable child states from section 5. Parent `assistant_requests.status` uses:

```text
queued -> running -> ready
             |-> partial
             |-> failed
             |-> paused -> queued
             `-> cancelled
```

Use `stage` for `preliminary`, `analyzing`, `embedding`, `aggregating`, `reranking`, `verifying`. Preliminary results and progress are fields, **not a terminal `preliminary_ready` status**. Ordinary V1 polling remains unchanged.

Progress includes selected books, reusable/new/completed/failed windows, analyzed unique pages, summary/embedding counts, attempts consumed/reserved, next retry time, active budget remaining and sanitized per-gallery errors. Never display “90% complete” based only on gallery count when windows vary greatly.

### 31.2 Coordination and recovery

A lightweight coordinator advances dependencies in short transactions and dispatches due child work through the existing provider scheduler. Do not put the parent into the one-worker V1 `RequestQueue` and wait for all children. Do not hold a database transaction while decoding/networking or polling child futures.

On restart, recover parent state and expired child leases. Reuse committed summaries/chunks; resume only missing dependencies. Store a stable idempotency key for double-click/retry admission; reuse an existing parent only when key and normalized request agree, otherwise reject the conflict. Persisted plans must be validated on load; unsupported versions fail safely.

### 31.3 Pause, cancel, continuation and retention

- Pause stops new child dispatch for that parent and pauses its active-time clock. Shared work for another active parent may continue.
- Cancel stops scheduling that parent's work. Cancel a queued shared child only when no active parent/policy needs it.
- A sent HTTP call finishes; valid reusable evidence may be committed, but a cancelled parent does not resume or publish a final recommendation.
- Partial completion is terminal for that budget. A continuation is a new explicit parent referencing the prior one, with a fresh capped budget and reuse of existing evidence.
- Browser navigation or a hidden tab stops/reduces polling only; it does not cancel server work.
- Terminal parent query/results expire after seven days by default; derived reusable documents have a separate cleanup policy. Expire paused parents after 30 days without an explicit resume; detach their dependencies and cancel only children with no remaining active owner. Expiry must not delete reusable evidence.

The page stores the durable request ID and can reattach after restart. `GET /deep/{id}` must return a clear expired/not-found state rather than silently creating another upload operation.

## 32. V3 UI and acceptance

The dedicated page shows ordinary results immediately, with an explicit selected-book deep action. Its durable progress area includes book/window counts, remote budget, retries, preliminary cards, partial/failed coverage and pause/resume/cancel controls. Returning to the page can restore the durable request; no overlay or long-held recommendation HTTP connection is introduced.

V3 acceptance:

- [ ] deep upload requires explicit action and both feature gates;
- [ ] durable parent/child dependencies and idempotent admission survive restart;
- [ ] 40-second ordinary search budget and background deep budget remain separate;
- [ ] per-book windows, total attempts and active time are enforced transactionally;
- [ ] retries/repairs/embedding/aggregation all consume the defined budget;
- [ ] shared-child cancellation and stale-source completion are correct;
- [ ] one book's refusal does not fail useful results from the others;
- [ ] evidence ranges, overlap-aware coverage and ending uncertainty are preserved;
- [ ] partial continuation reuses evidence instead of repeating successful uploads;
- [ ] concurrent ordinary search remains usable during deep analysis.

# Cross-version operational requirements

## 33. UI and browser safety

- Serve the workspace through the existing server-generated page/assets. Keep native JS/CSS; no replacement frontend application is required.
- Mount the navigation link idempotently on proxy pages after hydration. Ordinary/reader pages make no automatic assistant API requests.
- Keep reader layout/fit/original size and navigation independent from assistant state.
- Use deployment-scoped sessionStorage for bounded display state and job IDs. Send only validated plans/current messages, not a growing transcript.
- Render user/model/source prose with `textContent`, not model HTML/Markdown. Validate IDs and create URLs locally.
- Show initialization failure, missing key, failed connection check and provider throttling as different states.
- Keep connection/index administration collapsible; card content must fit mobile width and keyboard navigation.
- Browser polling endpoints are short, same-origin and no-store. Network reconnection must not duplicate work or infer completion from a timeout.

## 34. Worker lifecycle and resource isolation

Current V1 has one ephemeral request worker, one remote scheduler worker, one metadata indexer and bounded explicit-scan/lease-renewal threads. Shut down admission first, stop scans/indexer dispatch, close provider queues and avoid leaving unresolved futures. In-flight remote calls may finish under their own timeout.

V2/V3 add bounded image decode and a durable coordinator, not one thread per gallery/window. Keep SQL transactions short. Rate-limit local scanning/decode as well as remote requests; a host already under disk pressure must not decode 15k archives in parallel.

Lease duration must exceed a single attempt and be renewed while queued behind remote work (current metadata leases: 600 s, heartbeat 30 s). Recover only eligible expired leases, protect writes by attempt token plus source/document identity, and make successful document/embedding completion and job transitions atomic.

Generalize job claiming by kind/namespace. The present metadata-only `claim(model)` and per-gallery cancellation logic are not sufficient for visual/narrative jobs. Fairness must include pending interactive work at each **child-call boundary**, while accepting that in-flight HTTP is not preempted.

## 35. Failure matrix

| Failure | Ordinary V1/V2 search | Durable V2/V3 analysis |
|---|---|---|
| assistant disabled/init failed | accurate diagnostics; library unaffected | no new dispatch |
| key missing/401/403 | local fallback / sanitized key state | pause affected remote work; preserve progress |
| parser invalid JSON | one budgeted repair, then semantic fallback preserving prior filters | validate stored plan; never let invalid plan authorize uploads |
| interactive deadline/circuit open | finish local fallback with warnings | parent unaffected unless its own budget expires |
| 429/503/timeout | no default HTTP retries; caller deadline applies | Retry-After/backoff, attempts and active-time budget apply |
| optional quality failure | local ranking and verified reasons | use available observations; partial if needed |
| empty/stale visual/narrative index | metadata eligibility remains | explicit rebuild/continue action |
| deterministic image refusal | metadata remains usable | terminate that version; no endless generic retry |
| archive deleted/changed in flight | drop invalid result on final verification | invalidate dependencies; discard stale write |
| server restart | ephemeral `/jobs` IDs return 404; re-submit | durable scans/parents recover expired leases/checkpoints |
| sidecar missing/corrupt | recreate/quarantine only recognized sidecar corruption | lost disposable AI state must be rebuilt explicitly |
| disk overload | report local timings; no artificial inference claim | bound local decode/scanning and allow pause |

## 36. Remote data boundary

V1 sends query/plan text to the parser, canonical metadata for indexing, query text for embeddings and candidate metadata only when optional reranking is enabled. V2 sends sampled/resized pages under an explicit index operation/policy. V3 sends bounded windows under an explicit deep request. None sends whole CBZ files, cookies, private paths or the entire catalog as one prompt.

Do not claim hosted processing is local/private merely because archives are local. Provider terms and model availability must be rechecked before enabling a new hosted capability. The image flag must block new page dispatch regardless of parser output; disabling it does not remove existing compatible derived evidence from local search.

## 37. Call budgets and performance measurement

### 37.1 Ordinary search

| Path | Normal remote calls |
|---|---:|
| V1 configured + nonempty metadata index | 1 parser + 1 query embedding = 2 |
| V1 optional quality rerank | +1, only if enabled and budget remains |
| V2/V3 stored-evidence search | target 1 parser + 1 batched query embedding; no new page analysis |
| empty eligible universe | parser only, then immediate zero matches |
| missing key | 0; local lexical/previous-plan fallback |

Repairs and failures may change counts but never bypass the interactive budget. Do not publish an end-to-end latency promise based on model parameter counts or this call table.

### 37.2 Metadata build

15,000 documents at batch size 32 require approximately 469 successful embedding calls, plus bounded retries/splits. This is indexing work, not per-query work. Preserve the current model's existing embeddings when changing UI/parser/rerank behavior.

### 37.3 Visual build

A full 15k visual build can require approximately 15k VLM calls plus 469 batched text-embedding calls before retries. At the initial 30-second VLM attempt ceiling, worst-case occupancy is far beyond an interactive task. Use a pilot, explicit admission, durable progress and pause/cancel; do not run it on restart or on an ordinary visual query.

### 37.4 Initial deep request

With 4 books × 3 new windows, a planned batch can use 12 VLM window calls, 1 batched chunk-embedding call, up to 4 optional text aggregations, 1 batched book-embedding call and 1 optional final rank call: **up to 19 initial calls**, leaving at most 5 attempts within a 24-attempt cap. This assumes batching and successful payloads; retries/splits or early incremental flushes consume the same budget and may reduce coverage.

Batch embedding tasks where possible, and reserve necessary downstream calls before scheduling additional windows. The older 8-books × 6-windows assumption is not the new default. Raising book/window limits does not automatically raise the attempt/time caps.

### 37.5 Measurement before further model changes

Measure admission wait, parser, remote scheduler wait, embedding, snapshot refresh, SQL/materialization, optional rerank and verification separately. Compare cold/warm cache, concurrent indexing and normal disk conditions. Use a fixed multilingual intent/hard-filter suite and report median/p95 plus relevance/constraint failures before choosing a smaller model.

Record the existing one-query 3.111→0.153-second local result as a diagnostic observation only. Hosted-NIM timing after the fast-path change remains an open measurement item. HTTP 202/polling solves long-held-request timeouts; it does not guarantee a fast upstream model or eliminate other causes of HTTP 504.

## 38. Validation strategy

### 38.1 Current deterministic coverage

Use `FakeModelProvider`; no key or live endpoint is required in normal CI. Current files cover:

- `test_assistant_schema.py`: schema, counts, previous plans and config;
- `test_assistant_db.py`: dedupe, leases, stale writes, cleanup, namespaces and 15k-scale vector search;
- `test_assistant_recommender.py`: hard filters, unknown IDs, repairs, fallback, aliases, model changes, scan resume, corruption recovery and bounded fast path;
- `test_assistant_http.py`: network/origin/body checks, diagnostics, dedicated route and asynchronous polling;
- `test_assistant_requests.py`: queue capacity/expiry, progress, deadline removal, late completion and whole-request budget;
- `test_nim_client.py`: auth/payload, input types, sanitized errors, Retry-After, timeout/circuit and priority;
- `tests/e2e/assistant_ui_test.ts`: dedicated navigation, desktop/mobile width, polling/reload, follow-up exclusions, diagnostics, XSS and reader independence;
- existing gallery/catalog/presentation suites: regression coverage for shared UI/server assets.

### 38.2 Required V2 tests

Add migration-from-real-V1 fixtures; per-kind claims/cancellation; separate summary/embedding recovery; source replacement/deletion during inference; active namespace selection; coverage states; batch query embeddings and balanced fusion under the 400-record cap.

Image tests must include traversal/encrypted members, corrupt/decompression-bomb images, pixel/member/request caps including base64, one-page books, duplicate sample positions, deterministic natural page order, EXIF stripping and the disabled-upload gate. Mock image payload checks ensure raw bytes never reach logs/job JSON.

UI tests must prove ordinary queries with low coverage still return, do not start VLM work, and render explicit index operations and sampled evidence safely.

### 38.3 Required V3 tests

Test persistent parent/child recovery and idempotency, attempt reservations across crashes, Retry-After beyond remaining time, shared-child cancel/pause, refused books mixed with successes, partial continuation reuse and retention/expiry.

Check page-range ownership and bounds, overlap-aware coverage, unreadable/unknown endings, chunk-to-book aggregation caps, no long-book domination, hallucinated IDs/references, and final verification after concurrent deletion. Run an ordinary recommendation while deep work is queued/in flight and assert the ordinary caller's remote budget remains enforced.

### 38.4 Commands and optional live checks

```sh
python3 -m unittest discover -s tests
deno task e2e
# Explicit opt-in only; ordinary CI skips this hosted test:
NH_RUN_NIM_SMOKE=1 python3 -m unittest discover -s tests -p test_nim_live.py
```

Current live smoke covers text chat/embedding, not image capabilities or end-to-end recommendation latency. Add separate explicit-opt-in V2/V3 pilots with bounded sample sizes and remote-attempt budgets. No secret should be printed to demonstrate that a test loaded the key.

## 39. Remaining implementation sequence

### Phase 0–2 — V1 baseline complete

Configuration, provider/scheduler, diagnostics, metadata sidecar/indexing, bounded retrieval, dedicated page, async ephemeral requests and regression tests are implemented. Maintain them as the baseline. Remaining measurement work is hosted latency/relevance and cold-cache/I/O behavior, not rebuilding V1 as another service.

### Phase 3A — V2 foundation

1. Add tested ordered DB migrations and generalized per-kind job/document APIs.
2. Add provenance, upload gates, bounded image decoding and capability pilot.
3. Add visual-summary and visual-embedding jobs as separate dependencies.
4. Add durable visual operations with checkpoints and pause/resume/cancel.

### Phase 3B — V2 retrieval/UI

1. Add active visual snapshots, invalidation and batched query embedding.
2. Add bounded balanced fusion, sampled evidence and coverage counts.
3. Extend the dedicated page with explicit indexing operations.
4. Complete pilot/acceptance tests before permitting an explicit full build.

### Phase 4A — V3 durability and limits

1. Add/generalize durable request parents and shared dependencies.
2. Implement idempotent admission, budget reservations, active time and cancellation.
3. Implement deterministic windows and persistent chunk observations/embeddings.
4. Prove crash/source-change/partial-recovery behavior before UI expansion.

### Phase 4B — V3 evidence and interaction

1. Add bounded aggregation, cache dependency hashes and book embeddings.
2. Add cached narrative retrieval and optional evidence-reference reranking.
3. Add explicit selected-book deep actions, progress, partial results and continuation.
4. Verify ordinary-search latency under deep load, then run a bounded opt-in pilot.

## 40. Expected future code changes

| Area | V2 work | V3 work |
|---|---|---|
| settings/config examples | validated visual policy/resource limits | deep gates, budgets, retention |
| `db.py` | migrations, per-kind operations, provenance, durable operation state | parent dependencies, reservations, retention |
| `provider.py` / `nim_client.py` | image capabilities/errors, explicit priorities/timeouts | per-attempt budget hooks and deep purposes |
| `images.py` (new) | safe deterministic sparse samples | contiguous windows and source reuse |
| `documents.py` / `schema.py` / `prompts.py` | visual schema/canonical evidence | chunk/book schemas and evidence refs |
| `vector_index.py` | per-kind active namespaces | bounded per-book chunks/aggregation |
| `indexer.py` / coordinator | summary → embedding stages, scans | parent/child dependency progression |
| `recommender.py` | cached visual fusion, coverage | cached narrative fusion, verified refs |
| `requests.py` | preserve ephemeral fast-search contract | do not host durable deep orchestration here |
| `service.py` / `nh_server.py` | operation routes and diagnostics | explicit durable deep routes |
| `local-ui.js/css` | operations/evidence in full page | deep progress/controls/continuation in full page |
| dependencies/tests | Pillow + fixtures/pilots | reuse same stack; add durability/evidence tests |

## 41. Non-goals

No local CUDA/ROCm requirement, second inference machine, Kubernetes/Redis/Celery/vector database, browser-held API key, model access to arbitrary paths, whole-catalog prompts, automatic full visual/deep rebuild on restart, model fine-tuning, or inference-generated authoritative source tags. No new sidebar or frontend replacement. Public multi-user authentication/session isolation is outside this personal allowed-network deployment scope.

## 42. Cross-version invariants

1. CBZs and `library.sqlite3` are authoritative; sidecar AI state is disposable.
2. Model output cannot create a gallery link, taxonomy ID or page-evidence range.
3. Resolved source hard filters apply before retrieval and again before returning results.
4. Ordinary library operation does not depend on NIM or assistant initialization.
5. Ordinary recommendations use short submission/poll HTTP requests and bounded remote waits.
6. New page analysis never becomes a hidden prerequisite of ordinary search.
7. One provider layer owns API/auth/scheduling behavior; new purposes require explicit policies.
8. Reusable durable child evidence is distinct from ephemeral V1 requests and durable V3 parents.
9. Remote image dispatch requires a feature gate and explicit operation/policy; no whole archives.
10. Missing/sampled/partial evidence is represented honestly, not as absence/full-book proof.
11. Retries, repairs and continuation are bounded, source-aware and restart-safe.
12. The dedicated `/AI_assistant` workspace preserves base-path behavior and reader independence.

## 43. Reference material and verification status

Local implementation is the source for this revision's **current behavior**:

- [Operational guide](doc/assistant.md)
- [Settings](server/assistant/settings.py)
- [HTTP/server integration](server/nh_server.py)
- [Recommendation queue](server/assistant/requests.py)
- [Recommender](server/assistant/recommender.py)
- [Sidecar database](server/assistant/db.py)
- [NIM adapter/scheduler](server/assistant/nim_client.py)

Provider documentation consulted during the earlier NIM/V1 implementation is listed below for later re-verification. This 2026-09-26 documentation update does **not** assert a new availability/terms check:

- [NVIDIA API quickstart](https://docs.api.nvidia.com/nim/docs/api-quickstart)
- [NVIDIA LLM API reference](https://docs.api.nvidia.com/nim/re/reference/llm-apis)
- [Nemotron 3 Embed 1B API](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-embed-1b-infer)
- [Nemotron 3 Super catalog page](https://build.nvidia.com/nvidia/nemotron-3-super-120b-a12b)
- [Nemotron 3.5 Lightning catalog page](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b)
- [NVIDIA hosted model catalog](https://build.nvidia.com/models)

Before V2/V3 implementation, re-check the selected visual model's actual image API and limits with official documentation and an explicit pilot. Do not treat an old model table as proof that free hosted image analysis is still available.
