import logging
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from api.agent_loop import MAX_TOOL_ROUNDS, run_agent_chat
from api.chat_models import ChatCompletionRequest, ChatMessage  # noqa: F401 (ChatMessage re-exported for callers)
from api.config import get_model_config, configs, OPENROUTER_API_KEY, OPENAI_API_KEY, LITELLM_API_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
from api.data_pipeline import count_tokens, get_file_content
from api.provider_streaming import stream_provider_response
from api.rag import RAG
from api import search_tool
from api import zim_reader
from api.prompts import (
    DEEP_RESEARCH_FIRST_ITERATION_PROMPT,
    DEEP_RESEARCH_FINAL_ITERATION_PROMPT,
    DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT,
    SIMPLE_CHAT_SYSTEM_PROMPT,
    SIMPLE_CHAT_SYSTEM_PROMPT_ZIM,
    TOOL_CALLING_INSTRUCTIONS,
)

# Configure logging
from api.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# Initialize FastAPI app
app = FastAPI(
    title="Simple Chat API",
    description="Simplified API for streaming chat completions"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

@app.post("/chat/completions/stream")
async def chat_completions_stream(request: ChatCompletionRequest):
    """Stream a chat completion response directly using Google Generative AI"""
    try:
        # Check if request contains very large input
        input_too_large = False
        if request.messages and len(request.messages) > 0:
            last_message = request.messages[-1]
            if hasattr(last_message, 'content') and last_message.content:
                tokens = count_tokens(last_message.content, request.provider == "ollama")
                logger.info(f"Request size: {tokens} tokens")
                if tokens > 8000:
                    logger.warning(f"Request exceeds recommended token limit ({tokens} > 7500)")
                    input_too_large = True

        # ZIM archives never go through RAG/prepare_retriever -- see the
        # matching branch in websocket_wiki.py for why. repo_url IS the
        # .zim file's absolute path for type == 'zim'.
        is_zim = request.type == "zim"
        request_rag = None

        if is_zim:
            try:
                zim_reader.open_archive(request.repo_url)
            except Exception as e:
                logger.error(f"Failed to open ZIM file {request.repo_url}: {e}")
                raise HTTPException(status_code=400, detail=f"Error opening ZIM archive: {str(e)}")
        else:
            # Create a new RAG instance for this request
            try:
                request_rag = RAG(
                    provider=request.provider,
                    model=request.model,
                    api_key=request.api_key,
                    api_endpoint=request.api_endpoint
                )

                # Extract custom file filter parameters if provided
                excluded_dirs = None
                excluded_files = None
                included_dirs = None
                included_files = None

                if request.excluded_dirs:
                    excluded_dirs = [unquote(dir_path) for dir_path in request.excluded_dirs.split('\n') if dir_path.strip()]
                    logger.info(f"Using custom excluded directories: {excluded_dirs}")
                if request.excluded_files:
                    excluded_files = [unquote(file_pattern) for file_pattern in request.excluded_files.split('\n') if file_pattern.strip()]
                    logger.info(f"Using custom excluded files: {excluded_files}")
                if request.included_dirs:
                    included_dirs = [unquote(dir_path) for dir_path in request.included_dirs.split('\n') if dir_path.strip()]
                    logger.info(f"Using custom included directories: {included_dirs}")
                if request.included_files:
                    included_files = [unquote(file_pattern) for file_pattern in request.included_files.split('\n') if file_pattern.strip()]
                    logger.info(f"Using custom included files: {included_files}")

                request_rag.prepare_retriever(request.repo_url, request.type, request.token, excluded_dirs, excluded_files, included_dirs, included_files)
                logger.info(f"Retriever prepared for {request.repo_url}")
            except ValueError as e:
                if "No valid documents with embeddings found" in str(e):
                    logger.error(f"No valid embeddings found: {str(e)}")
                    raise HTTPException(status_code=500, detail="No valid document embeddings found. This may be due to embedding size inconsistencies or API errors during document processing. Please try again or check your repository content.")
                else:
                    logger.error(f"ValueError preparing retriever: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"Error preparing retriever: {str(e)}")
            except Exception as e:
                logger.error(f"Error preparing retriever: {str(e)}")
                # Check for specific embedding-related errors
                if "All embeddings should be of the same size" in str(e):
                    raise HTTPException(status_code=500, detail="Inconsistent embedding sizes detected. Some documents may have failed to embed properly. Please try again.")
                else:
                    raise HTTPException(status_code=500, detail=f"Error preparing retriever: {str(e)}")

        # Validate request
        if not request.messages or len(request.messages) == 0:
            raise HTTPException(status_code=400, detail="No messages provided")

        last_message = request.messages[-1]
        if last_message.role != "user":
            raise HTTPException(status_code=400, detail="Last message must be from the user")

        # Process previous messages to build conversation history. ZIM chats
        # have no RAG/Memory instance, so their history is rendered directly
        # from request.messages in the same <turn> XML shape Memory produces
        # (see conversation_history assembly below) instead of going through
        # request_rag.memory.
        zim_conversation_history = ""
        for i in range(0, len(request.messages) - 1, 2):
            if i + 1 < len(request.messages):
                user_msg = request.messages[i]
                assistant_msg = request.messages[i + 1]

                if user_msg.role == "user" and assistant_msg.role == "assistant":
                    if is_zim:
                        zim_conversation_history += (
                            f"<turn>\n<user>{user_msg.content}</user>\n"
                            f"<assistant>{assistant_msg.content}</assistant>\n</turn>\n"
                        )
                    else:
                        request_rag.memory.add_dialog_turn(
                            user_query=user_msg.content,
                            assistant_response=assistant_msg.content
                        )

        # Check if this is a Deep Research request
        is_deep_research = False
        research_iteration = 1

        # Process messages to detect Deep Research requests
        for msg in request.messages:
            if hasattr(msg, 'content') and msg.content and "[DEEP RESEARCH]" in msg.content:
                is_deep_research = True
                # Only remove the tag from the last message
                if msg == request.messages[-1]:
                    # Remove the Deep Research tag
                    msg.content = msg.content.replace("[DEEP RESEARCH]", "").strip()

        # Count research iterations if this is a Deep Research request
        if is_deep_research:
            research_iteration = sum(1 for msg in request.messages if msg.role == 'assistant') + 1
            logger.info(f"Deep Research request detected - iteration {research_iteration}")

            # Check if this is a continuation request
            if "continue" in last_message.content.lower() and "research" in last_message.content.lower():
                # Find the original topic from the first user message
                original_topic = None
                for msg in request.messages:
                    if msg.role == "user" and "continue" not in msg.content.lower():
                        original_topic = msg.content.replace("[DEEP RESEARCH]", "").strip()
                        logger.info(f"Found original research topic: {original_topic}")
                        break

                if original_topic:
                    # Replace the continuation message with the original topic
                    last_message.content = original_topic
                    logger.info(f"Using original topic for research: {original_topic}")

        # Get the query from the last message
        query = last_message.content

        # Pages actually consulted while answering (initial context +
        # anything looked up via a SEARCH_WIKI tool call), shown as a
        # distinct footer after the answer -- see search_tool.format_sources_footer.
        collected_refs: list = []

        # Agent tool-calling (SEARCH_WIKI: <query> mid-answer, see
        # api/agent_loop.py) is opt-out via the request flag and an env var
        # killswitch, and never runs for Deep Research. Shared with
        # websocket_wiki.py so the two transports can't drift on this.
        tool_calling_enabled, search_fn = search_tool.resolve_tool_calling(
            enable_tool_calling=request.enable_tool_calling,
            is_deep_research=is_deep_research,
            is_zim=is_zim,
            zim_path=request.repo_url if is_zim else None,
            request_rag=request_rag,
            language=request.language,
            refs_sink=collected_refs,
        )

        # Only retrieve documents if input is not too large
        context_text = ""
        retrieved_documents = None

        if is_zim:
            # Scoped context: the current entry (if the chat was opened from
            # one) plus a handful of related entries via libzim's own
            # full-text index -- never the whole archive, which can hold
            # millions of entries. Falls back to searching the user's query
            # when there's no "current page" anchor.
            try:
                context_text = search_tool.build_zim_context(
                    request.repo_url, query, request.current_page_id, limit=5,
                    refs_sink=collected_refs,
                )
            except Exception as e:
                logger.error(f"Error building ZIM context: {str(e)}")
                context_text = ""
        elif not input_too_large:
            try:
                # If filePath exists, modify the query for RAG to focus on the file
                rag_query = request.retrieval_query or query
                if request.filePath:
                    # Use the file path to get relevant context about the file
                    rag_query = f"Contexts related to {request.filePath}"
                    logger.info(f"Modified RAG query to focus on file: {request.filePath}")
                elif request.current_page_id:
                    # Anchor retrieval to the wiki page the chat was opened
                    # from, so results are scoped to that page instead of the
                    # whole repo.
                    rag_query = f"Contexts related to {request.current_page_id}"
                    logger.info(f"Modified RAG query to focus on current page: {request.current_page_id}")

                # Try to perform RAG retrieval
                try:
                    # This will use the actual RAG implementation
                    retrieved_documents = request_rag(rag_query, language=request.language)

                    if retrieved_documents and retrieved_documents[0].documents:
                        # Format context for the prompt in a more structured way
                        documents = retrieved_documents[0].documents
                        logger.info(f"Retrieved {len(documents)} documents")

                        # Group documents by file path
                        docs_by_file = {}
                        for doc in documents:
                            file_path = doc.meta_data.get('file_path', 'unknown')
                            if file_path not in docs_by_file:
                                docs_by_file[file_path] = []
                            docs_by_file[file_path].append(doc)

                        collected_refs.extend(
                            {"title": file_path, "ref": file_path} for file_path in docs_by_file
                        )

                        # Format context text with file path grouping
                        context_parts = []
                        for file_path, docs in docs_by_file.items():
                            # Add file header with metadata
                            header = f"## File Path: {file_path}\n\n"
                            # Add document content
                            content = "\n\n".join([doc.text for doc in docs])

                            context_parts.append(f"{header}{content}")

                        # Join all parts with clear separation
                        context_text = "\n\n" + "-" * 10 + "\n\n".join(context_parts)
                    else:
                        logger.warning("No documents retrieved from RAG")
                except Exception as e:
                    logger.error(f"Error in RAG retrieval: {str(e)}")
                    # Continue without RAG if there's an error

            except Exception as e:
                logger.error(f"Error retrieving documents: {str(e)}")
                context_text = ""

        # Get repository information
        repo_url = request.repo_url
        repo_name = repo_url.split("/")[-1] if "/" in repo_url else repo_url

        # Determine repository type
        repo_type = request.type

        # Wording used in the system prompt's <role> line below: a .zim is an
        # offline wiki archive, not a git code repository -- keeping the
        # "{repo_type} repository" framing for it would confuse the model
        # into looking for source files that don't exist.
        subject = "offline wiki archive" if is_zim else f"{repo_type} repository"

        # Get language information
        language_code = request.language or configs["lang_config"]["default"]
        supported_langs = configs["lang_config"]["supported_languages"]
        language_name = supported_langs.get(language_code, "English")

        # Create system prompt
        if is_deep_research:
            # Check if this is the first iteration
            is_first_iteration = research_iteration == 1

            # Check if this is the final iteration
            is_final_iteration = research_iteration >= 5

            if is_first_iteration:
                system_prompt = DEEP_RESEARCH_FIRST_ITERATION_PROMPT.format(
                    subject=subject,
                    repo_url=repo_url,
                    repo_name=repo_name,
                    language_name=language_name
                )
            elif is_final_iteration:
                system_prompt = DEEP_RESEARCH_FINAL_ITERATION_PROMPT.format(
                    subject=subject,
                    repo_url=repo_url,
                    repo_name=repo_name,
                    research_iteration=research_iteration,
                    language_name=language_name
                )
            else:
                system_prompt = DEEP_RESEARCH_INTERMEDIATE_ITERATION_PROMPT.format(
                    subject=subject,
                    repo_url=repo_url,
                    repo_name=repo_name,
                    research_iteration=research_iteration,
                    language_name=language_name
                )
        else:
            template = SIMPLE_CHAT_SYSTEM_PROMPT_ZIM if is_zim else SIMPLE_CHAT_SYSTEM_PROMPT
            system_prompt = template.format(
                subject=subject,
                repo_url=repo_url,
                repo_name=repo_name,
                language_name=language_name
            )

        # Fetch file content if provided
        file_content = ""
        if request.filePath:
            try:
                file_content = get_file_content(request.repo_url, request.filePath, request.type, request.token)
                logger.info(f"Successfully retrieved content for file: {request.filePath}")
            except Exception as e:
                logger.error(f"Error retrieving file content: {str(e)}")
                # Continue without file content if there's an error

        # Format conversation history (ZIM chats have no RAG/Memory instance;
        # their history was already rendered into zim_conversation_history
        # above, in the same <turn> shape Memory produces).
        if is_zim:
            conversation_history = zim_conversation_history
        else:
            conversation_history = ""
            for turn_id, turn in request_rag.memory().items():
                if not isinstance(turn_id, int) and hasattr(turn, 'user_query') and hasattr(turn, 'assistant_response'):
                    conversation_history += f"<turn>\n<user>{turn.user_query.query_str}</user>\n<assistant>{turn.assistant_response.response_str}</assistant>\n</turn>\n"

        # Create the prompt with context
        prompt = f"/no_think {system_prompt}\n\n"

        if conversation_history:
            prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"

        # Check if filePath is provided and fetch file content if it exists
        if file_content:
            # Add file content to the prompt after conversation history
            prompt += f"<currentFileContent path=\"{request.filePath}\">\n{file_content}\n</currentFileContent>\n\n"

        # Only include context if it's not empty
        CONTEXT_START = "<START_OF_CONTEXT>"
        CONTEXT_END = "<END_OF_CONTEXT>"
        if context_text.strip():
            prompt += f"{CONTEXT_START}\n{context_text}\n{CONTEXT_END}\n\n"
        else:
            # Add a note that we're skipping RAG due to size constraints or because it's the isolated API
            logger.info("No context available from RAG")
            prompt += "<note>Answering without retrieval augmentation.</note>\n\n"

        if tool_calling_enabled:
            prompt += TOOL_CALLING_INSTRUCTIONS.format(
                subject="ZIM archive" if is_zim else "repository",
                max_rounds=MAX_TOOL_ROUNDS,
            ) + "\n\n"

        prompt += f"<query>\n{query}\n</query>\n\nAssistant: "

        model_config = get_model_config(request.provider, request.model)["model_kwargs"]

        # Warn-only checks for missing API keys, preserved from the original
        # per-branch dispatch: the request still proceeds and the provider
        # client itself returns a friendly error if the key is truly required.
        if request.provider == "openrouter" and not OPENROUTER_API_KEY:
            logger.warning("OPENROUTER_API_KEY not configured, but continuing with request")
        elif request.provider in ("openai", "openai_custom") and not OPENAI_API_KEY and not request.api_key:
            logger.warning("API key not configured, but continuing with request")
        elif request.provider == "claude" and not request.api_key:
            logger.warning("Anthropic API key/subscription token not configured, but continuing with request")
        elif request.provider == "litellm" and not LITELLM_API_KEY and not request.api_key:
            logger.warning("API key not configured, but continuing with request")
        elif request.provider == "bedrock" and (not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY):
            logger.warning("AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY not configured, but continuing with request")

        # Create a streaming response. When tool calling is enabled,
        # run_agent_chat wraps stream_provider_response with the SEARCH_WIKI
        # sniff-and-relay loop (api/agent_loop.py); otherwise this is the
        # original single-shot stream.
        async def response_stream():
            try:
                if tool_calling_enabled:
                    stream = run_agent_chat(
                        provider=request.provider,
                        requested_model=request.model,
                        prompt=prompt,
                        model_config_kwargs=model_config,
                        api_key=request.api_key,
                        api_endpoint=request.api_endpoint,
                        search_fn=search_fn,
                    )
                else:
                    stream = stream_provider_response(
                        provider=request.provider,
                        requested_model=request.model,
                        prompt=prompt,
                        model_config_kwargs=model_config,
                        api_key=request.api_key,
                        api_endpoint=request.api_endpoint,
                    )
                async for text in stream:
                    yield text
                footer = search_tool.format_sources_footer(
                    collected_refs, is_zim, request.repo_url if is_zim else None
                )
                if footer:
                    yield footer

            except Exception as e_outer:
                logger.error(f"Error in streaming response: {str(e_outer)}")
                error_message = str(e_outer)

                # Check for token limit errors
                if "maximum context length" in error_message or "token limit" in error_message or "too many tokens" in error_message:
                    # If we hit a token limit error, try again without context
                    logger.warning("Token limit exceeded, retrying without context")
                    try:
                        # Create a simplified prompt without context
                        simplified_prompt = f"/no_think {system_prompt}\n\n"
                        if conversation_history:
                            simplified_prompt += f"<conversation_history>\n{conversation_history}</conversation_history>\n\n"

                        # Include file content in the fallback prompt if it was retrieved
                        if request.filePath and file_content:
                            simplified_prompt += f"<currentFileContent path=\"{request.filePath}\">\n{file_content}\n</currentFileContent>\n\n"

                        simplified_prompt += "<note>Answering without retrieval augmentation due to input size constraints.</note>\n\n"
                        simplified_prompt += f"<query>\n{query}\n</query>\n\nAssistant: "

                        if request.provider == "ollama":
                            simplified_prompt += " /no_think"

                        # Google's original fallback branch recomputed model_config
                        # from scratch instead of reusing the outer one; every other
                        # provider reused the outer `model_config`. stream_provider_response
                        # takes the same dict regardless of provider, so recompute
                        # unconditionally here (cheap, side-effect-free) to match Google's
                        # behavior and keep the rest identical for the other providers.
                        fallback_model_config = get_model_config(request.provider, request.model)["model_kwargs"]
                        async for text in stream_provider_response(
                            provider=request.provider,
                            requested_model=request.model,
                            prompt=simplified_prompt,
                            model_config_kwargs=fallback_model_config,
                            api_key=request.api_key,
                            api_endpoint=request.api_endpoint,
                        ):
                            yield text
                    except Exception as e2:
                        logger.error(f"Error in fallback streaming response: {str(e2)}")
                        yield f"\nI apologize, but your request is too large for me to process. Please try a shorter query or break it into smaller parts."
                else:
                    # For other errors, return the error message
                    yield f"\nError: {error_message}"

        # Return streaming response
        return StreamingResponse(response_stream(), media_type="text/event-stream")

    except HTTPException:
        raise
    except Exception as e_handler:
        error_msg = f"Error in streaming chat completion: {str(e_handler)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/")
async def root():
    """Root endpoint to check if the API is running"""
    return {"status": "API is running", "message": "Navigate to /docs for API documentation"}
