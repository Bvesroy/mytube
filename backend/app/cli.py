"""CLI entry points for the backend."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.engine import Database
from app.repositories.jobs import enqueue, list_jobs
from app.services.importer import import_channels
from app.services.search import search_videos, rebuild_fts_if_needed
from app.services.recommendations import recommend

logger = logging.getLogger(__name__)


def _require_session(settings: Settings):
    db = Database.open(settings.db_path)
    return db, db.session_factory


def _wait_for_worker(settings: Settings, *, timeout_s: float = 60.0, poll_s: float = 2.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        jobs = list_jobs(session=settings.session_for_worker, limit=1, status="running")
        if not jobs:
            return True
        time.sleep(poll_s)
    return False


def cmd_dev(args: argparse.Namespace, settings: Settings) -> None:
    from subprocess import Popen, DEVNULL
    import os

    db, session_factory = _require_session(settings)
    logger.info("engine ready at %s", settings.db_path)

    cwd = Path(__file__).resolve().parent
    proc = Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(settings.port), "--reload"],
        cwd=str(cwd),
        stdout=DEVNULL,
        stderr=DEVNULL,
    )
    try:
        import requests
        url = f"http://127.0.0.1:{settings.port}"
        deadline = time.time() + 120.0
        while time.time() < deadline:
            try:
                r = requests.get(f"{url}/api/v1/health", timeout=3)
                if r.status_code == 200:
                    logger.info("dev server up at %s", url)
                    break
            except Exception:
                time.sleep(1)
        else:
            logger.warning("dev server did not become ready within 120s")
    except Exception:
        pass
    logger.info("stopping dev worker")
    proc.terminate()
    proc.wait(timeout=10)


def cmd_import(args: argparse.Namespace, settings: Settings) -> None:
    db, session_factory = _require_session(settings)
    logger.info("import data from %s", args.payload)
    with open(args.payload, "r", encoding="utf-8") as f:
        payloads = json.load(f)
    if isinstance(payloads, dict):
        payloads = [payloads]
    result = import_channels(
        session_factory,
        payloads,
        library_name=args.library or "main",
        source_path=str(args.payload) if args.payload else None,
    )
    logger.info("import result: %s", json.dumps({
        "channels_read": result.channels_read,
        "channels_upserted": result.channels_upserted,
        "videos_buffered": result.videos_buffered,
        "videos_upserted": result.videos_upserted,
        "videos_before": result.videos_before,
        "videos_after": result.videos_after,
        "duration_max_s": result.duration_max_s,
        "view_count_max": result.view_count_max,
        "upsert_duration_s": round(result.upsert_duration_s, 3),
    }, indent=2))


def cmd_migrate(args: argparse.Namespace, settings: Settings) -> None:
    from alembic.config import Config
    from alembic import command

    cwd = Path(__file__).resolve().parent
    cfg = Config(str(cwd / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(cfg, "head")
    logger.info("migrated %s to head", settings.db_path)


def cmd_search(args: argparse.Namespace, settings: Settings) -> None:
    db, session_factory = _require_session(settings)
    with db.session() as session:
        rebuild_fts_if_needed(session)
        hits, cursor, count = search_videos(
            session,
            args.q,
            limit=args.limit or 25,
            cursor=args.cursor,
            filters=args.filters,
        )
        out = {
            "query": args.q,
            "count": count,
            "hits": [
                {
                    "id": h.id,
                    "title": h.title,
                    "duration_s": h.duration_s,
                    "view_count": h.view_count,
                    "published_at": int(h.published_at) if h.published_at else None,
                    "snippet": h.snippet,
                    "rank": h.rank,
                    "channel": {
                        "id": h.channel.id if h.channel else None,
                        "name": h.channel.name if h.channel else None,
                        "handle": h.channel.handle if h.channel else None,
                    } if h.channel else None,
                }
                for h in hits
            ],
            "next_cursor": cursor,
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")


def _make_filters(raw):
    if not raw:
        return None
    return {
        "shorts": raw.get("shorts", False),
        "duration_min_s": raw.get("duration_min_s"),
        "duration_max_s": raw.get("duration_max_s"),
        "category_slug": raw.get("category_slug"),
        "sort": raw.get("sort", "recent"),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mytube-backend", description="My Tube backend CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("dev", help="start development server with live reload")
    p.set_defaults(func=cmd_dev)

    p = sub.add_parser("import", help="import a JSON playlist/library payload")
    p.add_argument("payload", type=Path, help="path to JSON payload")
    p.add_argument("--library", help="library name")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("migrate", help="run alembic upgrade head")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("search-demo", help="run a search against the current DB")
    p.add_argument("q", help="query string")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--cursor", default=None)
    p.add_argument("--filters", type=json.loads, default=None)
    p.set_defaults(func=cmd_search)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    settings = get_settings()
    args.func(args, settings)


if __name__ == "__main__":
    main()
