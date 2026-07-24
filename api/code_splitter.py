"""Code-aware document splitter for the embedding pipeline.

Why this exists (B18): the default pipeline used a single adalflow
``TextSplitter(split_by="word", chunk_size=500)`` for *every* document -- code
included. Word-splitting code is fine for identifiers but the 500-word sliding
window lands in the middle of functions, statements, and even multi-line
strings, producing chunks that cut a function in half. When the retriever
then surfaces such a chunk, the model sees an amputated function with no
signature or no body, which directly hurts wiki/code-answer quality.

``CodeAwareSplitter`` is a drop-in replacement for ``TextSplitter`` in the
``adal.Sequential(splitter, embedder)`` pipeline. It dispatches per document:

* ``is_code`` documents -> line-aware splitting that respects logical
  boundaries (top-level ``def``/``class``/``function``/``export``/``fn``/...
  and markdown headings, then blank-line paragraphs) so a chunk never breaks
  a function or class mid-body.
* everything else -> the original ``TextSplitter`` (word-based) unchanged.

The output contract matches ``TextSplitter.call`` exactly: a flat
``List[Document]`` whose chunks carry the parent's ``meta_data`` (deep-copied),
a ``parent_doc_id``, an ``order`` index, and an empty ``vector`` -- so the
downstream embedder and ``LocalDB`` see no difference.

Portability: stdlib only (``re``, ``copy``, ``logging``). No new dependency.
"""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from typing import List

from adalflow.components.data_process import TextSplitter
from adalflow.core.types import Document

logger = logging.getLogger(__name__)

# Character budget for a single code chunk. Code has far fewer "words" per line
# than prose, and a word-count window is the wrong unit; we size by characters
# (~= 4 chars/token => ~1000 tokens at the default 4000). Override with the env
# var so large repos / small embedder windows can tune it without a code change.
_CODE_CHUNK_CHARS = int(os.environ.get("HACKDEEPWIKI_CODE_CHUNK_CHARS", "4000"))
# Lines of overlap carried between adjacent code chunks so a chunk boundary
# never loses the closing lines of the previous block (keeps context for the
# embedder). Small on purpose: code context is dense.
_CODE_OVERLAP_LINES = int(os.environ.get("HACKDEEPWIKI_CODE_OVERLAP_LINES", "4"))
# A single logical block larger than this is force-split on blank lines so we
# don't end up with one giant chunk when a file has no top-level boundaries
# (e.g. a long shell script or a minified file).
_MAX_BLOCK_CHARS = int(os.environ.get("HACKDEEPWIKI_CODE_MAX_BLOCK_CHARS", "8000"))

# Lines that START a new top-level logical unit. CRITICAL: must be at column 0
# (no leading whitespace) so an indented nested def/class inside a function
# body is NOT treated as a boundary -- that would fragment the very function
# bodies we're trying to keep intact. Covers Python, JS/TS, Rust, Go,
# Java/Kotlin, C/C++, Ruby, etc. -- deliberately permissive: a false
# "boundary" just makes a slightly smaller chunk, while a missed boundary is
# the real failure mode we're avoiding.
_BOUNDARY_RE = re.compile(
    r"""^(?:                # anchored at col 0, no leading whitespace allowed
          (async\s+)?def\s            |   # python def / async def
          class\s                     |   # python / js / ts class
          function\s                  |   # js function (function name(...))
          export\s+(default\s+)?(async\s+)?function\s  |
          export\s+(default\s+)?(const|let|var)\s      |
          export\s+class\s            |
          pub\s+(async\s+)?fn\s       |   # rust pub fn / pub async fn
          fn\s                        |   # rust fn
          impl\s                      |   # rust impl
          func\s                      |   # go func
          @implementation\s           |   # objc
          @end                        |
          module\s                    |   # ruby / js module
          interface\s                 |   # ts / java interface
          type\s                      |   # ts / haskell type
          enum\s                      |   # ts / java / rust enum
          struct\s                    |   # rust / c struct
          namespace\s                   # c++ / c#
        )""",
    re.VERBOSE,
)

# NOTE: we intentionally do NOT treat `#`-prefixed lines as markdown headings
# here. In a code file (the whole point of this splitter) `#` is a comment in
# Python/Ruby/bash/etc.; treating `# foo` as a chunk boundary would fragment
# every commented block. Docs flagged is_code still split cleanly on the
# blank-line paragraph fallback below.


def _is_boundary_line(line: str) -> bool:
    """True if this line should START a new chunk (a top-level definition at
    column 0). Indented definitions inside a body return False, so function
    bodies stay intact. Blank/comment lines never start a chunk."""
    if not line or line[0] in (" ", "\t"):
        return False
    return _BOUNDARY_RE.match(line) is not None


def _split_code_into_blocks(text: str) -> List[str]:
    """Break a code file into logical blocks at top-level boundaries and
    blank-line paragraphs. Each returned block is a contiguous slice of the
    original text (with its trailing newline preserved) that should not be
    split further unless it exceeds ``_MAX_BLOCK_CHARS``."""
    lines = text.splitlines(keepends=True)
    blocks: List[str] = []
    current: List[str] = []

    def flush():
        if current:
            blocks.append("".join(current))
            current.clear()

    for line in lines:
        starts_new_unit = _is_boundary_line(line.rstrip("\n"))
        # A top-level boundary flushes whatever accumulated before it (so the
        # new def/class starts its own block), but we keep the boundary line
        # itself as the start of the new block.
        if starts_new_unit and current:
            # Only flush if the current block isn't just a leading shebang/blank
            if any(ln.strip() for ln in current):
                flush()
        current.append(line)
    flush()

    # Force-split any block that's still too large (no top-level boundaries
    # found -- e.g. a long script or a data file) on blank-line paragraphs.
    final: List[str] = []
    for block in blocks:
        if len(block) <= _MAX_BLOCK_CHARS:
            final.append(block)
            continue
        paragraphs = re.split(r"(\n\s*\n)", block)  # keep separators
        buf = ""
        for piece in paragraphs:
            if len(buf) + len(piece) > _MAX_BLOCK_CHARS and buf:
                final.append(buf)
                buf = piece if piece.strip() else ""
            else:
                buf += piece
        if buf:
            final.append(buf)
    return final


def _pack_code_blocks(text: str, chunk_chars: int, overlap_lines: int) -> List[str]:
    """Greedily pack logical blocks into chunks of at most ``chunk_chars``
    characters, never breaking a block. Between chunks, carry the last
    ``overlap_lines`` lines of the previous chunk as overlap so the embedder
    retains closing-context."""
    blocks = _split_code_into_blocks(text)
    if not blocks:
        return [text] if text else []

    chunks: List[str] = []
    current = ""
    for block in blocks:
        # A block larger than the whole budget is emitted as its own chunk(s);
        # we don't slice mid-block, but if it's truly huge we split it on
        # lines as a last resort.
        if len(block) > chunk_chars:
            if current:
                chunks.append(current)
                current = ""
            # last-resort line split for oversized blocks
            blines = block.splitlines(keepends=True)
            buf = ""
            for ln in blines:
                if len(buf) + len(ln) > chunk_chars and buf:
                    chunks.append(buf)
                    buf = ln
                else:
                    buf += ln
            if buf:
                current = buf
            continue

        if len(current) + len(block) > chunk_chars and current:
            chunks.append(current)
            # overlap: carry the tail lines of the just-finished chunk
            tail = "".join(current.splitlines(keepends=True)[-overlap_lines:])
            current = tail + block
        else:
            current += block
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


class CodeAwareSplitter:
    """A drop-in replacement for adalflow's ``TextSplitter`` that splits code
    on logical boundaries and delegates prose to the standard word splitter.

    Construct it with the same ``text_splitter`` config the pipeline already
    loads from ``embedder.json``; the prose path stays identical to before.
    Code path is controlled by env vars (see module docstring).
    """

    def __init__(
        self,
        split_by: str = "word",
        chunk_size: int = 500,
        chunk_overlap: int = 150,
        **kwargs,
    ):
        # Prose documents keep the exact previous behaviour.
        self._prose_splitter = TextSplitter(
            split_by=split_by, chunk_size=chunk_size, chunk_overlap=chunk_overlap, **{k: v for k, v in kwargs.items() if k in ("batch_size", "separators")}
        )
        self._chunk_chars = _CODE_CHUNK_CHARS
        self._overlap_lines = _CODE_OVERLAP_LINES
        logger.info(
            f"CodeAwareSplitter: prose={split_by}/{chunk_size}, "
            f"code<= {_CODE_CHUNK_CHARS}chars / {_CODE_OVERLAP_LINES}line overlap"
        )

    def split_text(self, text: str, is_code: bool = False) -> List[str]:
        if is_code:
            return _pack_code_blocks(text, self._chunk_chars, self._overlap_lines)
        return self._prose_splitter.split_text(text)

    def call(self, documents: List[Document]) -> List[Document]:
        """Match ``TextSplitter.call`` exactly: emit chunk Documents carrying
        the parent's deep-copied meta_data, a parent_doc_id, an order index,
        and an empty vector."""
        if not isinstance(documents, list) or any(
            not isinstance(d, Document) for d in documents
        ):
            raise TypeError("Input should be a list of Documents.")

        split_docs: List[Document] = []
        for doc in documents:
            if doc.text is None:
                raise ValueError(f"Text should not be None. Doc id: {doc.id}")
            is_code = bool((doc.meta_data or {}).get("is_code"))
            text_splits = self.split_text(doc.text, is_code=is_code)
            meta_data = deepcopy(doc.meta_data)
            split_docs.extend(
                Document(
                    text=txt,
                    meta_data=meta_data,
                    parent_doc_id=f"{doc.id}",
                    order=i,
                    vector=[],
                )
                for i, txt in enumerate(text_splits)
            )
        logger.info(
            f"CodeAwareSplitter: {len(documents)} docs -> {len(split_docs)} chunks"
        )
        return split_docs