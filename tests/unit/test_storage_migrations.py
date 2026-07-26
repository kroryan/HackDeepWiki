from __future__ import annotations

import sqlite3

import pytest

from api.storage import (
    backup_database,
    connect,
    database_integrity,
    repo_key,
    restore_database,
)


def test_profile_legacy_schema_is_backed_up_and_migrated(tmp_path):
    path = tmp_path / "profile.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE jobs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "repo_key TEXT NOT NULL, kind TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'queued', payload_json TEXT)"
    )
    raw.execute(
        "INSERT INTO jobs(repo_key, kind, status) VALUES ('repo', 'test', 'queued')"
    )
    raw.commit()
    raw.close()

    conn = connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        assert "attempts" in columns
        assert "available_at" in columns
        assert {"result_json", "error", "created_at", "started_at", "finished_at"} <= columns
        assert conn.execute("SELECT attempts FROM jobs").fetchone()[0] == 0
    finally:
        conn.close()

    backups = list((tmp_path / "backups").glob("profile.db.schema-0-to-4.*.bak"))
    assert len(backups) == 1
    assert database_integrity(str(backups[0]))["ok"]


def test_repository_schema_starts_at_version_one(tmp_path):
    path = tmp_path / "owner_repo.db"
    conn = connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"chat_history", "embeddings", "wiki_releases"} <= tables
    finally:
        conn.close()


def test_backup_restore_round_trip(tmp_path):
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample(value TEXT NOT NULL)")
    conn.execute("INSERT INTO sample VALUES ('before')")
    conn.commit()
    conn.close()

    backup = backup_database(str(source), str(tmp_path / "source.backup"))
    conn = sqlite3.connect(source)
    conn.execute("UPDATE sample SET value='after'")
    conn.commit()
    conn.close()

    restored = restore_database(backup, str(source))
    assert restored == str(source)
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "before"
    finally:
        conn.close()
    assert database_integrity(str(source)) == {"ok": True, "messages": ["ok"]}


def test_restore_refuses_corrupt_backup(tmp_path):
    corrupt = tmp_path / "bad.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_database(str(corrupt), str(tmp_path / "destination.db"))


def test_repo_keys_do_not_collide_after_flattening():
    first = repo_key("a_b", "c", "github")
    second = repo_key("a", "b_c", "github")
    assert first != second
    assert first.startswith("a_b_c-")
    assert second.startswith("a_b_c-")


def test_repo_key_is_stable_and_filesystem_safe():
    value = repo_key("owner/name", "repo.git", "gitlab")
    assert value == repo_key("owner/name", "repo.git", "gitlab")
    assert "/" not in value
    assert len(value) <= 97
