#!/usr/bin/env python3
"""Build My Tube data from a Google Takeout subscriptions export.

Reads every channel in subscriptions.csv, lists each channel's latest uploads
with yt-dlp (cached in .cache/, fetched in parallel), and writes
site/data.js + site/videos.json.

Usage:
    python3 build.py                    # 50 latest videos per channel, cached
    python3 build.py --refresh          # re-download channel listings
    python3 build.py --per-channel 0    # every upload of every channel (slow, huge)
    python3 build.py --per-channel 20 --jobs 12
    python3 build.py --limit 5          # only the first 5 channels (smoke test)
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
SITE = ROOT / "site"
DEFAULT_CSV = Path(
    "/Users/bves/Downloads/Takeout 2/YouTube and YouTube Music/subscriptions/subscriptions.csv"
)

_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


def read_channels(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        sys.exit(f"subscriptions.csv not found at {csv_path}")
    out, seen = [], set()
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            cid = (row.get("Channel Id") or "").strip()
            name = (row.get("Channel Title") or "").strip()
            if not cid.startswith("UC") or not name or cid in seen:
                continue
            seen.add(cid)
            out.append(
                {
                    "id": cid,
                    "name": name,
                    "url": (row.get("Channel Url") or f"https://www.youtube.com/channel/{cid}").strip(),
                }
            )
    if not out:
        sys.exit("No channels found in subscriptions.csv")
    out.sort(key=lambda c: c["name"].casefold())
    return out


def fetch(channel: dict, per_channel: int, refresh: bool) -> dict | None:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"raw_{channel['id']}.json"
    if refresh or not path.exists() or path.stat().st_size < 64:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "-J",
            "--ignore-errors",
            "--no-warnings",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
        ]
        if per_channel:
            cmd += ["--playlist-end", str(per_channel)]
        cmd.append(f"https://www.youtube.com/channel/{channel['id']}/videos")
        for attempt in (1, 2):
            try:
                with path.open("wb") as fh:
                    subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
            except subprocess.TimeoutExpired:
                pass
            if path.exists() and path.stat().st_size > 64:
                break
            time.sleep(1.5 * attempt)
    try:
        return json.loads(path.read_text())
    except Exception:
        path.unlink(missing_ok=True)
        return None


def pick_avatar(raw: dict) -> str:
    thumbs = raw.get("thumbnails") or []
    for t in thumbs:
        if t.get("id") == "avatar_uncropped" and t.get("url"):
            return t["url"].split("=s0")[0] + "=s176-c-k-c0x00ffffff-no-rj"
    for t in thumbs:
        if t.get("width", 0) >= 176 and t.get("height", 0) >= 176:
            return t["url"]
    return ""


def work(channel: dict, per_channel: int, refresh: bool):
    raw = fetch(channel, per_channel, refresh)
    if raw is None:
        return channel, None, "fetch failed"
    videos = []
    for e in raw.get("entries") or []:
        vid = e.get("id")
        title = (e.get("title") or "").strip()
        if not vid or len(vid) != 11 or not title:
            continue
        if e.get("_type") not in (None, "url", "url_transparent"):
            continue
        dur = e.get("duration")
        views = e.get("view_count")
        videos.append(
            [
                vid,
                title,
                int(dur) if isinstance(dur, (int, float)) and dur > 0 else None,
                int(views) if isinstance(views, (int, float)) and views >= 0 else None,
            ]
        )
    if not videos:
        return channel, None, "no videos"
    handle = raw.get("uploader_id") or ""
    info = {
        "name": channel["name"],
        "id": channel["id"],
        "url": raw.get("uploader_url") or channel["url"],
        "handle": handle if handle.startswith("@") else "",
        "avatar": pick_avatar(raw),
        "desc": " ".join((raw.get("description") or "").split())[:400],
        "subs": raw.get("channel_follower_count"),
        "n": len(videos),
    }
    return channel, (info, videos), None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DEFAULT_CSV), help="path to subscriptions.csv")
    ap.add_argument("--per-channel", type=int, default=50,
                    help="latest N uploads per channel (0 = every upload)")
    ap.add_argument("--jobs", type=int, default=8, help="parallel yt-dlp processes")
    ap.add_argument("--refresh", action="store_true", help="ignore the yt-dlp cache")
    ap.add_argument("--limit", type=int, help="only the first N channels (smoke test)")
    args = ap.parse_args()

    channels = read_channels(Path(args.csv))
    if args.limit:
        channels = channels[: args.limit]
    print(
        f"Building {len(channels)} channel(s), "
        f"{args.per_channel or 'all'} videos each, {args.jobs} at a time",
        flush=True,
    )

    started = time.time()
    results, failures = [], []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(work, c, args.per_channel, args.refresh): c for c in channels}
        for done, fut in enumerate(as_completed(futures), 1):
            channel, payload, err = fut.result()
            if payload:
                results.append((channel, payload))
            else:
                failures.append((channel, err))
            if done % 25 == 0 or done == len(channels):
                rate = done / max(time.time() - started, 1e-6)
                eta = (len(channels) - done) / max(rate, 1e-6)
                log(f"  {done}/{len(channels)} done · {rate:.1f}/s · ~{eta / 60:.1f} min left")

    results.sort(key=lambda r: r[0]["name"].casefold())
    out_channels, out_videos, seen = [], [], set()
    for channel, (info, videos) in results:
        kept = 0
        for vid, title, dur, views in videos:
            if vid in seen:
                continue
            seen.add(vid)
            out_videos.append([vid, title, dur, views, len(out_channels)])
            kept += 1
        info["n"] = kept
        out_channels.append(info)

    data = {
        "channels": out_channels,
        "videos": out_videos,
        "meta": {
            "per_channel": args.per_channel,
            "channels_in_csv": len(channels),
            "built": time.strftime("%Y-%m-%d"),
        },
    }
    SITE.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (SITE / "videos.json").write_text(payload, encoding="utf-8")
    (SITE / "data.js").write_text(
        "// Generated by build.py — channel uploads from your YouTube subscriptions.\n"
        f"window.JEE_DATA = {payload};\n",
        encoding="utf-8",
    )

    total_dur = sum(v[2] or 0 for v in out_videos)
    print(
        f"\nDone: {len(out_videos)} videos, "
        f"{total_dur / 3600:.0f} hours of footage, "
        f"{len(out_channels)} channels → site/data.js "
        f"({len(payload) / 1e6:.1f} MB) in {time.time() - started:.0f}s"
    )
    if failures:
        print(f"\n{len(failures)} channel(s) returned nothing:")
        for channel, err in failures[:40]:
            print(f"  {channel['name']} ({channel['id']}): {err}")
        if len(failures) > 40:
            print(f"  … and {len(failures) - 40} more")
        (CACHE / "failed.txt").write_text(
            "\n".join(f"{c['id']}\t{c['name']}\t{e}" for c, e in failures), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
