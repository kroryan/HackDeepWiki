"""Anthropic (Claude) ModelClient integration.

Talks directly to api.anthropic.com instead of routing through a LiteLLM
proxy, which requires a locally-running gateway that the portable AppImage
never starts. Accepts either:

- A standard Anthropic API key (starts with "sk-ant-api"), sent via the
  `x-api-key` header.
- A Claude Pro/Max subscription OAuth access token (obtained via `claude
  login` in Claude Code, or any Anthropic OAuth login flow), sent via
  `Authorization: Bearer` with the `anthropic-beta: oauth-2025-04-20` header
  Anthropic requires for OAuth-authenticated requests.
"""

import json
import logging
import os
from typing import Any, AsyncIterator, Dict

import aiohttp
import requests
from adalflow.core.model_client import ModelClient
from adalflow.core.types import ModelType

log = logging.getLogger(__name__)

ANTHROPIC_API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
# Beta header that unlocks >16384 output tokens on Claude Sonnet/Opus 4.x.
# Without it, max_tokens above 16384 is rejected by the API; with it, Sonnet
# 4.5 can emit up to 64k and Opus 4.1 up to 32k, which is what large-repo wiki
# pages need (the old 8192 cap truncated long pages mid-sentence).
LONG_OUTPUT_BETA = "output-128k-2025-02-19"
DEFAULT_MAX_TOKENS = 8192


class AnthropicClient(ModelClient):
    """A component wrapper for the native Anthropic Messages API."""

    def __init__(self, api_key: str = None, base_url: str = None, **kwargs) -> None:
        super().__init__()
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.base_url = (base_url or os.getenv("ANTHROPIC_BASE_URL") or ANTHROPIC_API_BASE).rstrip("/")

    def _headers(self, max_tokens: int = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if not self._api_key:
            return headers
        if self._api_key.startswith("sk-ant-api"):
            headers["x-api-key"] = self._api_key
        else:
            # Subscription OAuth token (e.g. from `claude login`)
            headers["Authorization"] = f"Bearer {self._api_key}"
            headers["anthropic-beta"] = OAUTH_BETA_HEADER
        # Requesting more than the standard 16384-token output cap requires
        # the long-output beta header on the same request. Combine it with the
        # OAuth beta when present (Anthropic accepts a comma-separated list).
        if max_tokens and int(max_tokens) > 16384:
            existing_beta = headers.get("anthropic-beta")
            headers["anthropic-beta"] = (
                f"{existing_beta},{LONG_OUTPUT_BETA}" if existing_beta else LONG_OUTPUT_BETA
            )
        return headers

    def convert_inputs_to_api_kwargs(
        self, input: Any, model_kwargs: Dict = None, model_type: ModelType = None
    ) -> Dict:
        model_kwargs = model_kwargs or {}
        if model_type != ModelType.LLM:
            raise ValueError(f"Unsupported model type for Anthropic client: {model_type}")

        if isinstance(input, str):
            messages = [{"role": "user", "content": input}]
        elif isinstance(input, list) and all(isinstance(m, dict) for m in input):
            messages = input
        else:
            raise ValueError(f"Unsupported input format for Anthropic: {type(input)}")

        api_kwargs = {**model_kwargs, "messages": messages}
        api_kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        # Streamed via a single buffered response, matching the OpenRouter client's pattern.
        api_kwargs.pop("stream", None)
        return api_kwargs

    @staticmethod
    def _extract_text(data: Dict) -> str:
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )

    async def _post_messages(self, api_kwargs: Dict) -> Dict:
        """Raw POST to /messages, shared by acall (text-only) and
        acall_with_tools (needs the full parsed response, since a tool_use
        block isn't text)."""
        if not self._api_key:
            raise RuntimeError(
                "Anthropic API key not configured. Set an API key or paste a Claude "
                "Pro/Max subscription token (from `claude login`) for the Claude provider."
            )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/messages",
                headers=self._headers(api_kwargs.get("max_tokens")),
                json=api_kwargs,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"Anthropic API error ({response.status}): {error_text}")
                return await response.json()

    async def astream_with_tools(self, *, messages: list, tools: list, model_kwargs: Dict):
        """Native tool-calling round-trip for api/agent_loop.py's
        run_native_tool_chat, streamed: takes a full messages array (system
        goes in model_kwargs["system"], not as a message, per the Messages
        API) plus a tools schema, and yields events as they arrive over SSE
        instead of buffering the whole round -- so a Claude answer streams
        live even while native tool-calling is active, matching every other
        provider (see astream() above for the equivalent without tools).

        Yields `{"type": "text", "text": str}` for each text delta (relay
        these to the user immediately), then exactly one
        `{"type": "final", "tool_calls": [{"id", "name", "input"}]}` once the
        stream ends (empty list if the model didn't call a tool this round).
        A `tool_use` block's `input` arrives as fragmented `partial_json`
        deltas and is only assembled into a real dict once its block closes,
        so tool_calls is never available before the "final" event.
        """
        api_kwargs = {**model_kwargs, "messages": messages, "stream": True}
        if tools:
            api_kwargs["tools"] = tools
        api_kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        if not self._api_key:
            raise RuntimeError(
                "Anthropic API key not configured. Set an API key or paste a Claude "
                "Pro/Max subscription token (from `claude login`) for the Claude provider."
            )

        blocks: Dict[int, Dict[str, Any]] = {}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/messages",
                headers=self._headers(api_kwargs.get("max_tokens")),
                json=api_kwargs,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"Anthropic API error ({response.status}): {error_text}")

                async for raw_line in response.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")
                    if etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            blocks[event.get("index")] = {
                                "id": block.get("id"), "name": block.get("name"), "json": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                yield {"type": "text", "text": text}
                        elif delta.get("type") == "input_json_delta":
                            block = blocks.get(event.get("index"))
                            if block is not None:
                                block["json"] += delta.get("partial_json", "")
                    elif etype == "error":
                        error = event.get("error", {})
                        raise RuntimeError(
                            f"Anthropic API stream error: {error.get('message', event)}"
                        )

        tool_calls = []
        for block in blocks.values():
            try:
                input_data = json.loads(block["json"]) if block["json"] else {}
            except json.JSONDecodeError:
                input_data = {}
            tool_calls.append({"id": block["id"], "name": block["name"], "input": input_data})
        yield {"type": "final", "tool_calls": tool_calls}

    async def astream(self, api_kwargs: Dict) -> AsyncIterator[str]:
        """True token-by-token streaming via the Messages API's own `stream:
        true` SSE mode (api.anthropic.com/v1/messages), used by
        api/provider_streaming.py so Claude answers appear progressively
        like every other provider instead of arriving as one buffered blob
        (the previous behavior via acall(), which does a single
        non-streaming POST and yields the whole answer at once).

        Anthropic's stream sends one SSE event per line pair (`event: ...`
        then `data: {...}`); the only events carrying answer text are
        `content_block_delta` with `delta.type == "text_delta"`. Other event
        types (message_start, content_block_start/stop, message_delta,
        message_stop, ping) are ignored here since none of them carry text.
        """
        if not self._api_key:
            raise RuntimeError(
                "Anthropic API key not configured. Set an API key or paste a Claude "
                "Pro/Max subscription token (from `claude login`) for the Claude provider."
            )
        api_kwargs = {**api_kwargs, "stream": True}
        api_kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/messages",
                headers=self._headers(api_kwargs.get("max_tokens")),
                json=api_kwargs,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"Anthropic API error ({response.status}): {error_text}")

                async for raw_line in response.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
                            if text:
                                yield text
                    elif event.get("type") == "error":
                        error = event.get("error", {})
                        raise RuntimeError(
                            f"Anthropic API stream error: {error.get('message', event)}"
                        )

    async def acall(self, api_kwargs: Dict = None, model_type: ModelType = None) -> Any:
        api_kwargs = api_kwargs or {}

        if not self._api_key:
            error_msg = (
                "Anthropic API key not configured. Set an API key or paste a Claude "
                "Pro/Max subscription token (from `claude login`) for the Claude provider."
            )
            log.error(error_msg)

            async def error_generator():
                yield error_msg
            return error_generator()

        headers = self._headers(api_kwargs.get("max_tokens"))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/messages",
                    headers=headers,
                    json=api_kwargs,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        log.error(f"Anthropic API error ({response.status}): {error_text}")

                        async def error_response_generator():
                            yield f"Anthropic API error ({response.status}): {error_text}"
                        return error_response_generator()

                    data = await response.json()
        except Exception as e:
            log.error(f"Error calling Anthropic API: {str(e)}")
            # Bind the message now: Python unbinds `e` when the except block
            # ends, and the generator runs after that (NameError otherwise).
            error_message = f"Error calling Anthropic API: {str(e)}"

            async def exception_generator():
                yield error_message
            return exception_generator()

        text = self._extract_text(data)

        async def content_generator():
            yield text
        return content_generator()

    def call(self, api_kwargs: Dict = None, model_type: ModelType = None) -> Any:
        api_kwargs = api_kwargs or {}
        if not self._api_key:
            raise ValueError("ANTHROPIC_API_KEY (or a subscription token) must be set")

        response = requests.post(
            f"{self.base_url}/messages",
            headers=self._headers(api_kwargs.get("max_tokens")),
            json=api_kwargs,
            timeout=300,
        )
        response.raise_for_status()
        return self._extract_text(response.json())
