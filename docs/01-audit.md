# 01 — Existing architecture audit (Stage 1)

Scope: `build.py` (234 lines), `site/index.html` (2,992 lines), `site/videos.json`, `.cache/`,
and the Takeout CSV. Everything below was read or measured on this machine, not inferred from
the README. Claims that could not be verified are marked **unverified**.

---

## 1. Verified implementation

### 1.1 Ingestion (build.py)

| Stage | Location | Actual behaviour |
|---|---|---|
| CSV parse | `build.py:44-77` | Reads `Channel Id`, `Channel Title`, `Channel Url`; keeps only ids starting `UC`; dedupes in a `set`; sorts by `name.casefold()`; exits 1 if the file or channel list is empty. |
| Fetch | `build.py:80-119` | `yt-dlp --flat-playlist -J --ignore-errors --no-warnings --socket-timeout 20 --retries 2 --playlist-end N`, per channel, into `.cache/raw_<UCID>.json`. Two attempts with `1.5 * attempt` sleep. 300 s subprocess timeout. Cache is reused unless `--refresh`. A truncated JSON file raises, is `unlink`ed and returns `None`. |
| Normalise channel | `build.py:167-176` | `name` (from CSV, not yt-dlp), `id`, `url` (`uploader_url`), `handle` (only if it starts with `@`), `avatar`, `desc` (whitespace-collapsed, **truncated to 400 chars**), `subs` (`channel_follower_count`, nullable), `n`. |
| Normalise video | `build.py:145-166` | Keeps entries whose `_type` is `None`/`url`/`url_transparent`, id length exactly 11, non-empty title. Record is the positional tuple `[id, title, duration|None, view_count|None]`. **No publish date, no description, no thumbnail URL, no chapters, no captions.** |
| Avatar rewrite | `build.py:122-133` | Prefers `avatar_uncropped`, strips `=s0`, forces `=s176-c-k-c0x00ffffff-no-rj`. |
| Dedupe | `build.py:200-208` | First video id wins globally, so a video present in two channels is credited to the alphabetically earlier channel. |
| Output | `build.py:212-227` | `site/videos.json` + `site/data.js` (`window.JEE_DATA = ...`), single pass, no temp file, no atomic rename. |
| Failure handling | `build.py:228-233` | Failures only appended to `.cache/failed.txt`; **not** surfaced in the UI or in `meta`. |

**Measured library (2026-11-09):**

| Metric | Value |
|---|---|
| CSV rows / unique `UC` ids | 1,476 / 1,476 |
| Channels in `videos.json` | **1,427** (49 absent: 48 in `.cache/failed.txt`, 1 dropped after dedupe/yield) |
| Videos | 58,903, zero duplicate ids |
| Videos per channel | min 1, median 50, max 50 (`--per-channel 50`) |
| Videos missing duration | 5 |
| Videos missing view count | 1,896 |
| Channels missing avatar / subs / handle | 0 / 0 / 0 |
| Payload | 5.9 MB JSON, 5.9 MB `data.js` |

### 1.2 Client state (`site/index.html`)

Persistence is a 6-line wrapper over `localStorage` with a `mytube-` prefix
(`index.html:456-461`). Thirteen app keys exist:

| Key | Shape | Cap / notes |
|---|---|---|
| `mytube-hidden-ch` | `string[]` channel ids | — |
| `mytube-hidden-vid` | `string[]` video ids | — |
| `mytube-hidden-cat` | `string[]` category ids | hides every channel in the bucket |
| `mytube-cat-ov` | `{channelId: categoryId}` | manual bucket moves |
| `mytube-ratings` | `{channelId: 1..5}` | star rating, `rateFactor` = `1 + (r-3)*0.25` |
| `mytube-courses` | `[{id,name,videos:[id],created}]` | — |
| `mytube-history` | `[{id,ch,t,w}]` | **capped at 800** (`index.html:1560`), one row per video |
| `mytube-seen` | `{cats:[], chs:[]}` | "mark whole bucket watched" without flooding history |
| `mytube-neg` | `string[]` video ids | "not interested" |
| `mytube-reco` | `{affinity,halfLife,watched,serendipity}` | defaults `60/90/30/30` (`index.html:484`) |
| `mytube-filters` | `{shorts,unwatched}` | **both default `false`** (`index.html:488`) |
| `mytube-ranker` | learned BPR weight vector | derived, resettable (`index.html:1500`) |
| `mytube-bandit` | Thompson-sampling shelf state | derived |

Plus `jeetube-theme`, `jeetube-autoplay` (`index.html:10`, `index.html:2254`).

Engagement weight: on leaving a video, `w = 1.4` if it ended, else
`clamp(seconds/120, 0.3, 1.3)` (`index.html:1567-1580`).

### 1.3 Recommendation engine (client-side only)

Two-stage scorer inside the page, `index.html:700-1600`:

- BM25 lexical index over titles (`index.html:700-780`).
- 64-dim signed random-projection TF-IDF topic vectors, `EMB_D = 64` (`index.html:769`).
- Channel topic graph / centroid neighbours.
- Session transition model (SKNN/FPMC) over `history`.
- 10-feature linear ranker, features `[lexical, semantic, channel, neighbours, session, popular,
  rating, duration, newChannel, negative]` (`index.html:875`), prior
  `DEFAULT_W` (`index.html:877-879`), trained pairwise BPR (`index.html:1480-1520`),
  `newChannel` pinned to the prior to avoid exposure bias (`index.html:1491`).
- Serendipity scales the new-channel feature (`index.html:889`) and MMR
  `lambda = 1 - serendipity*0.55/100`, channel cap 1/2/3 (`index.html:1140-1143`).
- Temporal-holdout evaluator Report: recall/NDCG/MRR/coverage/ILD (`index.html:1520-1552`).
- Category assignment is a fresh lexicon pass on every load (`CATS`, `index.html:1606+`),
  never persisted; overrides live in `mytube-cat-ov`.

**Consequence:** the entire product's intelligence is per-browser. Two devices produce two
different feeds; clearing site data destroys the model; the server cannot answer "why this".

### 1.4 Export / import

`STATE_KEYS` (`index.html:2350`) is **nine** keys: `hidden-ch, hidden-vid, hidden-cat, cat-ov,
ratings, courses, history, seen, neg`. Import validates `payload.mytube === 1` and replaces state
after keeping an in-memory undo (`applyState`, `index.html:2370-2395`).

---

## 2. README and brief claims vs reality

| Claim | Verdict | Evidence |
|---|---|---|
| 1,476 usable channels | **Partial** — 1,476 in the CSV, 1,427 in the app | measured above; 49 silently absent |
| 58,903 video records | **Verified** | `site/videos.json` |
| 50 latest videos per channel | **Verified**, min 1 / median 50 | `meta.per_channel: 50` |
| "No Shorts in the default library" | **False** | Shorts are ingested like any upload; `filters.shorts` defaults to `false` (`index.html:488`), so sub-60 s videos are in the default feed until the user opts out |
| Metadata via yt-dlp | **Verified** | `build.py:80-119` |
| Cache of per-channel listings | **Verified**, 1,479 files, 50 MB | `.cache/` |
| User state in `mytube-*` | **Verified**, but 13 keys, not 9 | §1.2 |
| Backup captures "everything that makes the app yours" | **False** | tuned knobs, feed filters, learned ranker and bandit state are excluded from `STATE_KEYS` |
| Recommendation engine described in README | **Verified**, all listed algorithms are present client-side | §1.3 |
| "Temporal recommendation evaluation" | **Verified** | `index.html:1520-1552` |
| No external API keys / no cloud | **Verified** | static files only |
| Multi-device / server / scheduling | **Absent** | no API, no DB, no worker |

---

## 3. Undocumented behaviour worth preserving or fixing

1. **Silent channel loss.** 48 channels fail and only a text file records it; `meta` reports
   `channels_in_csv` without the delta. A backend must expose per-channel sync status (§E).
2. **Non-atomic writes.** `data.js`/`videos.json` are written in place; an interrupted build
   leaves a half-written 5.9 MB file that the page still loads.
3. **Dedupe attribution.** Global first-wins by channel order, not by upload recency.
4. **`desc` truncated at 400 chars** — the search corpus is smaller than it looks.
5. **No publish timestamp**, so "newest" ordering and temporal evaluation can only use the
   position in the yt-dlp listing.
6. **History cap 800 + one row per video** — engagement is overwritten, not event-sourced, so
   rewatching is invisible.
7. **Categories are recomputed every load** from a lexicon; adding a channel changes other
   channels' buckets with no record.
8. **`mytube-seen` bypasses history**, so "watched" and "history" are different sets.

---

## 4. Constraints the new backend inherits

- Subscription-only corpus; no YouTube recommendations, no Shorts by default, no ads/tracking.
- Local-first, no mandatory cloud, no mandatory API key.
- The offline single-file app must keep working unchanged (Mode A).
- Existing user state must migrate losslessly (§H) — including the 4 keys the current export drops.
- ~60k videos today; schema must stay healthy at 10x.

## 5. Claims I could not verify inside this repo

- Whether `.cache/failed.txt` channels are permanently dead or transiently throttled (**unverified**;
  the cache holds no HTTP status).
- The BPR weights in `mytube-ranker` for this user: they live in the browser profile, not on disk,
  so no snapshot exists to migrate until the frontend is run (handled by an in-app export).
