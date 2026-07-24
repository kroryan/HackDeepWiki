"""Pydantic request/response models for the HackDeepWiki API.

Extracted verbatim from ``api/api.py`` (the original ``# --- Pydantic Models
---`` block) so the 3800-line route module doesn't also own every schema.
These are plain data schemas -- no behavior, no shared mutable state -- so
the move is mechanical: ``api/api.py`` imports them back, and nothing else
in the tree imported them from ``api.api`` (verified), so no external
consumer breaks.

Note: a handful of request models that are tightly co-located with their
route groups (``ModelProbeRequest``, the ``Fanwiki*`` request models,
``ZimImportRequest``) stay in ``api/api.py`` next to the routes that use
them -- moving those would be the higher-risk entangled split, not this one.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class WikiPage(BaseModel):
    """
    Model for a wiki page.
    """
    id: str
    title: str
    content: str
    filePaths: List[str]
    importance: str # Should ideally be Literal['high', 'medium', 'low']
    relatedPages: List[str]


class ProcessedProjectEntry(BaseModel):
    id: str  # Filename
    owner: str
    repo: str
    name: str  # owner/repo
    repo_type: str # Renamed from type to repo_type for clarity with existing models
    submittedAt: int # Timestamp
    language: str # Extracted from filename
    status: Literal["generated", "imported"] = "generated"
    start_url: Optional[str] = None
    page_count: Optional[int] = None


class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: Optional[str] = None
    localPath: Optional[str] = None
    repoUrl: Optional[str] = None


class WikiSection(BaseModel):
    """
    Model for the wiki sections.
    """
    id: str
    title: str
    pages: List[str]
    subsections: Optional[List[str]] = None


class WikiStructureModel(BaseModel):
    """
    Model for the overall wiki structure.
    """
    id: str
    title: str
    description: str
    pages: List[WikiPage]
    sections: Optional[List[WikiSection]] = None
    rootSections: Optional[List[str]] = None


class WikiCacheData(BaseModel):
    """
    Model for the data to be stored in the wiki cache.
    """
    wiki_structure: WikiStructureModel
    generated_pages: Dict[str, WikiPage]
    repo_url: Optional[str] = None  #compatible for old cache
    repo: Optional[RepoInfo] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    comprehensive: Optional[bool] = None
    page_count: Optional[int] = None
    # Monotonically increasing release version for this repo/language/type.
    # v1 = first generation, v2 = first update, v3 = second update, ... so an
    # update never overwrites the previous wiki. Legacy caches without a version
    # are treated as v0.
    version: Optional[int] = None


class WikiCacheRequest(BaseModel):
    """
    Model for the request body when saving wiki cache.
    """
    repo: RepoInfo
    language: str
    wiki_structure: WikiStructureModel
    generated_pages: Dict[str, WikiPage]
    provider: str
    model: str
    comprehensive: bool = True
    page_count: int = Field(default=10, ge=1, le=50)
    version: Optional[int] = None


class WikiExportRequest(BaseModel):
    """
    Model for requesting a wiki export.
    """
    repo_url: str = Field(..., description="URL of the repository")
    pages: List[WikiPage] = Field(..., description="List of wiki pages to export")
    format: Literal["markdown", "json", "obsidian", "hdwreader", "mediawiki_xml", "zim"] = Field(
        ..., description="Export format (markdown, json, obsidian vault zip, hdwreader portable bundle, MediaWiki-compatible XML, or offline .zim archive)")
    title: Optional[str] = Field(None, description="Wiki title (used for the Obsidian vault name/index)")
    version: Optional[int] = Field(None, description="Wiki release version being exported (informational)")
    # 🔐 Security Analysis — optional vulnerability report to embed in the
    # Obsidian vault / hdwreader bundle (ignored for markdown/json exports).
    vuln_report: Optional[Dict[str, Any]] = Field(None, description="Optional vulnerability report dict to include in the export")
    include_vulns: bool = Field(False, description="Include the dependency vulnerability report in the export")
    include_vuln_graph: bool = Field(True, description="Include the vulnerability graph (Canvas + Mermaid) in the Security folder (obsidian only)")
    # 🌐 Website Security — same idea as vuln_report/include_vulns above, but
    # for the separate website scan report. Only meaningful for website wikis.
    web_vuln_report: Optional[Dict[str, Any]] = Field(None, description="Optional website security report dict to include in the export")
    include_web_vulns: bool = Field(False, description="Include the website security report in the export")
    # 🗂️ hdwreader-only: full page hierarchy, dropped by the other formats.
    sections: Optional[List[WikiSection]] = Field(None, description="Wiki section tree (hdwreader only)")
    root_sections: Optional[List[str]] = Field(None, description="Top-level section ids (hdwreader only)")
    description: Optional[str] = Field(None, description="Wiki description (hdwreader only)")
    language: Optional[str] = Field(None, description="Wiki language code (hdwreader only)")
    provider: Optional[str] = Field(None, description="LLM provider used to generate the wiki (hdwreader only)")
    model: Optional[str] = Field(None, description="LLM model used to generate the wiki (hdwreader only)")
    repo_type: Optional[str] = Field(None, description="Repository type: github/gitlab/bitbucket/website/local (hdwreader only)")
    owner: Optional[str] = Field(None, description="Repository owner, or 'website' for a crawled site (hdwreader only)")
    repo: Optional[str] = Field(None, description="Repository name, or site hostname (hdwreader only)")


class PageEditRequest(BaseModel):
    """Model for saving a manually or AI-edited wiki page's content."""
    repo: RepoInfo
    language: str
    page_id: str = Field(..., description="Id of the page in generated_pages to update")
    content: str = Field(..., description="New markdown content for the page")
    version: Optional[int] = Field(
        None,
        description="Release version to base the edit on (latest release if omitted)",
    )


class PageEditAIRequest(BaseModel):
    """Model for requesting an AI-assisted rewrite of a wiki page. Streams the
    proposed markdown back -- never persists anything itself."""
    page_title: str
    current_content: str
    instruction: str = Field(..., description="What the user wants changed about the page")
    provider: str = Field("google", description="Model provider (google, openai, openrouter, ollama, bedrock, azure, dashscope)")
    model: Optional[str] = Field(None, description="Model name for the specified provider")
    language: Optional[str] = Field("en", description="Language for the rewritten content")
    api_key: Optional[str] = Field(None, description="Optional custom API key")
    api_endpoint: Optional[str] = Field(None, description="Optional custom API endpoint")


class FileContentRequest(BaseModel):
    """Fetches one file's full content for the in-app code viewer (see
    src/components/CodeViewer.tsx), opened when the user clicks a source
    file a repo chat cited. POST, not GET-with-query-params, since
    `token` (a private repo's access token) must never end up in a URL or
    browser history."""
    repo_url: str = Field(..., description="Repository URL, or local path for type='local'")
    repo_type: str = Field(..., description="Repository type: github, gitlab, bitbucket, or local")
    file_path: str = Field(..., description="Path of the file to read, relative to the repo root")
    token: Optional[str] = Field(None, description="Personal access token for private repositories")


class RepoStructureRequest(BaseModel):
    """File tree + README for a github/gitlab/bitbucket repo, read from a
    local git clone (made fresh if needed) instead of the provider's REST
    API -- see /api/repo/structure and /ws/repo/clone."""
    repo_url: str = Field(..., description="Repository URL")
    repo_type: str = Field(..., description="Repository type: github, gitlab, or bitbucket")
    token: Optional[str] = Field(None, description="Personal access token for private repositories")
    force: Optional[bool] = Field(False, description="'Refresh Wiki' semantics -- re-clone fresh instead of reusing the existing local clone")


# --- Model Configuration Models ---
class Model(BaseModel):
    """
    Model for LLM model configuration
    """
    id: str = Field(..., description="Model identifier")
    name: str = Field(..., description="Display name for the model")


class Provider(BaseModel):
    """
    Model for LLM provider configuration
    """
    id: str = Field(..., description="Provider identifier")
    name: str = Field(..., description="Display name for the provider")
    models: List[Model] = Field(..., description="List of available models for this provider")
    supportsCustomModel: Optional[bool] = Field(False, description="Whether this provider supports custom models")


class ModelConfig(BaseModel):
    """
    Model for the entire model configuration
    """
    providers: List[Provider] = Field(..., description="List of available model providers")
    defaultProvider: str = Field(..., description="ID of the default provider")


class AuthorizationConfig(BaseModel):
    code: str = Field(..., description="Authorization code")