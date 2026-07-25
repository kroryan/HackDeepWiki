"""
Shared request models for both chat transports (api/websocket_wiki.py's
WebSocket handler and api/simple_chat.py's HTTP streaming fallback). Kept in
one place so the two transports can never drift on what fields exist or
what they mean -- previously each file defined its own copy of
`ChatCompletionRequest`, and it was easy for one to gain a field (like
`current_page_id`) the other silently lacked.
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str


class ChatCompletionRequest(BaseModel):
    """Model for requesting a chat completion."""
    repo_url: str = Field(..., description="URL of the repository to query")
    messages: List[ChatMessage] = Field(..., description="List of chat messages")
    retrieval_query: Optional[str] = Field(
        None,
        description="Optional concise query used only for semantic retrieval",
    )
    filePath: Optional[str] = Field(None, description="Optional path to a file in the repository to include in the prompt")
    token: Optional[str] = Field(None, description="Personal access token for private repositories")
    type: Optional[str] = Field("github", description="Type of repository (e.g., 'github', 'gitlab', 'bitbucket', 'local', 'zim')")
    current_page_id: Optional[str] = Field(
        None,
        description=(
            "Wiki page id (repo) or ZIM entry path (type='zim') the chat was opened from. "
            "When set, the initial context is scoped to that page/entry plus a handful of "
            "related ones instead of the whole repo/archive."
        ),
    )
    enable_tool_calling: Optional[bool] = Field(
        True,
        description="Whether the agent may emit SEARCH_WIKI: <query> to fetch more context mid-answer.",
    )
    # 🔐 Security Analysis / 🌐 Website Security context -- when the user
    # checks "Include security analysis" in the chat, the latest saved scan
    # report(s) for this repo are summarized and injected into the prompt so
    # the LLM can answer questions about vulnerabilities without the user
    # pasting the report in manually. owner/repo identify which saved report
    # to load (mirrors the fields the frontend already sends to
    # /api/vuln_cache and /api/web_vuln_cache).
    include_security_context: Optional[bool] = Field(
        False,
        description="Include the latest saved vulnerability/website-security scan report as chat context.",
    )
    owner: Optional[str] = Field(None, description="Repository owner (or 'website' for a crawled site), for security context lookup")
    repo: Optional[str] = Field(None, description="Repository name (or site hostname for a crawled site), for security context lookup")

    # Engraphis memory scoping (api/engraphis_integration.py): the wiki
    # release the user has OPEN. Memory is hard-isolated per wiki release
    # (workspace = {owner}_{repo}_v{N}), shared between the chat and the code
    # editor for that release only -- so the chat needs to know which release
    # it is talking about, exactly like the code agent already does.
    wiki_version: Optional[int] = Field(
        None,
        description="Wiki release version the chat was opened on (memory scope anchor).",
    )

    # Fase 6 -- Agent skills. Opt-in: a list of skill names (e.g. ["think",
    # "security-review"]) the chat wants applied. Empty/None = no skill
    # workflow injected (default), so chats that don't need structured
    # reasoning don't pay the context cost. Skill discovery + rendering lives
    # in api.skills; render_skills_block(selected) emits a <skills> block the
    # model self-selects from. See apply_skills_to_system_prompt in
    # chat_common (shared by both transports so they can't drift).
    skills: Optional[List[str]] = Field(
        None,
        description="Optional list of skill names to inject into the system prompt (opt-in structured-reasoning workflows).",
    )

    # model parameters
    provider: str = Field(
        "google",
        description="Model provider (google, openai, openrouter, ollama, bedrock, azure, dashscope)",
    )
    model: Optional[str] = Field(None, description="Model name for the specified provider")

    language: Optional[str] = Field("en", description="Language for content generation (e.g., 'en', 'ja', 'zh', 'es', 'kr', 'vi')")
    excluded_dirs: Optional[str] = Field(None, description="Comma-separated list of directories to exclude from processing")
    excluded_files: Optional[str] = Field(None, description="Comma-separated list of file patterns to exclude from processing")
    included_dirs: Optional[str] = Field(None, description="Comma-separated list of directories to include exclusively")
    included_files: Optional[str] = Field(None, description="Comma-separated list of file patterns to include exclusively")
    api_key: Optional[str] = Field(None, description="Optional custom API key")
    api_endpoint: Optional[str] = Field(None, description="Optional custom API endpoint")
    force_refresh: Optional[bool] = Field(
        False,
        description=(
            "'Refresh Wiki' semantics: re-clone git-hosted repos fresh and rebuild the "
            "RAG embeddings index from scratch instead of trusting a cached .pkl that "
            "has no way to know the underlying files changed since it was built."
        ),
    )
    filter_file_paths: Optional[List[str]] = Field(
        None,
        description=(
            "When set (wiki page generation), post-filter RAG chunks to only those "
            "whose file_path metadata is in this set -- the page's relevant_files. "
            "Keeps unfiltered results as a fallback if too few match, so a page never "
            "gets zero context from an over-narrow filter."
        ),
    )
