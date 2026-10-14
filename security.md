# Security and privacy

Agent Dock is a local experimental Windows usage widget, not a security boundary. Use it only on a trusted Windows account. It does not bridge Claude and Codex, execute prompts, delegate work, create an MCP server, or modify Claude/Codex settings, hooks, skills, statuslines, or project files.

## Provider access

- Codex usage comes from the local native CLI app-server. Polling is read-only, performs no inference, does not rotate accounts, and never falls back to paid API billing. Without a usable CLI or a valid observation, Codex usage is unknown.
- Claude usage polling is off by default. `--enable-claude-usage` authorizes use of the local Claude OAuth credential only with Anthropic's unofficial, read-only usage endpoint. This is not a public supported API and may change or stop working. Agent Dock does not refresh, print, export, or otherwise reuse the credential. Keep it disabled if policy does not permit this access.
- Missing, stale, invalid, or future-dated observations are displayed as unknown, never as zero or an estimated value.

## Local state

Agent Dock may retain its own configuration, installation record, backups, lifecycle/error logs, usage snapshots, executable paths, session titles/project names, process IDs/start times, and system telemetry under `%LOCALAPPDATA%\MPAgentDock`. These can reveal private machine, account-usage, and path information. They inherit the Windows user's directory permissions; they are not additionally encrypted and there is no automatic retention cleanup. Never publish this directory, provider credential stores, or diagnostic captures containing private information.

System telemetry reads CPU, RAM, fixed-disk capacity, and process metadata needed to identify observable local CLI sessions. Command lines are used locally for classification and are not included in telemetry snapshots. There is no analytics or remote telemetry service. Lifecycle logs do not contain conversation contents.

## Installation boundary

The installer may write only Agent Dock's local state and, with explicit `--startup`, its own HKCU Run value. It never edits Claude settings/hooks/MCP, Claude skills, Codex settings, or user projects. It deliberately leaves older private integrations alone; review and remove those manually if wanted. Do not run the installer as administrator.

## Reporting

Use GitHub's private vulnerability reporting on this repository if available. Otherwise open an issue requesting a private contact without exploit details, credentials, private code, logs, or local state. The maintainer will coordinate privately.
