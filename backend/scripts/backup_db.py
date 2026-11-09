"""Safe local SQLite backup (Phase 7). Run from backend/: python3 scripts/backup_db.py
Creates a timestamped copy alongside the DB (no network, no destructive changes)."""
from __future__ import annotations
import shutil, sqlite3
from datetime import datetime, timezone
from pathlib import Path
import os, sys

def main():
    db = Path(os.environ.get("MYTUBE_DB_PATH") or Path(__file__).resolve().parents[1] / "mytube.db")
    if not db.exists():
        print(f"DB not found: {db}"); sys.exit(1)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = db.with_name(f"{db.stem}.backup-{ts}{db.suffix}")
    # prefer SQLite online backup API
    try:
        src = sqlite3.connect(str(db)); dst = sqlite3.connect(str(out))
        with dst: src.backup(dst)
        src.close(); dst.close()
    except Exception:
        shutil.copy2(db, out)
    print(str(out))

if __name__ == "__main__":
    main()
