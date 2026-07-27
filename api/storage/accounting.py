"""Token usage accounting + model pricing (Fase 5).

Mirrors OpenDeepWiki's AiUsageAccounting + AddAiModelCachePricing: pricing
lives in a DB table (``model_pricing``), seeded once but editable at runtime
via the API, so prices update WITHOUT rebuilding the AppImage and without a
single hardcoded list that goes stale as models are renamed/retired.

Bootstrap (the contract [[project_accounting_bootstrap]] depends on): the
default account row is created lazily by api.storage.bootstrap_default_account
on every profile.db connect, so the very first server start -- or a start
against a pre-accounting profile.db -- always has account id=1 to record
against. This module never creates the account; it assumes it exists.

Pricing resolution: ``price_for`` queries the ``model_pricing`` table by
substring match on the model name (so 'gpt-4o-2024-08-06' hits the 'gpt-4o'
row). A model with no row => cost 0 (usage is still recorded, so the totals
are visible even when pricing is unknown). ``set_price`` adds/updates a row
at runtime; ``list_pricing`` returns the table for an admin UI.
"""

from __future__ import annotations

import logging
from typing import Optional

from api.storage import connect, profile_db_path

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_ID = 1  # created by bootstrap_default_account on profile.db init

# Seed prices (USD per 1,000,000 tokens, input : output) inserted ONCE into
# the model_pricing table on first connect, then never touched by the code
# again -- the user edits them at runtime via /api/pricing. Kept minimal and
# conservative: the point is order-of-magnitude local visibility, not billing
# accuracy, and a stale/missing row degrades to $0 (recorded anyway) rather
# than a wrong nonzero guess.
_SEED_PRICING: list[tuple[str, float, float]] = [
    # OpenAI
    ("gpt-4o", 2.5, 10.0),
    ("gpt-4o-mini", 0.15, 0.6),
    ("gpt-4.1", 2.0, 8.0),
    ("o3", 10.0, 40.0),
    ("o4-mini", 1.1, 4.4),
    # Anthropic
    ("claude-3-5-sonnet", 3.0, 15.0),
    ("claude-3-7-sonnet", 3.0, 15.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-opus-4", 15.0, 75.0),
    ("claude-haiku-4", 1.0, 5.0),
    # Google
    ("gemini-2.5-pro", 1.25, 10.0),
    ("gemini-2.5-flash", 0.075, 0.3),
    # Others
    ("deepseek-chat", 0.27, 1.1),
    ("deepseek-reasoner", 0.55, 2.19),
    # local/self-hosted models -> $0 by convention
    ("llama-3", 0.0, 0.0),
    ("qwen", 0.0, 0.0),
    ("mistral", 0.0, 0.0),
    ("gemma", 0.0, 0.0),
]

_PRICING_INITIALIZED = False


def _ensure_pricing(conn) -> None:
    """Create the model_pricing table and seed it once. Idempotent: the seed
    only inserts rows that don't exist yet, so a user's runtime edits are
    never overwritten by a restart."""
    global _PRICING_INITIALIZED
    if _PRICING_INITIALIZED:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_pricing (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            model_pattern   TEXT NOT NULL UNIQUE,
            -- USD per 1,000,000 tokens. NULL means "unknown" -> cost 0.
            input_per_m     REAL,
            output_per_m    REAL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for pattern, in_p, out_p in _SEED_PRICING:
        conn.execute(
            "INSERT OR IGNORE INTO model_pricing (model_pattern, input_per_m, output_per_m) "
            "VALUES (?, ?, ?)",
            (pattern, in_p, out_p),
        )
    conn.commit()
    _PRICING_INITIALIZED = True


def _db():
    conn = connect(profile_db_path())
    _ensure_pricing(conn)
    return conn


def price_for(provider: Optional[str], model: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """(input_per_M, output_per_M) USD for a model, looked up from the
    model_pricing table by substring match (so 'gpt-4o-2024-08-06' hits the
    'gpt-4o' row). Returns (None, None) for an unknown model -- the caller
    treats None as $0 but records usage anyway. Substring match is tried
    longest-pattern-first so a more specific row ('gpt-4o-mini') wins over a
    less specific one ('gpt-4o')."""
    m = (model or "").lower().strip()
    if not m:
        return (None, None)
    with _db() as conn:
        rows = conn.execute(
            "SELECT model_pattern, input_per_m, output_per_m FROM model_pricing"
        ).fetchall()
    # longest matching pattern wins (more specific)
    matches = [r for r in rows if r["model_pattern"].lower() in m]
    if not matches:
        return (None, None)
    matches.sort(key=lambda r: len(r["model_pattern"]), reverse=True)
    best = matches[0]
    return (best["input_per_m"], best["output_per_m"])


def estimate_cost(provider: Optional[str], model: Optional[str],
                  prompt_tokens: int, completion_tokens: int) -> float:
    in_p, out_p = price_for(provider, model)
    in_p = in_p or 0.0
    out_p = out_p or 0.0
    return (prompt_tokens / 1_000_000.0) * in_p + (completion_tokens / 1_000_000.0) * out_p


def record(account_id: int, provider: str, model: Optional[str],
           prompt_tokens: int, completion_tokens: int) -> float:
    """Record one usage event. Returns the estimated USD cost (0 for
    unknown/local models). The default account (id=1) is guaranteed to exist
    by bootstrap_default_account, so this never needs to create one."""
    cost = estimate_cost(provider, model, prompt_tokens, completion_tokens)
    with connect(profile_db_path()) as conn:
        conn.execute(
            "INSERT INTO token_accounting "
            "(account_id, provider, model, prompt_tokens, completion_tokens, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, provider, model, int(prompt_tokens), int(completion_tokens), cost),
        )
        conn.commit()
    return cost


def record_default(provider: str, model: Optional[str],
                   prompt_tokens: int, completion_tokens: int) -> float:
    """Record against the default account (the local-first single account)."""
    return record(DEFAULT_ACCOUNT_ID, provider, model, prompt_tokens, completion_tokens)


def summary(since_days: Optional[int] = None) -> dict:
    """Aggregate usage: total tokens + cost, optionally within the last
    ``since_days``. Also breaks down by provider so the UI can show
    'claude: $1.20, openai: $0.40'."""
    where = ""
    params: list = []
    if since_days:
        where = "WHERE recorded_at >= datetime('now', ?)"
        params.append(f"-{int(since_days)} days")
    sql = (
        "SELECT provider, "
        "SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, SUM(cost_usd) AS cost, "
        "COUNT(*) AS calls FROM token_accounting " + where +
        " GROUP BY provider ORDER BY cost DESC"
    )
    with connect(profile_db_path()) as conn:
        rows = conn.execute(sql, params).fetchall()
        totals = conn.execute(
            "SELECT SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, "
            "SUM(cost_usd) AS cost, COUNT(*) AS calls FROM token_accounting " + where,
            params,
        ).fetchone()
    by_provider = [
        {"provider": r["provider"], "prompt_tokens": r["p"] or 0,
         "completion_tokens": r["c"] or 0, "cost_usd": round(r["cost"] or 0, 6),
         "calls": r["calls"]}
        for r in rows
    ]
    return {
        "total_prompt_tokens": totals["p"] or 0,
        "total_completion_tokens": totals["c"] or 0,
        "total_cost_usd": round(totals["cost"] or 0, 6),
        "total_calls": totals["calls"] or 0,
        "by_provider": by_provider,
    }


# --- runtime pricing management (the editable-in-place part) ---------------

def list_pricing() -> list[dict]:
    """The full pricing table, for an admin UI to view/edit."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, model_pattern, input_per_m, output_per_m, updated_at "
            "FROM model_pricing ORDER BY model_pattern"
        ).fetchall()
    return [dict(r) for r in rows]


def set_price(model_pattern: str, input_per_m: Optional[float],
              output_per_m: Optional[float]) -> None:
    """Add or update a pricing row at runtime. ``model_pattern`` is matched
    as a substring of the model name (lowercased), so set 'gpt-4o' to cover
    every gpt-4o-* variant. None prices mean 'unknown' ($0). This is how
    prices stay current without an AppImage rebuild."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO model_pricing (model_pattern, input_per_m, output_per_m) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(model_pattern) DO UPDATE SET "
            "input_per_m=excluded.input_per_m, output_per_m=excluded.output_per_m, "
            "updated_at=datetime('now')",
            (model_pattern.lower().strip(), input_per_m, output_per_m),
        )
        conn.commit()


def delete_price(model_pattern: str) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM model_pricing WHERE model_pattern = ?",
            (model_pattern.lower().strip(),),
        )
        conn.commit()
        return cur.rowcount > 0
