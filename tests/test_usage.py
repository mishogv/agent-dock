from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "usage.py"
SPEC = importlib.util.spec_from_file_location("agent_dock_usage", MODULE_PATH)
assert SPEC and SPEC.loader
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._environment = patch.dict(os.environ, {
            "MP_AGENT_DOCK_STATE_DIR": self._temporary_directory.name,
        }, clear=False)
        self._environment.start()
        self._path_home = patch.object(Path, "home", return_value=Path(self._temporary_directory.name))
        self._path_home.start()
        self._claude_usage_enabled = patch.object(usage, "claude_usage_enabled", return_value=False)
        self._claude_usage_enabled.start()
        self.assertEqual(usage._state_dir(), Path(self._temporary_directory.name))

    def tearDown(self) -> None:
        self._claude_usage_enabled.stop()
        self._path_home.stop()
        self._environment.stop()
        self._temporary_directory.cleanup()

    def test_normalize_nullable_and_multiple_quota_buckets(self) -> None:
        payload = {
            "primary": {"used_percent": 12.5, "resets_at": 1_800_000_000},
            "secondary": {"remaining_percent": 40, "resets_at": None},
            "model_windows": [
                {"label": "opus", "utilization": None, "resets_at": None},
                {"label": "sonnet", "utilization": 4},
            ],
        }
        windows = usage.normalize_rate_limits(payload)
        by_label = {item["label"]: item for item in windows}
        self.assertEqual(by_label["5h"]["used_percent"], 12.5)
        self.assertEqual(by_label["5h"]["remaining_percent"], 87.5)
        self.assertEqual(by_label["7d"]["used_percent"], 60.0)
        self.assertIsNone(by_label["opus"]["used_percent"])
        self.assertEqual(by_label["sonnet"]["remaining_percent"], 96.0)

    def test_codex_camel_case_duration_and_bucket_ids_are_preserved(self) -> None:
        windows = usage.normalize_rate_limits({
            "rateLimitsByLimitId": {
                "team-a": {"limitId": "team-a", "primary": {"usedPercent": 20, "windowDurationMins": 90, "resetsAt": 1_800_000_000}},
                "team-b": {"limitId": "team-b", "primary": {"usedPercent": 80, "windowDurationMins": 90, "resetsAt": 1_800_000_000}},
            }
        })
        self.assertEqual({window["label"] for window in windows}, {"team-a 90m", "team-b 90m"})
        provider = usage._provider_from_windows(windows)
        self.assertEqual(provider["used_percent"], 80.0)
        self.assertEqual(provider["remaining_percent"], 20.0)

    def test_claude_statusline_used_percentage_is_supported(self) -> None:
        windows = usage.normalize_rate_limits({"five_hour": {"used_percentage": 33}}, "claude")
        self.assertEqual(windows[0]["used_percent"], 33.0)

    def test_disabled_claude_oauth_makes_no_network_request(self) -> None:
        with patch.object(usage, "claude_usage_enabled", return_value=False), patch.object(usage, "urlopen") as request:
            result = usage.read_claude_usage()
        request.assert_not_called()
        self.assertFalse(result["available"])
        self.assertIn("disabled", result["error"])

    def test_codex_process_strips_api_environment(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "AZURE_OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://example.invalid", "KEEP": "yes"}, clear=False), patch.object(usage.subprocess, "Popen") as popen:
            usage._hidden_popen(["codex.exe", "app-server"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["KEEP"], "yes")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("AZURE_OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)

    def test_nullable_only_windows_are_not_available(self) -> None:
        provider = usage._provider_from_windows([{"label": "5h", "used_percent": None, "remaining_percent": None, "resets_at": None}])
        self.assertFalse(provider["available"])
        self.assertIsNone(provider["used_percent"])

    def test_canonical_windows_drive_summary_over_model_pool(self) -> None:
        provider = usage._provider_from_windows([
            {"label": "5h", "used_percent": 30, "remaining_percent": 70, "resets_at": None},
            {"label": "7d", "used_percent": 20, "remaining_percent": 80, "resets_at": None},
            {"label": "opus 5h", "used_percent": 100, "remaining_percent": 0, "resets_at": None},
        ])
        self.assertEqual(provider["used_percent"], 30)
        self.assertEqual(provider["remaining_percent"], 70)

    def test_one_week_canonical_window_constrains_summary(self) -> None:
        provider = usage._provider_from_windows([
            {"label": "5h", "used_percent": 46, "remaining_percent": 54, "resets_at": None},
            {"label": "1w", "used_percent": 62, "remaining_percent": 38, "resets_at": None},
        ])
        self.assertEqual(provider["used_percent"], 62)
        self.assertEqual(provider["remaining_percent"], 38)

    def test_statusline_writes_atomic_sanitized_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MP_AGENT_DOCK_STATE_DIR": directory}, clear=False):
            result = usage.store_claude_statusline({"rate_limits": {"five_hour": {"utilization": 22, "resets_at": "2030-01-01T00:00:00Z"}}})
            self.assertTrue(result["available"])
            path = Path(directory) / "snapshot.json"
            self.assertTrue(path.is_file())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"updated_at", "codex", "claude"})
            self.assertEqual(saved["claude"]["used_percent"], 22.0)
            self.assertNotIn("token", path.read_text(encoding="utf-8").lower())

    def test_statusline_merge_keeps_newer_codex_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MP_AGENT_DOCK_STATE_DIR": directory}, clear=False):
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            snapshot = usage._empty_snapshot()
            snapshot["updated_at"] = now
            snapshot["codex"] = usage._provider_from_windows([{"label": "5h", "used_percent": 44, "remaining_percent": 56, "resets_at": None}], updated_at=now, account="acct-current")
            usage._write_snapshot(snapshot)
            usage.store_claude_statusline({"rate_limits": {"five_hour": {"used_percentage": 3}}})
            saved = usage._parse_raw_snapshot()
            self.assertEqual(saved["codex"]["used_percent"], 44)
            self.assertEqual(saved["codex"]["account"], "acct-current")

    def test_future_snapshot_does_not_win_freshness_merge(self) -> None:
        future = usage._empty_snapshot()
        future["codex"] = usage._provider_from_windows(
            [{"label": "5h", "used_percent": 90, "remaining_percent": 10, "resets_at": None}],
            updated_at="2030-01-01T00:00:00Z", account="acct-old")
        usage.snapshot_path().parent.mkdir(parents=True, exist_ok=True)
        usage.snapshot_path().write_text(json.dumps(future), encoding="utf-8")
        fresh = usage._empty_snapshot()
        fresh["codex"] = usage._provider_from_windows(
            [{"label": "5h", "used_percent": 20, "remaining_percent": 80, "resets_at": None}],
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), account="acct-current")
        usage._write_snapshot(fresh)
        saved = usage._parse_raw_snapshot()
        self.assertEqual(saved["codex"]["used_percent"], 20)
        self.assertEqual(saved["codex"]["account"], "acct-current")

    def test_malformed_naive_and_future_timestamps_are_safe(self) -> None:
        malformed = usage._mark_stale({"available": True, "windows": [], "updated_at": "not-a-date"})
        self.assertFalse(malformed["available"])
        self.assertEqual(malformed["error"], "stale usage snapshot")
        naive = usage._utc_now().replace(tzinfo=None, microsecond=0).isoformat()
        self.assertTrue(usage._mark_stale({"available": True, "windows": [], "updated_at": naive})["available"])
        future = usage._mark_stale({"available": True, "windows": [], "updated_at": "2030-01-01T00:00:00Z"})
        self.assertFalse(future["available"])
        self.assertIsNone(future["updated_at"])
        self.assertEqual(future["error"], "invalid future usage snapshot")

    def test_refresh_failure_keeps_last_observation_and_records_attempt(self) -> None:
        observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        cached = usage._empty_snapshot()
        cached["codex"] = usage._provider_from_windows(
            [{"label": "5h", "used_percent": 20, "remaining_percent": 80, "resets_at": None}],
            updated_at=observed, source="app-server")
        usage._write_snapshot(cached)
        with patch.object(usage, "read_codex_usage", return_value=usage._provider_from_windows([], error="offline")), patch.object(usage, "read_claude_usage", return_value=usage._provider_from_windows([], error="offline")):
            usage._failure_until.clear()
            result = usage.get_snapshot(refresh=True)["codex"]
        self.assertEqual(result["updated_at"], observed)
        self.assertIsNotNone(result["last_attempt_at"])
        self.assertEqual(result["source"], "app-server")
        self.assertEqual(result["error"], "offline")

    def test_stale_codex_cache_is_not_reported_as_zero_or_available(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        cache = {"observed_at": old, "primary": {"used_percent": 58}, "secondary": {"used_percent": 40}}
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)), patch.object(usage, "_codex_app_server", return_value=(None, [], "offline")):
            codex = Path(directory) / ".codex"
            codex.mkdir()
            (codex / ".codex-rate-limit-cache.json").write_text(json.dumps(cache), encoding="utf-8")
            result = usage.read_codex_usage()
            self.assertFalse(result["available"])
            self.assertEqual(result["used_percent"], 58.0)
            self.assertEqual(result["windows"][0]["used_percent"], 58.0)
            self.assertIn("stale", result["error"])

    def test_corrupt_cache_is_clearly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MP_AGENT_DOCK_STATE_DIR": directory}, clear=False):
            (Path(directory) / "snapshot.json").write_text("not-json", encoding="utf-8")
            snapshot = usage.get_snapshot(refresh=False)
            self.assertIsNone(snapshot["updated_at"])
            self.assertFalse(snapshot["codex"]["available"])
            self.assertIn("cache", snapshot["codex"]["error"])
            self.assertFalse(snapshot["claude"]["available"])

    def test_refresh_failure_backoff_does_not_emit_unknown_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MP_AGENT_DOCK_STATE_DIR": directory}, clear=False), patch.object(usage, "read_codex_usage", return_value=usage._provider_from_windows([], error="offline")), patch.object(usage, "read_claude_usage", return_value=usage._provider_from_windows([], error="offline")):
            usage._failure_until.clear()
            first = usage.get_snapshot(refresh=True)
            second = usage.get_snapshot(refresh=True)
            self.assertIsNone(first["codex"]["used_percent"])
            self.assertIsNone(second["codex"]["used_percent"])
            self.assertEqual(second["codex"]["error"], "refresh backoff active")

    def test_codex_account_switch_does_not_use_unattributed_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            codex = Path(directory) / ".codex"
            codex.mkdir()
            (codex / ".codex-rate-limit-cache.json").write_text(json.dumps({"account_id": "old", "observed_at": "2030-01-01T00:00:00Z", "primary": {"used_percent": 90}}), encoding="utf-8")
            self.assertEqual(usage._local_codex_cache("new"), ([], None))

    def test_snapshot_hides_limits_after_account_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MP_AGENT_DOCK_STATE_DIR": directory}, clear=False), patch.object(usage, "_codex_identity", return_value=("new", "acct-new")):
            snapshot = usage._empty_snapshot()
            snapshot["codex"] = usage._provider_from_windows([{"label": "5h", "used_percent": 90, "remaining_percent": 10, "resets_at": None}], updated_at="2030-01-01T00:00:00Z", account="acct-old")
            usage._write_snapshot(snapshot)
            codex = usage.get_snapshot(refresh=False)["codex"]
            self.assertFalse(codex["available"])
            self.assertEqual(codex["windows"], [])
            self.assertIn("account changed", codex["error"])

    def test_rpc_result_parses_usage_without_live_provider_calls(self) -> None:
        result = usage.read_codex_usage({"result": {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300}}}})
        self.assertTrue(result["available"])
        self.assertEqual(result["used_percent"], 12.0)


if __name__ == "__main__":
    unittest.main()
