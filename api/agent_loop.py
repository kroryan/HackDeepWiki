"""
Textual tool-calling agent loop layered on top of api.provider_streaming.

Both `.zim` chats and normal repo-wiki chats are single-shot prompt
completions today (see api/provider_streaming.py) -- no client here
normalizes native function-calling across all 8 supported providers. So
this reuses the same textual protocol Deep Research already uses (headings
like "## Research Plan" detected by substring): the system prompt instructs
the model that if it needs more context than it was given, its ENTIRE
response should be exactly one line, `<TOOL_PREFIX>: <query>` (e.g.
`SEARCH_WIKI: <query>`, or `READ_FILE: <path>` for repo chats -- see
api/search_tool.py's TOOL_LABELS for the full set). This module detects
that line as it streams in, resolves it via a caller-supplied handler for
whichever prefix matched, and re-prompts the model with the result
appended -- transparently to the caller, which only ever sees the final
answer text (plus a small "(Buscando: ...)"-style marker the backend
itself emits, never text the model wrote).
"""
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from api.anthropic_client import AnthropicClient
from api.openai_client import OpenAIClient
from api.litellm_client import LiteLLMClient
from api.provider_streaming import stream_provider_response
from api.search_tool import native_tool_name_to_prefix
from api.stream_events import SendProcess, ThinkingSink, encode_process

logger = logging.getLogger(__name__)

# Generous enough for a multi-hop chase (search -> follow a related result's
# title -> follow one of ITS related results -> ... -> answer) since a
# single search often isn't enough to answer a question that requires
# "clicking through" a couple of linked pages, not just bounded to one flat
# lookup. Still capped -- see the forced-final-round handling below, which
# guarantees an answer is always produced instead of looping forever.
MAX_TOOL_ROUNDS = 5
# Cap on how much of the "query" line we'll buffer before giving up on ever
# seeing a newline -- guards against a model that emits the prefix and then
# just keeps generating without ever closing the line.
MAX_QUERY_BUFFER = 500

SendChunk = Callable[[str], Awaitable[None]]


async def sniff_and_relay(
    stream: AsyncIterator[str], send_chunk: SendChunk, prefixes: list[str]
) -> Optional[tuple[str, str]]:
    """Relay `stream` to `send_chunk` verbatim UNLESS it turns out to be a
    `<prefix>: <query>` tool call for one of `prefixes`, in which case
    nothing is relayed and `(matched_prefix, query)` is returned instead
    (None if this was ordinary text).

    Only the first few characters need buffering: as soon as the
    accumulated text (leading whitespace ignored) stops being a valid
    case-insensitive prefix of ANY candidate, it's flushed in one shot and
    every later chunk is forwarded immediately -- the buffer never grows
    past the longest candidate prefix, so this adds no perceptible latency
    to ordinary answers. Candidates that share a common start (there are
    none today, but nothing here assumes otherwise) narrow down naturally
    as more characters arrive, same as a trie.
    """
    max_len = max(len(p) for p in prefixes)
    buffer = ""
    relaying = False
    confirmed: Optional[str] = None
    async for chunk in stream:
        if relaying:
            await send_chunk(chunk)
            continue

        buffer += chunk
        stripped = buffer.lstrip()
        if not stripped:
            if len(buffer) > MAX_QUERY_BUFFER:
                await send_chunk(buffer)
                relaying = True
            continue

        upper = stripped.upper()
        if confirmed is None:
            matched = [p for p in prefixes if upper.startswith(p)]
            if matched:
                confirmed = max(matched, key=len)
            elif len(upper) < max_len and any(p.startswith(upper) for p in prefixes):
                continue  # still an ambiguous prefix, need more characters
            else:
                # Definitely not a tool call: flush what we buffered and
                # relay everything else directly from here on.
                await send_chunk(buffer)
                relaying = True
                continue

        if "\n" in stripped or len(stripped) - len(confirmed) > MAX_QUERY_BUFFER:
            break
        continue  # confirmed tool-call line, keep collecting the query
    else:
        # Stream ended without ever diverging from (or completing) the
        # prefix check above.
        if not relaying and buffer:
            stripped = buffer.lstrip()
            upper = stripped.upper()
            matched = [p for p in prefixes if upper.startswith(p)]
            if matched:
                prefix = max(matched, key=len)
                query = stripped[len(prefix):].strip()
                if query:
                    return prefix, query
            await send_chunk(buffer)
        return None

    # Reached via `break`: buffer is a confirmed "<prefix>: ..." line.
    stripped = buffer.lstrip()
    line, _, _ = stripped.partition("\n")
    query = line[len(confirmed):].strip()
    if query:
        return confirmed, query
    # Full prefix but an empty query is ambiguous -- treat as ordinary text
    # rather than looping on a request we can't act on.
    await send_chunk(buffer)
    return None


async def _run_agent_rounds(
    *,
    provider: str,
    requested_model: Optional[str],
    prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str],
    api_endpoint: Optional[str],
    tools: dict[str, Callable[[str], str]],
    tool_labels: dict[str, str],
    send_chunk: SendChunk,
    send_process: Optional[SendProcess] = None,
    thinking_sink: Optional[ThinkingSink] = None,
) -> None:
    # Some models (seen with a reasoning-heavy cloud model under a longer,
    # tool-instructions-laden prompt) can legitimately end their stream
    # having produced zero content chunks -- no error, just nothing. Track
    # whether anything at all has reached the caller so that if the whole
    # loop ends without ever relaying a single character, we say SOMETHING
    # instead of leaving the user looking at a blank response with no
    # indication of what happened.
    sent_anything = False

    async def tracked_send_chunk(text: str) -> None:
        nonlocal sent_anything
        if text:
            sent_anything = True
        await send_chunk(text)

    current_prompt = prompt
    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        is_last_round = round_num == MAX_TOOL_ROUNDS
        if is_last_round:
            current_prompt += (
                "\n<note>You have used all available searches for this answer. "
                "Answer now using the information already gathered -- do not "
                "request another search.</note>\n"
            )

        stream = stream_provider_response(
            provider=provider,
            requested_model=requested_model,
            prompt=current_prompt,
            model_config_kwargs=model_config_kwargs,
            api_key=api_key,
            api_endpoint=api_endpoint,
            thinking_sink=thinking_sink,
        )

        if is_last_round:
            # No round left to act on a tool call even if the model ignores
            # the note above -- relay raw so the user never sees a blank
            # response because we swallowed a stray "SEARCH_WIKI:" line.
            async for chunk in stream:
                await tracked_send_chunk(chunk)
            break

        result = await sniff_and_relay(stream, tracked_send_chunk, list(tools.keys()))
        if not result:
            break
        prefix, query = result
        handler = tools[prefix]

        logger.info(f"Agent tool call {prefix} {query!r} (round {round_num})")
        label = tool_labels.get(prefix, "Buscando")
        # A tool call is a behind-the-scenes step, not part of the answer:
        # route it to the Process panel when one is attached, otherwise keep
        # the old inline "_(Buscando: ...)_" marker in the answer text.
        if send_process is not None:
            await send_process("tool", {"label": label, "query": query, "round": round_num})
        else:
            await tracked_send_chunk(f"\n\n_({label}: {query})_\n\n")
        try:
            tool_result = handler(query)
        except Exception as e:
            logger.warning(f"tool handler for {prefix} failed for query {query!r}: {e}")
            tool_result = "Tool call failed."

        current_prompt += (
            f"{prefix} {query}\n\n"
            f"<tool_result>\n{tool_result}\n</tool_result>\n\nAssistant: "
        )
    if not sent_anything:
        logger.warning("Agent loop produced no output at all; sending fallback message")
        await send_chunk(
            "I wasn't able to generate a response for that. Please try rephrasing your question."
        )


async def run_agent_chat(
    *,
    provider: str,
    requested_model: Optional[str],
    prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str] = None,
    api_endpoint: Optional[str] = None,
    tools: dict[str, Callable[[str], str]],
    tool_labels: Optional[dict[str, str]] = None,
) -> AsyncIterator[str]:
    """Async-generator facade over `_run_agent_rounds`'s callback-based loop,
    so both call sites (the WebSocket handler's `await websocket.send_text`
    and the HTTP handler's `yield`) can consume this the same way they
    already consume `stream_provider_response`: `async for text in ...`.

    `tools` maps a textual prefix (e.g. "SEARCH_WIKI:") to a
    `query -> tool_result text` handler -- see api/search_tool.py's
    resolve_tool_calling for how it's built (SEARCH_WIKI for every source
    type, plus READ_FILE for repo chats). `tool_labels` optionally maps the
    same prefixes to a human-readable label for the "(Buscando: ...)"-style
    status marker shown while a tool runs.

    Behind-the-scenes events -- tool calls and reasoning/"thinking" tokens
    -- are routed through the SAME internal queue as the answer text, as
    framed "process" messages (see api/stream_events.py), and yielded
    interleaved with it. So both transports just iterate this generator and
    forward every chunk verbatim: answer text goes to the answer bubble, a
    framed process message goes to the collapsible Process panel. Keeping
    answer and process on one queue preserves their order for either
    transport (WebSocket or HTTP), and a callback that can't `yield` into
    its own driver -- the HTTP case -- works without special handling.

    A background task drives the round loop and feeds an internal queue via
    the `send_chunk` callback; this generator just relays the queue in
    order. If the consumer stops iterating early (e.g. the client
    disconnects mid-round), the task is cancelled instead of left running.
    """
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    _DONE = object()

    async def send_chunk(text: str) -> None:
        await queue.put(text)

    async def send_process(kind: str, payload: dict) -> None:
        await queue.put(encode_process(kind, payload))

    async def thinking_sink(text: str) -> None:
        await send_process("thinking", {"text": text})

    async def runner() -> None:
        try:
            await _run_agent_rounds(
                provider=provider,
                requested_model=requested_model,
                prompt=prompt,
                model_config_kwargs=model_config_kwargs,
                api_key=api_key,
                api_endpoint=api_endpoint,
                tools=tools,
                tool_labels=tool_labels or {},
                send_chunk=send_chunk,
                send_process=send_process,
                thinking_sink=thinking_sink,
            )
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(runner())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        # Only reached normally once the loop above breaks, or via
        # GeneratorExit if the consumer stops iterating early (client
        # disconnect mid-round) -- either way, don't leave the round loop
        # running in the background.
        if not task.done():
            task.cancel()

    # Only reached on normal completion (GeneratorExit propagates past this
    # point without running it). Surfaces any exception the round loop hit
    # (e.g. a provider error), matching stream_provider_response's behavior
    # of propagating to the caller's own try/except.
    try:
        await task
    except asyncio.CancelledError:
        pass


def _build_native_client(provider: str, api_key: Optional[str], api_endpoint: Optional[str]):
    if provider == "claude":
        return AnthropicClient(api_key=api_key, base_url=api_endpoint)
    client_kwargs: dict = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if api_endpoint:
        client_kwargs["base_url"] = api_endpoint
    if provider == "litellm":
        return LiteLLMClient(**client_kwargs)
    return OpenAIClient(**client_kwargs)  # openai, openai_custom


async def _run_native_tool_rounds(
    *,
    provider: str,
    requested_model: Optional[str],
    system_prompt: str,
    user_prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str],
    api_endpoint: Optional[str],
    tools: dict[str, Callable[[str], str]],
    tool_labels: dict[str, str],
    tool_schemas_anthropic: list[dict],
    tool_schemas_openai: list[dict],
    send_chunk: SendChunk,
    send_process: Optional[SendProcess] = None,
) -> None:
    """Structured/native tool-calling loop: unlike _run_agent_rounds (which
    sniffs a textual convention out of a token stream), the model's actual
    API schema is what makes it emit a tool call, not a hope that it
    follows prompted-in text. Each round streams live over SSE
    (astream_with_tools on both clients) and relays text deltas to
    `send_chunk` as they arrive -- so an answer appears progressively even
    while native tool-calling is active, exactly like every other provider,
    instead of buffering the whole round before showing anything.
    """
    client = _build_native_client(provider, api_key, api_endpoint)
    is_claude = provider == "claude"
    tool_schemas = tool_schemas_anthropic if is_claude else tool_schemas_openai

    if is_claude:
        messages: list = [{"role": "user", "content": user_prompt}]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    model_kwargs_base = dict(model_config_kwargs)
    model_kwargs_base["model"] = requested_model
    if is_claude:
        model_kwargs_base["system"] = system_prompt
        model_kwargs_base.setdefault("max_tokens", 8192)

    sent_anything = False

    for round_num in range(1, MAX_TOOL_ROUNDS + 1):
        is_last_round = round_num == MAX_TOOL_ROUNDS
        round_tools = [] if is_last_round else tool_schemas

        tool_calls: list = []
        round_text_parts: list = []
        try:
            async for event in client.astream_with_tools(
                messages=messages, tools=round_tools, model_kwargs=dict(model_kwargs_base)
            ):
                if event["type"] == "text":
                    text = event["text"]
                    if text:
                        sent_anything = True
                        round_text_parts.append(text)
                        await send_chunk(text)
                elif event["type"] == "final":
                    tool_calls = event.get("tool_calls") or []
        except Exception as e:
            logger.error(f"Native tool-calling stream failed (round {round_num}): {e}")
            await send_chunk(f"\nError calling the model: {e}\n")
            return

        round_text = "".join(round_text_parts)

        if not tool_calls or is_last_round:
            if not sent_anything:
                await send_chunk(
                    "I wasn't able to generate a response for that. Please try rephrasing your question."
                )
            return

        # Append the assistant's tool-call turn, then a result for each call,
        # in whichever shape this provider's continuation expects.
        if is_claude:
            assistant_content = []
            if round_text:
                assistant_content.append({"type": "text", "text": round_text})
            assistant_content.extend(
                {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("input", {})}
                for tc in tool_calls
            )
            messages.append({"role": "assistant", "content": assistant_content})
            tool_result_blocks = []
        else:
            messages.append({
                "role": "assistant",
                "content": round_text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {})),
                        },
                    }
                    for tc in tool_calls
                ],
            })

        for tc in tool_calls:
            prefix = native_tool_name_to_prefix(tc["name"])
            handler = tools.get(prefix) if prefix else None
            args = tc.get("input") if is_claude else tc.get("arguments")
            args = args or {}
            query = next(iter(args.values()), "") if args else ""

            if handler is None:
                tool_result = f"Unknown tool {tc['name']!r}."
            else:
                logger.info(f"Native agent tool call {tc['name']} {query!r} (round {round_num})")
                label = tool_labels.get(prefix, "Buscando")
                # Route the tool call to the Process panel when attached
                # (mirrors the textual loop), else keep the inline marker.
                if send_process is not None:
                    await send_process("tool", {"label": label, "query": query, "tool": tc["name"], "round": round_num})
                else:
                    await send_chunk(f"\n\n_({label}: {query})_\n\n")
                try:
                    tool_result = handler(query)
                except Exception as e:
                    logger.warning(f"tool handler for {tc['name']} failed for {query!r}: {e}")
                    tool_result = "Tool call failed."

            if is_claude:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": tool_result,
                })
            else:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

        if is_claude:
            messages.append({"role": "user", "content": tool_result_blocks})

    # Unreachable: the loop above always returns by is_last_round, since
    # MAX_TOOL_ROUNDS >= 1 -- kept only as a defensive fallback.
    await send_chunk("I wasn't able to generate a response for that. Please try rephrasing your question.")


async def run_native_tool_chat(
    *,
    provider: str,
    requested_model: Optional[str],
    system_prompt: str,
    user_prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str] = None,
    api_endpoint: Optional[str] = None,
    tools: dict[str, Callable[[str], str]],
    tool_labels: Optional[dict[str, str]] = None,
    tool_schemas_anthropic: list[dict],
    tool_schemas_openai: list[dict],
) -> AsyncIterator[str]:
    """Async-generator facade over `_run_native_tool_rounds`, mirroring
    `run_agent_chat`'s queue-bridging shape so both call sites can consume
    either interchangeably (`async for text in ...`). Use this instead of
    `run_agent_chat` when `provider in api.search_tool.NATIVE_TOOL_PROVIDERS`.

    Like run_agent_chat, native tool calls are routed through the same queue
    as the answer text as framed "process" messages (see api/stream_events.py),
    so both transports just iterate and forward every chunk verbatim.
    """
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    _DONE = object()

    async def send_chunk(text: str) -> None:
        await queue.put(text)

    async def send_process(kind: str, payload: dict) -> None:
        await queue.put(encode_process(kind, payload))

    async def runner() -> None:
        try:
            await _run_native_tool_rounds(
                provider=provider,
                requested_model=requested_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model_config_kwargs=model_config_kwargs,
                api_key=api_key,
                api_endpoint=api_endpoint,
                tools=tools,
                tool_labels=tool_labels or {},
                tool_schemas_anthropic=tool_schemas_anthropic,
                tool_schemas_openai=tool_schemas_openai,
                send_chunk=send_chunk,
                send_process=send_process,
            )
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(runner())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass


async def stream_chat(
    *,
    provider: str,
    requested_model: Optional[str],
    prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str] = None,
    api_endpoint: Optional[str] = None,
) -> AsyncIterator[str]:
    """Single-shot (no tool-calling) chat stream with the same out-of-band
    Process channel as run_agent_chat: reasoning/"thinking" tokens are routed
    as framed "process" messages through one queue alongside the answer
    text, so both transports consume this identically to the tool-calling
    facades (`async for text in ...`, forward every chunk verbatim). This is
    the tool-calling-OFF path -- a plain stream_provider_response, except its
    thinking tokens (Ollama thinking models) reach the Process panel instead
    of being dropped.

    Kept here next to the other two chat facades so all three share one shape
    and the transports never branch on "is there a Process panel" -- they
    always iterate a chat facade and forward.
    """
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    _DONE = object()

    async def send_chunk(text: str) -> None:
        await queue.put(text)

    async def thinking_sink(text: str) -> None:
        await queue.put(encode_process("thinking", {"text": text}))

    async def runner() -> None:
        try:
            async for text in stream_provider_response(
                provider=provider,
                requested_model=requested_model,
                prompt=prompt,
                model_config_kwargs=model_config_kwargs,
                api_key=api_key,
                api_endpoint=api_endpoint,
                thinking_sink=thinking_sink,
            ):
                await send_chunk(text)
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(runner())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
