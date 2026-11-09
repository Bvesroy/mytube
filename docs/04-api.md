# 04 — API specification (`/api/v1`)

Transport: JSON only, UTF-8. Base path `/api/v1`; OpenAPI at `/api/v1/openapi.json`, docs at
`/api/v1/docs`. Every response carries `X-Request-ID`.

## 1. Conventions

**Errors** — one shape, always:

```json
{"error": {"code": "not_found", "message": "No video with id dQw4w9WgXcQ",
           "details": {"resource": "video"}, "request_id": "9f2c…"}}
```

| HTTP | `code` values |
|---|---|
| 400 | `invalid_argument`, `invalid_cursor`, `invalid_filter` |
| 401 | `unauthorized` |
| 403 | `forbidden` (non-loopback without token) |
| 404 | `not_found` |
| 409 | `conflict`, `job_already_running` |
| 422 | `validation_error` (Pydantic detail included in `details.fields`) |
| 429 | `rate_limited` |
| 500 | `internal_error` (message is generic; request id is the lookup key) |
| 503 | `database_busy`, `worker_unavailable` |

**Pagination.** List endpoints use opaque keyset cursors:

```
GET /videos?limit=50&cursor=eyJrIjoxNzExMjM0NTY3ODkwLCJpZCI6MTIzNH0
→ {"items":[…], "next_cursor":"…"|null, "total_estimated": 58903}
```

`limit` default 50, max 100. Search is the one exception: ranked results use `offset` (max 1,000)
because FTS rank is not a stable sort key; this is documented in the response as
`"pagination": "offset"`.

**Idempotency.** Mutating long-running commands accept `Idempotency-Key: <string>`; the value is
stored as `background_jobs.dedupe_key`, so a retried request returns the existing job
(`202` + `{"job_id": …}`) instead of starting a second one.

**Auth.** Loopback (`127.0.0.1`/`::1`) needs no header. Any other bind requires
`Authorization: Bearer $MYTUBE_API_TOKEN`; otherwise the process refuses to start.

## 2. Endpoints

### System

| Method | Path | Purpose | Request | Response |
|---|---|---|---|---|
| GET | `/health` | liveness + schema + worker | — | `{status, version, schema_version, db: {ok, latency_ms}, worker: {alive, last_seen_ms}}` |
| GET | `/diagnostics` | counts, sizes, failures, latencies, integrity | — | `{library:…, db:…, jobs:…, sync:…, search:…, integrity_check}` |

`/health` returns 200 with `status:"degraded"` when the DB is readable but the worker is stale,
and 503 when the DB fails.

### Library

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/library/stats` | totals | `{channels, videos, hours, categories, last_import_at, channels_failed, videos_unavailable}` |
| GET | `/channels` | list | filters `q`, `category`, `hidden`, `sort=name\|recent\|videos`, cursor |
| GET | `/channels/{yt_id}` | one channel | 404 if unknown; includes `sync_state`, `rating`, `hidden`, `video_count` |
| GET | `/channels/{yt_id}/videos` | channel page | `sort=recent\|popular`, cursor, `include_hidden` |
| GET | `/videos` | list | filters `channel`, `category`, `min_duration`, `max_duration`, `available`, `sort` |
| GET | `/videos/{yt_id}` | one video | includes channel summary, tags, `watched`, `hidden`, `bookmarked` |
| GET | `/categories` | buckets | `{items:[{slug,name,channel_count,video_count}]}` |

`Channel` = `{yt_id, name, url, handle, avatar_url, description, subscriber_count, category,
video_count, rating, hidden, last_synced_at}`.
`Video` = `{yt_id, title, channel_yt_id, channel_name, duration_s, view_count, published_at,
thumbnail_url, available, watched, hidden}`.

### Search

| Method | Path | Purpose |
|---|---|---|
| GET | `/search` | hybrid retrieval |
| GET | `/search/suggest` | prefix suggestions, max 10 |

Parameters: `q` (required, ≤ 200 chars), `type=video\|channel\|all` (default `all`),
`channel`, `category`, `min_duration`, `max_duration`, `include_watched` (default true),
`include_hidden` (default false), `sort=relevance\|recent\|views`, `limit`, `offset`.

Response items carry `{score, matched_fields:["title","channel"], snippet, …entity}` and
`"pagination":"offset"`. An empty/whitespace `q` is a 400, never a full table scan.

### Recommendations

| Method | Path | Purpose | Request |
|---|---|---|---|
| GET | `/recommendations` | ranked feed | `surface=for_you\|similar\|new_to_you\|because`, `seed` (video id for `similar`/`because`), `limit` (≤100), `explain=true` |
| GET | `/recommendations/up-next` | continuation | `seed` video id, `limit` ≤ 30 |
| POST | `/recommendations/feedback` | thumbs / not-interested | `{video_id, kind: not_interested\|undo}` |
| GET | `/recommendations/model` | active model | — → `{kind, version, weights, trained_at, metrics}` |
| POST | `/recommendations/train` | enqueue training | `{epochs?, negatives?}` → 202 `{job_id}` |
| POST | `/recommendations/evaluate` | enqueue holdout eval | `{k?}` → 202 `{job_id}` |

Recommendation item: `{video: Video, score, reasons:[{feature, contribution}], shelf}`.
`explain=false` omits `reasons` (smaller payload).

### User state

| Method | Path | Purpose |
|---|---|---|
| GET | `/history` | paged, `limit`, `cursor`, newest first |
| POST | `/history` | watch event `{video_id, engagement?, position_s?, completed?}` — upsert, returns the row |
| DELETE | `/history/{video_id}` | forget one video |
| DELETE | `/history` | clear all (requires `{"confirm": true}` body) |
| GET/PUT | `/preferences` | `{reco:{affinity,halfLife,watched,serendipity}, filters:{shorts,unwatched}}`, validated ranges |
| GET/PUT/DELETE | `/ratings/{channel_yt_id}` | `{rating: 1..5}` |
| PUT/DELETE | `/hidden/{kind}/{yt_id}` | `kind ∈ channel\|video\|category` |
| GET/POST | `/courses`, PATCH/DELETE `/courses/{id}` | create from `{name, video_ids[]}` |
| POST/DELETE | `/courses/{id}/videos/{video_yt_id}` | membership, `position` optional |
| GET/POST/PATCH/DELETE | `/collections[/{id}]` | same shape as courses |
| GET/POST/PATCH/DELETE | `/notes[/{id}]` | `{video_id?, course_id?, body, timestamp_s?}` |
| PUT/DELETE | `/bookmarks/{video_yt_id}` | |
| GET | `/state/export` | `{mytube:1, exportedAt, state:{…}}` — **byte-compatible with the frontend's backup**, including `reco`, `filters`, `ranker`, `bandit` |
| POST | `/state/import` | validates `mytube:1`, applies in one transaction, returns counts |

`/state/import` accepts both the 9-key frontend export and the extended 13-key form.

### Ingestion and sync

| Method | Path | Purpose | Body |
|---|---|---|---|
| POST | `/ingest/import` | seed/refresh from a JSON export | `{path?}` → 202 `{job_id}` |
| POST | `/sync` | full incremental sync | `{channels?, refresh?, per_channel?}` → 202 `{job_id}` |
| POST | `/sync/channels/{yt_id}` | one channel | `{refresh?}` → 202 `{job_id}` |
| GET | `/sync/status` | per-channel state | `{ok, failed, never, throttled, due}` + up to 50 failures |

### Jobs

| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs` | `status`, `type`, `limit`, cursor |
| GET | `/jobs/{id}` | one job incl. `progress`, `stage`, `error` |
| POST | `/jobs/{id}/cancel` | sets `cancel_requested`; worker exits at the next checkpoint; terminal jobs → 409 |

### Backup and maintenance

| Method | Path | Purpose |
|---|---|---|
| POST | `/backup` | 202, enqueues `backup`; `{kind: sqlite\|json_state}` |
| GET | `/backups` | history with sizes and sha256 |
| POST | `/restore` | `{backup_id, confirm:true}` — targets the **enqueued** restore, validates into a temp DB first |
| POST | `/maintenance` | retention pass + `VACUUM` + FTS optimize |

## 3. Validation rules (excerpt)

| Field | Rule |
|---|---|
| `yt_id` | `^UC[\w-]{22}$` (channel) or `^[\w-]{11}$` (video) |
| `limit` | 1..100 (up-next ≤ 30) |
| `q` | stripped length 1..200 |
| `reco.affinity` | 0..100 |
| `reco.halfLife` | 1..365 days |
| `reco.watched` | 0..100 |
| `reco.serendipity` | 0..100 |
| `rating` | 1..5 |
| `engagement` | 0..5 |
| `cursor` | base64url JSON `{k, id}`; invalid → 400 `invalid_cursor` |

## 4. Status-code map for multi-step operations

`POST /sync` → `202 {job_id, status:"queued"}`; poll `GET /jobs/{id}` for
`{status, progress, stage, result:{channels_ok, channels_failed, videos_new}}`. A partial failure
returns `200` with `status:"partial"` — the endpoint never rolls back good rows.

## 5. What is deliberately absent

- No endpoint returns ORM rows directly; every response has an explicit Pydantic schema.
- No write endpoint accepts a filesystem path from the client except `POST /ingest/import`'s
  `path`, which is resolved against the configured data directory and rejected if it escapes it.
- No endpoint triggers yt-dlp synchronously; the API only enqueues.
