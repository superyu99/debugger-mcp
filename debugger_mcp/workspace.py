from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, MutableMapping, Optional
from urllib.parse import unquote, urlparse

# Explicit per-debug-run workspace marker carried inside the standard
# launch.json ``env`` object. The proxy consumes it before forwarding the
# launch/attach request to debugpy, so the debuggee never receives it.
WORKSPACE_ENV_KEY = "DEBUGGER_MCP_WORKSPACE"

# Old v0.5.0 preview builds briefly documented this invalid top-level field.
# It is consumed only so it cannot leak to debugpy; it is deliberately ignored
# for routing. VS Code's debug schema rejects it.
LEGACY_WORKSPACE_CONFIG_KEY = "debuggerMcpWorkspace"

# Internal RPC metadata added by each MCP stdio frontend. It is never exposed
# as a public tool argument.
CLIENT_WORKSPACE_PARAM = "__debuggerMcpClientWorkspace"

# Stable identity for one MCP stdio frontend process. The shared daemon uses
# it to remember which debugger stop that specific Agent has already seen.
CLIENT_ID_PARAM = "__debuggerMcpClientId"


def normalize_workspace_root(value: object) -> Optional[str]:
    """Return a stable absolute workspace path, or ``None`` for missing input."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    expanded = os.path.expandvars(os.path.expanduser(text))
    try:
        # strict=False keeps this usable before a workspace is mounted/created.
        resolved = str(Path(expanded).resolve(strict=False))
    except (OSError, RuntimeError):
        resolved = os.path.abspath(expanded)
    return os.path.normcase(os.path.normpath(resolved))


def diagnostic_debug_cwd(value: object) -> Optional[str]:
    """Normalize a DAP cwd for diagnostics only.

    This value must never be used to assign a session to an MCP workspace.
    Relative paths are retained as relative diagnostic text because resolving
    them against the daemon process would create a false association.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    expanded = os.path.expandvars(os.path.expanduser(text))
    if os.path.isabs(expanded):
        return normalize_workspace_root(expanded)
    return os.path.normpath(expanded)


def pop_workspace_from_debug_arguments(
    arguments: MutableMapping[str, Any],
) -> Optional[str]:
    """Consume the explicit workspace marker from DAP start arguments.

    Only ``env.DEBUGGER_MCP_WORKSPACE`` establishes workspace ownership. The
    DAP ``cwd`` and source paths are intentionally excluded from routing to
    avoid cross-project guesses. The old invalid top-level field is discarded
    but never accepted as a routing signal.
    """
    arguments.pop(LEGACY_WORKSPACE_CONFIG_KEY, None)
    env_value: object = None
    env = arguments.get("env")
    if isinstance(env, dict):
        env_value = env.pop(WORKSPACE_ENV_KEY, None)
        if not env:
            arguments.pop("env", None)
    return normalize_workspace_root(env_value)


def inject_workspace_into_debug_configuration(
    configuration: MutableMapping[str, Any], workspace_root: str
) -> None:
    """Put the explicit marker in an internal child startDebugging request."""
    configuration.pop(LEGACY_WORKSPACE_CONFIG_KEY, None)
    env = configuration.get("env")
    if not isinstance(env, dict):
        env = {}
        configuration["env"] = env
    env[WORKSPACE_ENV_KEY] = workspace_root


def detect_client_workspace(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the project workspace owned by one MCP stdio frontend.

    This function resolves only the startup fallback: explicit argument, then
    ``DEBUGGER_MCP_WORKSPACE``, then process cwd. In v0.6.3 the MCP server may
    replace the cwd fallback after initialization when the client advertises
    the standard Roots capability and ``roots/list`` returns exactly one root.
    Frontend identity remains separate from the per-debug-run marker consumed
    from launch.json.
    """
    candidate = explicit
    if candidate is None:
        candidate = os.getenv(WORKSPACE_ENV_KEY)
    if candidate is None:
        candidate = os.getcwd()
    return normalize_workspace_root(candidate)


def workspace_from_mcp_root_uri(uri: object) -> Optional[str]:
    """Convert one MCP ``file://`` root URI into a normalized local path.

    MCP Roots are client-provided filesystem hints. The current Roots
    specification requires file URIs, but this parser intentionally tolerates
    a plain absolute path as a defensive compatibility fallback.
    """
    if not isinstance(uri, str):
        return None
    text = uri.strip()
    if not text:
        return None

    parsed = urlparse(text)
    if not parsed.scheme:
        return normalize_workspace_root(text) if os.path.isabs(text) else None
    if parsed.scheme.lower() != "file":
        return None

    path = unquote(parsed.path or "")
    netloc = unquote(parsed.netloc or "")
    if netloc and netloc.lower() != "localhost":
        # UNC/network file URI: file://server/share/path
        path = f"//{netloc}{path}"
    elif os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
        # Windows file:///C:/path URI. Removing the leading slash is harmless
        # for a Windows-hosted MCP frontend and keeps tests platform-neutral.
        path = path[1:]
    return normalize_workspace_root(path)
