# My Tube 3.0 — Intelligence Pipeline, AI and Restore

This doc records how the optional AI/embedding pipeline is wired, how deterministic
fallbacks work without any model, and how the database is backed up and restored so the
platform stays genuinely local-first and reversible.

## 1. What the pipeline does

The intelligence layer is a set of optional stages that read and enrich the same imported
subscription library that the rest of My Tube already uses:

- transcript ingestion and FTS5 indexing
- deterministic knowledge extraction and concept graphs
- optional local LLM-assisted extraction/answers/comparison with citation validation
- deterministic or opted-in embedding vectors
- generated chapters (only from timed transcripts), navigation bookmarks
- FSRS-5 flashcard decks and review logs
- prerequisite-ordered learning paths
- subscription-only discovery radar scans
- analytics from real watch/review/creation events

Nothing in the pipeline fetches YouTube, invents content, or phones home. Every derived
row keeps a source, an optional model identity, and provenance JSON.

## 2. Deterministic core (no AI required)

The default path is fully offline and uses no model:

- transcripts are normalized, hashed and stored in segments plus `transcript_fts`
- knowledge extraction is deterministic by default and produces summaries, definitions,
  terms, examples, formulas, code, timestamps, prerequisites, generated questions and
  flashcards seeds, plus concepts/relations/video-concept links
- text is indexed in `knowledge_fts`
- embeddings default to a deterministic 256-dim signed-hashing vectorizer
  (`mytube-hash-v1`)
- Ask, graph, comparison and radar all have deterministic retrieval paths
- generated chapters only exist when `timing_source='supplied'` and enough timed segments
  are present; untimed transcripts report chapters as unavailable with a reason

This is the baseline. A user who never enables AI still gets transcripts, search, knowledge
extracts, concept graphs, comparison, chapters from timed transcripts, flashcards from
deterministic seeds, paths, radar and analytics.

## 3. Optional AI (local only)

AI is opt-in through environment and UI configuration:

- `MYTUBE_AI_ENABLED=1` enables AI features
- `MYTUBE_AI_PROVIDER` selects the provider; the default/local path targets an already-run
  local LLM backend such as Ollama or llama.cpp
- `MYTUBE_AI_URL` is the loopback endpoint used by the local provider
- `MYTUBE_ALLOW_REMOTE_AI=1` is required for any non-loopback provider endpoint; the
  default rejects remote endpoints
- `MYTUBE_EMBEDDING_MODEL` opts a project into sentence-transformers only if it is already
  installed; otherwise the deterministic hash vectorizer is used

Rules for AI output:

- the local provider is loopback-only by default
- an LLM-generated answer is accepted only when every `[n]` citation points to a supplied
  source; otherwise the extractive answer is shown with a note
- extraction rows accept LLM content only through the approved ingestion path, and
  regeneration never overwrites a row marked `edited`
- model identity and provenance are recorded so the effect of changing a model is auditable

No model is downloaded by the platform. If the user has not installed or started a local
LLM backend, AI features report the real status and degrade to deterministic behavior.

## 4. Jobs and provenance

Long-running stages run through the existing job service with progress, stages,
cancellation and retry:

- transcript ingest
- knowledge extraction
- embedding indexing
- chapter generation
- flashcard generation
- radar scans
- optional AI-assisted passes

The `processing_provenance` table is generic: `entity_type`, `entity_id`, `step`,
`model_id`, content hashes and timestamp. It is the audit trail for any stage without
adding one column per feature. `model_configs` stores the model identities in use, and
`pipeline_configs` stores persisted pipeline choices such as embedding model selection.

## 5. Idempotency and reprocessing

- Transcripts are keyed by `(video_id, content_hash)`; identical input is a no-op unless
  `force=true`.
- Extraction rows marked `edited` are never overwritten by regeneration.
- `regenerate_extract()` creates a replacement for non-edited rows.
- Auto-generated chapters delete only prior `source='generated'` rows.
- Embeddings are upserted by `(owner_type, owner_id, model_id)` so re-indexing is
  incremental.
- Jobs support dedupe keys and cancellation/retry through the existing job service.

## 6. Restore, backup and downgrade

My Tube remains a file on your machine. The database is a single SQLite file.

- Stop the backend, then copy `mytube.db` (and any related files you care about) to a
  backup location. A small helper script exists in `backend/scripts/backup_db.py` for
  online copies, and `restore_db.py` refuses to overwrite the current database unless you
  pass `--force`.
- To remove the intelligence layer and return to the earlier behavior, delete the
  `backend/` data directory used for testing and remove the backend artifacts you added.
  Existing library, watch history, settings and frontend state are preserved because the
  schema changes were additive and the frontend still runs standalone from `site/`.
- The frontend intelligence workspace also degrades gracefully: if the backend is not
  reachable, the views show an offline state and the rest of the app still works from
  `site/index.html`.

No cloud, no token, no mandatory AI, no implicit data upload. The platform is the database
file and the static app; everything else is optional.

## 7. Configuration summary

| Variable | Purpose | Default behavior |
| --- | --- | --- |
| `MYTUBE_AI_ENABLED` | enable AI features | disabled |
| `MYTUBE_AI_PROVIDER` | provider selection | local/default |
| `MYTUBE_AI_URL` | loopback AI endpoint | provider default |
| `MYTUBE_ALLOW_REMOTE_AI` | allow non-loopback AI | rejected |
| `MYTUBE_EMBEDDING_MODEL` | embedding model choice | deterministic hash |

These are the only AI-related switches. There is no secret, no account, and no paid tier.
