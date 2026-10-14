"""Read-only Windows integration checks: no desktop/focus change in this suite."""
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == "nt", "Windows-only widget")
class DesktopTests(unittest.TestCase):
    def test_hover_preview_never_opens_or_closes_click_panel(self):
        root = Path(__file__).parents[1]
        script = r"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw -Encoding UTF8 '%s'
$source = $source.Replace('$PSScriptRoot', "'" + (Split-Path '%s') + "'")
. ([ScriptBlock]::Create($source.Substring(0, $source.LastIndexOf("`ntry {"))))
Add-WpfAssemblies
Add-Type -Path '%s'
$null = New-WidgetWindow
Position-OnPrimaryWorkArea
Update-Panel
$idle = $script:Window.Width -eq 28 -and $script:Window.Height -eq 84
$enter = [Windows.Input.MouseEventArgs]::new([Windows.Input.Mouse]::PrimaryDevice, 0)
$enter.RoutedEvent = [Windows.UIElement]::MouseEnterEvent
$script:CompactButton.RaiseEvent($enter)
$hover = $script:IsHoverPreview -and -not $script:IsExpanded -and $script:Window.Width -eq 84 -and $script:Window.Height -eq 276
Complete-HoverLeave $true
$inside = $script:IsHoverPreview
Complete-HoverLeave $false
$leave = -not $script:IsHoverPreview -and $script:Window.Width -eq 28
$script:CompactButton.RaiseEvent($enter)
Set-Expanded $true
Complete-HoverLeave $false
$clicked = $script:IsExpanded -and $script:Window.Width -eq 454
Set-Expanded $false
$closed = -not $script:IsExpanded -and -not $script:IsHoverPreview -and $script:Window.Width -eq 28
$script:HoverLeaveTimer.Stop()
@{idle=$idle; hover=$hover; inside=$inside; leave=$leave; clicked=$clicked; closed=$closed; logo=($script:LogoMark -is [Windows.Controls.Image])} | ConvertTo-Json -Compress
""" % (str(root / "widget.ps1").replace("'", "''"), str(root / "widget.ps1").replace("'", "''"), str(root / "desktop_windows.cs").replace("'", "''"))
        result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                                capture_output=True, text=True, timeout=25,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        data = json.loads(result.stdout)
        self.assertTrue(all(data.values()), data)

    def test_toolwindow_style_is_applied_and_idempotent(self):
        source = Path(__file__).parents[1] / "desktop_windows.cs"
        script = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework
Add-Type -Path '%s'
$window = [Windows.Window]::new()
$hwnd = [Windows.Interop.WindowInteropHelper]::new($window).EnsureHandle()
try {
    @{ first=[DesktopWindows]::HideFromSwitcher($hwnd); second=[DesktopWindows]::HideFromSwitcher($hwnd); hidden=[DesktopWindows]::HiddenFromSwitcher($hwnd) } | ConvertTo-Json -Compress
} finally { $window.Close() }
""" % str(source).replace("'", "''")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                                capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        self.assertEqual({"first": True, "second": True, "hidden": True}, json.loads(result.stdout))

    def test_click_open_survives_leave_refresh_and_preserves_handle_position(self):
        root = Path(__file__).parents[1]
        script = r"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw -Encoding UTF8 '%s'
$source = $source.Replace('$PSScriptRoot', "'" + (Split-Path '%s') + "'")
. ([ScriptBlock]::Create($source.Substring(0, $source.LastIndexOf("`ntry {"))))
Add-WpfAssemblies
Add-Type -Path '%s'
$null = New-WidgetWindow
Position-OnPrimaryWorkArea
Update-Panel
$script:Window.Top = [Windows.SystemParameters]::WorkArea.Bottom - 290
$anchorBefore = $script:Window.Top + $script:CompactButton.Height / 2
function Click-Handle {
    foreach ($event in @([Windows.UIElement]::MouseLeftButtonDownEvent, [Windows.UIElement]::MouseLeftButtonUpEvent)) {
        $args = [Windows.Input.MouseButtonEventArgs]::new([Windows.Input.Mouse]::PrimaryDevice, 0, [Windows.Input.MouseButton]::Left)
        $args.RoutedEvent = $event
        $script:CompactButton.RaiseEvent($args)
    }
}
Click-Handle
$opened = $script:IsExpanded
$anchorAfter = $script:Window.Top + $script:CompactButton.Margin.Top + $script:CompactButton.Height / 2
$leave = [Windows.Input.MouseEventArgs]::new([Windows.Input.Mouse]::PrimaryDevice, 0)
$leave.RoutedEvent = [Windows.UIElement]::MouseLeaveEvent
$script:Window.RaiseEvent($leave)
1..3 | ForEach-Object { Update-Panel }
$remained = $script:IsExpanded
$label = $script:CompactLabel.Text
Click-Handle
$collapsed = -not $script:IsExpanded
@{opened=$opened; remained=$remained; collapsed=$collapsed; anchor_delta=($anchorAfter-$anchorBefore); label=$label} | ConvertTo-Json -Compress
""" % (str(root / "widget.ps1").replace("'", "''"), str(root / "widget.ps1").replace("'", "''"), str(root / "desktop_windows.cs").replace("'", "''"))
        result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                                capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        data = json.loads(result.stdout)
        self.assertTrue(data["opened"])
        self.assertTrue(data["remained"])
        self.assertTrue(data["collapsed"])
        self.assertAlmostEqual(0, data["anchor_delta"])
        for field in ("CL 5h", "CL 7d", "CX 5h", "CX 7d", "CPU", "RAM"):
            self.assertIn(field, data["label"])

    def test_native_guards_and_desktop_inventory(self):
        source = Path(__file__).parents[1] / "desktop_windows.cs"
        script = """
Add-Type -Path '%s'
$ready = [DesktopWindows]::Initialize()
[DesktopWindows]::RefreshWindows()
@{
    ready = $ready
    count = [DesktopWindows]::DesktopCount()
    invalid = [DesktopWindows]::ResolveWindow(0)
    stale_activation = [DesktopWindows]::ActivateSession(0, 'stale', 1)
    started = [DesktopWindows]::ProcessStarted($PID)
} | ConvertTo-Json -Compress
""" % str(source).replace("'", "''")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                                capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        data = json.loads(result.stdout)
        self.assertEqual(0, data["invalid"])
        self.assertFalse(data["stale_activation"])
        self.assertTrue(data["started"].isdigit())
        if data["ready"]:
            self.assertGreater(data["count"], 0)

    def test_all_views_construct_without_showing_window(self):
        source = Path(__file__).parents[1] / "widget.ps1"
        for tab in range(4):
            with self.subTest(tab=tab):
                result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                         "-File", str(source), "-ValidateOnly", "-InitialTab", str(tab)],
                                        capture_output=True, text=True, timeout=60,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("validation OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
