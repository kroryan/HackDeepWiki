"""Typed runtime settings and deployment safety checks.

HackDeepWiki is intentionally a single-process, local-first application.  The
old configuration surface was spread over module-level ``os.environ`` reads,
which made it easy for a container to bind publicly while forgetting to enable
authentication.  This module is the canonical registry for cross-cutting
runtime settings; feature-specific knobs may still live beside their feature,
but every security- or deployment-sensitive value belongs here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

_TRUE_VALUES = frozenset({"1", "true", "t", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "f", "no", "off", ""})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DeploymentProfile(str, Enum):
    """Explicitly supported operating modes.

    ``server`` and ``multiuser`` are reserved so an accidental value cannot
    silently opt the current single-user architecture into an unsupported
    security model.
    """

    DESKTOP = "desktop"
    TRUSTED_LAN = "trusted-lan"


def _bool_env(
    environ: Mapping[str, str],
    name: str,
    default: bool = False,
) -> bool:
    raw = environ.get(name, "true" if default else "false").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be a boolean value "
        "(true/false, 1/0, yes/no, on/off)"
    )


def _positive_int_env(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise ValueError(f"{name} must be in range {bound}")
    return value


@dataclass(frozen=True)
class Settings:
    deployment_profile: DeploymentProfile
    host: str
    auth_mode: bool
    auth_code: str
    allow_insecure_remote: bool
    internal_proxy_token: str
    allowed_origins: tuple[str, ...]
    allowed_local_roots: tuple[str, ...]
    auth_session_seconds: int
    data_dir: str | None
    config_dir: str | None
    log_level: str

    @property
    def is_loopback(self) -> bool:
        return self.host.strip().lower() in _LOOPBACK_HOSTS

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        host_override: str | None = None,
    ) -> "Settings":
        env = os.environ if environ is None else environ
        raw_profile = env.get(
            "HACKDEEPWIKI_DEPLOYMENT_PROFILE", DeploymentProfile.DESKTOP.value
        ).strip().lower()
        try:
            profile = DeploymentProfile(raw_profile)
        except ValueError as exc:
            supported = ", ".join(item.value for item in DeploymentProfile)
            raise ValueError(
                "HACKDEEPWIKI_DEPLOYMENT_PROFILE must be one of: "
                f"{supported}. Internet-facing and multiuser modes are not supported."
            ) from exc

        origins = tuple(
            value.strip().rstrip("/")
            for value in env.get("HACKDEEPWIKI_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        )
        roots = tuple(
            value.strip()
            for value in env.get("HACKDEEPWIKI_ALLOWED_LOCAL_ROOTS", "").split(
                os.pathsep
            )
            if value.strip()
        )
        return cls(
            deployment_profile=profile,
            host=(host_override or env.get("HACKDEEPWIKI_HOST", "127.0.0.1")).strip(),
            auth_mode=_bool_env(env, "HACKDEEPWIKI_AUTH_MODE"),
            auth_code=env.get("HACKDEEPWIKI_AUTH_CODE", ""),
            allow_insecure_remote=_bool_env(
                env, "HACKDEEPWIKI_ALLOW_INSECURE_REMOTE"
            ),
            internal_proxy_token=env.get(
                "HACKDEEPWIKI_INTERNAL_PROXY_TOKEN", ""
            ),
            allowed_origins=origins,
            allowed_local_roots=roots,
            auth_session_seconds=_positive_int_env(
                env,
                "HACKDEEPWIKI_AUTH_SESSION_SECONDS",
                8 * 60 * 60,
                minimum=300,
                maximum=7 * 24 * 60 * 60,
            ),
            data_dir=env.get("HACKDEEPWIKI_DATA_DIR") or None,
            config_dir=env.get("HACKDEEPWIKI_CONFIG_DIR") or None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )

    def validate_deployment(self) -> None:
        """Fail closed for every non-loopback bind.

        The escape hatch is deliberately explicit and is never enabled by a
        deployment profile.  It exists only for isolated test networks.
        """
        if self.deployment_profile is DeploymentProfile.TRUSTED_LAN and self.is_loopback:
            raise RuntimeError(
                "trusted-lan deployment requires a non-loopback HACKDEEPWIKI_HOST"
            )
        if self.is_loopback:
            return
        if self.allow_insecure_remote:
            return
        if not self.auth_mode:
            raise RuntimeError(
                "Refusing non-loopback bind without HACKDEEPWIKI_AUTH_MODE=true. "
                "Set authentication or, only on an isolated network, explicitly "
                "set HACKDEEPWIKI_ALLOW_INSECURE_REMOTE=true."
            )
        if len(self.auth_code) < 16:
            raise RuntimeError(
                "Refusing non-loopback bind: HACKDEEPWIKI_AUTH_CODE must contain "
                "at least 16 characters."
            )

    def public_diagnostic(self) -> dict[str, object]:
        """Return non-secret settings suitable for health/diagnostic output."""
        return {
            "deployment_profile": self.deployment_profile.value,
            "host": self.host,
            "loopback": self.is_loopback,
            "auth_enabled": self.auth_mode,
            "auth_code_configured": bool(self.auth_code),
            "internal_proxy_configured": bool(self.internal_proxy_token),
            "allowed_origin_count": len(self.allowed_origins),
            "allowed_local_root_count": len(self.allowed_local_roots),
            "data_dir_configured": bool(self.data_dir),
            "config_dir_configured": bool(self.config_dir),
            "log_level": self.log_level,
            "single_process": True,
        }


PUBLIC_ENVIRONMENT_VARIABLES: dict[str, str] = {
    "HACKDEEPWIKI_DEPLOYMENT_PROFILE": "desktop or trusted-lan",
    "HACKDEEPWIKI_HOST": "Backend bind host",
    "HACKDEEPWIKI_AUTH_MODE": "Enable shared-code authentication",
    "HACKDEEPWIKI_AUTH_CODE": "Shared secret (minimum 16 chars off loopback)",
    "HACKDEEPWIKI_AUTH_SESSION_SECONDS": "Signed browser-session lifetime",
    "HACKDEEPWIKI_ALLOWED_ORIGINS": "Comma-separated extra browser origins",
    "HACKDEEPWIKI_ALLOWED_LOCAL_ROOTS": "OS-path-separated local repository roots",
    "HACKDEEPWIKI_ALLOW_INSECURE_REMOTE": "Unsafe isolated-network escape hatch",
    "HACKDEEPWIKI_DATA_DIR": "Persistent data root",
    "HACKDEEPWIKI_CONFIG_DIR": "Model configuration directory",
    "HACKDEEPWIKI_ENC_KEY": "Provider-profile encryption passphrase",
    "HACKDEEPWIKI_MCP_TOKEN": "Explicit MCP bearer token",
    "HACKDEEPWIKI_EGRESS_POLICY": "Outbound policy: any or public",
    "HACKDEEPWIKI_OPENCODE_BIN": "Explicit OpenCode executable path",
    "HACKDEEPWIKI_EMBEDDER_TYPE": "Repository embedding provider",
    "HACKDEEPWIKI_ENGRAPHIS_EMBEDDER": "Enable shared Engraphis embedder",
    "HACKDEEPWIKI_DISABLE_AGENT_LOOP": "Disable iterative agent tools",
    "HACKDEEPWIKI_DEEP_RESEARCH_MAX_ITERATIONS": "Research iteration bound",
    "HACKDEEPWIKI_CODE_CHUNK_CHARS": "Code-context chunk size",
    "HACKDEEPWIKI_CODE_MAX_BLOCK_CHARS": "Maximum code-context block",
    "HACKDEEPWIKI_CODE_OVERLAP_LINES": "Code-context overlap",
    "HACKDEEPWIKI_JOB_MAX_ATTEMPTS": "Durable job retry limit",
    "HACKDEEPWIKI_JOB_BACKOFF_BASE": "Durable job retry backoff",
    "HACKDEEPWIKI_JOB_POLL": "Worker polling interval",
    "HACKDEEPWIKI_JOB_STALE_SECONDS": "Stale running-job recovery age",
    "HACKDEEPWIKI_MCP_LIST_TIMEOUT": "External MCP discovery timeout",
    "HACKDEEPWIKI_MCP_STDIO_TIMEOUT": "External MCP request timeout",
    "HACKDEEPWIKI_NEO4J_URI": "Optional vulnerability graph URI",
    "HACKDEEPWIKI_NEO4J_USER": "Optional vulnerability graph user",
    "HACKDEEPWIKI_NEO4J_PASSWORD": "Optional vulnerability graph password",
    "HACKDEEPWIKI_OLLAMA_MODE": "Bundled Ollama operating mode",
    "HACKDEEPWIKI_WIKI_CACHE_MAX_AGE_DAYS": "Cache retention age",
    "HACKDEEPWIKI_WIKI_CACHE_MAX_BYTES": "Cache retention size",
    "HACKDEEPWIKI_WIKI_CACHE_MAX_FILES": "Cache retention file count",
    "HACKDEEPWIKI_AUTH_MAX_FAILED": "Authentication failure limit",
    "HACKDEEPWIKI_AUTH_LOCKOUT_WINDOW": "Authentication lockout window",
    "HACKDEEPWIKI_PROJECT_NAME": "Docker Compose project name",
    "HACKDEEPWIKI_NETWORK_MODE": "Docker launcher network mode",
    "HACKDEEPWIKI_API_PORT": "Preferred backend port",
}

INTERNAL_ENVIRONMENT_VARIABLES: dict[str, str] = {
    "HACKDEEPWIKI_INTERNAL_PROXY_TOKEN": "Next-to-FastAPI internal credential",
    "HACKDEEPWIKI_BACKEND_PORT": "Selected backend port in portable builds",
    "HACKDEEPWIKI_BUILD_COMMIT": "Packaged source commit",
    "HACKDEEPWIKI_BUILD_RUN": "CI run/build identifier",
    "HACKDEEPWIKI_BUILD_CHANNEL": "Build channel",
    "HACKDEEPWIKI_OLD_ENC_KEY": "Offline rotation input only",
    "HACKDEEPWIKI_NEW_ENC_KEY": "Offline rotation input only",
}


def get_settings(*, host_override: str | None = None) -> Settings:
    """Read current settings without caching, keeping env-mutating tests safe."""
    return Settings.from_environ(host_override=host_override)
