# My Tube 3.0 — Intelligence API

Base path: `/api/v1`. Authenticated only when `MYTUBE_API_TOKEN` is set and the request
is not loopback, consistent with the existing policy. JSON only. Errors are `4xx` with a
`detail` message; `500` is reserved for unhandled failures and returns a server trace id.

A few endpoints are **both a resource and an action** (extract, regenerate, scan, review).
Status and side effects are documented per endpoint.

## 1. Transcript + knowledge

### `POST /api/v1/transcripts/ingest`
Store a transcript (normalized text or segment list) for a license-catalogue video.
Idempotent by `(video_id, content_hash)`. Timed segments set `timing_source:'supplied'`;
untimed segments set `timing_source:'none'`. Returns transcript metadata, segment count
and `timing_available`.

### `GET /api/v1/transcripts/{video_id}`
Loaded transcript for a video.

### `GET /api/v1/transcripts/{video_id}/search`
Full-text search over that video's stored transcript segments. Returns hit count and
segment hits with offsets.

### `POST /api/v1/knowledge/extract`
Extract deterministic knowledge for a video, or accept a local LLM response when approved.
Returns counts, concepts, extracts by kind and `timing_available`.

### `GET /api/v1/knowledge/videos/{video_id}`
Summary for a video: extracts, concepts, chapters, bookmarks and channel link.

### `GET /api/v1/knowledge/videos/{video_id}/export`
Export the video's knowledge draft as a named JSON document (`format: mytube-knowledge-v1`).

### `GET /api/v1/knowledge/search`
Search across stored extracts only. Supports prefix matching and a limit.

### `GET /api/v1/knowledge/concepts`
Catalogued extract concepts (exploration aid).

### `POST /api/v1/knowledge/extracts/{id}/regenerate`
Regenerate a non-edited extract. Edited rows are not overwritten.

### `PATCH /api/v1/knowledge/extracts/{id}`
Edit content at a positioned extract, or flip its `rejected` flag.

## 2. Graph

### `GET /api/v1/graph`
Build a subgraph from stored extraction. Optional `limit`, `include_videos`,
`include_collections`, `watched` filter. Returns `nodes`, `edges`, `stats`. Node ids are
prefixed by type (`concept:`, `video:`, `channel:`, `collection:`).

### `GET /api/v1/graph/{node_id}`
Node detail + neighbors. Returns `node` and `neighbors` (related nodes with relation,
confidence, explanation and score).

## 3. Ask My Tube

### `POST /api/v1/ask`
Answer a question using only stored transcripts, extracts and video metadata. Always cites
sources; with a local LLM enabled it rephrases but validates every `[n]`. Returns
`answer`, `mode` (`extractive`|`llm`), `sufficient_evidence`, `citations`,
`matched_concepts`, `retrieval`, `notes`.

### `POST /api/v1/research/ask/save`
Save a question + accepted answer + citations as a research document (`kind: answer`).

## 4. Cross-video comparison and research

### `POST /api/v1/research/compare`
Compare at least two videos with stored knowledge. Deterministic only; label sets and
heuristic conflict detection are conservative. Returns `document_id`, videos, common/unique
explanations, agreements, possible contradictions, unique concepts, different approaches,
missing explanations, complementary videos, and `claim_types`.

### `GET /api/v1/research/documents`
List saved research documents (comparisons and answers, plus future kinds). Optional
`kind` and `limit`.

### `GET /api/v1/research/documents/{document_id}`
A saved document. For `kind: answer` the body is question/answer/citations; for
`kind: comparison` the body is the full comparison payload.

## 5. Learning paths

### `POST /api/v1/learning/paths`
Generate a prerequisite-ordered path from an objective. Reports missing library material
rather than inventing it.

### `GET /api/v1/learning/paths`
List paths (default active). Optional `status`.

### `GET /api/v1/learning/paths/{path_id}`
A path with stages, items, progress, prerequisite gaps and the `next_item`.

### `GET /api/v1/learning/paths/{path_id}/next`
The next todo item.

### `POST /api/v1/learning/paths/{path_id}/items/{item_id}/status`
Set item status to `todo`, `done` or `skipped`.

### `POST /api/v1/learning/paths/{path_id}/items/{item_id}/move`
Move an item within its stage by ordinal position.

## 6. Flashcards

### `POST /api/v1/flashcards`
Create a manual card into a deck (by default the first deck; descriptive fallback deck
when none exist).

### `GET|POST /api/v1/flashcards/decks`
List or create decks.

### `POST /api/v1/flashcards/generate`
Generate cards from a processed video's stored seeds. Reports `created` and `skipped`.

### `GET /api/v1/flashcards/due`
Due cards in a deck. Optional `limit`, `deck_id`.

### `POST /api/v1/flashcards/{card_id}/review`
Submit a 1–4 FSRS rating; updates state/stability/due.

### `GET /api/v1/flashcards/{card_id}`
One card.

### `PATCH /api/v1/flashcards/{card_id}`
Edit a card's front/back.

### `DELETE /api/v1/flashcards/{card_id}`
Delete a card. Content lineage is preserved in the extract for regenerate/restore.

### `POST /api/v1/flashcards/{card_id}/restore`
Restore a deleted card when the chain (deck, origin video/extract) exists.

### `GET /api/v1/flashcards/stats`
Review stats for the last 30 days: due, active, new, reviews window, rating counts,
retention estimate, streak. Retention is a Good/Easy proxy, not mastery.

### `GET /api/v1/flashcards/export/{deck_id}`
Full deck export with cards, history and metadata.

### `POST /api/v1/flashcards/import`
Import a deck from the export format. Skips duplicates; warns on malformed entries.

## 7. Chapters, navigation and multi-perspective discovery

### `POST /api/v1/videos/{video_id}/chapters/generate`
Generate chapters only when `timing_source='supplied'` and enough timed segments exist.
Labels `authored_by:'auto_generated'`.

### `GET /api/v1/videos/{video_id}/chapters`
Chapters for a video, with `count`, `available` and `chapters`.

### `GET|PATCH /api/v1/chapters/{chapter_id}`
Read or edit an existing chapter (title/start).

### `DELETE /api/v1/chapters/{chapter_id}`
Delete a chapter.

### `GET /api/v1/templates/chapters`
Creator-authored chapter templates indexed by exact title hash.

### `POST /api/v1/templates/chapters/{template_id}/use`
Clone a creator chapter template to a video at the best-matching timestamp.

### `GET /api/v1/videos/{video_id}/transcript/search`
Transcript search (also reachable under videos).

### `POST /api/v1/videos/{video_id}/bookmarks`
Save a timestamp bookmark (`timestamp_s`, `label`).

### `GET /api/v1/videos/{video_id}/bookmarks`
Bookmarks for a video.

### `DELETE /api/v1/bookmarks/{bookmark_id}`
Delete a bookmark.

### `GET /api/v1/videos/{video_id}/perspectives`
Subscription-library discovery across topics, novelty, continuation, deep cuts, cross
preferred, underrated, returning and questions. Reuses the existing `recommend()` engine,
deduplicates across views and caps per channel. Always truthful about local-only scope.

## 8. Analytics

### `GET /api/v1/analytics/summary`
Library summary for a period: sessions, completions, early exits, exposure, engagement,
revision, retention proxy, knowledge growth, top subjects, emerging/declining interests.
Divides metrics into known and inferred.

### `GET /api/v1/analytics/timeline`
Time buckets with exposure, engagement, completion, revision, alone/watch ratios,
completions, early exits, knowledge items, concepts, flashcards, review stats, metric
definitions and topic shifts with evidence.
