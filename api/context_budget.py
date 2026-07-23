"""Keeps the `<file_tree>` block of a wiki-structure-planning prompt within
the selected model's actual context window, proactively -- instead of only
reacting after the provider already rejected an oversized prompt (see
CONTEXT_LIMIT_ERROR_PHRASES in websocket_wiki.py/simple_chat.py, which stays
in place as a defense-in-depth fallback for whatever this estimate misses).

Why this exists: the frontend (src/app/[owner]/[repo]/page.tsx) embeds the
COMPLETE, unfiltered file tree of the repository into the structure-planning
prompt with no size cap -- fine for a small/medium repo and a large-context
cloud model, but a large monorepo (thousands of files) against a
small-context local model (Ollama models often default to a few thousand
tokens unless configured otherwise) reliably blows the context window,
producing a hard 500 error with no usable wiki at all.

Shared by both chat transports (websocket_wiki.py, simple_chat.py) so they
can't drift on this -- mirrors how MAX_FALLBACK_QUERY_CHARS/
CONTEXT_LIMIT_ERROR_PHRASES are already independently duplicated between the
two with a cross-referencing comment, the established pattern in this
codebase for HTTP/WebSocket transport parity.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Dict

logger = logging.getLogger(__name__)

_FILE_TREE_RE = re.compile(r"(<file_tree>\n)(.*?)(\n</file_tree>)", re.DOTALL)

# Ollama models default to a modest context window unless the caller (or the
# model's own Modelfile) configures num_ctx explicitly -- generator.json
# specifies num_ctx per listed model, but a custom/unlisted Ollama model (or
# any provider with no num_ctx in its resolved model_kwargs) has no reliable
# value here, so this is a conservative assumption for "provider says nothing
# concrete about its window", NOT applied when num_ctx IS present.
_DEFAULT_CONTEXT_TOKENS: Dict[str, int] = {
    "ollama": 8192,
}
# Cloud providers (OpenAI/Anthropic/Google/Bedrock/OpenRouter/...) all have
# context windows far larger than any repo's file tree could plausibly need,
# so this is just a conservative floor for "no better information available",
# not a real observed limit for any of them.
_FALLBACK_CLOUD_CONTEXT_TOKENS = 100_000

# Fraction of the model's context window reserved for the file tree
# specifically -- leaves room for the README, task instructions, output XML
# schema, conversation history, and the model's own generated output.
_FILE_TREE_BUDGET_FRACTION = 0.35

# Per-directory entry caps tried in order (most generous first) until the
# summarized tree fits the token budget.
_PER_DIR_CAPS = (50, 30, 20, 12, 8, 5, 3, 2, 1)


def resolve_context_window(provider: str, model_config_kwargs: dict) -> int:
    """The effective context window (tokens) to budget the file tree
    against: the model's own configured num_ctx if we have one (Ollama),
    else a provider-appropriate default."""
    num_ctx = model_config_kwargs.get("num_ctx") if model_config_kwargs else None
    if isinstance(num_ctx, (int, float)) and num_ctx > 0:
        return int(num_ctx)
    return _DEFAULT_CONTEXT_TOKENS.get(provider, _FALLBACK_CLOUD_CONTEXT_TOKENS)


def _summarize_tree_text(tree_text: str, budget_tokens: int, count_tokens_fn, is_ollama: bool) -> str:
    """Group file paths by directory and progressively cap how many entries
    of each directory are shown, so a directory with hundreds/thousands of
    near-identical files (or a monorepo with thousands of directories) never
    silently loses whole sections -- every directory keeps at least a
    representative sample, with an explicit count of what's hidden, instead
    of a naive head/tail character truncation losing entire directories
    outright depending on where they land alphabetically."""
    lines = [line for line in tree_text.split("\n") if line.strip()]
    groups: Dict[str, list] = defaultdict(list)
    for line in lines:
        directory = "/".join(line.split("/")[:-1]) or "."
        groups[directory].append(line)

    candidate = tree_text
    for per_dir_cap in _PER_DIR_CAPS:
        out_lines = []
        for directory in sorted(groups):
            entries = sorted(groups[directory])
            shown = entries[:per_dir_cap]
            out_lines.extend(shown)
            hidden = len(entries) - len(shown)
            if hidden > 0:
                out_lines.append(f"... and {hidden} more file(s) in {directory}/ (not shown individually)")
        candidate = "\n".join(out_lines)
        if count_tokens_fn(candidate, is_ollama_embedder=is_ollama) <= budget_tokens:
            return candidate

    # Every directory already capped at 1 entry and still too big (an
    # extreme case: tens of thousands of distinct directories) -- fall back
    # to a hard character truncate as an absolute last resort.
    approx_chars = max(budget_tokens * 4, 1000)
    return candidate[:approx_chars] + "\n... [tree truncated further -- too large even summarized]"


def summarize_file_tree_in_query(
    query: str,
    *,
    provider: str,
    model_config_kwargs: dict,
    count_tokens_fn,
) -> str:
    """If `query` contains a `<file_tree>...</file_tree>` block (the
    wiki-structure-planning prompt built by determineWikiStructure in
    page.tsx) and it doesn't fit the resolved model's context budget,
    replace it in place with a directory-aware summary. Returns `query`
    unchanged if there's no file_tree block or it already fits."""
    match = _FILE_TREE_RE.search(query)
    if not match:
        return query

    tree_text = match.group(2)
    is_ollama = provider == "ollama"
    context_window = resolve_context_window(provider, model_config_kwargs)
    budget_tokens = int(context_window * _FILE_TREE_BUDGET_FRACTION)

    if count_tokens_fn(tree_text, is_ollama_embedder=is_ollama) <= budget_tokens:
        return query

    logger.warning(
        "File tree in prompt exceeds ~%d token budget for provider=%s (context_window=%d); summarizing per-directory",
        budget_tokens, provider, context_window,
    )
    summarized = _summarize_tree_text(tree_text, budget_tokens, count_tokens_fn, is_ollama)
    return query[: match.start(2)] + summarized + query[match.end(2):]
