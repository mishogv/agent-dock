from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config


class ConfigTests(unittest.TestCase):
    def test_find_codex_rejects_wrappers_and_uses_native_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); wrapper = root / "codex.cmd"; native = root / "codex.exe"
            wrapper.write_text("", encoding="utf-8"); native.write_text("", encoding="utf-8")
            with patch.object(config, "load_config", return_value={}), patch.dict("os.environ", {"MP_AGENT_DOCK_CODEX": str(wrapper)}, clear=False), patch("config.shutil.which", return_value=None):
                self.assertIsNone(config._configured_or_path("MP_AGENT_DOCK_CODEX", "codex_executable", "codex"))
            with patch.object(config, "load_config", return_value={}), patch.dict("os.environ", {"MP_AGENT_DOCK_CODEX": str(native)}, clear=False):
                self.assertEqual(config._configured_or_path("MP_AGENT_DOCK_CODEX", "codex_executable", "codex"), native.resolve())

    def test_npm_shims_resolve_only_the_expected_native_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_shim = root / "bin" / "codex.cmd"; codex_shim.parent.mkdir(); codex_shim.write_text("", encoding="utf-8")
            codex = root / "bin" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x64" / "bin" / "codex.exe"
            codex.parent.mkdir(parents=True); codex.write_text("", encoding="utf-8")
            with patch.object(config, "load_config", return_value={}):
                self.assertEqual(config._npm_native(codex_shim), codex.resolve())

    def test_common_local_native_and_explicit_invalid_do_not_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"; codex = home / ".local" / "bin" / "codex.exe"
            codex.parent.mkdir(parents=True); codex.write_text("", encoding="utf-8")
            with patch.object(config.Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True), patch("config.shutil.which", return_value=None), patch("config.load_config", return_value={}):
                self.assertEqual(config.find_codex(), codex.resolve())
            invalid = Path(directory) / "missing.exe"
            with patch.dict(os.environ, {"MP_AGENT_DOCK_CODEX": str(invalid)}, clear=True), patch("config.shutil.which", return_value=str(codex)), patch("config.load_config", return_value={}):
                self.assertIsNone(config.find_codex())

    def test_malformed_existing_config_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"; config_path.write_text("{bad", encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", config_path):
                with self.assertRaises(config.ConfigError):
                    config.load_config()

    def test_claude_usage_requires_explicit_opt_in(self) -> None:
        with patch.object(config, "load_config", return_value={}):
            self.assertFalse(config.claude_usage_enabled())
        with patch.object(config, "load_config", return_value={"enable_claude_usage": True}):
            self.assertTrue(config.claude_usage_enabled())

    def test_public_config_has_no_workspace_or_claude_cli_discovery(self) -> None:
        source = Path(config.__file__).read_text(encoding="utf-8")
        self.assertNotIn("def workspace_roots", source)
        self.assertNotIn("def find_claude", source)


if __name__ == "__main__":
    unittest.main()
