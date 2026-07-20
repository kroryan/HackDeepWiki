"""Out-of-band "process" event framing for the chat stream.

The chat transports (api/websocket_wiki.py, api/simple_chat.py) carry the
model's ANSWER as raw text over a single WebSocket/HTTP text stream. To also
surface what the model did behind the scenes -- its reasoning/thinking tokens
and the tool calls it made (SEARCH_WIKI / READ_FILE) -- without polluting the
answer a user reads as markdown, those events are sent as separately-framed
"process" messages over the SAME transport, distinguished from answer text by
a control-char sentinel that never occurs in normal prose:

    \\x01FDW\\x01<kind>\\x02<json-payload>\\x03

  - \\x01 (SOH) + "FDW" + \\x01 : frame intro (the answer path is plain text
    and never starts with a control char, so a single leading-byte check
    separates the two channels).
  - <kind>               : "tool" or "thinking".
  - \\x02 (STX)           : separates the kind from the JSON payload.
  - <json-payload>       : json.dumps of the event's fields (any control chars
    inside are json-escaped, so the payload can't contain a raw \\x03).
  - \\x03 (ETX)           : frame terminator (lets a byte-stream consumer --
    the HTTP SSE path -- split frames exactly, the same way the WS path does
    per-message).

The frontend mirrors this in src/utils/streamParser.ts.
"""
import json
from typing import Any, Awaitable, Callable

# A process frame, complete: SOH + "FDW" + SOH + kind + STX + json + ETX.
PROC_INTRO = "\x01FDW\x01"
PROC_FIELD = "\x02"
PROC_END = "\x03"

# `send_process(kind, payload)` is what a transport calls to emit one process
# event; the chat transports implement it as "send one framed WS/HTTP chunk".
SendProcess = Callable[[str, dict], Awaitable[None]]

# `thinking_sink(text)` is what stream_provider_response calls per reasoning
# chunk; the chat transports route it to send_process("thinking", ...). Kept
# as a distinct type from SendProcess so the provider layer doesn't need to
# know about framing -- it just hands reasoning text upward.
ThinkingSink = Callable[[str], Awaitable[None]]


def encode_process(kind: str, payload: dict) -> str:
    """Serialize one process event as a complete, self-terminating frame."""
    return f"{PROC_INTRO}{kind}{PROC_FIELD}{json.dumps(payload, ensure_ascii=False)}{PROC_END}"


def is_process_frame(text: str) -> bool:
    """True if `text` is (or begins) a process frame rather than answer text.

    Answer text from any model/provider is plain markdown -- it never starts
    with the SOH control char -- so this single check is what separates the
    two channels on the receiving end.
    """
    return text.startswith(PROC_INTRO)