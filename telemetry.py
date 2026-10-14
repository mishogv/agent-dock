"""Small, local-only Agent Dock telemetry snapshot.

The collector intentionally reports metadata, never command lines or transcript text.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
from config import app_state_dir

ROOT = app_state_dir()
OUTPUT = ROOT / "telemetry.json"
CLAUDE_HOME = Path.home() / ".claude"
_cpu_previous: tuple[int, int] | None = None
_system_cache: tuple[float, dict[str, Any]] | None = None
_sessions_cache: tuple[float, list[dict[str, Any]]] | None = None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _cpu_percent() -> float | None:
    global _cpu_previous
    if os.name != "nt":
        return None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        idle = wintypes.FILETIME(); kernel = wintypes.FILETIME(); user = wintypes.FILETIME()
        if not k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        def val(x: wintypes.FILETIME) -> int: return (x.dwHighDateTime << 32) | x.dwLowDateTime
        total = val(kernel) + val(user); idle_n = val(idle)
        old = _cpu_previous; _cpu_previous = (total, idle_n)
        if not old or total <= old[0]: return None
        return round(max(0.0, min(100.0, 100.0 * (1 - (idle_n - old[1]) / (total - old[0])))), 1)
    except (OSError, AttributeError):
        return None


def _system() -> dict[str, Any]:
    ram_used = ram_total = ram_percent = None
    if os.name == "nt":
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            class Mem(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong), ("virt", ctypes.c_ulonglong),
                            ("avail_virt", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]
            m = Mem(); m.length = ctypes.sizeof(m)
            if k.GlobalMemoryStatusEx(ctypes.byref(m)):
                ram_total = round(m.total / 2**30, 2); ram_used = round((m.total - m.avail) / 2**30, 2); ram_percent = round((m.total - m.avail) * 100 / m.total, 1)
        except (OSError, AttributeError): pass
    disks = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        try:
            if os.name == "nt":
                k = ctypes.WinDLL("kernel32", use_last_error=True)
                k.GetDriveTypeW.restype = wintypes.UINT
                if k.GetDriveTypeW(drive) != 3:  # DRIVE_FIXED
                    continue
            usage = shutil.disk_usage(drive)
            disks.append({"drive": drive, "free_gb": round(usage.free / 2**30, 2), "total_gb": round(usage.total / 2**30, 2), "used_percent": round((usage.total - usage.free) * 100 / usage.total, 1)})
        except OSError: pass
    return {"cpu_percent": _cpu_percent(), "ram_used_gb": ram_used, "ram_total_gb": ram_total, "ram_percent": ram_percent, "disks": disks}


def _alive(pid: Any, start: Any = None) -> bool:
    try: pid = int(pid)
    except (TypeError, ValueError): return False
    if pid <= 0: return False
    try:
        if os.name != "nt": os.kill(pid, 0); return True
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return False
            if start is None:
                return True
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return True  # Cannot disprove identity; retain the conservative semantics.
            created = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            return created == str(start)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, ImportError): return False


def _project(cwd: str) -> str:
    p = Path(cwd)
    return p.name or str(p)


def _started(pid: int) -> str | None:
    if os.name != "nt":
        return None
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    except (OSError, AttributeError):
        return None


def _claude_sessions() -> list[dict[str, Any]]:
    rows = []
    for file in (CLAUDE_HOME / "sessions").glob("*.json"):
        try: row = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        if not isinstance(row, dict) or row.get("kind") != "interactive" or not _alive(row.get("pid"), row.get("procStart")): continue
        cwd = str(row.get("cwd") or "")
        status = row.get("status")
        rows.append({"provider": "claude", "id": str(row.get("sessionId") or file.stem)[:80], "title": str(row.get("name") or "")[:120], "project": _project(cwd), "status": "active" if status == "busy" else ("idle" if status == "idle" else "unknown"), "pid": int(row.get("pid")), "started_at": row.get("startedAt"), "process_started": _started(int(row["pid"]))})
    return rows


def _codex_processes() -> list[dict[str, Any]]:
    """Read-only process inventory; command lines are used only for classification and never emitted."""
    if os.name != "nt": return []
    script = "Get-CimInstance Win32_Process | Select-Object Name,ProcessId,CommandLine | ConvertTo-Json -Compress"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, errors="replace", timeout=8, creationflags=flags).stdout
        data = json.loads(raw) if raw.strip() else []
        if isinstance(data, dict): data = [data]
    except (OSError, ValueError, subprocess.TimeoutExpired): return []
    rows = []
    for item in data:
        name, cmd = str(item.get("Name") or "").lower(), str(item.get("CommandLine") or "").lower()
        pid = item.get("ProcessId")
        if not pid or "codex" not in (name + " " + cmd) or "app-server" in cmd or "telemetry.py" in cmd: continue
        # Native codex.exe is the session process; cmd/node wrappers are not.
        if name != "codex.exe" or not cmd or any(flag in cmd for flag in ("app-server", "mcp-server", "--type", "--version", "--help", " login", " logout")): continue
        rows.append({"provider": "codex", "id": f"pid-{pid}", "title": f"Codex CLI · PID {pid}", "project": "", "status": "unknown", "pid": int(pid), "process_started": _started(int(pid))})
    return rows


def collect_once(output: Path | str = OUTPUT) -> dict[str, Any]:
    global _system_cache, _sessions_cache
    now = time.monotonic()
    if not _system_cache or now - _system_cache[0] >= 5: _system_cache = (now, _system())
    if not _sessions_cache or now - _sessions_cache[0] >= 15: _sessions_cache = (now, _claude_sessions() + _codex_processes())
    sessions = _sessions_cache[1]
    sessions = sorted(sessions, key=lambda s: (s.get("status") != "active", s["provider"], s["id"]))
    snapshot = {"updated_at": _now(), "system": _system_cache[1], "sessions": sessions, "session_counts": {"claude": sum(s["provider"] == "claude" for s in sessions), "codex": sum(s["provider"] == "codex" for s in sessions)}, "notes": ["Codex: броим само откритите Codex CLI процеси. Разговорите в desktop/VS Code не са включени.", "За Codex CLI статусът и моделът са неизвестни, когато няма надеждна връзка с конкретен разговор."]}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, prefix=output.name + ".", suffix=".writing", delete=False) as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False, indent=2)); temp = Path(fh.name)
    try:
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--once", action="store_true"); parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.once or not args.watch: collect_once(); return
    lock = ROOT / "telemetry.watch.lock"; ROOT.mkdir(parents=True, exist_ok=True)
    fh = lock.open("a+b")
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            try: msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: return
        else:
            import fcntl
            try: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: return
        while True:
            try:
                collect_once()
            except OSError:
                pass  # Keep previous snapshot; the widget marks it stale.
            time.sleep(5)
    finally: fh.close()


if __name__ == "__main__": main()
