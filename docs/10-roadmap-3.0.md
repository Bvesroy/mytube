# My Tube 3.0 — Roadmap and Acceptance Criteria

## Status

This is the intelligence increment of My Tube. It is additive: it preserves the existing
subscription-only library, the existing recommendation engine, the standalone frontend and
the existing localStorage behavior. It adds a local-first intelligence layer with ten
features, all of which degrade gracefully when the backend or optional AI is unavailable.

**What is open source here:** the backend in `backend/`, the static app in `site/`, the
build tooling, and the docs. There is no proprietary AI API, no mandatory cloud, no paid
tier, no telemetry, and no automatic model download.

**What is optional:** a local LLM backend and/or an installed sentence-transformers model.
If neither is configured, the platform uses deterministic fallbacks for extraction,
search, graph, Ask, comparison, embeddings and chapters.

**What is not implemented:** real YouTube transcript fetch, network yt-dlp transcript
retrieval, bundled ASR, and any claim of synchronization with a YouTube account's watch
history. External playback is recorded as My Tube-initiated only.

## Ten features

| # | Feature | Status |
| --- | --- | --- |
| 1 | Knowledge graph | Implemented and tested. Graph from stored extraction, concepts, relations, video/channel nodes, neighbors, confidence and provenance. |
| 2 | Ask My Tube | Implemented and tested. Answers from stored transcripts/extracts/metadata with citations; optional local LLM with citation validation; extractive fallback. |
| 3 | Video-to-knowledge extraction | Implemented and tested. Deterministic extraction plus optional local LLM path; concepts, extracts by kind, chapters, bookmarks, export. |
| 4 | Cross-video comparison | Implemented and tested. Deterministic comparison with common/unique explanations, agreements, conservative conflict detection, research documents. |
| 5 | Learning paths | Implemented and tested. Prerequisite-ordered paths, stages, items, status, move, next, progress, missing-material reporting. |
| 6 | Spaced repetition / flashcards | Implemented and tested. Decks, generate from seeds, due cards, FSRS-5 review, stats, export, import, restore. |
| 7 | Chapters / navigation | Implemented and tested. Auto-generated chapters only from timed transcripts, read/edit/delete, timestamp bookmarks, transcript search. |
| 8 | Multi-perspective discovery | Implemented and tested. Subscription-library discovery views reusing the existing recommender; deduped and capped per channel; local-only truthful. |
| 9 | Interest timeline | Implemented and tested. Period summary and timeline buckets with exposure, engagement, completion, revision, knowledge growth and topic shifts. |
| 10 | Discovery radar | Implemented and tested. Subscription-only scan with reports, events, config, scheduled/one-off jobs, disable/pause. |

## Acceptance criteria

| Area | Criterion | Status |
| --- | --- | --- |
| Open source | No proprietary AI, mandatory cloud, telemetry, automatic model download or paid tier | ✅ |
| Local first | One machine, SQLite, loopback default, optional token guard | ✅ |
| Additive | Existing library, recommender, frontend and localStorage preserved | ✅ |
| Ten features | All ten features have backend API + frontend workspace and tests | ✅ (backend tests green) |
| Deterministic fallback | Every feature has a path that works without AI | ✅ |
| Truthful | No invented videos, quotes, timestamps, relationships, viewing history or YouTube sync claims | ✅ |
| Migrations | Additive schema changes; existing data untouched | ✅ |
| Tests | Backend test suite passes | ✅ |
| Docs | Architecture, API, pipeline, registry, roadmap and progress present | ✅ (this doc plus 01–09) |

## Known gaps

- Real transcript fetch and ASR are not implemented by design; the platform accepts
  user-supplied transcripts and captions.
- Full browser E2E is not run in this environment; the frontend JS passed a Node syntax
  check and the backend API tests cover the contracts.
- Performance at large library scale has not been benchmarked.
- The quality gate for this increment is run as part of handoff, not assumed.

## Non-goals

- Replacing the existing recommendation engine.
- Adding cloud services, accounts, or proprietary AI.
- Claiming YouTube account history sync or unverified playback confirmation.
- Bundling model weights.

## Next milestones

- Finalize handoff evidence, including the quality gate verdict.
- Optional: larger-library performance passes if radar, search or graph become the bottleneck.
- Optional: deeper frontend polish for the intelligence workspace if wanted.
