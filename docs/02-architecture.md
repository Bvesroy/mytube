# 02 — Proposed backend architecture

Status: **proposed**, awaiting approval on the decisions in §3. Nothing in `site/` is modified
by this plan; the backend lives beside it in `backend/` and can be deleted without touching the
working app.

---

## 1. Shape: modular monolith + ports & adapters + one worker process

```
                 ┌──────────────────────────────┐
  browser (A) ──►│ site/index.html  (unchanged) │  data.js + localStorage
  browser (B) ──►│ site/index.html → data layer │──┐  fetch /api/v1
                 └──────────────────────────────┘  │
                                                   ▼
    ┌──────────────────────────────────────────────────────────────┐
    │ FastAPI  app/api/*        transport + validation only        │
    ├──────────────────────────────────────────────────────────────┤
    │ Services app/services/*   importer · sync · search ·         │
    │                           recommendations · backup · jobs     │
    ├──────────────────────────────────────────────────────────────┤
    │ Repositories app/repositories/*   SQL, no business rules     │
    ├──────────────────────────────────────────────────────────────┤
    │ Domain app/domain/*       entities, weights, pure algorithms │
    └──────────────────────────────────────────────────────────────┘
                 ▲                                    ▲
                 │ same services, no HTTP             │
    ┌────────────┴───────────┐          ┌─────────────┴──────────────┐
    │ CLI app/cli.py         │          │ Worker app/worker/runner.py│
    │ import · serve · sync  │          │ claims background_jobs     │
    └────────────────────────┘          └────────────────────────────┘
                                                   │
                              ┌────────────────────┴─────────────────┐
                              │ Adapters app/adapters/*              │
                              │ YtDlpLister · filesystem · clock     │
                              └──────────────────────────────────────┘
```

One process can run API + worker (`app.cli serve --worker`); the worker can also run alone.
Nothing is shared in memory, so the API never depends on the frontend being open.

Dependency rule: `api → services → repositories → domain`. Domain imports nothing from
FastAPI, SQLAlchemy or yt-dlp; adapters are injected into services.

## 2. Module responsibility matrix

| Directory | Owns | Must not |
|---|---|---|
| `app/api/` | HTTP routes, status codes, pagination params, OpenAPI schemas | contain SQL or ranking maths |
| `app/schemas/` | Pydantic request/response contracts | leak ORM models |
| `app/services/importer.py` | `videos.json`/CSV → DB, validation, counts | shell out to yt-dlp |
| `app/services/sync.py` | incremental refresh, backoff, per-channel state | touch HTTP requests |
| `app/services/search.py` | FTS5 query building, filters, ranking merge | write user state |
| `app/services/recommendations/` | candidate → features → score → rerank → explain | know about FastAPI |
| `app/services/jobs.py` | enqueue, claim, cancel, retry, progress | run jobs inline |
| `app/repositories/` | SQLAlchemy queries, transactions | decide policy |
| `app/domain/` | entities, weight vectors, BM25/MMR/lexicon maths | import infrastructure |
| `app/adapters/ytdlp.py` | subprocess call, cache file IO, normalisation | decide retry policy |
| `app/worker/` | polling loop, heartbeat, stale-job recovery | implement job bodies |
| `app/db/` | engine, PRAGMAs, models, FTS5 DDL | business logic |
| `migrations/` | Alembic revisions | be edited by hand after release |

## 3. Decisions (ADR summary)

### ADR-001 — Modular monolith over microservices
**Chosen.** One deployable, four internal layers, one worker.
**Why.** A single user's 60k-row library does not have a scaling problem; it has a complexity
budget. Microservices would add network failure modes, service discovery and 5× the ops work.
**Alternatives.** Microservices (rejected: no independently scaling component), serverless
(rejected: cold starts + no persistent worker + no local-first story).
**Cost.** The worker and API share a Python package; a bug in `services/` can affect both. Mitigated
by layer rules and by tests importing services directly.

### ADR-002 — SQLite (WAL) + FTS5 as the only datastore
**Chosen.** SQLite with `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`,
`busy_timeout=5000`, one writer (the worker) and many readers (API).
**Why.** Zero-install, single-file backup, 60k rows is small (a full scan is ~10 ms), FTS5 gives
BM25 for free, and the file is portable to Postgres later because the SQL stays in repositories.
**Alternatives.** Postgres (rejected: daemon to install, no benefit at this size), DuckDB
(rejected: weak concurrent writes), JSON files (rejected: no transactions, no indexes).
**Cost.** SQLite allows one writer; the worker must be the only writer during a sync. Enforced by a
`BEGIN IMMEDIATE` retry wrapper and by queue design (jobs are claimed, never parallel).

### ADR-003 — Alembic, one linear revision chain
**Chosen.** Alembic with `render_as_batch=True` (SQLite needs table-rebuild batches for ALTER).
**Why.** Schema will change during migration stages 2-9; hand-written migrations drift.
**Cost.** One extra dependency and a `migrations/` tree. Guarded by a test that runs
`alembic upgrade head` on an empty DB and asserts the schema matches the models.

### ADR-004 — Database-backed job queue, no broker
**Chosen.** `background_jobs` table + `UPDATE … RETURNING` claim, heartbeat column, stale-job
recovery, per-type uniqueness index.
**Why.** Durable across restarts (Redis/Celery are not), no extra daemon, and at this scale
(≤ a few jobs per minute) polling every 500 ms is free.
**Alternatives.** Celery/RQ (rejected: need Redis), APScheduler (kept only as an optional cron
trigger that enqueues rows — it never executes work).
**Cost.** Polling adds ≤ 0.5 s latency; long jobs must heartbeat or they are reclaimed.

### ADR-005 — Preserve the recommender as ports, not a rewrite
**Chosen.** `CandidateGenerator`, `FeatureExtractor`, `Ranker`, `Reranker`, `Explainer`,
`Evaluator` as Protocols. Baseline implementations reproduce the current weights and MMR.
Semantic retrieval is an interface with a **null** implementation; no external API.
**Why.** The brief forbids silently changing ranking. The existing JS engine stays the reference
implementation until the Python port passes a parity test on the same history.
**Cost.** Two implementations temporarily exist (§15 migration stages 6-7).

### ADR-006 — Categories reuse the existing lexicon
**Chosen.** Extract `CATS` from `site/index.html` into `app/resources/categories.json` and port the
idf-weighted scorer; overrides still win.
**Why.** One taxonomy, no retraining, no model download, and a user's manual moves keep working.
**Cost.** Changing the lexicon means regenerating a resource file (one script, checked in).

### ADR-007 — Localhost by default, token for network mode
**Chosen.** Bind `127.0.0.1` unless `MYTUBE_HOST` says otherwise; if the host is not loopback,
require `MYTUBE_API_TOKEN` or refuse to start.
**Why.** The brief's failure mode "unauthenticated server exposed to the LAN" must be impossible,
not merely discouraged.
**Cost.** Multi-device use needs one env var and a bearer token.

### ADR-008 — Thumbnails and durations are stored as given, not invented
No scrape of `i.ytimg.com` at import time; the API exposes `thumbnail_url` when present and the
frontend keeps deriving `https://i.ytimg.com/vi/<id>/hqdefault.jpg` when it is null.

## 4. Directory tree

```
tube/
├── site/                    # unchanged offline app (Mode A) — not touched by this work
├── build.py                 # unchanged Takeout builder — still the source of data.js
├── docs/                    # this design set
└── backend/
    ├── pyproject.toml
    ├── alembic.ini
    ├── migrations/          # env.py + versions/
    ├── app/
    │   ├── config.py        # Settings (env-driven), paths, feature flags
    │   ├── main.py          # create_app(): routers, CORS, error handlers
    │   ├── cli.py           # import-json, sync, serve, worker, job, backup, diag
    │   ├── db/
    │   │   ├── engine.py    # engine factory, PRAGMAs, session_scope, retry
    │   │   ├── models.py    # SQLAlchemy 2.0 mapped classes
    │   │   └── fts.py       # FTS5 DDL + trigger management
    │   ├── domain/
    │   │   ├── weights.py   # DEFAULT_W / BASE_W, feature order, BPR helpers
    │   │   ├── bm25.py      # pure BM25 scoring (parity with index.html)
    │   │   ├── mmr.py       # MMR + channel cap
    │   │   └── categories.py# idf-weighted lexicon classifier
    │   ├── repositories/
    │   │   ├── library.py   # channels, videos, categories, stats
    │   │   ├── user_state.py# history, ratings, hidden, courses, prefs
    │   │   └── jobs.py      # enqueue/claim/heartbeat/finish/recover
    │   ├── services/
    │   │   ├── importer.py
    │   │   ├── sync.py
    │   │   ├── search.py
    │   │   ├── recommendations.py
    │   │   ├── jobs.py      # job type registry, dispatch table
    │   │   ├── backup.py
    │   │   └── diagnostics.py
    │   ├── adapters/
    │   │   └── ytdlp.py     # ChannelLister protocol + real + fake
    │   ├── api/
    │   │   ├── deps.py      # session, pagination, auth
    │   │   └── v1.py        # routers
    │   ├── schemas/         # Pydantic contracts
    │   ├── worker/
    │   │   └── runner.py    # loop, heartbeat, cancel, recover
    │   └── resources/
    │       └── categories.json
    └── tests/
        ├── conftest.py      # temp DB, seeded fixtures, stub lister
        ├── test_import.py
        ├── test_api.py
        ├── test_search.py
        ├── test_jobs.py
        ├── test_reco.py
        └── test_migration.py
```

## 5. Runtime data layout

```
~/.local/share/mytube/          # MYTUBE_DATA_DIR
├── mytube.db                   # SQLite (+ -wal, -shm)
├── backups/mytube-<ts>.db
├── cache/raw_<UCID>.json       # moved from .cache/ by the importer on first run
└── logs/mytube.log             # rotating, structured JSON lines
```

## 6. Frontend integration (Mode A / Mode B)

One frontend, one adapter file (`site/data-layer.js`, added in stage 6) that exposes:

```js
DataLayer.load()          // → {channels, videos, meta}
DataLayer.state.get/set   // localStorage | /api/v1/state
DataLayer.recommend(q)    // client engine | /api/v1/recommendations
DataLayer.watch(event)    // localStorage | POST /api/v1/history
```

`DataLayer.mode` is `"local"` when `/api/v1/health` does not answer within 800 ms, else
`"backend"`. Offline is therefore the default, not an error path. The current inline engine is
kept behind `mode: "local"` until parity tests pass.

## 7. Security threat model (summary)

| Threat | Control |
|---|---|
| SQL injection | SQLAlchemy bound params only; FTS5 query built from tokens, never raw SQL |
| Shell injection | `subprocess.run([...], shell=False)`, channel id validated `^UC[\w-]{22}$` |
| Path traversal | no user-supplied paths in API; backup paths generated server-side |
| LAN exposure | loopback bind + token required when `MYTUBE_HOST` is not loopback |
| Untrusted backup file | schema + magic check, size cap, restore into a temp DB and validate before swap |
| DoS by expensive query | FTS query length cap, result cap, `limit ≤ 100`, timeout |
| Data exfiltration | no outbound calls except yt-dlp metadata; no telemetry |
| PII in logs | log channel/video ids and counts, never titles of private notes |

## 8. Performance targets (to be measured, not assumed)

| Operation | Target | How measured |
|---|---|---|
| `GET /videos?limit=50` | < 15 ms p95 | pytest benchmark on 58,903-row DB |
| FTS5 search (1 term) | < 25 ms p95 | same |
| `/recommendations` (cold) | < 250 ms | same |
| Full import of `videos.json` | < 30 s | import timing recorded in `ingestion_runs` |
| Worker claim → start | < 1 s | heartbeat timestamps |
| DB size | < 40 MB for 60k videos | file size |

## 9. Testing strategy

Unit (domain maths, no DB) · repository (temp SQLite, real SQL) · API (`TestClient`, contract
shapes, error codes) · import (full `videos.json` fixture subset, duplicate + malformed rows) ·
sync (stub lister: success, transient failure, 429, partial) · jobs (claim, retry, cancel,
recovery of a stale `running` row) · migration (`upgrade head` on empty DB) · reco regression
(weights + ordering golden test so the port cannot drift silently) · backup/restore round-trip.
No network in CI: yt-dlp is always the stub adapter.

## 10. Migration stages (see docs/05-roadmap.md for the implementation checklist)

1. Audit (**done**, docs/01-audit.md).
2. Schema + import of existing data.
3. Repositories + API read paths.
4. Ingestion moved into services.
5. Incremental sync + worker.
6. User state behind repository, localStorage compatibility.
7. Search + recommendation services wired to the API, parity tests.
8. Optional multi-device sync (token auth).
9. Optimise + validate; frontend switches to Mode B when a backend is present.

Rollback: `site/` and `build.py` are untouched at every stage; deleting `backend/` returns the
product to exactly today's behaviour.
