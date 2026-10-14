"""Read-only widget diagnostics. No login, inference or credential output."""
import json
import os
from pathlib import Path
import shutil
import sys
import config


def diagnose() -> dict:
    checks = []
    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    check("windows", os.name == "nt", "Native WPF requires Windows")
    check("python", sys.version_info >= (3, 10), "Python 3.10+ required")
    check("pythonw", Path(sys.executable).with_name("pythonw.exe").is_file(), "Hidden background processes")
    check("powershell", shutil.which("powershell.exe"), "Windows PowerShell required on PATH")
    providers = {}
    try:
        configuration = config.load_config()
        check("configuration", True, "Valid local configuration; defaults work without setup")
        providers = {"codex_cli_found": config.find_codex() is not None,
                     "claude_usage_opt_in": configuration.get("enable_claude_usage") is True}
    except (OSError, ValueError, RuntimeError) as exc:
        check("configuration", False, type(exc).__name__ + ": invalid or unavailable local configuration")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "providers": providers,
            "notes": ["Provider data is optional; missing sources display unknown.",
                      "No authentication or quota freshness is verified by doctor.",
                      "No MCP server, delegation hooks or skills are installed.",
                      "Desktop pinning/navigation: Windows 10 builds 17763-19045 only."]}


if __name__ == "__main__":
    result = diagnose()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
