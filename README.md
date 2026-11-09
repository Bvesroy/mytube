# My Tube

> **A YouTube viewer that only shows the channels you actually subscribed to.**  
> *Because the algorithm thinks "you watched one 3Blue1Brown video" means you want 400 thumbnails of people pointing at explosion emojis.* 📉

[![Platform](https://img.shields.io/badge/Platform-Any_Browser-111111?logo=googlechrome&logoColor=white)](site/index.html)
[![Dependencies](https://img.shields.io/badge/Runtime_Dependencies-Zero-blueviolet)](site/index.html)
[![Data Source](https://img.shields.io/badge/Data-Your_Own_Google_Takeout-34A853?logo=googledrive&logoColor=white)](#rebuild)
[![Recommendation Engine](https://img.shields.io/badge/Algorithm-Hand--Rolled_Inside_One_HTML_File-ff4242)](#the-ranking-algorithm)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🧐 What Is This?

You exported your YouTube subscriptions from Google Takeout like a responsible digital adult. You got a CSV with 1,476 channels and the sudden realization that YouTube's homepage has not shown you 90% of them since 2019.

**My Tube** takes that `subscriptions.csv`, scrapes each channel's latest uploads with `yt-dlp`, and shoves the whole thing into a static site that runs from a folder on your disk. No backend. No recommendations from the wider internet. No Shorts. No "people also watched this 19-minute apology video". Just your subscriptions, doing subscription things.

The "app" is one HTML file, one data file, and enough client-side ranking math to make a data scientist mildly uncomfortable. It works over `file://` because apparently we still have to earn that in 2026.

---

## ⚡ What You Get (Besides Your Dignity)

- **Your channels, not YouTube's mood swings.** Built from the channels in your Takeout export — and nothing else.
- **Zero runtime dependencies.** Open `site/index.html`. That's the install guide. You're welcome.
- **A recommendation system you can actually tune.** Channel affinity, history half-life, watched-video penalty, serendipity — four sliders, four presets, and a tiny pairwise BPR ranker you can train on your own clicks. The algorithm is yours to argue with.
- **Topic buckets that mostly work.** A shallow Rocchio-style text classifier groups channels into 30 inferred categories. It's not an LLM, it doesn't need your API key, and it won't confidently hallucinate a category called "Vibes". You can override any channel manually when the classifier has an opinion.
- **Recommendation shelves with receipts.** "Because you watched X", "New to you", "Continue this thread", "More like <channel>", "Deep cuts" — each with a reason, because mysterious recommendations are how we got into this mess.
- **Local-only state.** Watch history, ratings, hidden channels/videos/categories, courses, filters, learned ranker, and shelf bandit stats live in `localStorage`. Export/import as one JSON file when you want a backup or a new browser.
- **Courses, stars, hide buttons, and undo toasts.** All the little controls that make a personal library feel like yours instead of a hostage situation.

---

## 🚀 Run It

```bash
open site/index.html                     # works straight from disk, like nature intended
python3 -m http.server -d site 8001      # or serve it, if you enjoy ceremony
```

If you can open a file, you can run this. Congratulations.

---

## 🔄 Rebuild From Your Own Subscriptions

Point `build.py` at a Google Takeout `subscriptions.csv` and let it do the mildly tedious part:

```bash
python3 build.py                    # cached; reuses .cache/, usually ~5s
python3 build.py --refresh          # re-download every channel listing, ~5 min
python3 build.py --per-channel 0    # every upload of every channel (slow, huge, brave)
python3 build.py --per-channel 20 --jobs 12
python3 build.py --limit 5          # first 5 channels only, for a quick sanity check
```

Requirements: Python 3 and [`yt-dlp`](https://github.com/yt-dlp/yt-dlp). The CSV path, per-channel cap, and parallelism live in `build.py`'s argument parser; pass `--csv` to point at a different export.

**Reality check:** the default build grabs the latest 50 uploads per channel. Full catalogs for 1,476 channels would be hundreds of thousands of videos, many hours of scraping, and a data file your browser would open with the enthusiasm of a Windows XP laptop. `--per-channel 0` removes the cap if you enjoy pushing limits.

---

## 📁 What's In The Box

```
build.py          subscriptions.csv → yt-dlp → site/data.js
site/index.html   the whole app (single file, no dependencies)
site/data.js      the library, loaded as window.JEE_DATA (works over file://)
site/videos.json  the same data as plain JSON
.cache/           per-channel yt-dlp listings, so rebuilds are instant (gitignored)
```

---

## 🧠 The Ranking Algorithm

**For You**, the home shelves, and **Up Next** all run one hybrid recommender with the same general shape as YouTube's pipeline — candidate generation, ranking, re-ranking — except assembled from published open algorithms instead of a neural net with a server farm.

There is no server here to train one, and one user is not a training set. So every stage is a classic algorithm that runs inside the page without asking permission:

| Stage | Algorithm | Where it comes from |
| --- | --- | --- |
| candidates | BM25 over title unigrams and bigrams | Robertson & Zaragoza; the logic behind Lucene / Elasticsearch |
| candidates | 64-dim topic vectors: TF-IDF → signed random projection | Johnson–Lindenstrauss 1984 / Random Indexing |
| candidates | channel topic graph: cosine between channel centroid vectors | plain vector math, no venture capital required |
| candidates | session transitions — what followed what in a session | SKNN / FPMC session-based recsys |
| ranking | 10-feature ranker trained with pairwise BPR (SGD) | Rendle et al., BPR 2009 |
| re-ranking | MMR diversification + per-channel spread cap | Carbonell & Goldstein, SIGIR 1998 |
| shelves | Thompson sampling over shelf click-through | Agrawal & Goyal 2012 |
| evaluation | temporal hold-out: Recall@10, NDCG@10, MRR, coverage, ILD | the "did this actually help?" department |

**Why topic vectors exist:** BM25 can only find videos that share *words* with what you watched. The vectors place titles in a 64-dimension space by the company their words keep, so "rust borrow checker" can land near "memory safety in C++" with no shared term. All titles project in-browser; cosine similarity is a dot product, not a spiritual journey.

**Why it leaves the channel:** "Because you watched X" excludes X's own channel; the channel topic graph pulls in neighbours of channels you like; MMR plus a per-channel spread cap stop one channel from colonizing the feed; and `newChannel` boosts channels you've never watched. Exposure bias is deliberately *not* learned, because the model should not conclude new channels are bad just because you've only sampled 8 out of 1,427.

**Scoring is relevance × prior boost.** Lexical/topic/session scores decide whether a video is relevant; channel affinity, topic graph, and new-channel push amplify relevant videos. They are not added as a floor, because giving every video from a favorite channel a participation trophy is how chess queries surface non-chess videos.

### Tuning It

**Tune** on the For You line opens four knobs, saved in `localStorage` and applied live:

| Knob | Default | What it does |
| --- | --- | --- |
| Channel affinity | 60% | Boost for channels you keep watching. 0% = pure topics; higher = your favorites get louder. |
| History half-life | 90 days | A watch this old counts half as much. Long = stable taste, short = "I watched one woodworking video this week" energy. |
| Already-watched weight | 30% | Watched videos sink but stay reachable. 0% drops them to the bottom like they never happened. |
| Serendipity | 30% | How far the feed reaches beyond the obvious. Higher = more variety, stricter per-channel spread. |

Presets: **Balanced**, **Topic first**, **Channel first**, **This week only**. The panel can also train the tiny ranker on your history (~30 ms), show its ten weights, retrain, or forget it. One user is a small sample — treat the Recs Lab numbers as a sanity check, not a peer-reviewed benchmark.

---

## 🏷️ How Categories Are Inferred

Every channel is bucketed by a weighted keyword lexicon scored over the channel name, description, and up to 30 recent video titles:

1. **Weighted by inverse library frequency.** Common terms count for little; specific terms count for more. Terms in more than a third of the library are dropped as signal-free noise.
2. **Titles and names count more.** Descriptions are often SEO soup; titles are closer to what a channel actually publishes.
3. **Length-normalized.** Long descriptions don't win by brute word count.
4. **Low-confidence channels become Uncategorised** instead of being shoved into a wrong bucket with false confidence.

No embeddings, no API calls, no model download. Classification runs once at startup and needs no network. If the classifier guesses wrong, the channel page lets you override its bucket; your pick wins and follows the effective category everywhere.

---

## 💾 Data Format

```js
window.JEE_DATA = {
  channels: [{ name, id, url, handle, avatar, desc, subs, n }, /* … */],
  videos:   [[id, title, duration, views, channelIndex], /* … */],
  meta:     { per_channel, channels_in_csv, built }
}
```

Thumbnails come from `i.ytimg.com`. Avatars are normalized to a consistent size. Your personal state lives separately in `localStorage` under `mytube-*` keys (hidden items, history, ratings, overrides, courses, filters, learned ranker, bandit stats). **About → Export JSON** downloads it; **Import** restores it with validation and an Undo snapshot.

---

## ⚠️ Limits (A.K.A. The Fine Print)

- **Upload dates aren't in channel listings.** Cards show view counts, and *Latest* means each channel's newest-first order — not a global timeline across the universe.
- **Public Videos-tab uploads only.** No Shorts, no live streams, no members-only content. If it hides behind YouTube's extra tabs, this viewer does not cosplay as a detective.
- **Metadata is a snapshot.** Titles, durations, view counts, and subscriber counts are as fresh as your last build. The videos themselves still play from YouTube and still count toward YouTube.
- **Your data file can get chunky.** That's the price of making a static, offline-ish viewer carry a real library in one go.

---

## 📜 License

[MIT](LICENSE). Take it, fork it, rebuild it, point it at your own subscriptions, and finally watch the channels you asked for.
