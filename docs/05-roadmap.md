# My Tube Backend — Roadmap & Acceptance Criteria

## Current status (v0.1 skeleton)
A local-first FastAPI + SQLite backend skeleton exists in `backend/` alongside the existing static app (`site/`). The existing app is untouched.

### Implemented (verified)
- **Schema/migrations**: Alembic + idempotent DDL migration (`001_initial`), SQLite WAL/FTK pragmas, FTS5 (`video_fts`) with triggers. Fresh-DB `upgrade head` and schema creation work.
- **Data model**: SQLAlchemy 2.x models for libraries/channels/videos/categories/tags, user state, jobs, sync metadata, exports, settings, and supporting tables.
- **Importer**: `app.services.importer.import_channels` parses channel/video payloads, bulk-upserts (SQLite `ON CONFLICT`), computes metadata hashes, and integrates with category classification hooks. Idempotent re-import does not create duplicates.
- **Search**: FTS5-backed `search_videos` service with safe query construction and cursor-based pagination (offset cursor).
- **Recommendations**: Domain primitives ported (`DEFAULT_W`, scoring, weight vector, prior blending, MMR/vector utilities) behind service/repository boundaries; `recommendations` service and explainer interfaces present.
- **Job infrastructure**: DB-backed job model/service scaffolding (`jobs_service`, worker runner) — queue/claim/heartbeat patterns in place; yt-dlp adapters not fully wired.
- **API skeleton**: FastAPI app with CORS, request IDs, error handling, lifespan DB wiring; v1 router mounted. Core service-layer functionality verified; some HTTP endpoint contracts still need alignment with service signatures/schemas.
- **Docs**: `docs/01-audit.md`, `02-architecture.md`, `03-database.md`, `04-api.md`.

### Known gaps / not yet production-hardened
- **HTTP API contract alignment**: Several v1 endpoints have signature/shape mismatches with underlying repos/services (e.g., stats args, list_videos cursor semantics, channel detail helpers, job/user-state serialization). Service-layer core works; endpoint integration needs a focused pass.
- **Job execution**: Worker/queue runtime and yt-dlp adapter wiring need end-to-end validation with real jobs and cancellation/retry semantics.
- **FTS maintenance**: Rebuild/optimize triggers and backfill strategy for large imports not fully automated.
- **Auth for non-loopback**: Token enforcement path exists in design/config intent; needs integration tests for bind/host modes.
- **Performance measurement**: No benchmark suite yet for 10x/100k+ video target; indexes and query plans should be measured before claims.
- **Frontend integration**: Backend is additive; `site/` not modified. A compatibility/export bridge and incremental migration plan remain.

## Acceptance criteria (v0.1)
| Area | Criterion | Status |
|---|---|---|
| Safety | No changes to `site/`, `build.py`, `.cache/` behavior | ✅ |
| Local-first | Runs on one machine with SQLite; no mandatory cloud/API keys | ✅ (core) |
| Migrations | `alembic upgrade head` on fresh DB creates schema + FTS + seeded `schema_version` | ✅ |
| Integrity | `PRAGMA integrity_check` passes on fresh DB | ✅ |
| Import | Can import channel/video payloads, upsert idempotently, persist to DB | ✅ |
| Search | FTS5 search returns relevant hits for imported content | ✅ |
| Recommendations | Scoring/weights primitives preserved; service interface exists | ✅ (core) |
| Jobs | DB job model + service scaffolding present | ⚠️ partial |
| Tests | Automated tests cover migrations, importer, search, domain primitives | ✅ (7 tests, core) |
| Docs | Audit, architecture, DB, API, roadmap docs present | ✅ |

## Next milestones
1. **API integration pass**: Align v1 endpoints with repo/service contracts; add HTTP-level tests (FastAPI TestClient).
2. **Worker E2E**: Implement/wire yt-dlp adapter behind port, run a real refresh job with progress/cancel/retry, verify persistence.
3. **FTS ops**: Auto-rebuild/backfill hooks after large imports; benchmark query latency and add indexes based on `EXPLAIN QUERY PLAN`.
4. **Auth & LAN**: Enforce token on non-loopback binds; add config/CLI docs and smoke tests.
5. **Frontend bridge**: Define compatibility layer (read from backend when available, fall back to static files) and migration/rollback steps.
6. **Perf/scale validation**: Import synthetic 100k+ video dataset; measure import time, search latency, recommendation latency; tune before scaling claims.

## Rollback
Delete `backend/` and any `MYTUBE_*/Application Support/mytube` data dir created for backend testing to restore the original static-only behavior. No `site/` files were modified.
