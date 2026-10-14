"""Installer tests never touch real profiles, registry, or provider CLIs."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install
import doctor


@unittest.skipUnless(sys.platform == "win32", "Windows installer")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / "state"
        self.native = self.root / "codex.exe"
        self.native.touch()
        self.stack.enter_context(patch.object(install, "STATE", self.state))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(patch.object(install.config, "find_codex", return_value=None))
        self.stack.enter_context(patch.object(install.shutil, "which", return_value="available"))
        self.registry = self.stack.enter_context(patch("winreg.SetValueEx"))

    def test_dry_run_has_no_side_effects(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = install.install(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["installed"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))
        self.registry.assert_not_called()

    def test_install_without_providers_or_workspace_leaves_profiles_unchanged(self):
        profiles = [self.root / ".claude.json", self.root / ".claude/settings.json",
                    self.root / ".claude/skills/codex-delegate/SKILL.md", self.root / ".codex/config.toml"]
        for path in profiles:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep exactly", encoding="utf-8")
        result = install.install()
        self.assertTrue(result["installed"])
        self.assertFalse(result["codex_available"])
        self.assertEqual(result["edition"], "widget")
        for path in profiles:
            self.assertEqual(path.read_text(encoding="utf-8"), "keep exactly")
        self.assertEqual(json.loads((self.state / "config.json").read_text()), {"enable_claude_usage": False})
        self.registry.assert_not_called()

    def test_explicit_invalid_executable_fails_before_writes(self):
        wrapper = self.root / "codex.cmd"
        wrapper.touch()
        for path in (wrapper, self.root / "missing.exe"):
            with self.assertRaises(ValueError):
                install.install(codex=str(path))
        self.assertFalse(self.state.exists())

    def test_reinstall_backs_up_own_config_and_preserves_unknown_fields(self):
        self.state.mkdir()
        path = self.state / "config.json"
        old = {"private_setting": "keep", "enable_claude_usage": True}
        path.write_text(json.dumps(old), encoding="utf-8")
        result = install.install(codex=str(self.native))
        self.assertEqual(json.loads((Path(result["backup"]) / "config.json").read_text()), old)
        configured = json.loads(path.read_text())
        self.assertEqual(configured["private_setting"], "keep")
        self.assertFalse(configured["enable_claude_usage"])
        self.assertEqual(configured["codex_executable"], str(self.native.resolve()))

    def test_startup_and_claude_polling_are_explicit_opt_ins(self):
        with patch("winreg.CreateKey"):
            result = install.install(startup=True, enable_claude_usage=True)
        self.assertTrue(result["startup"])
        self.assertTrue(json.loads((self.state / "config.json").read_text())["enable_claude_usage"])
        self.registry.assert_called_once()
        self.assertEqual(self.registry.call_args.args[1], "MPAgentDock")
        self.assertIn("launcher.py", self.registry.call_args.args[-1])

    def test_doctor_accepts_missing_optional_providers(self):
        with patch.object(doctor.config, "load_config", return_value={}):
            result = doctor.diagnose()
        self.assertTrue(result["ok"])
        self.assertFalse(result["providers"]["codex_cli_found"])


if __name__ == "__main__":
    unittest.main()
