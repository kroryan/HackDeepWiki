"""
Uniform text-chunk streaming across every LLM provider client.

This centralizes the per-provider dispatch that used to be duplicated in both
api/websocket_wiki.py and api/simple_chat.py (~8 branches x 2 files). It exists
so anything that needs to observe/intercept the raw text stream (e.g. the
tool-calling agent loop) has ONE place to wrap instead of 16.

Behavior-preserving extraction: the text-extraction logic per provider is
moved verbatim, not rewritten. In particular it preserves a subtle asymmetry
that already existed in the original code:
  - openrouter, openai/openai_custom, claude, litellm, bedrock, azure,
    dashscope: each catches its OWN errors internally and yields a friendly,
    provider-specific troubleshooting message instead of raising. Callers
    never see an exception for these -- this mirrors the original per-branch
    try/except that sent an error message and closed, without ever reaching
    the outer token-limit-retry-without-context fallback.
  - ollama, google (default): do NOT catch their own errors -- exceptions
    propagate to the caller, exactly as before, so the caller's outer
    except-based "retry without RAG context on token-limit errors" fallback
    keeps working ONLY for these two providers, unchanged.
"""
import logging
from typing import AsyncIterator, Optional

import google.generativeai as genai
from adalflow.components.model_client.ollama_client import OllamaClient
from adalflow.core.types import ModelType

from api.anthropic_client import AnthropicClient
from api.azureai_client import AzureAIClient
from api.bedrock_client import BedrockClient
from api.dashscope_client import DashscopeClient
from api.litellm_client import LiteLLMClient
from api.openai_client import OpenAIClient
from api.openrouter_client import OpenRouterClient

logger = logging.getLogger(__name__)


async def stream_provider_response(
    provider: str,
    requested_model: Optional[str],
    prompt: str,
    model_config_kwargs: dict,
    api_key: Optional[str] = None,
    api_endpoint: Optional[str] = None,
) -> AsyncIterator[str]:
    """Yield text chunks from the given provider for the given prompt.

    `model_config_kwargs` is the `model_kwargs` dict already resolved via
    `get_model_config(provider, requested_model)["model_kwargs"]` by the
    caller (kept as a caller responsibility since it does not vary by
    request-shape between websocket_wiki.py / simple_chat.py).
    """
    if provider == "ollama":
        # This branch used to unconditionally append a trailing "/no_think"
        # to the prompt -- a Qwen3-specific convention for disabling that
        # model family's reasoning mode for one turn. Ollama serves
        # arbitrary models under arbitrary names (impossible to enumerate
        # or pattern-match reliably), and appending text one specific
        # model family understands as a command to every other model is
        # exactly the kind of per-model special-casing that breaks
        # unrelated ones: confirmed live with an NVIDIA nemotron-3-super
        # cloud model, which produced only "thinking" output and zero
        # actual content with the suffix present, every time, and a normal
        # complete answer with it removed. The leading "/no_think
        # {system_prompt}" prefix already added by every caller (see
        # websocket_wiki.py / simple_chat.py) applies uniformly to every
        # provider including this one, so nothing model-specific is lost
        # by dropping the extra suffix here.
        model = OllamaClient()
        model_kwargs = {
            "model": model_config_kwargs.get("model", requested_model),
            "stream": True,
            "options": {
                "temperature": model_config_kwargs.get("temperature", 0.7),
                "top_p": model_config_kwargs.get("top_p", 0.8),
                "num_ctx": model_config_kwargs.get("num_ctx", 32000),
            },
        }
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        # No internal try/except: errors propagate to the caller's outer
        # token-limit-retry fallback, matching original behavior.
        response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
        got_content = False
        thinking_parts: list[str] = []
        async for chunk in response:
            text = None
            thinking = None
            if isinstance(chunk, dict):
                message = chunk.get("message")
                if isinstance(message, dict):
                    text = message.get("content")
                    thinking = message.get("thinking")
                else:
                    text = message
            else:
                message = getattr(chunk, "message", None)
                if message is not None:
                    if isinstance(message, dict):
                        text = message.get("content")
                        thinking = message.get("thinking")
                    else:
                        text = getattr(message, "content", None)
                        thinking = getattr(message, "thinking", None)

            if not text:
                text = getattr(chunk, "response", None) or getattr(chunk, "text", None)

            if not text and hasattr(chunk, "__dict__"):
                message = chunk.__dict__.get("message")
                if isinstance(message, dict):
                    text = message.get("content")

            if isinstance(thinking, str) and thinking:
                thinking_parts.append(thinking)

            if (
                isinstance(text, str)
                and text
                and not text.startswith("model=")
                and not text.startswith("created_at=")
            ):
                got_content = True
                clean_text = text.replace("<think>", "").replace("</think>", "")
                yield clean_text

        if not got_content and thinking_parts:
            # Reasoning-capable models (seen with an NVIDIA nemotron-3-super
            # cloud model, but this is a general shape any Ollama "thinking"
            # model can produce, not something to special-case by name) can
            # spend an entire turn's budget on internal reasoning and never
            # emit a final `content` message -- silently returning nothing
            # would be worse than surfacing what the model actually reasoned
            # through, so fall back to that instead of a blank response.
            logger.warning(
                "Ollama model produced only reasoning/thinking output, no final "
                "content -- falling back to the reasoning text"
            )
            yield "".join(thinking_parts)
        return

    if provider == "openrouter":
        logger.info(f"Using OpenRouter with model: {requested_model}")
        model = OpenRouterClient()
        model_kwargs = {
            "model": requested_model,
            "stream": True,
            "temperature": model_config_kwargs.get("temperature", 0.7),
        }
        if "top_p" in model_config_kwargs:
            model_kwargs["top_p"] = model_config_kwargs.get("top_p", 0.8)
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making OpenRouter API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            async for chunk in response:
                yield chunk
        except Exception as e_openrouter:
            logger.error(f"Error with OpenRouter API: {str(e_openrouter)}")
            yield (
                f"\nError with OpenRouter API: {str(e_openrouter)}\n\n"
                "Please check that you have set the OPENROUTER_API_KEY environment "
                "variable with a valid API key."
            )
        return

    if provider in ("openai", "openai_custom"):
        logger.info(f"Using Openai protocol: provider={provider!r} model={requested_model!r}")
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if api_endpoint:
            client_kwargs["base_url"] = api_endpoint

        model = OpenAIClient(**client_kwargs)
        model_kwargs = {
            "model": requested_model,
            "stream": True,
            "temperature": model_config_kwargs.get("temperature", 0.7),
        }
        if "top_p" in model_config_kwargs:
            model_kwargs["top_p"] = model_config_kwargs.get("top_p", 0.8)
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making Openai API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            async for chunk in response:
                choices = getattr(chunk, "choices", [])
                if len(choices) > 0:
                    delta = getattr(choices[0], "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                        if text is not None:
                            yield text
        except Exception as e_openai:
            logger.error(f"Error with Openai API: {str(e_openai)}")
            yield (
                f"\nError with Openai API: {str(e_openai)}"
                f"\n[endpoint={api_endpoint or '(default https://api.openai.com/v1)'}"
                f" model={requested_model!r}]\n"
                "Please check your provider settings (API endpoint URL, API key, and selected model).\n"
            )
        return

    if provider == "claude":
        logger.info(f"Using native Anthropic API with model: {requested_model}")
        model = AnthropicClient(api_key=api_key, base_url=api_endpoint)
        model_kwargs = {
            "model": requested_model,
            "temperature": model_config_kwargs.get("temperature", 0.7),
            "max_tokens": 8192,
        }
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making Anthropic API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            async for chunk in response:
                if chunk:
                    yield chunk
        except Exception as e_claude:
            logger.error(f"Error with Anthropic API: {str(e_claude)}")
            yield (
                f"\nError with Anthropic API: {str(e_claude)}\n\n"
                "Please check that you have set a valid API key or subscription token for Claude."
            )
        return

    if provider == "litellm":
        logger.info(f"Using Openai protocol with model on LiteLLM for provider: {provider}")
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key

        model = LiteLLMClient(**client_kwargs)
        model_kwargs = {
            "model": requested_model,
            "stream": True,
            "temperature": model_config_kwargs.get("temperature", 0.7),
        }
        if "top_p" in model_config_kwargs:
            model_kwargs["top_p"] = model_config_kwargs.get("top_p", 0.8)
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making LiteLLM API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            async for chunk in response:
                choices = getattr(chunk, "choices", [])
                if len(choices) > 0:
                    delta = getattr(choices[0], "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                        if text is not None:
                            yield text
        except Exception as e_litellm:
            logger.error(f"Error with LiteLLM API: {str(e_litellm)}")
            yield (
                f"\nError with LiteLLM API: {str(e_litellm)}\n\n"
                "Please check that you have set the LITELLM_API_KEY environment "
                "variable with a valid API key."
            )
        return

    if provider == "bedrock":
        logger.info(f"Using AWS Bedrock with model: {requested_model}")
        model = BedrockClient()
        model_kwargs = {"model": requested_model}
        for key in ["temperature", "top_p"]:
            if key in model_config_kwargs:
                model_kwargs[key] = model_config_kwargs[key]
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making AWS Bedrock API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            if isinstance(response, str):
                yield response
            else:
                yield str(response)
        except Exception as e_bedrock:
            logger.error(f"Error with AWS Bedrock API: {str(e_bedrock)}")
            yield (
                f"\nError with AWS Bedrock API: {str(e_bedrock)}\n\n"
                "Please check that you have set the AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "environment variables with valid credentials."
            )
        return

    if provider == "azure":
        logger.info(f"Using Azure AI with model: {requested_model}")
        model = AzureAIClient()
        model_kwargs = {
            "model": requested_model,
            "stream": True,
            "temperature": model_config_kwargs.get("temperature", 0.7),
            "top_p": model_config_kwargs.get("top_p", 0.8),
        }
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making Azure AI API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            async for chunk in response:
                choices = getattr(chunk, "choices", [])
                if len(choices) > 0:
                    delta = getattr(choices[0], "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                        if text is not None:
                            yield text
        except Exception as e_azure:
            logger.error(f"Error with Azure AI API: {str(e_azure)}")
            yield (
                f"\nError with Azure AI API: {str(e_azure)}\n\n"
                "Please check that you have set the AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, "
                "and AZURE_OPENAI_VERSION environment variables with valid values."
            )
        return

    if provider == "dashscope":
        logger.info(f"Using Dashscope with model: {requested_model}")
        model = DashscopeClient()
        model_kwargs = {
            "model": requested_model,
            "stream": True,
            "temperature": model_config_kwargs.get("temperature", 0.7),
            "top_p": model_config_kwargs.get("top_p", 0.8),
        }
        api_kwargs = model.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM
        )
        try:
            logger.info("Making Dashscope API call")
            response = await model.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
            # DashscopeClient.acall with stream=True returns an async generator
            # of plain text chunks.
            async for text in response:
                if text:
                    yield text
        except Exception as e_dashscope:
            logger.error(f"Error with Dashscope API: {str(e_dashscope)}")
            yield (
                f"\nError with Dashscope API: {str(e_dashscope)}\n\n"
                "Please check that you have set the DASHSCOPE_API_KEY (and optionally "
                "DASHSCOPE_WORKSPACE_ID) environment variables with valid values."
            )
        return

    # Google Generative AI (default provider). No internal try/except: errors
    # propagate to the caller's outer token-limit-retry fallback, matching
    # original behavior (same category as ollama above).
    if api_key:
        genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name=model_config_kwargs.get("model", "gemini-2.5-flash"),
        generation_config={
            "temperature": model_config_kwargs.get("temperature", 0.7),
            "top_p": model_config_kwargs.get("top_p", 0.8),
            "top_k": model_config_kwargs.get("top_k", 40),
        },
    )
    response = model.generate_content(prompt, stream=True)
    for chunk in response:
        if hasattr(chunk, "text"):
            yield chunk.text
