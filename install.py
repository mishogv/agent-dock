"""Set up the standalone widget; never change Claude settings or register tools."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import config

ROOT = Path(__file__).resolve().parent
STATE = config.app_state_dir()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def install(*, startup: bool = False, dry_run: bool = False,
            codex: str | None = None, enable_claude_usage: bool = False) -> dict:
    if os.name != "nt" or sys.version_info < (3, 10):
        raise RuntimeError("Requires Windows and Python 3.10+.")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file() or not shutil.which("powershell.exe"):
        raise ValueError("Requires pythonw.exe and Windows PowerShell on PATH.")
    configuration = read_json(STATE / "config.json")
    codex_path = Path(codex).expanduser().resolve() if codex else config.find_codex()
    if codex and (not codex_path.is_file() or codex_path.suffix.lower() != ".exe"):
        raise ValueError("--codex must point to a native codex.exe, not a wrapper.")
    if codex_path:
        configuration["codex_executable"] = str(codex_path)
    configuration["enable_claude_usage"] = enable_claude_usage
    result = {"installed": False, "dry_run": dry_run, "source": str(ROOT),
              "edition": "widget", "startup": startup,
              "claude_usage_opt_in": enable_claude_usage,
              "codex_available": codex_path is not None,
              "warnings": [] if codex_path else ["Codex CLI not found; its usage will be unknown."]}
    if dry_run:
        return result
    backup = STATE / "backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "installation.json"):
        path = STATE / name
        if path.exists():
            shutil.copy2(path, backup / name)
    (STATE / "config.json").write_text(json.dumps(configuration, indent=2) + "\n", encoding="utf-8")
    if startup:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, "MPAgentDock", 0, winreg.REG_SZ, f'"{pythonw}" "{ROOT / "launcher.py"}"')
    result.update(installed=True, backup=str(backup))
    (STATE / "installation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Optional native Codex .exe path")
    parser.add_argument("--startup", action="store_true", help="Start at Windows login")
    parser.add_argument("--enable-claude-usage", action="store_true", help="Opt in to unofficial Claude OAuth usage reads")
    parser.add_argument("--dry-run", action="store_true", help="Validate without changes")
    args = parser.parse_args()
    try:
        print(json.dumps(install(startup=args.startup, dry_run=args.dry_run,
                                 codex=args.codex, enable_claude_usage=args.enable_claude_usage), indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
