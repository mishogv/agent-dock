"""Portable configuration and native CLI discovery for Agent Dock."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

APP_NAME = "MPAgentDock"
STATE = Path(os.environ.get("MP_AGENT_DOCK_STATE_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME)
CONFIG_PATH = STATE / "config.json"


class ConfigError(RuntimeError):
    pass


def app_state_dir() -> Path:
    override = os.environ.get("MP_AGENT_DOCK_STATE_DIR")
    return Path(override) if override else STATE


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"invalid configuration: {CONFIG_PATH}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration must be a JSON object: {CONFIG_PATH}")
    return value


def claude_usage_enabled() -> bool:
    return load_config().get("enable_claude_usage") is True


def _native(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_file() and resolved.suffix.lower() == ".exe" else None


def _npm_native(shim: Path) -> Path | None:
    """Resolve an npm shim to its vendor executable, never execute the shim."""
    if shim.suffix.lower() not in {".cmd", ".ps1", ""}:
        return None
    pattern = "node_modules/@openai/codex/node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"
    for base in (shim.parent, shim.parent.parent):
        for candidate in base.glob(pattern):
            native = _native(candidate)
            if native:
                return native
    return None


def _configured_or_path(env_name: str, config_name: str, command: str) -> Path | None:
    configured = os.environ.get(env_name) or load_config().get(config_name)
    if isinstance(configured, str) and configured:
        native = _native(Path(configured))
        if native:
            return native
        return _npm_native(Path(configured))
    found = shutil.which(command)
    if found:
        native = _native(Path(found))
        if native:
            return native
        resolved = _npm_native(Path(found))
        if resolved:
            return resolved
    return None


def find_codex() -> Path | None:
    configured = load_config()
    if "MP_AGENT_DOCK_CODEX" in os.environ or "codex_executable" in configured:
        return _configured_or_path("MP_AGENT_DOCK_CODEX", "codex_executable", "codex")
    found = _configured_or_path("MP_AGENT_DOCK_CODEX", "codex_executable", "codex")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe" if local else None,
        Path.home() / ".local" / "bin" / "codex.exe",
    ]
    return next((native for item in candidates if item and (native := _native(item))), None)
