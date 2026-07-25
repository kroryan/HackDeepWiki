"""What reaches Engraphis memory, and how it is connected.

Two defects these cover:
  * auto-capture stored ANY answer over 120 chars, so a model narrating its
    way through a broken tool loop became a permanent "memory" that was then
    re-injected into later questions as project context;
  * nothing ever called ``MemoryService.link``, so every memory was an
    isolated node -- a list, not a graph.

The linking tests run against a REAL Engraphis service on a temp SQLite file
(no mocks): the edges are read back out of ``mem_links``.
"""

import pytest

from api.chat_common import distill_exchange, is_worth_remembering

# The transcript from the bug report, trimmed but structurally identical: a
# model narrating around tool-call lines that never fired.
BROKEN_LOOP_ANSWER = """\
We need to search for FIXME or TODO.
SEARCH_WIKI: FIXME cor_society
Let me look at the technical safety module.
READ_FILE: cor_society/modules/technical_safety.js
It seems the tool is not functioning. Let me try once more.
SEARCH_WIKI: TODO cor_society
READ_FILE: cor_society/modules/technical_safety.js
I don't have access to the file contents, so I cannot list the FIXMEs.
"""

REAL_ANSWER = """\
The technical safety module validates every incoming control frame before it
reaches the actuator layer. Frames that fail the CRC check are dropped and
counted in `safety.rejected_frames`, which the watchdog polls every 500 ms.

Two FIXMEs remain in that module: the timeout is hard-coded to 500 ms instead
of reading `config.watchdog_interval`, and the rejection counter never resets
across restarts.
"""


# -- the relevance gate ---------------------------------------------------

def test_broken_tool_loop_is_not_remembered():
    assert not is_worth_remembering(
        "list the FIXMEs in the safety module", BROKEN_LOOP_ANSWER
    )


def test_real_answer_is_remembered():
    assert is_worth_remembering(
        "list the FIXMEs in the safety module", REAL_ANSWER
    )


def test_trivial_question_is_not_remembered():
    assert not is_worth_remembering("ok", REAL_ANSWER)


def test_answer_that_is_only_a_tool_call_is_not_remembered():
    assert not is_worth_remembering(
        "where is the auth code?", "SEARCH_WIKI: authentication\n"
    )


def test_failure_report_is_not_remembered():
    answer = (
        "I was unable to find that information in the wiki. " * 4
        + "\nPlease try rephrasing your question about the repository."
    )
    assert not is_worth_remembering("where is the auth code?", answer)


def test_repetition_loop_is_not_remembered():
    answer = "\n".join(["Let me check the module again." if i % 2 else
                        "Checking the technical safety module now."
                        for i in range(10)])
    assert not is_worth_remembering("what does the module do?", answer)


def test_an_answer_discussing_errors_is_still_remembered():
    """The gate rejects answers that ARE a failure, not answers ABOUT one."""
    answer = (
        "The parser raises ValueError when the header is truncated; the caller "
        "in api/data_pipeline.py catches it and logs 'error parsing chunk'. "
        "Every error path funnels into `_record_failure`, which increments the "
        "per-repo counter used by the retry budget in the ingest worker."
    )
    assert is_worth_remembering("how are parse errors handled?", answer)


# -- distillation ---------------------------------------------------------

def test_distill_strips_protocol_and_narration():
    out = distill_exchange("what changed in v2?", BROKEN_LOOP_ANSWER)
    assert "SEARCH_WIKI:" not in out
    assert "READ_FILE:" not in out
    assert "Let me look at" not in out
    assert out.startswith("Q: what changed in v2?\nA: ")


def test_distill_drops_repeated_lines():
    answer = "The watchdog polls every 500 ms.\n" * 4
    out = distill_exchange("watchdog?", answer)
    assert out.count("The watchdog polls") == 1


def test_distill_keeps_head_and_tail_of_a_long_answer():
    from api.chat_common import MAX_AUTOMEMORY_ANSWER_CHARS

    answer = ("HEAD-MARKER starts here.\n"
              + "\n".join(f"filler line number {i} about the module" for i in range(400))
              + "\nTAIL-MARKER: the conclusion lives at the end.")
    out = distill_exchange("summarize the module", answer)
    assert "HEAD-MARKER" in out and "TAIL-MARKER" in out
    assert "[...]" in out
    assert len(out) < MAX_AUTOMEMORY_ANSWER_CHARS + 800


def test_distill_preserves_the_substance_of_a_real_answer():
    out = distill_exchange("list the FIXMEs", REAL_ANSWER)
    assert "safety.rejected_frames" in out
    assert "config.watchdog_interval" in out


# -- the graph (live Engraphis, real SQLite) ------------------------------

@pytest.fixture
def memory(tmp_path, monkeypatch):
    """A real MemoryService on a throwaway DB, wired into the integration
    module the same way ``_ensure_started`` wires the app's own."""
    pytest.importorskip("engraphis")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "0")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("HACKDEEPWIKI_ENGRAPHIS_EMBEDDER", "0")
    monkeypatch.setenv("HACKDEEPWIKI_DATA_DIR", str(tmp_path))

    from api import data_root
    monkeypatch.setattr(data_root, "_cached_root", None, raising=False)

    from engraphis.service import MemoryService
    service = MemoryService.create(str(tmp_path / "memory.db"),
                                   embed_model="", embed_dim=384)

    from api import engraphis_integration as eng
    monkeypatch.setattr(eng, "_service", service)
    monkeypatch.setattr(eng, "_start_error", None)
    monkeypatch.setattr(eng, "_bootstrapped", True)
    eng.ensure_workspace("acme_widgets_v1", "test workspace")
    try:
        yield eng
    finally:
        try:
            service.store.conn.close()
        except Exception:
            pass


def _edges(eng, workspace):
    rows = eng._service.store.conn.execute(
        "SELECT a, b, relation FROM mem_links"
    ).fetchall()
    return [(r["a"], r["b"], r["relation"]) for r in rows]


def test_remember_linked_chains_consecutive_memories(memory):
    ws = "acme_widgets_v1"
    first = memory.remember_linked(
        ws, "Q: how does auth work?\nA: JWT issued by the gateway.",
        chain_key="exchange_chat")
    second = memory.remember_linked(
        ws, "Q: where is the gateway?\nA: services/gateway/main.py.",
        chain_key="exchange_chat")

    assert first and second and first != second
    assert (second, first, "follows") in _edges(memory, ws)


def test_separate_chains_do_not_cross_link(memory):
    ws = "acme_widgets_v1"
    chat = memory.remember_linked(ws, "Q: chat one\nA: an answer about auth.",
                                  chain_key="exchange_chat")
    editor = memory.remember_linked(ws, "Q: editor one\nA: an answer about a patch.",
                                    chain_key="exchange_code_agent")
    edges = _edges(memory, ws)
    assert (editor, chat, "follows") not in edges
    assert not [e for e in edges if e[0] == editor and e[2] == "follows"]


def test_remember_linked_references_related_memories(memory):
    ws = "acme_widgets_v1"
    earlier = memory.remember_linked(
        ws, "The gateway issues JWT tokens signed with RS256 for every login.")
    later = memory.remember_linked(
        ws, "Q: which algorithm signs the JWT?\nA: RS256, in the gateway.",
        relate_query="JWT tokens signed gateway login")

    references = [e for e in _edges(memory, ws)
                  if e[0] == later and e[2] == "references"]
    assert references, "the new memory should reference what it builds on"
    assert earlier in [e[1] for e in references]


def test_a_memory_never_links_to_itself(memory):
    ws = "acme_widgets_v1"
    mem_id = memory.remember_linked(
        ws, "The watchdog polls safety.rejected_frames every 500 ms.",
        chain_key="exchange_chat",
        relate_query="watchdog polls rejected frames")
    assert not [e for e in _edges(memory, ws) if e[0] == e[1] == mem_id]


def test_capture_chat_exchange_skips_the_broken_loop(memory):
    from api.chat_common import capture_chat_exchange

    capture_chat_exchange(owner="acme", repo="widgets", wiki_version=1,
                          question="list the FIXMEs in the safety module",
                          answer=BROKEN_LOOP_ANSWER)
    count = memory._service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories"
    ).fetchone()["n"]
    assert count == 0


def test_capture_chat_exchange_stores_and_connects_real_answers(memory):
    from api.chat_common import capture_chat_exchange

    second_answer = (
        "The rejection counter is reset only by `safety.reset_counters()`, "
        "which nothing calls on startup -- that is the second FIXME. The "
        "watchdog therefore reports frames rejected before the last restart "
        "as if they had just happened."
    )
    for question, answer in (
        ("list the FIXMEs in the safety module", REAL_ANSWER),
        ("what resets the rejection counter?", second_answer),
    ):
        capture_chat_exchange(owner="acme", repo="widgets", wiki_version=1,
                              question=question, answer=answer)

    rows = memory._service.store.conn.execute(
        "SELECT id, content FROM memories ORDER BY rowid"
    ).fetchall()
    assert len(rows) == 2
    assert "SEARCH_WIKI:" not in rows[0]["content"]
    assert (rows[1]["id"], rows[0]["id"], "follows") in _edges(
        memory, "acme_widgets_v1")
