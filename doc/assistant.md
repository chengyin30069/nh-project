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

Open **Assistant → Metadata index → Build / resume index** for the first index.
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
  V1 returns synchronously; `deep` requests receive metadata results with a warning.
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
