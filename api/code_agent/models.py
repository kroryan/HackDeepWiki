"""Request/response schemas for the code-agent routes.

Deliberately separate from api/chat_models.py::ChatCompletionRequest -- Code
Editing mode has its own transport (routes.py) and its own payload; keeping
the schemas apart means the hand-mirrored /ws/chat + simple_chat pair never
grows a half-implemented flag.
"""

from typing import Optional

from pydantic import BaseModel, Field


class CodeSessionRequest(BaseModel):
    repo_url: str = Field(..., description="Repo URL, or filesystem path for type='local'")
    type: str = Field("github", description="Repo type (github, gitlab, bitbucket, local)")
    owner: str = ""
    repo: str = ""
    provider: str = Field("google", description="Model provider, same values as the chat")
    model: Optional[str] = None
    api_key: Optional[str] = Field(None, description="Per-request provider key (localStorage)")
    api_endpoint: Optional[str] = Field(None, description="Per-request provider endpoint")
    language: str = Field("en", description="Language of the OPEN wiki release")
    wiki_version: Optional[int] = Field(
        None, description="The wiki release the user has open; anchors the version check")
    include_security_context: Optional[bool] = False
    existing_session_id: Optional[str] = Field(
        None, description="Resume this opencode session if it still exists")
    title: Optional[str] = None


class CodeSessionResponse(BaseModel):
    session_id: str
    repo_key: str
    repo_dir: str
    is_local_type: bool
    opencode_version: Optional[str] = None
    version_warning: Optional[str] = None
    active_sessions: int = 1
    # "provider/model → endpoint" the agent actually talks to; shown in the
    # panel header so connection failures are self-diagnosable.
    model_target: Optional[str] = None


class CodeChatRequest(BaseModel):
    """First (and only) frame the client sends on WS /ws/code/chat."""
    repo_key: str
    session_id: str
    content: str
    provider: str = "google"
    model: Optional[str] = None
    api_key: Optional[str] = None
    api_endpoint: Optional[str] = None


class CodeAbortRequest(BaseModel):
    repo_key: str
    session_id: str


class CodeAgentUpdateRequest(BaseModel):
    version: str = Field("pinned", description="'pinned', 'latest', or an explicit tag like v1.18.5")
