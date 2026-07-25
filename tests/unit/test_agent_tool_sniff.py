"""Tool-call detection in the textual agent loop (api/agent_loop.py).

The bug these cover: detection used to happen ONLY at the very start of the
stream. A model that narrated before acting ("We need to search for FIXME or
TODO." then "SEARCH_WIKI: FIXME") never triggered a tool -- the protocol line
was relayed as literal answer text, the search never ran, and the model went
on to tell the user its tools were broken.
"""

import asyncio

import pytest

from api.agent_loop import (
    relay_without_tool_lines,
    sniff_and_relay,
)

PREFIXES = ["SEARCH_WIKI:", "READ_FILE:", "MEMORY_REMEMBER:", "MEMORY_RECALL:"]


async def _stream(*chunks):
    for chunk in chunks:
        yield chunk


class _Sink:
    def __init__(self):
        self.answer = []
        self.discarded = []

    async def send(self, text):
        self.answer.append(text)

    async def discard(self, text):
        self.discarded.append(text)

    @property
    def text(self):
        return "".join(self.answer)

    @property
    def dropped(self):
        return "".join(self.discarded)


def _run(chunks, sink):
    return asyncio.run(
        sniff_and_relay(_stream(*chunks), sink.send, PREFIXES,
                        on_discarded=sink.discard)
    )


def test_clean_tool_call_is_detected_and_not_relayed():
    sink = _Sink()
    result = _run(["SEARCH_WIKI: ", "authentication flow\n"], sink)
    assert result == ("SEARCH_WIKI:", "authentication flow")
    assert sink.text == ""


def test_tool_call_after_narration_is_detected():
    """The exact shape from the bug report."""
    sink = _Sink()
    result = _run(
        ["We need to search for FIXME or TODO.\n",
         "SEARCH_WIKI: FIXME cor_society\n"],
        sink,
    )
    assert result == ("SEARCH_WIKI:", "FIXME cor_society")
    # Narration is process noise, not the answer.
    assert sink.text == ""
    assert "We need to search" in sink.dropped


def test_tool_call_after_long_narration_still_fires():
    """Past the hold-back window the narration is shown, but the protocol
    line is still consumed and the tool still runs."""
    sink = _Sink()
    narration = "Let me think about this in detail. " * 40  # > HOLD_BACK_CHARS
    result = _run([narration + "\n", "READ_FILE: api/main.py\n"], sink)
    assert result == ("READ_FILE:", "api/main.py")
    assert "READ_FILE:" not in sink.text


def test_tool_call_without_trailing_newline():
    sink = _Sink()
    result = _run(["Checking the wiki.\n", "SEARCH_WIKI: safety module"], sink)
    assert result == ("SEARCH_WIKI:", "safety module")


def test_plain_answer_is_relayed_intact():
    sink = _Sink()
    chunks = ["The safety module ", "validates every input.\n", "It is fine."]
    assert _run(chunks, sink) is None
    assert sink.text == "".join(chunks)
    assert sink.dropped == ""


def test_prefix_mentioned_mid_sentence_is_not_a_tool_call():
    sink = _Sink()
    text = "You can call SEARCH_WIKI: like this from a prompt, but it is prose."
    assert _run([text], sink) is None
    assert sink.text == text


def test_bare_prefix_without_query_is_treated_as_text():
    sink = _Sink()
    assert _run(["SEARCH_WIKI:\n", "no query given here"], sink) is None
    assert "SEARCH_WIKI:" in sink.text


def test_final_round_drops_stray_tool_lines_but_keeps_the_answer():
    sink = _Sink()
    asyncio.run(relay_without_tool_lines(
        _stream("Here is what I found.\n",
                "SEARCH_WIKI: FIXME\n",
                "The module has no FIXMEs left.\n"),
        sink.send, PREFIXES, on_discarded=sink.discard,
    ))
    assert "SEARCH_WIKI:" not in sink.text
    assert "Here is what I found." in sink.text
    assert "The module has no FIXMEs left." in sink.text
    assert "SEARCH_WIKI: FIXME" in sink.dropped


def test_final_round_drops_an_unterminated_tool_line():
    sink = _Sink()
    asyncio.run(relay_without_tool_lines(
        _stream("Summary of the module.\n", "READ_FILE: api/main.py"),
        sink.send, PREFIXES, on_discarded=sink.discard,
    ))
    assert sink.text.strip() == "Summary of the module."


@pytest.mark.parametrize("split", [1, 3, 7, 13])
def test_detection_is_independent_of_chunk_boundaries(split):
    """Providers split tokens arbitrarily; the same text must behave the
    same however it arrives."""
    text = "Looking into it.\nSEARCH_WIKI: technical safety\n"
    sink = _Sink()
    chunks = [text[i:i + split] for i in range(0, len(text), split)]
    assert _run(chunks, sink) == ("SEARCH_WIKI:", "technical safety")
    assert sink.text == ""
