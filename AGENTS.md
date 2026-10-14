# Working on / setting up Agent Dock

Read README.md and security.md before setup. Agent Dock v0.2.0 is a standalone Windows usage widget: Python standard library, native Windows PowerShell/WPF, and optional locally installed provider CLIs. It has no project/workspace setup, no bridge, no agent execution, no delegation, no server, no MCP integration, and no paid API key.

## Setup for another user

1. Confirm Windows x64, Python 3.10+ with `pythonw`, PowerShell 5.1, and Git. Codex and Claude CLIs are optional sources of usage data; do not install unrelated packages, change PATH blindly, sign in, switch accounts, print tokens, or automate purchases.
2. Explain the exact installer interface: `python install.py [--dry-run] [--startup] [--enable-claude-usage] [--codex C:\path\to\codex.exe]`. Run `python install.py --dry-run` before an installation.
3. Ask whether the user explicitly wants the Agent Dock startup entry and unofficial read-only Claude usage polling. `--codex` is only a native `.exe` discovery override. A missing optional provider is unknown usage, not failure.
4. Explain that setup writes only Agent Dock `config.json`, `installation.json`, backups, and optionally its own HKCU Run value. It must not edit Claude settings, hooks, MCP, statuslines, skills, Codex configuration, or projects. Older private integrations are preserved, not automatically removed.
5. After authorization, run the selected command, `python doctor.py`, the offline test suite, and widget `-ValidateOnly` from README. Start the visible widget only when requested. Doctor neither authenticates providers nor performs inference.
6. Report exact results and limitations. Unknown quota, unavailable provider, or unsupported desktop behavior is not success.

## Development

- Make small, offline-testable changes. Keep `widget.ps1` UTF-8 with BOM for Windows PowerShell 5.1 Bulgarian labels.
- Preserve read-only native Codex app-server usage polling and its no-inference guarantee. Keep Claude OAuth polling opt-in, read-only, and honestly unofficial.
- Treat stale, missing, impossible, and future observations as unknown; never invent quota, reset, or session data.
- Keep CPU/RAM/fixed-disk/process data local. Hide background subprocess windows; do not move a user's console between desktops. Unsupported Windows desktop COM layouts must fail safely rather than guessing interface versions.
- Do not add, publish, or reintroduce any bridge, server, execution/delegation path, Claude hook, MCP registration, Claude skill, statusline integration, or modification of Claude/Codex settings. Do not publish local state, credentials, snapshots, logs, paths, or screenshots of real sessions.
- Run `python -m unittest discover -s tests -p "test_*.py" -v` and the widget validation command before shipping. Tests/CI must not perform inference, network credential polling, startup registry writes, or visible widget launches.
