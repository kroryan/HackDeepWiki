#!/usr/bin/env python3
"""Offline backup, integrity and restore operations for HackDeepWiki SQLite."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.data_root import get_data_root  # noqa: E402
from api.storage import (  # noqa: E402
    backup_all_databases,
    database_integrity,
    restore_database,
)


def _database_root() -> Path:
    return Path(get_data_root()).resolve() / "hackdeepwiki_db"


def _safe_database(name: str) -> Path:
    if Path(name).name != name or not name.endswith(".db"):
        raise ValueError("database must be a filename ending in .db")
    root = _database_root()
    destination = (root / name).resolve()
    if destination.parent != root:
        raise ValueError("database path escapes the data directory")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Manage HackDeepWiki SQLite data. Stop HackDeepWiki before restore."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)

    subparsers.add_parser("integrity")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("database")
    restore_parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="confirm no HackDeepWiki process is using the database",
    )
    rotate_parser = subparsers.add_parser("rotate-secrets")
    rotate_parser.add_argument(
        "--old-key-env",
        default="HACKDEEPWIKI_OLD_ENC_KEY",
    )
    rotate_parser.add_argument(
        "--new-key-env",
        default="HACKDEEPWIKI_NEW_ENC_KEY",
    )

    args = parser.parse_args()
    if args.command == "backup":
        destination = args.destination.expanduser().resolve()
        paths = backup_all_databases(str(destination))
        print(json.dumps({"backups": paths}, indent=2))
        return 0

    if args.command == "integrity":
        root = _database_root()
        checks = {
            path.name: database_integrity(str(path))
            for path in sorted(root.glob("*.db"))
        }
        print(json.dumps(checks, indent=2))
        return 0 if all(item["ok"] for item in checks.values()) else 1

    if args.command == "rotate-secrets":
        from api.storage.secret_rotation import rotate_profile_secrets

        old_key = os.environ.get(args.old_key_env) or getpass.getpass(
            "Old encryption passphrase: "
        )
        new_key = os.environ.get(args.new_key_env) or getpass.getpass(
            "New encryption passphrase: "
        )
        result = rotate_profile_secrets(
            old_passphrase=old_key,
            new_passphrase=new_key,
        )
        print(json.dumps(result, indent=2))
        return 0

    if not args.confirm_stopped:
        parser.error("restore requires --confirm-stopped")
    backup = args.backup.expanduser().resolve()
    destination = _safe_database(args.database)
    restored = restore_database(str(backup), str(destination))
    print(json.dumps({"restored": restored}, indent=2))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
