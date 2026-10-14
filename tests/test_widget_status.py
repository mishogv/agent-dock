"""Offscreen tray/settings and provider freshness regressions; isolated state."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows WPF')
class WidgetStatusTests(unittest.TestCase):
    def run_check(self, body):
        with tempfile.TemporaryDirectory() as state:
            env = dict(os.environ, MP_AGENT_DOCK_STATE_DIR=state)
            root = str(ROOT).replace("'", "''")
            prefix = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -Raw -Encoding UTF8 '%s\widget.ps1'
$source=$source.Replace('$PSScriptRoot', "'%s'")
. ([ScriptBlock]::Create($source.Substring(0,$source.LastIndexOf("`ntry {"))))
Add-WpfAssemblies
""" % (root, root)
            result = subprocess.run(['powershell.exe','-NoProfile','-STA','-Command',prefix+body],
                                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    timeout=30, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PASS', result.stdout)

    def test_stale_future_unavailable_quotas_are_not_remaining(self):
        self.run_check(r"""
foreach ($stamp in @([DateTimeOffset]::UtcNow.AddMinutes(-6),[DateTimeOffset]::UtcNow.AddMinutes(2))) {
 $provider=[pscustomobject]@{available=$true; updated_at=$stamp.ToString('o'); remaining_percent=5; used_percent=95; windows=@([pscustomobject]@{remaining_percent=5;used_percent=95})}
 $safe=Protect-ProviderDisplay $provider
 if ($safe.available -or $null -ne $safe.remaining_percent -or $null -ne $safe.windows[0].remaining_percent) {throw 'Unsafe stale display'}
}
$fresh=Protect-ProviderDisplay ([pscustomobject]@{available=$true;updated_at=[DateTimeOffset]::UtcNow.ToString('o');remaining_percent=5; windows=@()})
if (-not $fresh.available -or $fresh.remaining_percent -ne 5) {throw 'Fresh data lost'}
if ((Get-FreshnessText ([DateTimeOffset]::UtcNow.ToString('o'))) -notmatch '\d{2}:\d{2}:\d{2}') {throw 'Missing clock'}
'PASS'
""")

    def test_tray_menu_disposal_and_settings_guard(self):
        self.run_check(r"""
$script:WidgetSettings=@{show_tray_icon=$false;show_edge_dock=$false}
Save-WidgetSettings
$restored=Read-WidgetSettings
if ($restored.show_tray_icon -or -not $restored.show_edge_dock) {throw 'Both surfaces can disappear'}
Initialize-WidgetTray
if ($script:TrayIcon.Visible) {throw 'Test must never show tray icon'}
$script:WidgetSettings.show_edge_dock=$true
Set-EdgeDockVisible $false
if (-not $script:WidgetSettings.show_edge_dock) {throw 'Hidden tray must not allow hiding dock'}
if ($script:TrayMenu.Items.Count -ne 5) {throw 'Tray commands missing'}
$script:clicks=0
function Show-DockFromTray {$script:clicks++}
$script:TrayMenu.Items[0].PerformClick()
if ($script:clicks -ne 1) {throw 'Show command is not connected'}
Dispose-WidgetTray
if ($null -ne $script:TrayIcon -or $null -ne $script:TrayMenu) {throw 'Tray resources leaked'}
'PASS'
""")

    def test_widget_does_not_read_delegated_tasks(self):
        source = (ROOT / 'widget.ps1').read_text(encoding='utf-8-sig')
        self.assertNotIn('Get-TaskSummary', source)
        self.assertNotIn("'tasks'", source)
        self.assertNotIn('делегирана задача', source)


if __name__ == '__main__':
    unittest.main()
