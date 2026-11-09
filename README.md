# My Tube (Takeout 2)

A YouTube-style viewer built from exactly the channels in
`Takeout 2/YouTube and YouTube Music/subscriptions/subscriptions.csv` — 1,476 channels
and nothing else. No recommendations from the wider internet, no Shorts, no
algorithmic surprises.

```
build.py          subscriptions.csv → yt-dlp → site/data.js
site/index.html   the whole app (single file, no dependencies)
site/data.js      the library, loaded as window.JEE_DATA (works over file://)
site/videos.json  the same data as plain JSON
.cache/           per-channel yt-dlp listings, so rebuilds are instant
```

## Run

```bash
open site/index.html                     # works straight from disk
python3 -m http.server -d site 8001      # or over HTTP
```

## Rebuild

```bash
python3 build.py                    # cached; reuses .cache/, ~5s
python3 build.py --refresh          # re-download every channel listing, ~5 min
python3 build.py --per-channel 0    # every upload of every channel (slow, huge)
python3 build.py --per-channel 20 --jobs 12
python3 build.py --limit 5          # first 5 channels only, for a quick test
```

The CSV path, per-channel cap and parallelism live in `build.py`'s argument parser;
pass `--csv` to point at a different export.

## What's in it

- **1,478 channels in the export, 1,476 usable** — two rows repeat a channel ID,
  and dead/empty channels are dropped and logged to `.cache/failed.txt`.
- **Latest 50 uploads per channel** rather than full catalogs: 1,476 complete
  catalogs would be several hundred thousand videos and many hours of scraping,
  for a data file too large to load in a browser. `--per-channel 0` removes the cap.
- Fetched 10 channels at a time, so a full refresh takes about five minutes
  instead of an hour.

## Layout

Categories run across the top as a scrolling strip; courses sit at the top of the left
sidebar, above the channel list. Opening a category gives a page of that bucket's videos.

## Features

- **Categories across the top** — 30 inferred topics (Inventions & Making, Space &
  Astronomy, AI, Programming, Money & Investing …). Click one to see every video in it;
  the eye on a chip hides or shows the whole bucket. See
  [How categories are inferred](#how-categories-are-inferred).
- **Unwatched counts.** Once you've watched anything in a bucket, its chip shows how
  many videos are left to watch, and the category page repeats the number (with ✓ ticks
  on the videos you've seen).
- **Move a channel between categories.** The dropdown on a channel page overrides the
  classifier's bucket (the pill gets a ✎); pick *Automatic* to let the classifier decide
  again. A channel moved into a hidden category hides with it.
- **Star ratings.** Five stars on a channel page rate it 1–5 (or press `1`–`5` there):
  5 stars boosts its videos 1.5× in For you and Up next, 1 star halves them. The rating
  shows as a ★ badge in the sidebar; press the same star again to clear it.
- **Continue watching.** The home feed pins your most recently watched videos in a shelf
  at the top, newest first.
- **Recommendation shelves.** For you opens with rows the feed generates, not just a grid:
  **Because you watched <video>** (similar videos from *other* channels), **New to you**
  (channels you have never watched), **Continue this thread** (what usually follows what you
  just watched), **More like <channel>** (topic-graph neighbours) and **Deep cuts** (relevant,
  but under-watched). The rows re-order themselves with Thompson sampling on which ones you
  actually click.
- **Why this?** Hover any recommended card, or read the line under a shelf and Up-next title,
  to see the reason — the seed video, the matched terms, or "channel you haven't watched".
- **Not interested.** The ⊘ on any card sinks a video without hiding it, and Undo brings it
  back. Quick exits from a video also count for less in future rankings than full watches.
- **Similar channels.** Every channel page ends its header with the channels whose topic
  vectors sit closest to it, with a match percentage.
- **Learn your taste.** The Tune panel can train a small ranker on your own history (pairwise
  BPR) and shows the ten learned weights; the ranking knobs still apply on top.
- **Recs Lab.** About → Run evaluation scores the current feed against a temporal hold-out —
  Recall@10, NDCG@10, MRR, coverage and diversity, baseline vs hybrid.
- **Mark all watched.** A category or channel page can be marked entirely seen with one
  click, clearing its unread badge — without writing thousands of history entries (history
  is capped at 800 and feeds the ranking). Clear the mark any time to bring the count back.
- **Feed filters.** Two sidebar toggles — **Hide Shorts** (under a minute) and **Only
  unwatched** — apply across Home, category and channel pages, search and Up next.
- **Backup.** **Export JSON** on the About page downloads every piece of your state as
  one JSON file; **Import** restores it (validated, with Undo).
- Home feed with **For you**, **Latest** (round-robin across channels, so no single
  channel buries the rest), **Popular** (by view count) and **Shuffled**.
- **The ranking is yours to tune:** channel affinity, history half-life and
  already-watched weight, adjusted live from **Tune** in the For you feed — three
  sliders plus four presets, see [Tuning it](#tuning-it).
- **Enable / disable any channel or video.** The eye in the sidebar toggles a channel,
  the ✕ on a card (or **Hide** on the watch page) toggles a single video. Hidden items
  drop out of All videos, search, For you and Up next, and the sidebar shows a running
  count with **Show all** to bring everything back. Every action has an Undo toast.
- **Courses.** The top of the sidebar holds courses you build yourself: hit
  **+**, or use **Add to course** on a channel or watch page, pick videos in the
  searchable picker, and the course page shows total length and how much you've
  watched (progress comes from your local watch history). Rename, add to and delete
  courses freely; they live in this browser only.
- Sidebar channel list with a **filter box** — 1,476 channels don't fit in a
  sidebar, so it shows the 40 largest and narrows to any of them as you type.
- Channel pages with avatar, handle, subscriber count, description and their
  newest 50 uploads, sortable by newest/popular/shuffled.
- Search across every title and channel name, ranked by title matches then views.
- Watch page: YouTube embedded player, up-next ranked by topic similarity to the
  video you're watching, autoplay toggle persisted in `localStorage`.
- Dark/light theme, responsive down to phone widths.
- Keyboard: `/` focuses search, `Shift`+`N` plays a random video (or the next one
  while watching).

## How categories are inferred

Every channel is bucketed by a **shallow Rocchio-style text classifier** — a weighted
keyword lexicon scored over the channel name, description and up to 30 recent video
titles:

1. **Weights × inverse library frequency.** A term like `engineering` appears in 300
   channels and counts for little; a specific one counts for a lot. Terms in more than
   a third of the library are dropped outright as signal-free.
2. **Titles and names count double** (×2.2 and ×3), because a channel's description is
   often boilerplate while its titles are what it actually publishes.
3. **Length normalisation** stops long descriptions from winning on volume.
4. **Best score wins**, and a channel whose best score is under 0.33 becomes
   **Uncategorised** (154 of 1,427) rather than being forced into a wrong bucket.

Alternatives considered: sentence embeddings or an LLM would separate topics better,
but both need a model download or an API key, and this page has to stay one offline file
that works over `file://`. k-means over TF-IDF (spherical k-means in the open toolkits)
was the other real candidate, but its clusters come out unnamed, which is worse for
browsing than buckets that are labelled and explainable. Classification runs once at
startup (~250 ms for 1,427 channels) and needs no network.

Assignment is per channel, not per video: a channel's whole catalogue lands in the
bucket its title text points at most strongly.

**Overrides win.** The classifier is a guess, so the category dropdown on a channel's
page replaces it: your pick is stored in `mytube-cat-ov` (category id per channel id) and
applied after classification on every load. The channel's pill shows a ✎ while an override
is active; picking *Automatic: <bucket>* deletes the override and the classifier takes
over again. Counts, hiding and search all follow the effective bucket, so a channel you
move into a hidden category hides too. Every move has an Undo toast.

## The ranking algorithm

**For you**, the home shelves and the watch page's **Up next** all run one hybrid
recommender with the same shape as YouTube's own pipeline — candidate generation, ranking,
re-ranking — assembled from published open algorithms instead of a neural net. There is no
server here to train one, and one user is not a training set, so every stage is a classic
algorithm that runs inside the page:

| Stage | Algorithm | Where it comes from |
| --- | --- | --- |
| candidates | BM25 over title unigrams and bigrams | Robertson & Zaragoza; the function behind Lucene, Elasticsearch and `rank_bm25` |
| candidates | 64-dim topic vectors: TF-IDF → signed random projection | Johnson–Lindenstrauss 1984 / Random Indexing |
| candidates | channel topic graph: cosine between channel centroid vectors | — |
| candidates | session transitions — what followed what in a session | SKNN / FPMC session-based recsys |
| ranking | 10-feature ranker trained with pairwise BPR (SGD) | Rendle et al., BPR 2009 |
| re-ranking | MMR diversification + per-channel spread cap | Carbonell & Goldstein, SIGIR 1998 |
| shelves | Thompson sampling over shelf click-through | Agrawal & Goyal 2012 |
| evaluation | temporal hold-out: Recall@10, NDCG@10, MRR, coverage, ILD | — |

**Why the topic vectors matter.** BM25 can only find videos that share *words* with what you
watched, which drifts back to the same channels. The vectors place every title in a
64-dimension space by the company its words keep, so "rust borrow checker" lands near
"memory safety in C++" with no shared term. All 58,903 titles project in about 140 ms and
cost ~15 MB of `Float32Array`; cosine is then a plain dot product, so nearest-neighbour over
the whole library is a 4 M-multiply scan (~10 ms) and needs no ANN index.

**Why it leaves the channel.** Three separate mechanisms, not a hope:

1. **"Because you watched X" shelves** rank the neighbours of a seed video with its own
   channel excluded, so those rows cannot contain X's channel at all.
2. **The channel topic graph** pulls in channels whose centroids sit closest to your
   favourites — "more like Sebastian Lague" without re-watching Sebastian Lague.
3. **The channel-spread cap** (3 per channel at low serendipity, 2 default, 1 at high) plus
   MMR stops any one channel from filling the head of the feed.

The `newChannel` feature boosts channels you have never watched. It is deliberately **not**
learned: exposure bias would otherwise teach the model that new channels are bad, because you
have only watched 8 of 1,427.

**Scoring is relevance × prior boost.** Lexical, topic and session scores decide *whether* a
video is for you; channel affinity, the topic graph and the new-channel push then amplify
relevant videos. They used to be added, which gave every video of a favourite channel a floor
it had not earned — a chess query could surface a non-chess video from a channel you watch.

**The ranker** has ten bounded features: lexical match, topic cosine, channel affinity,
topic-graph affinity, session transition, popularity, your star rating, duration fit,
never-watched channel, and explicit "not interested". Pairwise BPR samples an unwatched video
for each video you watched — half from channels you already watch (hard negatives), half
uniform — and nudges the weights until the watched one scores higher. Weights are then shrunk
35% toward the hand-tuned prior, because a few hundred pairs from one person is a tiny
training set.

**Engagement weighting.** Leaving a video after ten seconds records a weight near 0.3;
watching to the end records 1.4. Those weights go straight into the decayed profile, so a
quick exit counts for far less than a full watch — and the ranker is told not to treat quick
exits as positives.

### Tuning it

**Tune** on the For you line (and on the About page) opens four knobs, saved in
`mytube-reco` and applied live as you drag:

| Knob | Default | What it does |
| --- | --- | --- |
| Channel affinity | 60% | Boost for channels you keep watching, on top of the topic score. 0% = pure topics; 60% lets a favourite channel score up to 1.6×, 200% up to 3×. |
| History half-life | 90 days | A watch this old counts half as much as a fresh one. Long = stable taste, short = the feed follows what you watched this week. |
| Already-watched weight | 30% | Share of score a watched video keeps. They sink but stay reachable; 0% drops them to the bottom. |
| Serendipity | 30% | How far the feed reaches. At 0% it stays strictly on-topic and a channel may appear three times in a row; at 100% it enforces one video per channel and pushes channels you have never watched. |

Presets set all four at once: **Balanced** (60/90/30/30), **Topic first** (0/120/30/25),
**Channel first** (160/180/60/15) and **This week only** (60/14/30/45).

The same panel shows the **learned ranker**: train it on your history (about 30 ms), see its
ten weights, retrain, or forget it. Until you train one, the hand-tuned weights above are
used. **Star ratings** multiply the score separately — 1★ halves a channel, 5★ gives 1.5× —
and live in `mytube-ratings`.

### Recs Lab

About → **Run evaluation** does a temporal hold-out in the page: train on all but the last
fifth of your history, replay those watches, and report **candidate-pool recall**, **Recall@10**,
**NDCG@10**, **MRR**, channel **coverage** and **intra-list diversity** for the BM25-only
baseline and the hybrid model. Relevance is measured on the ranked list and diversity on the
re-ranked feed, because MMR deliberately trades a little relevance for variety. One user is a
small sample — read the numbers as a sanity check, not a benchmark.

## Data format

```js
window.JEE_DATA = {
  channels: [{ name, id, url, handle, avatar, desc, subs, n }, …],
  videos:   [[id, title, duration, views, channelIndex], …],
  meta:     { per_channel, channels_in_csv, built }
}
```

Thumbnails are `i.ytimg.com/vi/<id>/hq720.jpg` with an `mqdefault.jpg` fallback.
Avatars are normalised to `=s176-c-k-c0x00ffffff-no-rj`.

Your own state is separate, in `localStorage` under `mytube-*`: `hidden-ch`,
`hidden-vid` and `hidden-cat` (channel, video and category ids, so they survive a
rebuild), `history` (watch history with timestamps and engagement weights, capped at 800),
`neg` (videos marked not interested), `courses` (name plus video ids), `reco` (the four
ranking knobs), `cat-ov` (channel id → manually chosen category id), `ratings` (channel
id → 1–5 stars), `seen` (categories/channels marked all-watched), `filters` (the two feed
toggles), `ranker` (learned weights, if you trained one) and `bandit` (shelf click counts).

**Export / import.** The About page's Export downloads `{ mytube: 1, exportedAt,
state: {…} }` with the nine state keys above except `reco`, `filters`, `ranker` and `bandit` (preferences and learned state,
kept on this browser). Import checks the `mytube: 1` marker, coerces and validates the
shape (entries of the wrong type are dropped), replaces the current state, and toasts
what it imported with an Undo that restores the pre-import snapshot.

## Limits

- Upload dates aren't in YouTube's channel listings, so cards show view counts
  and *Latest* means each channel's own newest-first order, not a global timeline.
- Public Videos-tab uploads only: no Shorts, no live streams, no members-only.
- Titles, durations, view counts and subscriber counts are a snapshot of the last
  build. The videos themselves still play from, and count towards, YouTube.
