import logging
import weakref
from dataclasses import dataclass
from typing import Dict, List
from uuid import uuid4

import adalflow as adal

from api.tools.embedder import get_embedder

# RAG_SYSTEM_PROMPT / RAG_TEMPLATE are no longer imported here: the
# adal.Generator that used them was dead code (chat generation goes through
# api.provider_streaming, not this component) and has been removed.

# How many extra candidates to fetch from FAISS beyond the configured
# retriever.top_k, so _diversify_doc_indices has room to swap same-file
# near-duplicates for coverage of more distinct source files.
RETRIEVER_OVERFETCH_MULTIPLIER = 4
# Max chunks any single source file may contribute to the final selection
# before its remaining candidates are deprioritized in favor of other files.
MAX_CHUNKS_PER_SOURCE_FILE = 3

# Create our own implementation of the conversation classes
@dataclass
class UserQuery:
    query_str: str

@dataclass
class AssistantResponse:
    response_str: str

@dataclass
class DialogTurn:
    id: str
    user_query: UserQuery
    assistant_response: AssistantResponse

class CustomConversation:
    """Custom implementation of Conversation to fix the list assignment index out of range error"""

    def __init__(self):
        self.dialog_turns = []

    def append_dialog_turn(self, dialog_turn):
        """Safely append a dialog turn to the conversation"""
        if not hasattr(self, 'dialog_turns'):
            self.dialog_turns = []
        self.dialog_turns.append(dialog_turn)

# Import other adalflow components
from adalflow.components.retriever.faiss_retriever import FAISSRetriever

from api.config import configs
from api.data_pipeline import DatabaseManager

# Configure logging
logger = logging.getLogger(__name__)

class Memory(adal.core.component.DataComponent):
    """Simple conversation management with a list of dialog turns."""

    def __init__(self):
        super().__init__()
        # Use our custom implementation instead of the original Conversation class
        self.current_conversation = CustomConversation()

    def call(self) -> Dict:
        """Return the conversation history as a dictionary."""
        all_dialog_turns = {}
        try:
            # Check if dialog_turns exists and is a list
            if hasattr(self.current_conversation, 'dialog_turns'):
                if self.current_conversation.dialog_turns:
                    logger.info(f"Memory content: {len(self.current_conversation.dialog_turns)} turns")
                    for i, turn in enumerate(self.current_conversation.dialog_turns):
                        if hasattr(turn, 'id') and turn.id is not None:
                            all_dialog_turns[turn.id] = turn
                            logger.info(f"Added turn {i+1} with ID {turn.id} to memory")
                        else:
                            logger.warning(f"Skipping invalid turn object in memory: {turn}")
                else:
                    logger.info("Dialog turns list exists but is empty")
            else:
                logger.info("No dialog_turns attribute in current_conversation")
                # Try to initialize it
                self.current_conversation.dialog_turns = []
        except Exception as e:
            logger.error(f"Error accessing dialog turns: {str(e)}")
            # Try to recover
            try:
                self.current_conversation = CustomConversation()
                logger.info("Recovered by creating new conversation")
            except Exception as e2:
                logger.error(f"Failed to recover: {str(e2)}")

        logger.info(f"Returning {len(all_dialog_turns)} dialog turns from memory")
        return all_dialog_turns

    def add_dialog_turn(self, user_query: str, assistant_response: str) -> bool:
        """
        Add a dialog turn to the conversation history.

        Args:
            user_query: The user's query
            assistant_response: The assistant's response

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create a new dialog turn using our custom implementation
            dialog_turn = DialogTurn(
                id=str(uuid4()),
                user_query=UserQuery(query_str=user_query),
                assistant_response=AssistantResponse(response_str=assistant_response),
            )

            # Make sure the current_conversation has the append_dialog_turn method
            if not hasattr(self.current_conversation, 'append_dialog_turn'):
                logger.warning("current_conversation does not have append_dialog_turn method, creating new one")
                # Initialize a new conversation if needed
                self.current_conversation = CustomConversation()

            # Ensure dialog_turns exists
            if not hasattr(self.current_conversation, 'dialog_turns'):
                logger.warning("dialog_turns not found, initializing empty list")
                self.current_conversation.dialog_turns = []

            # Safely append the dialog turn
            self.current_conversation.dialog_turns.append(dialog_turn)
            logger.info(f"Successfully added dialog turn, now have {len(self.current_conversation.dialog_turns)} turns")
            return True

        except Exception as e:
            logger.error(f"Error adding dialog turn: {str(e)}")
            # Try to recover by creating a new conversation
            try:
                self.current_conversation = CustomConversation()
                dialog_turn = DialogTurn(
                    id=str(uuid4()),
                    user_query=UserQuery(query_str=user_query),
                    assistant_response=AssistantResponse(response_str=assistant_response),
                )
                self.current_conversation.dialog_turns.append(dialog_turn)
                logger.info("Recovered from error by creating new conversation")
                return True
            except Exception as e2:
                logger.error(f"Failed to recover from error: {str(e2)}")
                return False


from dataclasses import dataclass, field


@dataclass
class RAGAnswer(adal.DataClass):
    rationale: str = field(default="", metadata={"desc": "Chain of thoughts for the answer."})
    answer: str = field(default="", metadata={"desc": "Answer to the user query, formatted in markdown for beautiful rendering with react-markdown. DO NOT include ``` triple backticks fences at the beginning or end of your answer."})

    __output_fields__ = ["rationale", "answer"]

class RAG(adal.Component):
    """RAG with one repo.
    If you want to load a new repos, call prepare_retriever(repo_url_or_path) first."""

    def __init__(self, provider="google", model=None, use_s3: bool = False, api_key: str = None, api_endpoint: str = None):
        """
        Initialize the RAG component.

        Args:
            provider: Model provider to use (google, openai, openrouter, ollama)
            model: Model name to use with the provider
            use_s3: Whether to use S3 for database storage (default: False)
            api_key: Optional API key for custom providers
            api_endpoint: Optional API endpoint for custom providers
        """
        super().__init__()

        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.api_endpoint = api_endpoint

        # Import the helper functions
        from api.config import get_embedder_config, get_embedder_type

        # Determine embedder type based on current configuration
        self.embedder_type = get_embedder_type()
        self.is_ollama_embedder = (self.embedder_type == 'ollama')  # Backward compatibility

        # Check if Ollama model exists before proceeding
        if self.is_ollama_embedder:
            from api.config import get_embedder_config
            from api.ollama_patch import check_ollama_model_exists
            
            embedder_config = get_embedder_config()
            if embedder_config and embedder_config.get("model_kwargs", {}).get("model"):
                model_name = embedder_config["model_kwargs"]["model"]
                if not check_ollama_model_exists(model_name):
                    raise Exception(f"Ollama model '{model_name}' not found. Please run 'ollama pull {model_name}' to install it.")

        # Initialize components
        self.memory = Memory()
        self.embedder = get_embedder(embedder_type=self.embedder_type)

        self_weakref = weakref.ref(self)
        # Patch: ensure query embedding is always single string for Ollama
        def single_string_embedder(query):
            # Accepts either a string or a list, always returns embedding for a single string
            if isinstance(query, list):
                if len(query) != 1:
                    raise ValueError("Ollama embedder only supports a single string")
                query = query[0]
            instance = self_weakref()
            assert instance is not None, "RAG instance is no longer available, but the query embedder was called."
            from api.ollama_patch import prepare_ollama_embedding_query
            return instance.embedder(
                input=prepare_ollama_embedding_query(query)
            )

        # Use single string embedder for Ollama, regular embedder for others
        self.query_embedder = single_string_embedder if self.is_ollama_embedder else self.embedder

        self.initialize_db_manager()

        # NOTE: an adal.Generator (RAG_SYSTEM_PROMPT + RAG_TEMPLATE + RAGAnswer
        # output parser) used to be constructed here and stored as
        # self.generator, but it was NEVER called -- chat generation goes
        # through api.provider_streaming with prompts assembled in
        # websocket_wiki.py/simple_chat.py, not through this component. The
        # dead generator (and the RAG_TEMPLATE/RAGAnswer/format_instructions
        # machinery below) was removed because keeping it was misleading
        # (the carefully-crafted RAG_SYSTEM_PROMPT was never sent to any
        # model) and it eagerly constructed a model client + sqlite cache on
        # every chat connection for nothing. RAG is purely a retriever now.


    def initialize_db_manager(self):
        """Initialize the database manager with local storage"""
        self.db_manager = DatabaseManager()
        self.transformed_docs = []

    def _validate_and_filter_embeddings(self, documents: List) -> List:
        """
        Validate embeddings and filter out documents with invalid or mismatched embedding sizes.

        Args:
            documents: List of documents with embeddings

        Returns:
            List of documents with valid embeddings of consistent size
        """
        if not documents:
            logger.warning("No documents provided for embedding validation")
            return []

        valid_documents = []
        embedding_sizes = {}

        # First pass: collect all embedding sizes and count occurrences
        for i, doc in enumerate(documents):
            if not hasattr(doc, 'vector') or doc.vector is None:
                logger.warning(f"Document {i} has no embedding vector, skipping")
                continue

            try:
                if isinstance(doc.vector, list):
                    embedding_size = len(doc.vector)
                elif hasattr(doc.vector, 'shape'):
                    embedding_size = doc.vector.shape[0] if len(doc.vector.shape) == 1 else doc.vector.shape[-1]
                elif hasattr(doc.vector, '__len__'):
                    embedding_size = len(doc.vector)
                else:
                    logger.warning(f"Document {i} has invalid embedding vector type: {type(doc.vector)}, skipping")
                    continue

                if embedding_size == 0:
                    logger.warning(f"Document {i} has empty embedding vector, skipping")
                    continue

                embedding_sizes[embedding_size] = embedding_sizes.get(embedding_size, 0) + 1

            except Exception as e:
                logger.warning(f"Error checking embedding size for document {i}: {str(e)}, skipping")
                continue

        if not embedding_sizes:
            logger.error("No valid embeddings found in any documents")
            return []

        # Find the most common embedding size (this should be the correct one)
        target_size = max(embedding_sizes.keys(), key=lambda k: embedding_sizes[k])
        logger.info(f"Target embedding size: {target_size} (found in {embedding_sizes[target_size]} documents)")

        # Log all embedding sizes found
        for size, count in embedding_sizes.items():
            if size != target_size:
                logger.warning(f"Found {count} documents with incorrect embedding size {size}, will be filtered out")

        # Second pass: filter documents with the target embedding size
        for i, doc in enumerate(documents):
            if not hasattr(doc, 'vector') or doc.vector is None:
                continue

            try:
                if isinstance(doc.vector, list):
                    embedding_size = len(doc.vector)
                elif hasattr(doc.vector, 'shape'):
                    embedding_size = doc.vector.shape[0] if len(doc.vector.shape) == 1 else doc.vector.shape[-1]
                elif hasattr(doc.vector, '__len__'):
                    embedding_size = len(doc.vector)
                else:
                    continue

                if embedding_size == target_size:
                    valid_documents.append(doc)
                else:
                    # Log which document is being filtered out
                    file_path = getattr(doc, 'meta_data', {}).get('file_path', f'document_{i}')
                    logger.warning(f"Filtering out document '{file_path}' due to embedding size mismatch: {embedding_size} != {target_size}")

            except Exception as e:
                file_path = getattr(doc, 'meta_data', {}).get('file_path', f'document_{i}')
                logger.warning(f"Error validating embedding for document '{file_path}': {str(e)}, skipping")
                continue

        logger.info(f"Embedding validation complete: {len(valid_documents)}/{len(documents)} documents have valid embeddings")

        if len(valid_documents) == 0:
            logger.error("No documents with valid embeddings remain after filtering")
        elif len(valid_documents) < len(documents):
            filtered_count = len(documents) - len(valid_documents)
            logger.warning(f"Filtered out {filtered_count} documents due to embedding issues")

        return valid_documents

    def prepare_retriever(self, repo_url_or_path: str, type: str = "github", access_token: str = None,
                      excluded_dirs: List[str] = None, excluded_files: List[str] = None,
                      included_dirs: List[str] = None, included_files: List[str] = None,
                      force: bool = False):
        """
        Prepare the retriever for a repository.
        Will load database from local storage if available.

        Args:
            repo_url_or_path: URL or local path to the repository
            access_token: Optional access token for private repositories
            excluded_dirs: Optional list of directories to exclude from processing
            excluded_files: Optional list of file patterns to exclude from processing
            included_dirs: Optional list of directories to include exclusively
            included_files: Optional list of file patterns to include exclusively
            force: "Refresh Wiki" semantics -- re-clone git-hosted repos and rebuild
                the embeddings index from scratch instead of trusting whatever's
                cached on disk. See DatabaseManager.prepare_database.
        """
        self.initialize_db_manager()
        self.repo_url_or_path = repo_url_or_path
        self.transformed_docs = self.db_manager.prepare_database(
            repo_url_or_path,
            type,
            access_token,
            embedder_type=self.embedder_type,
            excluded_dirs=excluded_dirs,
            excluded_files=excluded_files,
            included_dirs=included_dirs,
            included_files=included_files,
            force=force
        )
        logger.info(f"Loaded {len(self.transformed_docs)} documents for retrieval")

        # Validate and filter embeddings to ensure consistent sizes
        self.transformed_docs = self._validate_and_filter_embeddings(self.transformed_docs)

        if not self.transformed_docs:
            raise ValueError("No valid documents with embeddings found. Cannot create retriever.")

        logger.info(f"Using {len(self.transformed_docs)} documents with valid embeddings for retrieval")

        try:
            # Use the appropriate embedder for retrieval
            retrieve_embedder = self.query_embedder if self.is_ollama_embedder else self.embedder
            retriever_config = dict(configs["retriever"])
            # The number of chunks actually fed to the page-generation
            # prompt, per configs["retriever"]["top_k"] (default 20).
            self.final_top_k = int(retriever_config.get("top_k", 20))
            # Ask FAISS for more candidates than final_top_k so call() can
            # trade a few near-duplicate top hits for cross-file coverage
            # (see _diversify_doc_indices) -- naive top-k-by-raw-similarity
            # otherwise lets a handful of "distinctive" files anywhere in the
            # repo permanently crowd out a whole directory of many smaller,
            # textually-similar files (docs, generated code, data fixtures,
            # etc.), whose content then never reaches the LLM even though its
            # paths appear fine in the repo structure.
            overfetch_k = max(
                self.final_top_k,
                min(len(self.transformed_docs), self.final_top_k * RETRIEVER_OVERFETCH_MULTIPLIER),
            )
            retriever_config["top_k"] = overfetch_k
            self.retriever = FAISSRetriever(
                **retriever_config,
                embedder=retrieve_embedder,
                documents=self.transformed_docs,
                document_map_func=lambda doc: doc.vector,
            )
            logger.info("FAISS retriever created successfully")
        except Exception as e:
            logger.error(f"Error creating FAISS retriever: {str(e)}")
            # Try to provide more specific error information
            if "All embeddings should be of the same size" in str(e):
                logger.error("Embedding size validation failed. This suggests there are still inconsistent embedding sizes.")
                # Log embedding sizes for debugging
                sizes = []
                for i, doc in enumerate(self.transformed_docs[:10]):  # Check first 10 docs
                    if hasattr(doc, 'vector') and doc.vector is not None:
                        try:
                            if isinstance(doc.vector, list):
                                size = len(doc.vector)
                            elif hasattr(doc.vector, 'shape'):
                                size = doc.vector.shape[0] if len(doc.vector.shape) == 1 else doc.vector.shape[-1]
                            elif hasattr(doc.vector, '__len__'):
                                size = len(doc.vector)
                            else:
                                size = "unknown"
                            sizes.append(f"doc_{i}: {size}")
                        except Exception:
                            sizes.append(f"doc_{i}: error")
                logger.error(f"Sample embedding sizes: {', '.join(sizes)}")
            raise

    def _diversify_doc_indices(self, doc_indices: List[int]) -> List[int]:
        """Re-rank an over-fetched, similarity-ordered candidate list so the
        final self.final_top_k selection isn't dominated by (or entirely
        missing) any one source file.

        Walks the ranked candidates in order, taking each one unless its
        source file has already contributed MAX_CHUNKS_PER_SOURCE_FILE
        chunks; once final_top_k slots are filled this way, if the cap left
        us short (not enough distinct files existed among the candidates),
        backfill with the next-best candidates regardless of the cap so this
        never returns fewer chunks than configured -- it only ever trades
        which near-duplicate chunks fill the same budget, never shrinks it.
        """
        if len(doc_indices) <= self.final_top_k:
            return doc_indices

        per_file_count: Dict[str, int] = {}
        selected: List[int] = []
        seen: set = set()
        leftover: List[int] = []
        for idx in doc_indices:
            if idx in seen:
                continue  # FAISS can return ties; never include the same chunk twice
            if len(selected) >= self.final_top_k:
                leftover.append(idx)
                continue
            doc = self.transformed_docs[idx]
            file_path = getattr(doc, "meta_data", None) or {}
            key = file_path.get("file_path", idx) if isinstance(file_path, dict) else idx
            count = per_file_count.get(key, 0)
            if count < MAX_CHUNKS_PER_SOURCE_FILE:
                selected.append(idx)
                seen.add(idx)
                per_file_count[key] = count + 1
            else:
                leftover.append(idx)

        if len(selected) < self.final_top_k:
            # When we must backfill to reach final_top_k, prefer implementation
            # chunks over test fixtures -- a test file almost never belongs in a
            # wiki explanation of how the code works, and backfill only runs
            # when there weren't enough distinct files among the top candidates,
            # so this preference is only the tie-breaker, never the main rank.
            # (This is the consumer of the is_implementation meta_data flag
            # set in data_pipeline.read_all_documents; previously dead.)
            def _is_impl(idx: int) -> bool:
                md = getattr(self.transformed_docs[idx], "meta_data", None) or {}
                return bool(md.get("is_implementation", True))

            leftover.sort(key=lambda idx: (0 if _is_impl(idx) else 1,))
            for idx in leftover:
                if len(selected) >= self.final_top_k:
                    break
                if idx in seen:
                    continue
                selected.append(idx)
                seen.add(idx)

        return selected

    def call(self, query: str, language: str = "en", filter_file_paths=None):
        """
        Process a query using RAG (retrieval only; chat generation is done by
        the caller via api.provider_streaming).

        Returns the RetrievedData object (whose `[0].documents` and
        `[0].doc_indices` are populated), or None on error. Returning None
        (instead of a differently-shaped tuple) lets callers uniformly check
        `if retrieved_documents and retrieved_documents[0].documents:` without
        an AttributeError when the error path returned an RAGAnswer object
        that has no `.documents` attribute.

        When `filter_file_paths` (a set/list of file paths) is provided, the
        retrieved chunks are post-filtered to only those whose file_path
        metadata is in the set -- used by wiki page generation to keep the
        page's context focused on its designated relevant_files. If fewer
        than ~3 chunks survive the filter, the unfiltered results are kept
        as a fallback (some context beats none, and an over-narrow filter
        could otherwise starve a page whose relevant_files weren't well
        represented in the chunk index).
        """
        try:
            retrieved_documents = self.retriever(query)

            doc_indices = self._diversify_doc_indices(retrieved_documents[0].doc_indices)
            retrieved_documents[0].doc_indices = doc_indices

            # Fill in the documents
            retrieved_documents[0].documents = [
                self.transformed_docs[doc_index]
                for doc_index in doc_indices
            ]

            if filter_file_paths:
                filter_set = set(filter_file_paths)
                filtered_indices = [
                    idx for idx in doc_indices
                    if (getattr(self.transformed_docs[idx], "meta_data", None) or {})
                        .get("file_path", "") in filter_set
                ]
                # Keep the filtered set only if it's not starved; otherwise
                # fall back to the diverse unfiltered results so the page
                # still has context to work with.
                if len(filtered_indices) >= 3:
                    retrieved_documents[0].doc_indices = filtered_indices
                    retrieved_documents[0].documents = [
                        self.transformed_docs[doc_index]
                        for doc_index in filtered_indices
                    ]

            return retrieved_documents

        except Exception as e:
            logger.error(f"Error in RAG call: {str(e)}")
            # Return None so callers (websocket_wiki.py/simple_chat.py/
            # search_tool.py) handle the failure uniformly via their existing
            # `if not retrieved or not retrieved[0].documents:` guard, instead
            # of crashing on `retrieved[0].documents` against an RAGAnswer.
            return None
