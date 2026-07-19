from typing import Sequence
from copy import deepcopy
from tqdm import tqdm
import logging
import adalflow as adal
from adalflow.core.types import Document
from adalflow.core.component import DataComponent
import requests
import os
import tiktoken

# Configure logging
from api.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_QUERY_MAX_TOKENS = 1800


def prepare_ollama_embedding_query(
    text: str,
    max_tokens: int | None = None,
) -> str:
    """Keep retrieval queries within the context supported by small embedders.

    The generation prompt is never passed through this function. It is only
    used for the semantic-search query sent to the Ollama embedding model.
    Keeping both the beginning and end preserves the topic and any trailing
    file hints when an older client sends a full generation prompt.
    """
    configured_limit = max_tokens or int(
        os.getenv(
            "OLLAMA_QUERY_MAX_TOKENS",
            str(DEFAULT_OLLAMA_QUERY_MAX_TOKENS),
        )
    )
    configured_limit = max(128, configured_limit)

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        tokens = encoding.encode(text)
        if len(tokens) <= configured_limit:
            return text

        separator = "\n\n[... retrieval query shortened ...]\n\n"
        separator_tokens = encoding.encode(separator)
        available = max(1, configured_limit - len(separator_tokens))
        head_size = max(1, int(available * 0.7))
        tail_size = max(0, available - head_size)
        shortened_tokens = (
            tokens[:head_size]
            + separator_tokens
            + (tokens[-tail_size:] if tail_size else [])
        )
        shortened = encoding.decode(shortened_tokens)
        logger.info(
            "Shortened Ollama retrieval query from %s to %s tokens",
            len(tokens),
            len(shortened_tokens),
        )
        return shortened
    except Exception as exc:
        # A conservative character fallback keeps retrieval available even if
        # the tokenizer cannot be loaded in a minimal installation.
        max_characters = configured_limit * 3
        if len(text) <= max_characters:
            return text
        logger.warning(
            "Could not tokenize Ollama retrieval query; using character limit: %s",
            exc,
        )
        head_size = int(max_characters * 0.7)
        tail_size = max_characters - head_size
        return (
            text[:head_size]
            + "\n\n[... retrieval query shortened ...]\n\n"
            + text[-tail_size:]
        )


class OllamaModelNotFoundError(Exception):
    """Custom exception for when Ollama model is not found"""
    pass

def check_ollama_model_exists(model_name: str, ollama_host: str = None) -> bool:
    """
    Check if an Ollama model exists before attempting to use it.
    
    Args:
        model_name: Name of the model to check
        ollama_host: Ollama host URL, defaults to localhost:11434
        
    Returns:
        bool: True if model exists, False otherwise
    """
    if ollama_host is None:
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    
    try:
        # Remove /api prefix if present and add it back
        if ollama_host.endswith('/api'):
            ollama_host = ollama_host[:-4]
        
        response = requests.get(f"{ollama_host}/api/tags", timeout=5)
        if response.status_code == 200:
            models_data = response.json()
            available_models = [model.get('name', '').split(':')[0] for model in models_data.get('models', [])]
            model_base_name = model_name.split(':')[0]  # Remove tag if present
            
            is_available = model_base_name in available_models
            if is_available:
                logger.info(f"Ollama model '{model_name}' is available")
            else:
                logger.warning(f"Ollama model '{model_name}' is not available. Available models: {available_models}")
            return is_available
        else:
            logger.warning(f"Could not check Ollama models, status code: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        logger.warning(f"Could not connect to Ollama to check models: {e}")
        return False
    except Exception as e:
        logger.warning(f"Error checking Ollama model availability: {e}")
        return False

class OllamaDocumentProcessor(DataComponent):
    """
    Process Ollama embeddings through the native batch API.

    AdalFlow's Ollama client uses the legacy single-input endpoint. Current
    Ollama releases expose /api/embed, which accepts a list of inputs and is
    substantially faster for large repositories. A failed batch falls back to
    AdalFlow's single-document path for compatibility with older servers.
    """
    def __init__(
        self,
        embedder: adal.Embedder,
        model_name: str,
        batch_size: int = 32,
        ollama_host: str = None,
        request_timeout: float = None,
    ) -> None:
        super().__init__()
        self.embedder = embedder
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        self.ollama_host = (
            ollama_host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        ).removesuffix("/api").rstrip("/")
        self.request_timeout = request_timeout or float(
            os.getenv("OLLAMA_REQUEST_TIMEOUT", "1800")
        )

    def __call__(self, documents: Sequence[Document]) -> Sequence[Document]:
        output = deepcopy(documents)
        logger.info(
            "Processing %s documents with Ollama batch embeddings (batch size: %s)",
            len(output),
            self.batch_size,
        )
        successful_docs = []
        expected_embedding_size = None

        for start in tqdm(
            range(0, len(output), self.batch_size),
            desc="Processing Ollama embedding batches",
        ):
            batch = output[start : start + self.batch_size]
            try:
                response = requests.post(
                    f"{self.ollama_host}/api/embed",
                    json={
                        "model": self.model_name,
                        "input": [doc.text for doc in batch],
                    },
                    timeout=self.request_timeout,
                )
                response.raise_for_status()
                embeddings = response.json().get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                    raise ValueError(
                        "Ollama returned an unexpected number of embeddings "
                        f"({len(embeddings) if isinstance(embeddings, list) else 0}"
                        f" for {len(batch)} inputs)"
                    )
            except (requests.RequestException, ValueError) as exc:
                logger.warning(
                    "Ollama batch embedding failed at document %s; "
                    "falling back to individual requests: %s",
                    start,
                    exc,
                )
                embeddings = []
                for doc in batch:
                    try:
                        result = self.embedder(input=doc.text)
                        embedding = (
                            result.data[0].embedding
                            if result.data and len(result.data) > 0
                            else None
                        )
                    except Exception as fallback_exc:
                        logger.error(
                            "Individual Ollama embedding failed for '%s': %s",
                            getattr(doc, "meta_data", {}).get(
                                "file_path", f"document_{start + len(embeddings)}"
                            ),
                            fallback_exc,
                        )
                        embedding = None
                    embeddings.append(embedding)

            for offset, embedding in enumerate(embeddings):
                doc_index = start + offset
                doc = output[doc_index]
                if not isinstance(embedding, list) or not embedding:
                    logger.warning(
                        "No embedding returned for '%s'; skipping",
                        getattr(doc, "meta_data", {}).get(
                            "file_path", f"document_{doc_index}"
                        ),
                    )
                    continue
                if expected_embedding_size is None:
                    expected_embedding_size = len(embedding)
                    logger.info(
                        "Expected Ollama embedding size set to: %s",
                        expected_embedding_size,
                    )
                elif len(embedding) != expected_embedding_size:
                    logger.warning(
                        "Embedding size mismatch for '%s': %s != %s; skipping",
                        getattr(doc, "meta_data", {}).get(
                            "file_path", f"document_{doc_index}"
                        ),
                        len(embedding),
                        expected_embedding_size,
                    )
                    continue
                doc.vector = embedding
                successful_docs.append(doc)

        logger.info(
            "Successfully processed %s/%s documents with Ollama embeddings",
            len(successful_docs),
            len(output),
        )
        return successful_docs
