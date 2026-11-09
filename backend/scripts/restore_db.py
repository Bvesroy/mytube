#!/usr/bin/env python3
"""Minimal, safe SQLite restore helper for My Tube (Phase 7, local-first).

Copies a backup database file to a target path. Does NOT overwrite an existing
target unless --force is passed. No destructive changes by default.

Usage:
  python3 backend/scripts/restore_db.py --backup /path/to/mytube.backup.db [--target /path/to/mytube.db] [--force]

After restore: stop the backend/worker, replace the DB, then restart. Verify data.
"""
from __future__ import annotations
import argparse, shutil, sys
from pathlib import Path

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, help="Backup .db file (source)")
    ap.add_argument("--target", default=None, help="Target .db path (default: <repo>/backend/mytube.db)")
    ap.add_argument("--force", action="store_true", help="Overwrite target if it exists (DANGEROUS)")
    args = ap.parse_args()
    here = Path(__file__).resolve().parent
    backup = Path(args.backup).expanduser().resolve()
    target = Path(args.target).expanduser().resolve() if args.target else (here.parent / "mytube.db").resolve()
    if not backup.exists():
        print(f"backup not found: {backup}"); return 2
    if target.exists() and not args.force:
        print(f"target exists (refusing to overwrite): {target}")
        print("Pass --force to overwrite, or choose a different --target. Stop backend/worker first.")
        return 3
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    print(f"restored: {backup} -> {target}")
    print("Reminder: ensure backend/worker were stopped during replace; restart and verify.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
