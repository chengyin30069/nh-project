# V1 Library Assistant

V1 adds metadata-only recommendations to the existing library server. It uses
local SQLite filters and NumPy vector search; NVIDIA NIM parses requests and
ranks a bounded candidate packet. Only actual downloaded galleries can be linked.
Reasons shown in the UI are generated from verified metadata. V1 does not infer
page contents, visual style, or plot.

## Setup

Install `requirements-server.txt`, or rebuild the existing server Docker image.
Copy the `assistant` section from `config.example.yaml` into your configuration,
set `assistant.enabled: true`, and provide `NVIDIA_API_KEY` in the server process
or Compose environment. Keep the key out of YAML and Git. The default is disabled;
no key or remote calls are needed to run the ordinary library.

Open **Library Assistant → Connection & metadata index → Build / resume index** for the first index.
This is an explicit operation: queries, canonical metadata during indexing, and
bounded candidate metadata during reranking are sent to NVIDIA. No page images
are uploaded in V1, including when `remote_image_analysis_enabled` is true.

The same operation is available over the existing allowed-network API:

```sh
curl -X POST http://127.0.0.1:8766/_nh-local/api/assistant/index/metadata \
  -H 'Content-Type: application/json' -d '{}'
```

Add the deployment's base path before `/_nh-local` if configured. Browser POSTs
are same-origin only. Port 8765 is unchanged.

## Operation

- `GET /_nh-local/api/assistant/health` or `/index/status` reports index coverage,
  provider state, scan state, and durable job counts without credentials.
- `POST .../recommend` accepts `message`, `mode`, `limit`, and `previous_plan`.
  Requests return 202 and are polled through `/jobs/{job_id}`; `deep` uses metadata with a warning.
- `POST .../index/gallery/{id}` refreshes one downloaded gallery.
- `POST .../index/retry-failed` retries failed jobs for the active embedding model.
- New and updated metadata enqueue incremental jobs; deleted galleries invalidate
  the vector snapshot. Unchanged documents are never embedded again.
- Set `background_enabled: false` and restart to pause remote background work.
  Restore it to resume queued/retryable jobs. Full scans checkpoint their cursor.
- Model changes use a separate embedding namespace and queue a metadata rebuild.
  Old model vectors are not mixed with new query vectors.
- The scheduler uses one remote worker, which stays within the configured
  concurrency maximum. Interactive work overtakes queued background work.
- 429/503 and network failures use bounded retries, Retry-After and durable
  backoff; a circuit breaker supplies immediate local fallback during outages.
- Missing keys allow local metadata search. Natural-language interpretation is
  then unavailable; previously validated filters remain active and warnings
  explain the limitation. Ambiguous taxonomy names are shown as unresolved.
- **New search** clears follow-up state. **Exclude from next search** adds a local
  gallery exclusion. Browser state uses deployment-scoped sessionStorage.

State lives in `<storage>/.nh-local/assistant.sqlite3`. Its documents, jobs and
vectors are disposable; deleting it does not remove CBZs or the library catalog.
Stop the server before manually removing SQLite files. Corrupt assistant databases
are quarantined separately. Legacy records without page counts are filled from
local archive entries during the explicit index scan; unknown counts never satisfy
hard page bounds.

No full index is triggered merely by restarting with an unchanged model. Remote
request logs contain only purpose/model, timing, status, retry/input counts and
numeric token usage. Prompts, responses, image bytes, and keys are not logged.

## Validation

```sh
python3 -m unittest discover -s tests
# Includes deterministic assistant browser coverage using FakeModelProvider:
deno task e2e
# Optional, explicitly sends test text to NIM:
NH_RUN_NIM_SMOKE=1 python3 -m unittest discover -s tests -p test_nim_live.py
```

NIM's [embedding API reference](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-embed-1b-infer)
defines `query`/`passage` input modes and the input length limit. Model IDs are
configurable; normal startup makes no model-availability probe.

## Configuration and API key troubleshooting

The panel shows the configuration source, the effective `enabled` flag, whether
`NVIDIA_API_KEY` was actually loaded by the server, and initialization/provider
errors. It never displays the key. **Check NIM connection** sends one short test
text to the configured embedding model and distinguishes missing keys, rejected
credentials (401), access denial (403), unavailable models (404), and throttling
(429). A loaded key alone does not mean that NIM has accepted it.

For Docker Compose, copy `.env.example` to `.env`, set `NVIDIA_API_KEY` there,
and protect the file with `chmod 600 .env`. `.env` is ignored by Git and excluded
from the image build. Python launched directly reads its process environment;
it does not automatically read Compose's `.env` file.

After changing `.env` or YAML, use:

```sh
docker compose up -d --build --force-recreate nh-server
```

A plain `docker compose restart` does not load changed environment variables.
Editors that replace `config.yaml` by renaming a new file can also leave an
existing single-file Docker bind mount attached to the previous file; recreating
the container refreshes that mount. Changes are read at startup, not hot-reloaded.

## Dedicated assistant and responsive searches

Open `/AI_assistant` (or `<base_path>/AI_assistant`). Library and reader pages
link to this workspace instead of opening a sidebar. The workspace provides
full-width conversation turns, responsive recommendation cards, follow-ups and
an expandable connection/index section. It retains the last five turns in the
tab's sessionStorage; the server still receives only the latest message and
validated structured plan, not the transcript. **New search** clears that state.

`POST .../assistant/recommend` now validates and returns **202** immediately with
`job_id`, `status: queued`, and a progress stage. Poll
`GET .../assistant/jobs/{job_id}`; a `ready` job contains the recommendation in
`result`. The browser resumes polling after reload and pauses polling in hidden
tabs. This prevents inference from holding a reverse-proxy request open. Requests
are bounded to two active/queued searches, and completed jobs expire after 15
minutes (at most 64 retained). These interactive jobs are ephemeral: after a
server restart, submit the query again. Metadata indexing remains durable.

Fast defaults use the parser and query embedding, followed by local ranking:

```yaml
assistant:
  # Other settings are as in config.example.yaml.
  rerank_enabled: false
  interactive_timeout_seconds: 20
  interactive_budget_seconds: 40
  max_retries_interactive: 0
  rerank_candidate_count: 12
```

The provider applies the interactive budget to queue waits and HTTP calls.
Expired unsent requests are removed; already-sent calls finish in the provider
worker without blocking local fallback. Background indexing retains its separate
75-second timeout and durable retries. The optional quality rerank can be enabled
with `rerank_enabled: true`, but shares the same interactive budget.

The Nemotron chat adapter disables thinking for the configured Nemotron 3/3.5
models. Parser output is capped at 600 tokens; optional reranking returns only
IDs (250 tokens), because visible reasons come from verified local metadata.
Existing embedding model and stored vectors do not need to change. Candidate
metadata is loaded for at most 400 records plus final verification instead of
walking the entire collection. Responses include `elapsed_seconds` and per-stage
`timings` to distinguish local search time from hosted inference latency.
