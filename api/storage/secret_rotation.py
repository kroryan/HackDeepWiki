"""Transactional rotation of secrets stored in profile.db."""

from __future__ import annotations

from api.security import rotate_secret
from api.storage import connect, profile_db_path


def rotate_profile_secrets(
    *,
    old_passphrase: str,
    new_passphrase: str,
) -> dict[str, int]:
    """Rotate provider and MCP secrets in one SQLite transaction."""
    connection = connect(profile_db_path())
    provider_count = 0
    mcp_count = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        provider_rows = connection.execute(
            "SELECT id, api_key_enc FROM provider_profiles "
            "WHERE api_key_enc IS NOT NULL AND api_key_enc <> ''"
        ).fetchall()
        for row in provider_rows:
            connection.execute(
                "UPDATE provider_profiles SET api_key_enc = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    rotate_secret(
                        row["api_key_enc"],
                        old_passphrase=old_passphrase,
                        new_passphrase=new_passphrase,
                    ),
                    row["id"],
                ),
            )
            provider_count += 1

        mcp_rows = connection.execute(
            "SELECT id, config_json FROM mcp_servers"
        ).fetchall()
        for row in mcp_rows:
            connection.execute(
                "UPDATE mcp_servers SET config_json = ? WHERE id = ?",
                (
                    rotate_secret(
                        row["config_json"],
                        old_passphrase=old_passphrase,
                        new_passphrase=new_passphrase,
                    ),
                    row["id"],
                ),
            )
            mcp_count += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"provider_profiles": provider_count, "mcp_servers": mcp_count}
