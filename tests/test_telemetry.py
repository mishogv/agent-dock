import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
import telemetry


class TelemetryTests(unittest.TestCase):
    def test_unknown_claude_status_stays_unknown(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(telemetry, "CLAUDE_HOME", Path(td)), mock.patch.object(telemetry, "_alive", return_value=True):
            folder = Path(td) / "sessions"; folder.mkdir()
            (folder / "live.json").write_text(json.dumps({"kind": "interactive", "sessionId": "live", "pid": 44}), encoding="utf-8")
            self.assertEqual("unknown", telemetry._claude_sessions()[0]["status"])

    def test_system_and_session_refresh_intervals(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(telemetry, "_system_cache", None), \
             mock.patch.object(telemetry, "_sessions_cache", None), \
             mock.patch.object(telemetry.time, "monotonic", side_effect=[100, 104, 105, 115]), \
             mock.patch.object(telemetry, "_system", return_value={}) as system, \
             mock.patch.object(telemetry, "_claude_sessions", return_value=[]) as sessions, \
             mock.patch.object(telemetry, "_codex_processes", return_value=[]):
            output = Path(td) / "telemetry.json"
            for _ in range(4): telemetry.collect_once(output)
            self.assertEqual(3, system.call_count)
            self.assertEqual(2, sessions.call_count)
            self.assertEqual([], list(Path(td).glob("*.writing")))

    def test_stale_claude_pid_is_excluded(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(telemetry, "CLAUDE_HOME", Path(td)), mock.patch.object(telemetry, "_alive", return_value=False):
            folder = Path(td) / "sessions"; folder.mkdir()
            (folder / "stale.json").write_text(json.dumps({"kind": "interactive", "sessionId": "stale", "pid": 44, "cwd": "C:/work"}), encoding="utf-8")
            self.assertEqual([], telemetry._claude_sessions())

    def test_unavailable_system_values_stay_null(self):
        with mock.patch.object(telemetry, "_cpu_percent", return_value=None), mock.patch.object(telemetry.os, "name", "posix"):
            system = telemetry._system()
            self.assertIsNone(system["cpu_percent"])
            self.assertIsNone(system["ram_total_gb"])

    def test_cpu_delta_uses_two_samples(self):
        class FT:
            def __init__(self, value): self.dwHighDateTime, self.dwLowDateTime = 0, value
        class K:
            def __init__(self): self.i = 0
            def GetSystemTimes(self, idle, kernel, user):
                self.i += 1; idle._obj.dwLowDateTime = self.i * 10; kernel._obj.dwLowDateTime = self.i * 60; user._obj.dwLowDateTime = self.i * 40; return 1
        fake = K()
        with mock.patch.object(telemetry.os, "name", "nt"), mock.patch.object(telemetry.ctypes, "WinDLL", return_value=fake), mock.patch.object(telemetry, "_cpu_previous", None):
            self.assertIsNone(telemetry._cpu_percent())
            self.assertEqual(90.0, telemetry._cpu_percent())

    def test_codex_processes_count_only_native_cli(self):
        payload = [
            {"Name": "cmd.exe", "ProcessId": 1, "CommandLine": "codex.cmd"},
            {"Name": "node.exe", "ProcessId": 2, "CommandLine": "codex.js"},
            {"Name": "codex.exe", "ProcessId": 3, "CommandLine": "codex.exe"},
            {"Name": "codex.exe", "ProcessId": 4, "CommandLine": "codex.exe app-server"},
            {"Name": "codex.exe", "ProcessId": 5, "CommandLine": "codex.exe --version"},
        ]
        class Result: stdout = json.dumps(payload)
        with mock.patch.object(telemetry.os, "name", "nt"), mock.patch.object(telemetry.subprocess, "run", return_value=Result()):
            rows = telemetry._codex_processes()
        self.assertEqual([3], [row["pid"] for row in rows])
        self.assertEqual("unknown", rows[0]["status"])

    def test_current_pid_requires_matching_creation_filetime(self):
        if os.name != "nt":
            self.skipTest("FILETIME identity is Windows-specific")
        started = telemetry._started(os.getpid())
        self.assertTrue(started)
        self.assertTrue(telemetry._alive(os.getpid(), started))
        self.assertFalse(telemetry._alive(os.getpid(), str(int(started) + 1)))

    def test_public_telemetry_has_no_bridge_or_task_reader(self):
        source = Path(telemetry.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from bridge import", source)
        self.assertNotIn('ROOT / "tasks"', source)


if __name__ == "__main__": unittest.main()
