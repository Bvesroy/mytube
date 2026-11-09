# Implementation Progress Log

## Sessions covered
- 2026-02 — Backend v0.1 skeleton: schema/migrations/FTS, importer, search, recommendation
  primitives, job scaffolding, API skeleton, backup/restore scripts.
- 2026-02 — Watch-history foundation (Phase 2), playback bridge (Phase 3), recommendation
  consumption (Phase 4), DB-backed jobs/sync (Phase 5), localStorage import bridge (Phase 6),
  token guard + loopback defaults (Phase 7).
- 2026-11-09 — My Tube 3.0 intelligence increment: all ten features, additive schema,
  deterministic-first AI/embeddings, optional local LLM with citation validation, FSRS-5,
  inline SVG knowledge workspace in the existing `site/index.html`.

## My Tube 3.0 (intelligence increment)

### Completed and verified (backend)
- Additive schema: `Concept`, `ConceptRelation`, `VideoConcept`, `KnowledgeExtract`,
  `EmbeddingVector`, `FlashcardDeck`, `Flashcard`, `ReviewLog`, `LearningPath*`,
  `ResearchDocument`, `VideoComparison`, `TimestampBookmark`, `DiscoveryReport`,
  `DiscoveryEvent`, `ModelConfig`, `ProcessingProvenance`, `PipelineConfig`; additive
  `VideoChapter`/`Transcript` columns; `transcript_fts`, `knowledge_fts`.
- Migrations: Alembic `003_intelligence.py` plus additive-column guard in `engine.py`.
- Services: `app/services/ai/*` (embeddings, local LLM provider), `app/services/knowledge/*`
  (text, transcripts, extraction, index, graph, ask, compare), `app/services/learning/*`
  (FSRS-5, paths), `app/services/video/*` (chapters, bookmarks, perspectives),
  `app/services/analytics/*`, `app/services/discovery/*`, `app/services/intelligence_jobs.py`.
- API: `app/api/intelligence.py` mounted in `app/main.py`; lifespan honors
  `app.state.settings`; `HTTPException` import fix for the existing API-token guard.
- Deterministic-first retrieval/extraction; optional local LLM loopback-only with citation
  validation; LLM answers rejected unless citations point to supplied sources.
- Default embeddings deterministic signed-hashing TF vectors (`mytube-hash-v1`, dim 256);
  optional sentence-transformers only via `MYTUBE_EMBEDDING_MODEL` when already installed.
- Truthful chapters: generated only when `timing_source='supplied'`; untimed transcripts
  report `timing_source='none'` and citations return `timestamp_s: null`.
- FSRS-5 in stdlib; review history stored separately and preserved across card/video deletion
  via restore/export paths.
- Frontend: additive knowledge workspace in `site/index.html` with inline SVG force graph
  (no CDN), hash-routed sections, existing router/nav reused.

### Tested
- `backend/tests/test_intelligence_api.py` covers transcript ingest/search/export, graph,
  ask, comparison + documents, paths CRUD/status/move/next, flashcards generate/due/review/
  stats/export/manual/restore, chapters generate/patch/bookmarks/transcript-search,
  perspectives, timeline summary, radar scan/config/scan-blocking, AI status/config/models,
  pipeline config/run-now.
- Full backend suite passes: **69 tests, 1 deprecation warning**
  (`cd backend && python3 -m pytest tests/ -q`).

### Docs
- `docs/06-open-source-registry.md`
- `docs/07-intelligence-architecture.md`
- `docs/08-intelligence-api.md`
- `docs/09-intelligence-pipeline-ai.md`
- `docs/10-roadmap-3.0.md`
- `docs/progress.md` updated to reflect the 3.0 increment.

### Still open for this increment
- Remaining docs for this increment are written above; README 3.0 section is the last
  narrative doc to add.
- Quality-gate evidence still needs to be regenerated and the gate re-run for the record
  (test report, build metrics, artifact SHA-256, security scan).
- Full browser E2E is not run in this environment.
- Real yt-dlp network fetch/transcript retrieval and bundled ASR are not implemented by
  design.
- No verified YouTube account history sync (never claimed).

## Earlier phases
- **Phase 1 — Audit and API stabilization:** v1 contract fixes, HTTP integration tests,
  DB lifecycle fix.
- **Phase 2 — Watch history foundation:** additive `watch_history`/`watch_sessions` schema,
  idempotent service/APIs, truthful dual-history semantics (`youtube_history_confirmed=false`).
- **Phase 3 — Playback integration:** playback-mode preferences, embedded vs external-youtube
  behavior, truthful notices.
- **Phase 4 — Recommendation integration:** watch events feed the existing recommender's
  `negative` feature; algorithms/weights preserved.
- **Phase 5 — Worker/yt-dlp incremental sync:** DB-backed job service, idempotent `sync_import`,
  truthful yt-dlp adapter hook.
- **Phase 6 — Frontend adapter + settings + safe localStorage migration:** backend bridge in
  `site/index.html`, opt-in backend state migration, standalone fallback preserved.
- **Phase 7 — Security/perf/release readiness:** loopback-first defaults, minimal token guard
  when configured, minimal backup/restore scripts.

## Constraints honored across all work
- No proprietary AI API, paid SaaS, mandatory cloud, telemetry, or automatic model download.
- No undocumented YouTube APIs; no password/cookie collection.
- No fabricated videos, quotes, timestamps, relationships, viewing history, or YouTube sync
  claims.
- Additive schema; existing recommendation engine and standalone frontend preserved.
- Every feature reports status truthfully; the intelligence layer degrades gracefully when the
  backend or optional AI is unavailable.
