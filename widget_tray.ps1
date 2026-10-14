# Tray integration and independent per-user widget preferences.
$script:TrayIcon = $null
$script:TrayMenu = $null
$script:TrayToggleItem = $null
$script:WidgetSettings = @{ show_tray_icon = $true; show_edge_dock = $true }

function Write-WidgetEvent([string]$Event, [string]$Detail = '') {
    try {
        [IO.Directory]::CreateDirectory($script:StateDirectory) | Out-Null
        $line = [DateTimeOffset]::Now.ToString('o') + ' ' + $Event + ' ' + $Detail + [Environment]::NewLine
        [IO.File]::AppendAllText((Join-Path $script:StateDirectory 'widget-lifecycle.log'), $line, [Text.Encoding]::UTF8)
    } catch {}
}
function Read-WidgetSettings {
    $result = @{ show_tray_icon = $true; show_edge_dock = $true }
    $path = Join-Path $script:StateDirectory 'widget-settings.json'
    if (Test-Path -LiteralPath $path) {
        try {
            $data = Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json
            foreach ($key in @('show_tray_icon','show_edge_dock')) {
                $value = Get-PropertyValue $data $key
                if ($value -is [bool]) { $result[$key] = $value }
            }
        } catch { Write-WidgetEvent 'settings-invalid' $_.Exception.GetType().Name }
    }
    if (-not $result.show_tray_icon -and -not $result.show_edge_dock) { $result.show_edge_dock = $true }
    return $result
}
function Save-WidgetSettings {
    [IO.Directory]::CreateDirectory($script:StateDirectory) | Out-Null
    $path = Join-Path $script:StateDirectory 'widget-settings.json'
    $temp = $path + '.' + $PID + '.writing'
    [IO.File]::WriteAllText($temp, ($script:WidgetSettings | ConvertTo-Json), [Text.Encoding]::UTF8)
    Move-Item -LiteralPath $temp -Destination $path -Force
}
function Show-DockFromTray {
    $script:WidgetSettings.show_edge_dock = $true
    Save-WidgetSettings
    $script:Window.WindowState = [Windows.WindowState]::Normal
    $script:Window.Show()
    Position-OnPrimaryWorkArea
    Set-Expanded $true
    Initialize-DesktopIntegration
    Write-WidgetEvent 'shown-from-tray'
}
function Set-EdgeDockVisible([bool]$Visible) {
    if (-not $Visible -and ($null -eq $script:TrayIcon -or -not $script:TrayIcon.Visible)) { return }
    $script:WidgetSettings.show_edge_dock = $Visible
    Save-WidgetSettings
    if ($Visible) { Show-DockFromTray } else { $script:Window.Hide(); Write-WidgetEvent 'hidden-to-tray' }
}
function Set-TrayEnabled([bool]$Enabled) {
    $script:WidgetSettings.show_tray_icon = $Enabled
    if (-not $Enabled -and -not $script:WidgetSettings.show_edge_dock) { Show-DockFromTray }
    Save-WidgetSettings
    if ($Enabled -and $null -eq $script:TrayIcon) { Initialize-WidgetTray }
    if ($null -ne $script:TrayIcon) { $script:TrayIcon.Visible = $Enabled }
    if ($null -ne $script:TrayToggleItem) { $script:TrayToggleItem.IsChecked = $Enabled }
    Update-Panel
    Write-WidgetEvent 'tray-setting' ([string]$Enabled)
}
function Initialize-WidgetTray {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    if ($null -ne $script:TrayIcon) { $script:TrayIcon.Visible = $script:WidgetSettings.show_tray_icon; return }
    $icon = [Drawing.SystemIcons]::Application.Clone()
    $logo = Join-Path $PSScriptRoot 'assets\agent-dock-logo.png'
    if (Test-Path -LiteralPath $logo) {
        $bitmap = $null; $handle = [IntPtr]::Zero
        try {
            if ($null -eq ('DockIconNative' -as [type])) {
                Add-Type 'using System; using System.Runtime.InteropServices; public static class DockIconNative { [DllImport("user32.dll")] public static extern bool DestroyIcon(IntPtr h); }'
            }
            $bitmap = [Drawing.Bitmap]::new($logo)
            $handle = $bitmap.GetHicon()
            $custom = [Drawing.Icon]::FromHandle($handle).Clone()
            $icon.Dispose(); $icon = $custom
        } finally {
            if ($null -ne $bitmap) { $bitmap.Dispose() }
            if ($handle -ne [IntPtr]::Zero) { [DockIconNative]::DestroyIcon($handle) | Out-Null }
        }
    }
    $tray = [Windows.Forms.NotifyIcon]::new(); $tray.Icon = $icon; $tray.Text = 'Agent Dock'
    $menu = [Windows.Forms.ContextMenuStrip]::new()
    $show = $menu.Items.Add('Покажи панела'); $show.Add_Click({ Show-DockFromTray })
    $hide = $menu.Items.Add('Скрий страничния панел'); $hide.Add_Click({ Set-EdgeDockVisible $false })
    $settings = $menu.Items.Add('Настройки'); $settings.Add_Click({ Show-DockFromTray; Set-DockPage 3 })
    $menu.Items.Add([Windows.Forms.ToolStripSeparator]::new()) | Out-Null
    $exit = $menu.Items.Add('Изход'); $exit.Add_Click({ $script:Window.Close() })
    $tray.ContextMenuStrip = $menu
    $tray.Add_MouseClick({ param($sender,$eventArgs) if ($eventArgs.Button -eq [Windows.Forms.MouseButtons]::Left) { Show-DockFromTray } })
    $script:TrayIcon = $tray; $script:TrayMenu = $menu
    $tray.Visible = $script:WidgetSettings.show_tray_icon
}
function Dispose-WidgetTray {
    if ($null -ne $script:TrayIcon) {
        $script:TrayIcon.Visible = $false; $script:TrayIcon.Icon.Dispose(); $script:TrayIcon.Dispose(); $script:TrayIcon = $null
    }
    if ($null -ne $script:TrayMenu) { $script:TrayMenu.Dispose(); $script:TrayMenu = $null }
}
function New-WidgetSettingsCard {
    $stack = New-Stack
    $stack.Children.Add((New-Text 'Настройки на панела' 14 '#F0F5FF')) | Out-Null
    $check = [Windows.Controls.CheckBox]::new(); $check.Content = 'Икона до часовника (system tray)'
    $check.Foreground = New-Brush '#DCE6F5'; $check.Margin = [Windows.Thickness]::new(0,10,0,10)
    $check.IsChecked = $script:WidgetSettings.show_tray_icon
    $check.Add_Click({ param($sender,$eventArgs) Set-TrayEnabled ([bool]$sender.IsChecked) })
    $stack.Children.Add($check) | Out-Null
    $stack.Children.Add((New-Text 'Windows може да прибере иконата в менюто ^ до часовника. Можеш да я изтеглиш навън.' 10 '#91A3BC')) | Out-Null
    $hide = New-PillButton 'Скрий панела, остави tray иконата'; $hide.IsEnabled = $script:WidgetSettings.show_tray_icon
    $hide.Margin = [Windows.Thickness]::new(0,10,0,0); $hide.Add_Click({ Set-EdgeDockVisible $false })
    $stack.Children.Add($hide) | Out-Null
    return New-Card $stack
}
