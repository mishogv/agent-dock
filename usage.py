"""Read-only usage collector for the MP Agent Dock.

The collector deliberately keeps the provider adapters small and dependency free.  It
uses the installed Codex app-server over stdio, and Claude Code's OAuth usage endpoint
when credentials are already present.  Neither adapter refreshes credentials or changes
the active profile.

The Anthropic endpoint is an unofficial Claude Code endpoint.  Claude statusline input
is also accepted as a local, no-network fallback: the statusline hook receives rate
limits after a response and writes them to the same cache.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from config import app_state_dir, claude_usage_enabled, find_codex


APP_NAME = "MPAgentDock"
SNAPSHOT_NAME = "snapshot.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
WATCH_INTERVAL = 60.0
STALE_AFTER_SECONDS = 5 * 60
_MAX_FUTURE_OBSERVED_SECONDS = 30
_FAILURE_BACKOFF_SECONDS = 60.0
_failure_until: dict[str, float] = {}
_lock_depth = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        # API responses use both epoch seconds and epoch milliseconds.
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _timestamp(value: Any) -> datetime | None:
    """Parse a provider timestamp into an aware UTC datetime."""
    normalized = _iso(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _observed_at(value: Any) -> str | None:
    """Return an observation time only when it is plausible for this machine."""
    normalized = _iso(value)
    timestamp = _timestamp(normalized)
    if normalized is None or timestamp is None:
        return None
    return normalized if (timestamp - _utc_now()).total_seconds() <= _MAX_FUTURE_OBSERVED_SECONDS else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            result = float(value.strip().rstrip("%"))
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    return None


def _percent(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    # A percentage is always represented as 0..100.  Out-of-range API values are
    # clamped, never converted to zero (unknown must remain unknown).
    return round(max(0.0, min(100.0, number)), 4)


def _empty_provider() -> dict[str, Any]:
    return {
        "available": False,
        "used_percent": None,
        "remaining_percent": None,
        "windows": [],
        "updated_at": None,
        "last_attempt_at": None,
        "source": None,
        "error": None,
        "models": [],
        "account": None,
    }


def _empty_snapshot() -> dict[str, Any]:
    return {"updated_at": None, "codex": _empty_provider(), "claude": _empty_provider()}


def _state_dir() -> Path:
    return app_state_dir()


def snapshot_path() -> Path:
    return _state_dir() / SNAPSHOT_NAME


@contextmanager
def _snapshot_lock(blocking: bool = True, watcher: bool = False):
    """Cross-process lock for the cache and the long-lived watch singleton."""
    global _lock_depth
    if _lock_depth and not watcher:
        yield True
        return
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / ("watch.lock" if watcher else SNAPSHOT_NAME + ".lock")).open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
                acquired = True
            except OSError:
                acquired = False
        if acquired and not watcher:
            _lock_depth += 1
        yield acquired
    finally:
        if acquired:
            if not watcher:
                _lock_depth -= 1
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _parse_raw_snapshot() -> dict[str, Any] | None:
    try:
        raw = json.loads(snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    result = _empty_snapshot()
    result.update({key: raw[key] for key in ("updated_at", "codex", "claude") if key in raw})
    for provider in ("codex", "claude"):
        if not isinstance(result.get(provider), dict):
            result[provider] = _empty_provider()
    return result


def _mark_stale(provider: dict[str, Any]) -> dict[str, Any]:
    value = dict(provider)
    updated = value.get("updated_at")
    timestamp = _timestamp(updated)
    stale = (timestamp is None or (timestamp - _utc_now()).total_seconds() > _MAX_FUTURE_OBSERVED_SECONDS
             or (_utc_now() - timestamp).total_seconds() > STALE_AFTER_SECONDS)
    if timestamp is not None and (timestamp - _utc_now()).total_seconds() > _MAX_FUTURE_OBSERVED_SECONDS:
        value["updated_at"] = None
        value["error"] = "invalid future usage snapshot"
    if stale:
        value["available"] = False
        value["error"] = value.get("error") or "stale usage snapshot"
    return value


def _read_snapshot() -> dict[str, Any] | None:
    result = _parse_raw_snapshot()
    if result is None:
        return None
    for provider in ("codex", "claude"):
        result[provider] = _mark_stale(result[provider])
    current_codex_account = _codex_identity()[1]
    stored_codex_account = result["codex"].get("account")
    if current_codex_account and stored_codex_account and current_codex_account != stored_codex_account:
        result["codex"] = _empty_provider()
        result["codex"]["error"] = "Codex account changed; refresh required"
    return result


def _write_snapshot(snapshot: Mapping[str, Any]) -> None:
    with _snapshot_lock() as acquired:
        if not acquired:
            return
        directory = _state_dir()
        current = _parse_raw_snapshot()
        merged = dict(snapshot)
        if current:
            for provider in ("codex", "claude"):
                candidate = merged.get(provider)
                existing = current.get(provider)
                if not isinstance(candidate, Mapping):
                    merged[provider] = existing
                    continue
                if isinstance(existing, Mapping):
                    try:
                        candidate_time = _observed_at(candidate.get("updated_at"))
                    except (TypeError, ValueError):
                        candidate_time = None
                    try:
                        existing_time = _observed_at(existing.get("updated_at"))
                    except (TypeError, ValueError):
                        existing_time = None
                    if existing_time is not None and (candidate_time is None or existing_time > candidate_time):
                        merged[provider] = existing
        # os.replace is atomic on the same Windows volume.  NamedTemporaryFile
        # is closed before replacement because Windows does not replace open files.
        fd, name = tempfile.mkstemp(prefix="snapshot.", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(merged, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, snapshot_path())
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _label(key: str, provider: str) -> str:
    normalized = key.lower().replace("-", "_").replace(" ", "_")
    known = {
        "five_hour": "5h",
        "fivehour": "5h",
        "primary": "5h",
        "seven_day": "7d",
        "sevenday": "7d",
        "weekly": "7d",
        "secondary": "7d",
    }
    if normalized in known:
        return known[normalized]
    return key.replace("_", " ").strip() or ("window" if provider == "codex" else "usage")


def _duration_label(minutes: Any) -> str | None:
    value = _number(minutes)
    if value is None or value <= 0:
        return None
    value = int(value)
    if value % (7 * 24 * 60) == 0:
        return f"{value // (7 * 24 * 60)}w"
    if value % (24 * 60) == 0:
        return f"{value // (24 * 60)}d"
    if value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def _window_from_mapping(value: Mapping[str, Any], key: str, provider: str) -> dict[str, Any] | None:
    used = _percent(value.get("used_percent"))
    if used is None:
        used = _percent(value.get("usedPercent"))
    if used is None:
        used = _percent(value.get("used_percentage"))
    if used is None:
        used = _percent(value.get("usedPercentage"))
    if used is None:
        used = _percent(value.get("utilization"))
    if used is None:
        used = _percent(value.get("used"))
    remaining = _percent(value.get("remaining_percent"))
    if remaining is None:
        remaining = _percent(value.get("remainingPercent"))
    if remaining is None:
        remaining = _percent(value.get("remaining"))
    if used is None and remaining is not None:
        used = round(100.0 - remaining, 4)
    if remaining is None and used is not None:
        remaining = round(100.0 - used, 4)
    reset = value.get("resets_at")
    if reset is None:
        reset = value.get("resetsAt")
    if reset is None:
        reset = value.get("reset_at")
    if reset is None:
        reset = value.get("resets_at_epoch")
    if reset is None:
        reset = value.get("resetAt")
    reset_after = value.get("reset_after_seconds")
    if reset_after is None:
        reset_after = value.get("resetAfterSeconds")
    if reset is None and _number(reset_after) is not None:
        reset = _utc_now().timestamp() + float(_number(reset_after))
    if used is None and remaining is None:
        # Keep an explicitly supplied quota bucket even when its provider
        # returned nulls.  The widget must distinguish unknown from 0% used.
        quota_keys = {"used_percent", "usedPercent", "used_percentage", "usedPercentage", "utilization", "used", "remaining_percent", "remainingPercent", "remaining", "resets_at", "resetsAt", "reset_at", "resets_at_epoch", "resetAfterSeconds", "reset_after_seconds", "window_duration_mins", "windowDurationMins"}
        generic = {"rate_limits", "rate_limit", "windows", "model_windows", "extra_usage", "spend"}
        if not key or key.isdigit() or key.lower().replace("-", "_").replace(" ", "_") in generic or not quota_keys.intersection(value):
            return None
    base_label = _label(str(value.get("label") or value.get("name") or key), provider)
    duration_label = _duration_label(value.get("window_duration_mins") or value.get("windowDurationMins"))
    if duration_label and key.lower() in {"primary", "secondary"}:
        base_label = duration_label
    bucket_id = value.get("limit_id") or value.get("limitId") or value.get("bucket_id") or value.get("bucketId")
    if bucket_id and str(bucket_id).lower() not in {"codex", "default"} and str(bucket_id) not in base_label:
        base_label = f"{bucket_id} {base_label}"
    return {
        "label": base_label,
        "used_percent": used,
        "remaining_percent": remaining,
        "resets_at": _iso(reset),
    }


def normalize_rate_limits(payload: Any, provider: str = "codex") -> list[dict[str, Any]]:
    """Normalize Claude/Codex quota shapes into the widget's windows list."""
    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def visit(value: Any, key: str = "", inherited_bucket_id: Any = None) -> None:
        if isinstance(value, Mapping):
            bucket_id = value.get("limit_id") or value.get("limitId") or value.get("bucket_id") or value.get("bucketId") or inherited_bucket_id
            candidate_value = value
            if bucket_id and not any(value.get(name) for name in ("limit_id", "limitId", "bucket_id", "bucketId")):
                candidate_value = dict(value)
                candidate_value["limitId"] = bucket_id
            candidate = _window_from_mapping(candidate_value, key, provider)
            if candidate is not None:
                identity = (candidate["label"], candidate["resets_at"])
                if identity not in seen:
                    seen.add(identity)
                    windows.append(candidate)
            for child_key, child in value.items():
                if child_key in {"models", "data", "credits"}:
                    continue
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child, str(child_key), bucket_id)
        elif isinstance(value, (list, tuple)):
            for child in value:
                child_key = ""
                if isinstance(child, Mapping):
                    child_key = str(child.get("label") or child.get("name") or "")
                visit(child, child_key, inherited_bucket_id)

    visit(payload)
    # Stable, useful ordering regardless of object key order.
    rank = {"5h": 0, "7d": 1}
    windows.sort(key=lambda item: (rank.get(item["label"], 2), item["label"]))
    return windows


def _provider_from_windows(windows: list[dict[str, Any]], *, error: str | None = None,
                           updated_at: str | None = None, models: list[str] | None = None,
                           account: str | None = None, source: str | None = None) -> dict[str, Any]:
    result = _empty_provider()
    result["windows"] = windows
    result["updated_at"] = updated_at
    result["source"] = source
    result["error"] = error
    result["models"] = models or []
    result["account"] = account
    known_windows = [window for window in windows if isinstance(window.get("used_percent"), (int, float)) or isinstance(window.get("remaining_percent"), (int, float))]
    if known_windows:
        result["available"] = True
        canonical = [window for window in known_windows if window.get("label") in {"5h", "7d", "1w"}]
        summary_windows = canonical or known_windows
        used_values = [window.get("used_percent") for window in summary_windows if isinstance(window.get("used_percent"), (int, float))]
        remaining_values = [window.get("remaining_percent") for window in summary_windows if isinstance(window.get("remaining_percent"), (int, float))]
        # Summary is the most constrained active window: max used and min left.
        result["used_percent"] = max(used_values) if used_values else None
        result["remaining_percent"] = min(remaining_values) if remaining_values else None
    return result


def _find_codex_executable() -> str | None:
    executable = find_codex()
    return str(executable) if executable else None


def _codex_identity() -> tuple[str | None, str | None]:
    """Read only the account id from Codex auth, returning a non-secret label."""
    try:
        raw = json.loads((Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8"))
        tokens = raw.get("tokens", {}) if isinstance(raw, Mapping) else {}
    except (OSError, ValueError, TypeError):
        return None, None
    account_id = tokens.get("account_id") if isinstance(tokens, Mapping) else None
    if not isinstance(account_id, str) or not account_id:
        for token_name in ("id_token", "access_token"):
            token = tokens.get(token_name) if isinstance(tokens, Mapping) else None
            if not isinstance(token, str) or token.count(".") != 2:
                continue
            try:
                segment = token.split(".")[1]
                segment += "=" * (-len(segment) % 4)
                claims = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
            except (ValueError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(claims, Mapping):
                nested = claims.get("https://api.openai.com/auth")
                account_id = nested.get("chatgpt_account_id") if isinstance(nested, Mapping) else claims.get("account_id")
                if isinstance(account_id, str) and account_id:
                    break
    if not isinstance(account_id, str) or not account_id:
        return None, None
    return account_id, _masked_account(account_id)


def _masked_account(value: str) -> str:
    return "acct-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _rpc_account_label(responses: Mapping[Any, Any]) -> str | None:
    response = responses.get(4) or responses.get("4")
    result = response.get("result") if isinstance(response, Mapping) else None
    if not isinstance(result, Mapping):
        return None
    for key in ("accountId", "account_id", "id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return _masked_account(value)
    return None


def _hidden_popen(command: list[str]) -> subprocess.Popen[str]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1,
    }
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        kwargs["startupinfo"] = startup
    return subprocess.Popen(command, **kwargs)


def _codex_app_server() -> tuple[Any, list[str], str | None]:
    executable = _find_codex_executable()
    if not executable:
        return None, [], "Codex executable not found"
    process = _hidden_popen([executable, "app-server", "--listen", "stdio://",
                             "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                             "-c", "features.multi_agent=false"])
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "mp-agent-dock", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "account/read", "params": {}},
    ]
    lines: list[str] = []
    reader: threading.Thread | None = None
    try:
        assert process.stdin is not None and process.stdout is not None
        payload = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)
        process.stdin.write(payload)
        process.stdin.flush()

        def read_lines() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)

        reader = threading.Thread(target=read_lines, name="codex-app-server-reader", daemon=True)
        reader.start()
        deadline = time.monotonic() + 8.0
        account_deadline: float | None = None
        responses: dict[int, Any] = {}
        while time.monotonic() < (account_deadline or deadline) and len(responses) < 3:
            while lines:
                line = lines.pop(0)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and isinstance(message.get("id"), int):
                    responses[message["id"]] = message
            if len(responses) >= 3:
                break
            if 2 in responses and account_deadline is None:
                # Rate limits are the useful part of this read. Give account/read
                # a short grace period without making a stuck server cost 8s.
                account_deadline = time.monotonic() + 1.0
            time.sleep(0.05)
        return responses, [], None if responses else "Codex app-server returned no response"
    except (OSError, ValueError) as exc:
        return None, [], f"Codex app-server error: {type(exc).__name__}"
    finally:
        try:
            process.kill()
        except OSError:
            pass
        if reader is not None:
            reader.join(timeout=1)


def _local_codex_cache(identity: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    path = Path.home() / ".codex" / ".codex-rate-limit-cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(data, Mapping):
        return [], None
    cached_identity = data.get("account_id")
    if identity and not cached_identity:
        # A cache from before account identity was recorded cannot safely be
        # attributed after a possible account switch.
        return [], None
    if identity and cached_identity and cached_identity != identity:
        return [], None
    observed = _observed_at(data.get("observed_at"))
    return normalize_rate_limits({"primary": data.get("primary"), "secondary": data.get("secondary")}, "codex"), observed


def read_codex_usage(rpc_responses: Mapping[Any, Any] | None = None) -> dict[str, Any]:
    """Read current Codex limits without changing auth or profile state."""
    identity, account_label = _codex_identity()
    responses, _, launch_error = (rpc_responses, [], None) if rpc_responses is not None else _codex_app_server()
    if isinstance(responses, dict):
        account_label = account_label or _rpc_account_label(responses)
        rate_response = responses.get(2) or responses.get("2")
        if "rateLimits" in responses or "rate_limits" in responses or (isinstance(responses.get("result"), Mapping) and ("rateLimits" in responses["result"] or "rate_limits" in responses["result"])):
            rate_response = {"result": responses}
        limits = rate_response.get("result") if isinstance(rate_response, Mapping) else None
        windows = normalize_rate_limits(limits, "codex")
        if windows:
            observed = limits.get("updated_at") or limits.get("updatedAt") or limits.get("observed_at") if isinstance(limits, Mapping) else None
            return _provider_from_windows(windows, updated_at=_observed_at(observed) or _iso(_utc_now().timestamp()), account=account_label, source="app-server")
        error = "Codex rate limits unavailable"
    else:
        error = launch_error or "Codex app-server returned no response"
    windows, observed = _local_codex_cache(identity)
    if windows:
        try:
            stale = observed is None or (_utc_now() - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds() > STALE_AFTER_SECONDS
        except ValueError:
            stale = True
        message = "stale local Codex cache" if stale else (error or "local Codex cache")
        result = _provider_from_windows(windows, updated_at=observed, error=message, account=account_label, source="local-cache")
        if stale:
            result["available"] = False
        return result
    return _provider_from_windows([], error=error)


def _credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def read_claude_usage() -> dict[str, Any]:
    """Read Claude Code OAuth usage (the endpoint is unofficial and read-only)."""
    if not claude_usage_enabled():
        return _provider_from_windows([], error="Claude OAuth usage is disabled; enable it in config.json")
    try:
        raw = json.loads(_credentials_path().read_text(encoding="utf-8"))
        token = raw.get("claudeAiOauth", {}).get("accessToken") if isinstance(raw, Mapping) else None
    except (OSError, ValueError, TypeError):
        token = None
    if not isinstance(token, str) or not token:
        return _provider_from_windows([], error="Claude OAuth credentials unavailable")
    request = Request(CLAUDE_USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Content-Type": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return _provider_from_windows([], error=f"Claude usage HTTP {exc.code}")
    except (URLError, TimeoutError):
        return _provider_from_windows([], error="Claude usage network error")
    except (OSError, ValueError, TypeError):
        return _provider_from_windows([], error="Claude usage response invalid")
    windows = normalize_rate_limits(payload, "claude")
    return _provider_from_windows(windows, updated_at=_iso(_utc_now().timestamp()), error=None, source="oauth")


def store_claude_statusline(value: Any) -> dict[str, Any]:
    """Persist rate_limits supplied by Claude Code's statusline hook."""
    payload = value if isinstance(value, Mapping) else {}
    limits = payload.get("rate_limits", payload)
    windows = normalize_rate_limits(limits, "claude")
    observed = _iso(_utc_now().timestamp())
    provider = _provider_from_windows(windows, updated_at=observed, error=None if windows else "Claude statusline has no rate limits yet", source="statusline")
    provider["last_attempt_at"] = observed
    snapshot = _read_snapshot() or _empty_snapshot()
    snapshot["claude"] = provider
    snapshot["updated_at"] = _iso(_utc_now().timestamp())
    _write_snapshot(snapshot)
    return provider


def _statusline_text(provider: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for window in provider.get("windows", []):
        if not isinstance(window, Mapping):
            continue
        label = str(window.get("label") or "limit")
        used = window.get("used_percent")
        if isinstance(used, (int, float)):
            parts.append(f"{label} {used:g}% used")
        else:
            parts.append(f"{label} ? used")
    return "Claude " + " · ".join(parts) if parts else "Claude usage unavailable"


def get_snapshot(refresh: bool = False) -> dict[str, Any]:
    """Return the sanitized widget snapshot; optionally refresh both providers."""
    cached = _read_snapshot()
    if not refresh:
        if cached is not None:
            return cached
        result = _empty_snapshot()
        result["codex"]["error"] = "usage cache unavailable"
        result["claude"]["error"] = "usage cache unavailable"
        return result
    now = time.monotonic()
    attempted_at = _iso(_utc_now().timestamp())
    snapshot = cached or _empty_snapshot()
    for name, reader in (("codex", read_codex_usage), ("claude", read_claude_usage)):
        if name == "claude" and not claude_usage_enabled():
            current = snapshot.get("claude")
            if isinstance(current, Mapping) and current.get("available"):
                continue  # Preserve the local statusline-only fallback.
        if now < _failure_until.get(name, 0):
            current = snapshot.get(name, _empty_provider())
            current = dict(current) if isinstance(current, Mapping) else _empty_provider()
            current["error"] = "refresh backoff active"
            snapshot[name] = current
            continue
        try:
            value = reader()
        except Exception as exc:  # defensive boundary for the widget process
            value = _provider_from_windows([], error=f"{name} collector error: {type(exc).__name__}")
        previous = snapshot.get(name)
        if not value.get("available") and isinstance(previous, Mapping) and previous.get("windows"):
            # A failed attempt must remain visible, but must not erase the last
            # real observation or turn unknown quota into a synthetic zero.
            failure_error = value.get("error")
            value = dict(previous)
            value["error"] = failure_error or "usage refresh failed"
            value = _mark_stale(value)
        value["last_attempt_at"] = attempted_at
        snapshot[name] = value
        if value.get("available"):
            _failure_until.pop(name, None)
        else:
            _failure_until[name] = now + _FAILURE_BACKOFF_SECONDS
    snapshot["updated_at"] = _iso(_utc_now().timestamp())
    _write_snapshot(snapshot)
    return snapshot


def _print_snapshot(snapshot: Mapping[str, Any]) -> None:
    if sys.stdout is None:
        return
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MP Agent Dock usage collector")
    parser.add_argument("--once", action="store_true", help="refresh once and print sanitized JSON")
    parser.add_argument("--watch", action="store_true", help="refresh about every 60 seconds")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("statusline", help="consume Claude Code statusline JSON on stdin")
    args = parser.parse_args(argv)
    if args.command == "statusline":
        try:
            value = json.load(sys.stdin)
        except (ValueError, OSError):
            value = {}
        provider = store_claude_statusline(value)
        # Claude statusline commands are expected to emit human-readable text;
        # the JSON cache remains available to the desktop widget.
        if sys.stdout is not None:
            print(_statusline_text(provider))
        return 0
    if args.watch:
        with _snapshot_lock(blocking=False, watcher=True) as owned:
            if not owned:
                if sys.stderr is not None:
                    print("usage watcher already running", file=sys.stderr)
                return 0
            while True:
                _print_snapshot(get_snapshot(refresh=True))
                time.sleep(WATCH_INTERVAL)
    _print_snapshot(get_snapshot(refresh=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
