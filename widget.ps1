[CmdletBinding()]
param(
    [switch]$ValidateOnly,
    [ValidateRange(0, 86400)]
    [int]$SmokeTestSeconds = 0,
    [switch]$StartExpanded,
    [switch]$StartPreview,
    [string]$ScreenshotPath = '',
    [ValidateRange(0, 3)]
    [int]$InitialTab = 0
)
# MP Agent Dock: a small, read-only local status widget.
# The provider connector (usage.py) owns collection; this file only renders
# the published snapshot and local telemetry.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:StateDirectory = if ($env:MP_AGENT_DOCK_STATE_DIR) { $env:MP_AGENT_DOCK_STATE_DIR } else { Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'MPAgentDock' }
$script:SnapshotPath = Join-Path $script:StateDirectory 'snapshot.json'
$script:TelemetryPath = Join-Path $script:StateDirectory 'telemetry.json'
. (Join-Path $PSScriptRoot 'widget_tray.ps1')
$script:Mutex = $null
$script:Window = $null
$script:Panel = $null
$script:Snapshot = $null
$script:Telemetry = $null
$script:PinOpen = $false
$script:IsExpanded = $false
$script:IsHoverPreview = $false
$script:HoverLeaveTimer = $null
$script:DragStart = $null
$script:DragTop = 0.0
$script:DragMoved = $false
$script:RefreshTimer = $null
$script:InitialTab = $InitialTab
$script:SelectedPage = $InitialTab
$script:Offsets = @(0.0, 0.0, 0.0, 0.0)
$script:ButtonTemplate = $null
$script:ContentScroll = $null
$script:DesktopReady = $false
$script:DesktopPinned = $false
$script:Notice = ''
$script:LastPinCheck = [DateTime]::MinValue

function Add-WpfAssemblies {
    Add-Type -AssemblyName PresentationCore, PresentationFramework, WindowsBase
}

function Get-PropertyValue {
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()][object]$Default = $null
    )

    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Get-NumberText {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return '—' }
    try {
        $number = [double]$Value
        return ('{0:0}%' -f $number)
    } catch {
        return '—'
    }
}

function Get-RemainingPercent {
    param([AllowNull()][object]$Provider)
    $remaining = Get-PropertyValue $Provider 'remaining_percent'
    if ($null -ne $remaining) { return $remaining }
    $used = Get-PropertyValue $Provider 'used_percent'
    if ($null -eq $used) { return $null }
    try { return (100.0 - [double]$used) } catch { return $null }
}

function Convert-ToUtcDate {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    try {
        return [DateTimeOffset]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).ToUniversalTime()
    } catch {
        return $null
    }
}

function Get-FreshnessText {
    param([AllowNull()][object]$Value)
    $date = Convert-ToUtcDate $Value
    if ($null -eq $date) { return 'няма час на обновяване' }
    $age = ([DateTimeOffset]::UtcNow - $date).TotalSeconds
    if ($age -lt -30) { return 'невалидна дата в бъдещето' }
    $clock = $date.ToLocalTime().ToString('HH:mm:ss')
    if ($age -lt 60) { return ($clock + ' · преди ' + [Math]::Max(0,[int]$age) + ' сек') }
    if ($age -lt 3600) { return ($clock + (' · преди {0} мин' -f [Math]::Floor($age / 60))) }
    return ($date.ToLocalTime().ToString('dd.MM HH:mm:ss') + (' · преди {0} ч' -f [Math]::Floor($age / 3600)))
}

function Protect-ProviderDisplay($Provider) {
    if ($null -eq $Provider) { return $null }
    $date = Convert-ToUtcDate (Get-PropertyValue $Provider 'updated_at')
    $age = if ($null -ne $date) { ([DateTimeOffset]::UtcNow - $date).TotalSeconds } else { [double]::PositiveInfinity }
    if ($age -gt 300 -or $age -lt -30 -or -not [bool](Get-PropertyValue $Provider 'available' $false)) {
        $Provider | Add-Member -Force -NotePropertyName available -NotePropertyValue $false
        foreach ($field in @('used_percent','remaining_percent')) { $Provider | Add-Member -Force -NotePropertyName $field -NotePropertyValue $null }
        foreach ($window in @(Get-PropertyValue $Provider 'windows' @())) {
            foreach ($field in @('used_percent','remaining_percent')) { $window | Add-Member -Force -NotePropertyName $field -NotePropertyValue $null }
        }
        if ($age -gt 300 -or $age -lt -30) { $Provider | Add-Member -Force -NotePropertyName error -NotePropertyValue 'Остарели или невалидни данни. Изчаква се реално обновяване.' }
    }
    return $Provider
}

function Read-Snapshot {
    $fallback = [pscustomobject]@{
        updated_at = $null
        codex = [pscustomobject]@{ available = $false; used_percent = $null; remaining_percent = $null; windows = @(); updated_at = $null; error = 'Няма snapshot'; models = @() }
        claude = [pscustomobject]@{ available = $false; used_percent = $null; remaining_percent = $null; windows = @(); updated_at = $null; error = 'Няма snapshot'; models = @() }
    }
    if (-not (Test-Path -LiteralPath $script:SnapshotPath -PathType Leaf)) { return $fallback }
    # The connector writes atomically, but two quick attempts make a partial read harmless.
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $raw = Get-Content -LiteralPath $script:SnapshotPath -Raw -Encoding UTF8
            if ([string]::IsNullOrWhiteSpace($raw)) { throw 'empty snapshot' }
            $parsed = $raw | ConvertFrom-Json
            if ($null -eq $parsed.codex) { $parsed | Add-Member -NotePropertyName codex -NotePropertyValue $fallback.codex }
            if ($null -eq $parsed.claude) { $parsed | Add-Member -NotePropertyName claude -NotePropertyValue $fallback.claude }
            return $parsed
        } catch {
            if ($attempt -eq 1) { return $fallback }
        }
    }
    return $fallback
}

function Read-Telemetry {
    $fallback = [pscustomobject]@{
        updated_at = $null
        system = [pscustomobject]@{ cpu_percent = $null; ram_used_gb = $null; ram_total_gb = $null; ram_percent = $null; disks = @() }
        sessions = @()
        session_counts = [pscustomobject]@{ claude = $null; codex = $null }
        notes = @()
    }
    if (-not (Test-Path -LiteralPath $script:TelemetryPath -PathType Leaf)) { return $fallback }
    try {
        $raw = Get-Content -LiteralPath $script:TelemetryPath -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return $fallback }
        $parsed = $raw | ConvertFrom-Json
        if ($null -eq $parsed.system) { $parsed | Add-Member -NotePropertyName system -NotePropertyValue $fallback.system }
        if ($null -eq $parsed.sessions) { $parsed | Add-Member -NotePropertyName sessions -NotePropertyValue @() }
        if ($null -eq $parsed.session_counts) { $parsed | Add-Member -NotePropertyName session_counts -NotePropertyValue $fallback.session_counts }
        return $parsed
    } catch { return $fallback }
}

function New-Brush([string]$Hex) { return [Windows.Media.BrushConverter]::new().ConvertFromString($Hex) }

function New-Text {
    param([string]$Text, [double]$Size = 12, [string]$Color = '#DCE6F5', [Windows.FontWeight]$Weight = [Windows.FontWeights]::Normal)
    $tb = [Windows.Controls.TextBlock]::new()
    $null = ($tb.Text = $Text)
    $null = ($tb.FontFamily = [Windows.Media.FontFamily]::new('Segoe UI'))
    $null = ($tb.FontSize = $Size)
    $null = ($tb.Foreground = New-Brush $Color)
    $null = ($tb.FontWeight = $Weight)
    $null = ($tb.TextWrapping = [Windows.TextWrapping]::Wrap)
    Write-Output -NoEnumerate -InputObject $tb
}

function New-Row {
    param([string]$Label, [string]$Value, [string]$ValueColor = '#DCE6F5')
    $grid = [Windows.Controls.Grid]::new()
    $grid.Margin = [Windows.Thickness]::new(0, 2, 0, 2)
    $grid.ColumnDefinitions.Add([Windows.Controls.ColumnDefinition]::new())
    $right = [Windows.Controls.ColumnDefinition]::new(); $right.Width = [Windows.GridLength]::new(1, [Windows.GridUnitType]::Star); $grid.ColumnDefinitions.Add($right)
    $leftResults = @(New-Text $Label 11 '#8EA0B8')
    $valueResults = @(New-Text $Value 12 $ValueColor)
    $left = $leftResults[-1]
    $valueText = $valueResults[-1]
    $valueText.HorizontalAlignment = [Windows.HorizontalAlignment]::Right
    [Windows.Controls.Grid]::SetColumn($valueText, 1)
    $grid.Children.Add($left) | Out-Null; $grid.Children.Add($valueText) | Out-Null
    return $grid
}

function Get-CountdownText {
    param([AllowNull()][object]$Value)
    $date = Convert-ToUtcDate $Value
    if ($null -eq $date) { return 'няма дата за обновяване' }
    $span = $date - [DateTimeOffset]::UtcNow
    if ($span.TotalSeconds -le 0) { return 'очаква обновяване' }
    $hours = [Math]::Floor($span.TotalHours)
    $minutes = [Math]::Floor($span.TotalMinutes) % 60
    if ($hours -gt 24) { return ('след {0}д {1}ч' -f [Math]::Floor($hours / 24), ($hours % 24)) }
    return ('след {0}ч {1}м' -f $hours, $minutes)
}

function New-UsageWindowBlock {
    param([AllowNull()][object]$Window, [string]$FallbackLabel = 'Лимит')
    $label = $FallbackLabel
    $used = Get-PropertyValue $Window 'used_percent'
    $remaining = Get-RemainingPercent $Window
    $usedText = Get-NumberText $used
    $remainingText = Get-NumberText $remaining
    $reset = Get-PropertyValue $Window 'resets_at'
    $color = '#66E3A6'
    try { if ($null -ne $used -and [double]$used -ge 80) { $color = '#F6C453' }; if ($null -ne $used -and [double]$used -ge 95) { $color = '#F27474' } } catch {}
    $box = [Windows.Controls.Border]::new(); $box.Background = New-Brush '#1B293F'; $box.CornerRadius = [Windows.CornerRadius]::new(6); $box.Padding = [Windows.Thickness]::new(8, 6, 8, 6); $box.Margin = [Windows.Thickness]::new(0, 4, 0, 0)
    $stack = [Windows.Controls.StackPanel]::new()
    $stack.Children.Add((New-Row $label ('използвано ' + $usedText) $color)) | Out-Null
    $stack.Children.Add((New-Row 'Остава' $remainingText '#DCE6F5')) | Out-Null
    $bar = [Windows.Controls.ProgressBar]::new(); $bar.Height = 5; $bar.Minimum = 0; $bar.Maximum = 100; $bar.Margin = [Windows.Thickness]::new(0, 4, 0, 3); $bar.Foreground = New-Brush $color; $bar.Background = New-Brush '#2A3A53'; $bar.IsIndeterminate = $false
    try { if ($null -ne $used) { $bar.Value = [Math]::Max(0, [Math]::Min(100, [double]$used)) } } catch { $bar.Value = 0 }
    $stack.Children.Add($bar) | Out-Null
    $resetDate = Convert-ToUtcDate $reset
    $localReset = if ($null -eq $resetDate) { '—' } else { $resetDate.ToLocalTime().ToString('dd.MM HH:mm') }
    $stack.Children.Add((New-Text ('нулиране ' + $localReset + ' · ' + (Get-CountdownText $reset)) 10 '#8EA0B8')) | Out-Null
    $box.Child = $stack
    return $box
}

function New-ProviderBlock {
    param([string]$Name, [AllowNull()][object]$Provider)
    $box = [Windows.Controls.Border]::new()
    $box.Background = New-Brush '#172235'; $box.BorderBrush = New-Brush '#26364E'; $box.BorderThickness = [Windows.Thickness]::new(1); $box.CornerRadius = [Windows.CornerRadius]::new(8); $box.Padding = [Windows.Thickness]::new(12, 10, 12, 10); $box.Margin = [Windows.Thickness]::new(0, 0, 0, 9)
    $stack = [Windows.Controls.StackPanel]::new()
    $available = [bool](Get-PropertyValue $Provider 'available' $false)
    $error = [string](Get-PropertyValue $Provider 'error' '')
    $header = [Windows.Controls.Grid]::new()
    $header.ColumnDefinitions.Add([Windows.Controls.ColumnDefinition]::new())
    $hRight = [Windows.Controls.ColumnDefinition]::new(); $hRight.Width = [Windows.GridLength]::new(1, [Windows.GridUnitType]::Star); $header.ColumnDefinitions.Add($hRight)
    $title = New-Text $Name 14 '#F5F8FF' ([Windows.FontWeights]::SemiBold)
    $statusText = if (-not $available) { 'недостъпен' } elseif (-not [string]::IsNullOrWhiteSpace($error)) { 'грешка' } else { 'активен' }
    $statusColor = if (-not $available -or -not [string]::IsNullOrWhiteSpace($error)) { '#F27474' } else { '#66E3A6' }
    $status = New-Text $statusText 11 $statusColor; $status.HorizontalAlignment = [Windows.HorizontalAlignment]::Right; [Windows.Controls.Grid]::SetColumn($status, 1)
    $header.Children.Add($title) | Out-Null; $header.Children.Add($status) | Out-Null; $stack.Children.Add($header) | Out-Null
    $windows = @(Get-PropertyValue $Provider 'windows' @())
    $five = @($windows | Where-Object { ([string](Get-PropertyValue $_ 'label' '') -match '5.?ч|5.?h') } | Select-Object -First 1)
    $week = @($windows | Where-Object { ([string](Get-PropertyValue $_ 'label' '') -match '1.?w|7.?d|седми|week') } | Select-Object -First 1)
    if ($five.Count -gt 0) { $stack.Children.Add((New-UsageWindowBlock $five[0] '5-часов лимит')) | Out-Null }
    else { $stack.Children.Add((New-Text '5-часов лимит · няма данни' 10 '#8EA0B8')) | Out-Null }
    if ($week.Count -gt 0) { $stack.Children.Add((New-UsageWindowBlock $week[0] 'Седмичен лимит')) | Out-Null }
    else { $stack.Children.Add((New-Text 'Седмичен лимит · няма данни' 10 '#8EA0B8')) | Out-Null }
    $known = @(); if ($five.Count -gt 0) { $known += $five[0] }; if ($week.Count -gt 0) { $known += $week[0] }
    foreach ($other in @($windows | Where-Object { $known -notcontains $_ })) {
        $actual = [string](Get-PropertyValue $other 'label' 'неизвестен период')
        $stack.Children.Add((New-UsageWindowBlock $other ('Период: ' + $actual))) | Out-Null
    }
    $fresh = Get-FreshnessText (Get-PropertyValue $Provider 'updated_at' (Get-PropertyValue $script:Snapshot 'updated_at'))
    $freshText = New-Text $fresh 10 '#7488A5'; $freshText.Margin = [Windows.Thickness]::new(0, 5, 0, 0); $stack.Children.Add($freshText) | Out-Null
    $attempt = Get-PropertyValue $Provider 'last_attempt_at'
    if ($attempt) { $stack.Children.Add((New-Text ('Последен опит: ' + (Get-FreshnessText $attempt)) 10 '#7488A5')) | Out-Null }
    if (-not [string]::IsNullOrWhiteSpace($error)) { $errText = New-Text ('Грешка: ' + $error) 10 '#F6B3B3'; $errText.Margin = [Windows.Thickness]::new(0, 3, 0, 0); $stack.Children.Add($errText) | Out-Null }
    $box.Child = $stack
    return $box
}

function Initialize-DesktopIntegration {
    if ($null -eq ('DesktopWindows' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'desktop_windows.cs') }
    $script:DesktopReady = [DesktopWindows]::Initialize()
    if ($null -ne $script:Window -and $script:Window.IsVisible) {
        $hwnd = [Windows.Interop.WindowInteropHelper]::new($script:Window).Handle
        $script:DesktopPinned = [DesktopWindows]::Pin($hwnd)
        # Pin the shell view first, then exclude this utility from Alt+Tab.
        if (-not [DesktopWindows]::HideFromSwitcher($hwnd)) { $script:Notice = 'Windows не успя да скрие панела от Alt+Tab.' }
    }
}

function New-PillButton {
    param([string]$Text = '', [string]$Accent = '#223047')
    $button = [Windows.Controls.Button]::new()
    $button.Content = $Text; $button.Background = New-Brush $Accent; $button.Foreground = New-Brush '#DFE7F5'
    $button.BorderBrush = New-Brush '#2D3D56'; $button.BorderThickness = [Windows.Thickness]::new(1)
    $button.Padding = [Windows.Thickness]::new(12, 9, 12, 9); $button.FontSize = 11
    $button.Cursor = [Windows.Input.Cursors]::Hand
    if ($null -eq $script:ButtonTemplate) {
        $script:ButtonTemplate = [Windows.Markup.XamlReader]::Parse(@'
<ControlTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" TargetType="Button">
 <Border x:Name="Shell" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" CornerRadius="11"
 Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
 BorderThickness="{TemplateBinding BorderThickness}" Padding="{TemplateBinding Padding}">
 <ContentPresenter HorizontalAlignment="{TemplateBinding HorizontalContentAlignment}" VerticalAlignment="Center"/>
 </Border>
 <ControlTemplate.Triggers>
  <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="Shell" Property="Background" Value="#30435C"/><Setter TargetName="Shell" Property="BorderBrush" Value="#6D88AC"/></Trigger>
  <Trigger Property="IsPressed" Value="True"><Setter TargetName="Shell" Property="Background" Value="#3E536D"/></Trigger>
  <Trigger Property="IsKeyboardFocused" Value="True"><Setter TargetName="Shell" Property="BorderBrush" Value="#91E9CB"/><Setter TargetName="Shell" Property="BorderThickness" Value="2"/></Trigger>
  <Trigger Property="IsEnabled" Value="False"><Setter TargetName="Shell" Property="Opacity" Value="0.58"/></Trigger>
 </ControlTemplate.Triggers>
</ControlTemplate>
'@)
    }
    $button.Template = $script:ButtonTemplate
    return $button
}

function New-Card {
    param([Windows.UIElement]$Child, [string]$Background = '#182333')
    $card = [Windows.Controls.Border]::new(); $card.Child = $Child; $card.Background = New-Brush $Background
    $card.BorderBrush = New-Brush '#2A384C'; $card.BorderThickness = [Windows.Thickness]::new(1)
    $card.CornerRadius = [Windows.CornerRadius]::new(16); $card.Padding = [Windows.Thickness]::new(15)
    $card.Margin = [Windows.Thickness]::new(0, 0, 0, 10)
    return $card
}

function New-Stack { return [Windows.Controls.StackPanel]::new() }
function Add-Gap { param($Stack, [int]$Height = 10); $space = [Windows.Controls.Border]::new(); $space.Height = $Height; $Stack.Children.Add($space) | Out-Null }
function New-SectionTitle {
    param([string]$Title, [string]$Subtitle)
    $stack = New-Stack
    $stack.Children.Add((New-Text $Title 22 '#F0F5FF' ([Windows.FontWeights]::SemiBold))) | Out-Null
    if ($Subtitle) { $line = New-Text $Subtitle 11 '#91A3BC'; $line.Margin = [Windows.Thickness]::new(0, 5, 0, 10); $stack.Children.Add($line) | Out-Null }
    return $stack
}
function Set-DockPage {
    param([int]$Page)
    $script:Offsets[$script:SelectedPage] = $script:ContentScroll.VerticalOffset
    $script:SelectedPage = $Page
    $script:ContentScroll = $null
    Update-Panel
}
function Add-Drilldown {
    param($Stack, [string]$Text, [int]$Page)
    $button = New-PillButton $Text '#1F3041'; $button.HorizontalContentAlignment = [Windows.HorizontalAlignment]::Left; $button.Tag = $Page
    $button.Add_Click({ param($sender, $eventArgs) Set-DockPage ([int]$sender.Tag) })
    $Stack.Children.Add($button) | Out-Null
}
function Get-MetricColor {
    param($Percent)
    if ($null -eq $Percent) { return '#91A3BC' }
    if ([double]$Percent -ge 95) { return '#FF8E97' }
    if ([double]$Percent -ge 85) { return '#F2C779' }
    return '#8CE6C5'
}
function New-QuotaSummary {
    param([string]$Name, $Provider, [string]$Accent)
    $stack = New-Stack; $remaining = Get-RemainingPercent $Provider
    $head = New-Row $Name ((Get-NumberText $remaining) + ' свободни') $Accent
    $stack.Children.Add($head) | Out-Null; Add-Gap $stack 8
    foreach ($w in @(Get-PropertyValue $Provider 'windows' @())) {
        $label = [string](Get-PropertyValue $w 'label' '')
        $caption = switch -Regex ($label) { '^5h$' { '5 часа' }; '^(1w|7d)$' { 'Седмица' }; default { $label } }
        $stack.Children.Add((New-Row $caption ((Get-NumberText (Get-RemainingPercent $w)) + ' остават'))) | Out-Null
    }
    $track = [Windows.Controls.Grid]::new(); $track.Height = 4; $track.Margin = [Windows.Thickness]::new(0, 7, 0, 9)
    $back = [Windows.Controls.Border]::new(); $back.Background = New-Brush '#2B394E'; $back.CornerRadius = [Windows.CornerRadius]::new(2); $track.Children.Add($back) | Out-Null
    $bar = [Windows.Controls.Border]::new(); $bar.Background = New-Brush $Accent; $bar.CornerRadius = [Windows.CornerRadius]::new(2); $bar.HorizontalAlignment = 'Left'
    if ($null -ne $remaining) { $bar.Width = [Math]::Max(0, [Math]::Min(100, [double]$remaining)) * 2.75 }
    $track.Children.Add($bar) | Out-Null; $stack.Children.Add($track) | Out-Null
    $windows = @(Get-PropertyValue $Provider 'windows' @())
    if ($windows.Count -gt 0) { $stack.Children.Add((New-Text ('Следващ reset: ' + (Get-CountdownText (Get-PropertyValue $windows[0] 'resets_at'))) 10 '#91A3BC')) | Out-Null }
    if (-not [bool](Get-PropertyValue $Provider 'available' $false)) { $stack.Children.Add((New-Text 'Данните не са налични' 11 '#F2C779')) | Out-Null }
    $stack.Children.Add((New-Text ('Данни: ' + (Get-FreshnessText (Get-PropertyValue $Provider 'updated_at'))) 10 '#91A3BC')) | Out-Null
    if (Get-PropertyValue $Provider 'error') { $stack.Children.Add((New-Text 'Последни известни данни / грешка: виж „Лимити“' 10 '#F2C779')) | Out-Null }
    $stack.ToolTip = Get-FreshnessText (Get-PropertyValue $Provider 'updated_at')
    return New-Card $stack
}
function New-SessionRow {
    param($Session)
    $sessionPid = [int](Get-PropertyValue $Session 'pid' 0)
    $start = [string](Get-PropertyValue $Session 'process_started' '')
    $handle = if ($script:DesktopReady -and $start) { [DesktopWindows]::ResolveWindow($sessionPid) } else { 0 }
    $verified = $handle -ne 0 -and $start -eq [DesktopWindows]::ProcessStarted($sessionPid)
    $row = New-PillButton ''; $row.HorizontalContentAlignment = [Windows.HorizontalAlignment]::Stretch
    $row.Margin = [Windows.Thickness]::new(0, 0, 0, 9); $row.Padding = [Windows.Thickness]::new(12)
    $row.IsEnabled = $verified
    $row.Tag = [pscustomobject]@{ pid = $sessionPid; started = $start; hwnd = $handle }
    $stack = New-Stack
    $provider = [string](Get-PropertyValue $Session 'provider' ''); $title = [string](Get-PropertyValue $Session 'title' '')
    $project = [string](Get-PropertyValue $Session 'project' '')
    if (-not $title) { $title = $provider + ' · ' + $sessionPid }
    $state = [string](Get-PropertyValue $Session 'status' 'unknown')
    $labels = @{ active = 'работи'; idle = 'изчаква'; unknown = 'статус —' }
    $stateLabel = if ($labels.ContainsKey($state)) { $labels[$state] } else { $state }
    $accent = if ($provider -eq 'codex') { '#C1ADFF' } else { '#8CE6C5' }
    $stack.Children.Add((New-Row $provider.ToUpperInvariant() $stateLabel $accent)) | Out-Null
    $titleText = New-Text $title 13 '#F0F5FF' ([Windows.FontWeights]::SemiBold)
    $titleText.TextTrimming = [Windows.TextTrimming]::CharacterEllipsis; $titleText.TextWrapping = [Windows.TextWrapping]::NoWrap
    $titleText.Margin = [Windows.Thickness]::new(0, 5, 0, 4); $stack.Children.Add($titleText) | Out-Null
    $location = if ($verified) { if ([DesktopWindows]::IsCurrent([IntPtr]$handle)) { 'Този десктоп  ↗' } else { 'Към десктопа  ↗' } } else { 'Няма сигурна връзка с прозорец' }
    if ($verified) { $number = [DesktopWindows]::DesktopNumber([IntPtr]$handle); if ($number -gt 0) { $location = 'Десктоп ' + $number + '  ↗' } }
    $stack.Children.Add((New-Text (($project + '   ·   ' + $location).Trim(' ', '·')) 10 '#91A3BC')) | Out-Null
    $row.Content = $stack; $row.ToolTip = $title + [Environment]::NewLine + $location
    $row.Add_Click({ param($sender, $eventArgs) Invoke-SessionWindow $sender.Tag })
    return $row
}
function Invoke-SessionWindow {
    param($Target)
    if ([DesktopWindows]::ActivateSession([int]$Target.pid, [string]$Target.started, [long]$Target.hwnd)) {
        if (-not $script:PinOpen) { Set-Expanded $false }
    } else {
        $script:Notice = 'Прозорецът се е променил или Windows отказа фокуса. Опитай отново.'
        Update-Panel
    }
}
function Update-CompactLabel {
    $lines = @('ОСТАВА')
    foreach ($provider in @(@('claude', 'CL'), @('codex', 'CX'))) {
        $data = Get-PropertyValue $script:Snapshot $provider[0]
        $windows = @(Get-PropertyValue $data 'windows' @())
        foreach ($period in @(@('5h', '^5h$'), @('7d', '^(1w|7d)$'))) {
            $matched = @($windows | Where-Object { [string](Get-PropertyValue $_ 'label' '') -match $period[1] })
            $remaining = $null
            if ($matched.Count -gt 0 -and [bool](Get-PropertyValue $data 'available' $false)) { $remaining = Get-RemainingPercent $matched[0] }
            $lines += '{0} {1} {2}' -f $provider[1], $period[0], (Get-NumberText $remaining)
        }
    }
    $sys = Get-PropertyValue $script:Telemetry 'system'
    $stamp = Convert-ToUtcDate (Get-PropertyValue $script:Telemetry 'updated_at')
    if ($null -eq $stamp -or ([DateTimeOffset]::UtcNow - $stamp).TotalSeconds -gt 30) { $sys = $null }
    $lines += ''; $lines += 'CPU ' + (Get-NumberText (Get-PropertyValue $sys 'cpu_percent'))
    $lines += 'RAM ' + (Get-NumberText (Get-PropertyValue $sys 'ram_percent'))
    $disks = @(Get-PropertyValue $sys 'disks' @())
    $disk = @($disks | Where-Object { [string](Get-PropertyValue $_ 'drive' '') -match '^C:' } | Select-Object -First 1)
    if ($disk.Count -eq 0 -and $disks.Count -gt 0) { $disk = @($disks[0]) }
    if ($disk.Count -gt 0 -and $null -ne (Get-PropertyValue $disk[0] 'free_gb')) {
        $lines += '{0} {1:0.0} GB' -f ([string](Get-PropertyValue $disk[0] 'drive')).TrimEnd('\'), [double](Get-PropertyValue $disk[0] 'free_gb')
    } else { $lines += 'Диск —' }
    $counts = Get-PropertyValue $script:Telemetry 'session_counts'
    $a = Get-PropertyValue $counts 'claude'; $b = Get-PropertyValue $counts 'codex'
    $count = if ($null -eq $a -or $null -eq $b) { '?' } else { [int]$a + [int]$b }
    $lines += ''; $lines += "● $count сесии"
    $script:CompactLabel.Text = $lines -join "`n"
}
function Update-Panel {
    if ([Windows.Input.Mouse]::LeftButton -eq [Windows.Input.MouseButtonState]::Pressed) { return }
    if ($script:Window.IsVisible -and ([DateTime]::UtcNow - $script:LastPinCheck).TotalSeconds -gt 30) {
        $script:LastPinCheck = [DateTime]::UtcNow
        $hwnd = [Windows.Interop.WindowInteropHelper]::new($script:Window).Handle
        $script:DesktopPinned = [DesktopWindows]::IsPinned($hwnd)
        if (-not $script:DesktopPinned) { Initialize-DesktopIntegration }
    }
    $script:Snapshot = Read-Snapshot; $script:Telemetry = Read-Telemetry
    foreach ($provider in @('claude','codex')) { $script:Snapshot.$provider = Protect-ProviderDisplay $script:Snapshot.$provider }
    Update-CompactLabel
    if ($null -ne $script:TrayIcon) {
        $tip = 'Agent Dock | CL ' + (Get-NumberText (Get-RemainingPercent $script:Snapshot.claude)) + ' | CX ' + (Get-NumberText (Get-RemainingPercent $script:Snapshot.codex))
        $script:TrayIcon.Text = $tip.Substring(0,[Math]::Min(63,$tip.Length))
    }
    if ($null -ne $script:ContentScroll) { $script:Offsets[$script:SelectedPage] = $script:ContentScroll.VerticalOffset }
    if ($script:DesktopReady) { [DesktopWindows]::RefreshWindows() }
    $outer = [Windows.Controls.Grid]::new(); $outer.Margin = [Windows.Thickness]::new(18, 20, 18, 15)
    foreach ($height in @(64, 46, -1, 64)) { $r = [Windows.Controls.RowDefinition]::new(); if ($height -lt 0) { $r.Height = [Windows.GridLength]::new(1, [Windows.GridUnitType]::Star) } else { $r.Height = [Windows.GridLength]::new($height) }; $outer.RowDefinitions.Add($r) }
    $header = [Windows.Controls.Grid]::new(); $brand = New-Stack
    $brand.Children.Add((New-Text 'AGENT DOCK' 16 '#F0F5FF' ([Windows.FontWeights]::SemiBold))) | Out-Null
    $desktopText = if ($script:DesktopPinned) { '●  На всичките ' + [DesktopWindows]::DesktopCount() + ' десктопа' } else { '○  Закрепването е недостъпно' }
    $brand.Children.Add((New-Text $desktopText 10 '#8CE6C5')) | Out-Null; $header.Children.Add($brand) | Out-Null
    $actions = New-Stack; $actions.Orientation = [Windows.Controls.Orientation]::Horizontal; $actions.HorizontalAlignment = [Windows.HorizontalAlignment]::Right; $actions.VerticalAlignment = [Windows.VerticalAlignment]::Top
    $pin = New-PillButton '◇'; $pin.ToolTip = 'Дръж отворен и след избор на сесия'; $pin.Margin = [Windows.Thickness]::new(0, 0, 6, 0)
    if ($script:PinOpen) { $pin.Background = New-Brush '#285848'; $pin.Content = '◆' }
    $pin.Add_Click({ $script:PinOpen = -not $script:PinOpen; Update-Panel }); $actions.Children.Add($pin) | Out-Null
    $collapse = New-PillButton '‹'; $collapse.ToolTip = 'Свий панела'; $collapse.Add_Click({ Set-Expanded $false }); $actions.Children.Add($collapse) | Out-Null
    $header.Children.Add($actions) | Out-Null; $outer.Children.Add($header) | Out-Null
    $nav = [Windows.Controls.Primitives.UniformGrid]::new(); $nav.Rows = 1
    $titles = @('Обзор', 'Лимити', 'Сесии', 'Система')
    for ($i = 0; $i -lt 4; $i++) {
        $button = New-PillButton $titles[$i] '#141E2C'; $button.Tag = $i; $button.Margin = [Windows.Thickness]::new(0, 0, 5, 10); $button.Padding = [Windows.Thickness]::new(5, 7, 5, 7)
        if ($i -eq $script:SelectedPage) { $button.Background = New-Brush '#324B49'; $button.Foreground = New-Brush '#B6F4DD'; $button.BorderBrush = New-Brush '#52766B' }
        $button.Add_Click({ param($sender, $eventArgs) Set-DockPage ([int]$sender.Tag) }); $nav.Children.Add($button) | Out-Null
    }
    [Windows.Controls.Grid]::SetRow($nav, 1); $outer.Children.Add($nav) | Out-Null
    $content = New-Stack; $content.Margin = [Windows.Thickness]::new(0, 6, 4, 2)
    $sys = Get-PropertyValue $script:Telemetry 'system'; $sessions = @(Get-PropertyValue $script:Telemetry 'sessions' @())
    $counts = Get-PropertyValue $script:Telemetry 'session_counts'
    switch ($script:SelectedPage) {
        0 {
            $content.Children.Add((New-SectionTitle 'Всичко под контрол.' 'Ресурсът и работата ти — с един поглед.')) | Out-Null
            $content.Children.Add((New-QuotaSummary 'Claude' (Get-PropertyValue $script:Snapshot 'claude') '#8CE6C5')) | Out-Null
            $content.Children.Add((New-QuotaSummary 'Codex' (Get-PropertyValue $script:Snapshot 'codex') '#C1ADFF')) | Out-Null
            $s = New-Stack; $active = @($sessions | Where-Object { (Get-PropertyValue $_ 'status') -eq 'active' }).Count
            $s.Children.Add((New-Row 'СЕСИИ' ($sessions.Count.ToString() + ' отворени') '#C1ADFF')) | Out-Null
            $s.Children.Add((New-Text ($active.ToString() + ' работят · Claude ' + (Get-PropertyValue $counts 'claude' '?') + ' / Codex ' + (Get-PropertyValue $counts 'codex' '?')) 11 '#91A3BC')) | Out-Null
            Add-Gap $s 10; Add-Drilldown $s 'Отвори сесиите  ↗' 2; $content.Children.Add((New-Card $s)) | Out-Null
            $s = New-Stack
            $s.Children.Add((New-Row 'CPU / RAM' ((Get-NumberText (Get-PropertyValue $sys 'cpu_percent')) + ' / ' + (Get-NumberText (Get-PropertyValue $sys 'ram_percent'))) (Get-MetricColor (Get-PropertyValue $sys 'ram_percent')))) | Out-Null
            foreach ($d in @(Get-PropertyValue $sys 'disks' @())) { $s.Children.Add((New-Text ('{0}  {1:0.0} GB свободни' -f (Get-PropertyValue $d 'drive'), [double](Get-PropertyValue $d 'free_gb')) 11 (Get-MetricColor (Get-PropertyValue $d 'used_percent')))) | Out-Null }
            Add-Gap $s 8; Add-Drilldown $s 'Системни показатели  ↗' 3; $content.Children.Add((New-Card $s)) | Out-Null
        }
        1 {
            $content.Children.Add((New-SectionTitle 'Лимити и reset' 'Използвано, оставащо и точен местен час.')) | Out-Null
            $content.Children.Add((New-ProviderBlock 'Claude' (Get-PropertyValue $script:Snapshot 'claude'))) | Out-Null
            $content.Children.Add((New-ProviderBlock 'Codex' (Get-PropertyValue $script:Snapshot 'codex'))) | Out-Null
        }
        2 {
            $content.Children.Add((New-SectionTitle 'Работните ти сесии' 'Клик → към конзолата на нейния десктоп.')) | Out-Null
            if ($sessions.Count -eq 0) { $content.Children.Add((New-Text 'Няма отчетени сесии.' 12 '#91A3BC')) | Out-Null }
            foreach ($session in $sessions) { $content.Children.Add((New-SessionRow $session)) | Out-Null }
            foreach ($note in @(Get-PropertyValue $script:Telemetry 'notes' @())) { $content.Children.Add((New-Text ([string]$note) 10 '#91A3BC')) | Out-Null; Add-Gap $content 6 }
        }
        3 {
            $content.Children.Add((New-SectionTitle 'Твоят компютър' 'Локално измерване · без AI разход.')) | Out-Null
            $content.Children.Add((New-WidgetSettingsCard)) | Out-Null
            foreach ($metric in @(@('Процесор', 'cpu_percent'), @('Памет', 'ram_percent'))) {
                $s = New-Stack; $value = Get-PropertyValue $sys $metric[1]
                $s.Children.Add((New-Text $metric[0] 12 '#91A3BC')) | Out-Null
                $s.Children.Add((New-Text (Get-NumberText $value) 34 (Get-MetricColor $value) ([Windows.FontWeights]::SemiBold))) | Out-Null
                if ($metric[1] -eq 'ram_percent' -and $null -ne (Get-PropertyValue $sys 'ram_total_gb')) { $s.Children.Add((New-Text ('{0:0.0} от {1:0.0} GB' -f [double](Get-PropertyValue $sys 'ram_used_gb'), [double](Get-PropertyValue $sys 'ram_total_gb')) 11 '#91A3BC')) | Out-Null }
                $content.Children.Add((New-Card $s)) | Out-Null
            }
            foreach ($d in @(Get-PropertyValue $sys 'disks' @())) {
                $s = New-Stack; $s.Children.Add((New-Text ('Диск ' + (Get-PropertyValue $d 'drive')) 12 '#91A3BC')) | Out-Null
                $s.Children.Add((New-Text ('{0:0.0} GB' -f [double](Get-PropertyValue $d 'free_gb')) 30 (Get-MetricColor (Get-PropertyValue $d 'used_percent')) ([Windows.FontWeights]::SemiBold))) | Out-Null
                $s.Children.Add((New-Text ('свободни от {0:0.0} GB' -f [double](Get-PropertyValue $d 'total_gb')) 11 '#91A3BC')) | Out-Null
                $content.Children.Add((New-Card $s)) | Out-Null
            }
        }
    }
    if ($script:Notice) { $content.Children.Add((New-Text $script:Notice 11 '#F2C779')) | Out-Null }
    $scroll = [Windows.Controls.ScrollViewer]::new(); $scroll.VerticalScrollBarVisibility = 'Auto'; $scroll.HorizontalScrollBarVisibility = 'Disabled'; $scroll.Content = $content
    [Windows.Controls.Grid]::SetRow($scroll, 2); $outer.Children.Add($scroll) | Out-Null
    $fresh = 'CL: ' + (Get-FreshnessText (Get-PropertyValue $script:Snapshot.claude 'updated_at'))
    $fresh += "`nCX: " + (Get-FreshnessText (Get-PropertyValue $script:Snapshot.codex 'updated_at'))
    $fresh += "`nUsage: ~60 сек · CPU/RAM: ~5 сек"
    $freshColor = '#91A3BC'
    if (-not $script:Snapshot.claude.available -or -not $script:Snapshot.codex.available) { $freshColor = '#F2C779' }
    $footer = New-Text $fresh 10 $freshColor; $footer.VerticalAlignment = 'Bottom'; [Windows.Controls.Grid]::SetRow($footer, 3); $outer.Children.Add($footer) | Out-Null
    $script:Panel.Child = $outer; $script:ContentScroll = $scroll; $scroll.ScrollToVerticalOffset($script:Offsets[$script:SelectedPage])
}

function Set-DockMode {
    param([ValidateSet('idle', 'preview', 'expanded')][string]$Mode)
    # Anchor the handle's centre, so hover expansion never jumps away from the cursor.
    $centre = $script:Window.Top + $script:Window.Height / 2
    if ($script:CompactButton.VerticalAlignment -eq [Windows.VerticalAlignment]::Top) {
        $centre = $script:Window.Top + $script:CompactButton.Margin.Top + $script:CompactButton.Height / 2
    }
    if ($null -ne $script:HoverLeaveTimer) { $script:HoverLeaveTimer.Stop() }
    $script:IsExpanded = $Mode -eq 'expanded'
    $script:IsHoverPreview = $Mode -eq 'preview'
    $mini = $Mode -eq 'idle'
    $handleWidth = if ($mini) { 28 } else { 84 }
    $handleHeight = if ($mini) { 84 } else { 276 }
    $script:CompactButton.Width = $handleWidth; $script:CompactButton.Height = $handleHeight
    $script:CompactLabel.Visibility = if ($mini) { 'Collapsed' } else { 'Visible' }
    $script:Grip.Visibility = if ($mini) { 'Collapsed' } else { 'Visible' }
    $script:LogoMark.Width = if ($mini) { 24 } else { 30 }
    $script:LogoMark.Height = $script:LogoMark.Width
    $script:CompactButton.CornerRadius = [Windows.CornerRadius]::new(0, $(if ($mini) { 12 } else { 20 }), $(if ($mini) { 12 } else { 20 }), 0)
    $area = [Windows.SystemParameters]::WorkArea
    if ($script:IsExpanded) {
        $script:Window.Width = 454; $script:Window.Height = [Math]::Min(780, $area.Height)
        $script:Panel.Visibility = 'Visible'; $script:PanelColumn.Width = [Windows.GridLength]::new(370)
    } else {
        $script:Window.Width = $handleWidth; $script:Window.Height = $handleHeight
        $script:Panel.Visibility = 'Collapsed'; $script:PanelColumn.Width = [Windows.GridLength]::new(0)
    }
    $script:Window.Top = [Math]::Max($area.Top, [Math]::Min($area.Bottom - $script:Window.Height, $centre - $script:Window.Height / 2))
    $handleTop = [Math]::Max(0, [Math]::Min($script:Window.Height - $handleHeight, $centre - $script:Window.Top - $handleHeight / 2))
    $script:CompactButton.VerticalAlignment = 'Top'
    $script:CompactButton.Margin = [Windows.Thickness]::new(0, $handleTop, 0, 0)
    Update-Panel
}
function Set-Expanded {
    param([bool]$Expanded)
    Set-DockMode $(if ($Expanded) { 'expanded' } else { 'idle' })
}
function Complete-HoverLeave {
    param([bool]$PointerInside)
    if ($script:IsHoverPreview -and -not $script:IsExpanded -and -not $PointerInside -and $null -eq $script:DragStart) {
        Set-DockMode 'idle'
    }
}

function Position-OnPrimaryWorkArea {
    $area = [Windows.SystemParameters]::WorkArea
    $script:Window.Left = $area.Left
    $script:Window.Top = $area.Top + [Math]::Max(0, ($area.Height - $script:Window.Height) / 2)
}

function Save-WidgetScreenshot {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or $null -eq $script:Window) { return }
    try {
        $width = [Math]::Max(1, [int][Math]::Ceiling($script:Window.ActualWidth))
        $height = [Math]::Max(1, [int][Math]::Ceiling($script:Window.ActualHeight))
        $bitmap = [Windows.Media.Imaging.RenderTargetBitmap]::new($width, $height, 96, 96, [Windows.Media.PixelFormats]::Pbgra32)
        $bitmap.Render($script:Window)
        $encoder = [Windows.Media.Imaging.PngBitmapEncoder]::new(); $encoder.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
        $parent = Split-Path -Parent $Path
        if (-not [string]::IsNullOrWhiteSpace($parent)) { [IO.Directory]::CreateDirectory($parent) | Out-Null }
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $encoder.Save($stream) } finally { $stream.Dispose() }
    } catch {
        # Screenshot is an optional QA aid; never interrupt the widget.
    }
}

function Acquire-SingleInstance {
    $created = $false
    try { $script:Mutex = [Threading.Mutex]::new($true, 'Local\MPAgentDockWidget', [ref]$created) } catch { return $false }
    if (-not $created) { if ($null -ne $script:Mutex) { $script:Mutex.Dispose(); $script:Mutex = $null }; return $false }
    return $true
}

function New-WidgetWindow {
    $window = [Windows.Window]::new()
    $window.ShowActivated = $false
    $scrollStyle = [Windows.Markup.XamlReader]::Parse(@'
<Style xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" TargetType="ScrollBar">
 <Setter Property="Width" Value="6"/><Setter Property="Background" Value="Transparent"/>
 <Setter Property="Template"><Setter.Value><ControlTemplate TargetType="ScrollBar">
  <Track x:Name="PART_Track" IsDirectionReversed="True" Orientation="Vertical">
   <Track.DecreaseRepeatButton><RepeatButton Command="ScrollBar.PageUpCommand" Opacity="0" Focusable="False"/></Track.DecreaseRepeatButton>
   <Track.Thumb><Thumb><Thumb.Template><ControlTemplate TargetType="Thumb"><Border Background="#40546B" CornerRadius="3" Margin="1,0"/></ControlTemplate></Thumb.Template></Thumb></Track.Thumb>
   <Track.IncreaseRepeatButton><RepeatButton Command="ScrollBar.PageDownCommand" Opacity="0" Focusable="False"/></Track.IncreaseRepeatButton>
  </Track>
 </ControlTemplate></Setter.Value></Setter>
</Style>
'@)
    $window.Resources.Add([Windows.Controls.Primitives.ScrollBar], $scrollStyle)
    $progressStyle = [Windows.Markup.XamlReader]::Parse(@'
<Style xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" TargetType="ProgressBar">
 <Setter Property="Template"><Setter.Value><ControlTemplate TargetType="ProgressBar">
  <Grid x:Name="PART_Track"><Border Background="{TemplateBinding Background}" CornerRadius="3"/>
   <Border x:Name="PART_Indicator" HorizontalAlignment="Left" Background="{TemplateBinding Foreground}" CornerRadius="3"/>
  </Grid>
 </ControlTemplate></Setter.Value></Setter>
</Style>
'@)
    $window.Resources.Add([Windows.Controls.ProgressBar], $progressStyle)
    $window.Title = 'MP Agent Dock'; $window.Width = 28; $window.Height = 84; $window.MinWidth = 28; $window.WindowStyle = [Windows.WindowStyle]::None; $window.ResizeMode = [Windows.ResizeMode]::NoResize; $window.ShowInTaskbar = $false; $window.Topmost = $true; $window.AllowsTransparency = $true; $window.Background = [Windows.Media.Brushes]::Transparent
    $root = [Windows.Controls.Grid]::new(); $root.Background = [Windows.Media.Brushes]::Transparent
    $root.ColumnDefinitions.Add([Windows.Controls.ColumnDefinition]::new())
    $panelCol = [Windows.Controls.ColumnDefinition]::new(); $panelCol.Width = [Windows.GridLength]::new(0); $root.ColumnDefinitions.Add($panelCol); $script:PanelColumn = $panelCol
    $compact = [Windows.Controls.Border]::new(); $compact.Background = New-Brush '#172235'; $compact.BorderBrush = New-Brush '#2E4565'; $compact.BorderThickness = [Windows.Thickness]::new(0, 1, 1, 1); $compact.CornerRadius = [Windows.CornerRadius]::new(0, 12, 12, 0); $compact.Cursor = [Windows.Input.Cursors]::Hand; $compact.ToolTip = 'Agent Dock · Hover: показатели · Клик: панел'; $compact.Height = 84; $compact.VerticalAlignment = [Windows.VerticalAlignment]::Center
    $compactStack = [Windows.Controls.StackPanel]::new(); $compactStack.VerticalAlignment = [Windows.VerticalAlignment]::Center
    $logoPath = Join-Path $PSScriptRoot 'assets\agent-dock-logo.png'
    if (Test-Path -LiteralPath $logoPath) {
        $mark = [Windows.Controls.Image]::new(); $bitmap = [Windows.Media.Imaging.BitmapImage]::new()
        $bitmap.BeginInit(); $bitmap.CacheOption = [Windows.Media.Imaging.BitmapCacheOption]::OnLoad; $bitmap.UriSource = [Uri]::new($logoPath); $bitmap.DecodePixelWidth = 96; $bitmap.EndInit(); $bitmap.Freeze()
        $mark.Source = $bitmap; $mark.Stretch = 'Uniform'
    } else { $mark = New-Text '◈' 20 '#8CE6C5' ([Windows.FontWeights]::SemiBold) }
    $mark.Width = 24; $mark.Height = 24; $mark.HorizontalAlignment = 'Center'; $script:LogoMark = $mark; $compactStack.Children.Add($mark) | Out-Null
    $script:CompactLabel = New-Text "CL —`nCX —" 10 '#DCE6F5' ([Windows.FontWeights]::SemiBold); $script:CompactLabel.TextAlignment = [Windows.TextAlignment]::Center; $script:CompactLabel.Margin = [Windows.Thickness]::new(0, 6, 0, 0); $compactStack.Children.Add($script:CompactLabel) | Out-Null
    $grip = New-Text '⋮' 18 '#7488A5'; $grip.HorizontalAlignment = [Windows.HorizontalAlignment]::Center; $compactStack.Children.Add($grip) | Out-Null
    $script:Grip = $grip; $grip.Visibility = 'Collapsed'; $script:CompactLabel.Visibility = 'Collapsed'
    $compact.Child = $compactStack
    $compact.Focusable = $true
    $script:HoverLeaveTimer = [Windows.Threading.DispatcherTimer]::new()
    $script:HoverLeaveTimer.Interval = [TimeSpan]::FromMilliseconds(250)
    $script:HoverLeaveTimer.Add_Tick({
        $script:HoverLeaveTimer.Stop()
        $hwnd = [Windows.Interop.WindowInteropHelper]::new($script:Window).Handle
        Complete-HoverLeave ([DesktopWindows]::IsPointerInside($hwnd))
    })
    $compact.Add_MouseEnter({
        $script:HoverLeaveTimer.Stop()
        if (-not $script:IsExpanded -and -not $script:IsHoverPreview -and $null -eq $script:DragStart) { Set-DockMode 'preview' }
    })
    $compact.Add_MouseLeftButtonDown({
        $script:DragStart = $_.GetPosition($script:Window).Y + $script:Window.Top
        $script:DragTop = $script:Window.Top; $script:DragMoved = $false
        $script:CompactButton.CaptureMouse() | Out-Null
        $_.Handled = $true
    })
    $compact.Add_MouseMove({
        if ($null -ne $script:DragStart -and $_.LeftButton -eq [Windows.Input.MouseButtonState]::Pressed) {
            $delta = $_.GetPosition($script:Window).Y + $script:Window.Top - $script:DragStart
            if ([Math]::Abs($delta) -gt 4) { $script:DragMoved = $true }
            if ($script:DragMoved) { $script:Window.Top = [Math]::Max([Windows.SystemParameters]::WorkArea.Top, [Math]::Min([Windows.SystemParameters]::WorkArea.Bottom - $script:Window.Height, $script:DragTop + $delta)) }
        }
    })
    $compact.Add_MouseLeftButtonUp({
        if ($null -eq $script:DragStart) { return }
        $wasMoved = $script:DragMoved
        $script:DragStart = $null; $script:DragMoved = $false
        $script:CompactButton.ReleaseMouseCapture()
        if (-not $wasMoved) { Set-Expanded (-not $script:IsExpanded) }
        $_.Handled = $true
    })
    $compact.Add_KeyDown({ if ($_.Key -in @([Windows.Input.Key]::Enter, [Windows.Input.Key]::Space)) { Set-Expanded (-not $script:IsExpanded); $_.Handled = $true } })
    $root.Children.Add($compact) | Out-Null
    $script:Panel = [Windows.Controls.Border]::new(); $script:Panel.Background = New-Brush '#111827'; $script:Panel.BorderBrush = New-Brush '#26364E'; $script:Panel.BorderThickness = [Windows.Thickness]::new(1); $script:Panel.CornerRadius = [Windows.CornerRadius]::new(22); $script:Panel.Margin = [Windows.Thickness]::new(5, 5, 5, 5); $script:Panel.Visibility = [Windows.Visibility]::Collapsed; [Windows.Controls.Grid]::SetColumn($script:Panel, 1); $root.Children.Add($script:Panel) | Out-Null
    $window.UseLayoutRounding = $true; $root.SnapsToDevicePixels = $true; $window.Content = $root; $script:CompactButton = $compact; $script:Window = $window
    $menu = [Windows.Controls.ContextMenu]::new()
    $trayToggle = [Windows.Controls.MenuItem]::new(); $trayToggle.Header = 'Икона до часовника'; $trayToggle.IsCheckable = $true; $trayToggle.IsChecked = $script:WidgetSettings.show_tray_icon
    $trayToggle.Add_Click({ param($sender,$eventArgs) Set-TrayEnabled ([bool]$sender.IsChecked) }); $menu.Items.Add($trayToggle) | Out-Null; $script:TrayToggleItem = $trayToggle
    $quit = [Windows.Controls.MenuItem]::new(); $quit.Header = 'Изход'; $quit.Add_Click({ $script:Window.Close() }); $menu.Items.Add($quit) | Out-Null; $compact.ContextMenu = $menu
    $window.Add_SourceInitialized({ Position-OnPrimaryWorkArea })
    $window.Add_MouseLeave({ if ($script:IsHoverPreview -and -not $script:IsExpanded) { $script:HoverLeaveTimer.Stop(); $script:HoverLeaveTimer.Start() } })
    # MouseLeave can fire during resizing, transparent gaps and content refresh.
    # A click-open panel stays open until explicit close (or session activation).
    $window.Add_PreviewKeyDown({ if ($_.Key -eq [Windows.Input.Key]::Escape) { Set-Expanded $false; $_.Handled = $true } })
    $window.Add_Closed({ if ($null -ne $script:HoverLeaveTimer) { $script:HoverLeaveTimer.Stop() } })
    $window.Add_Closed({ Write-WidgetEvent 'closed'; Dispose-WidgetTray })
    $window.Add_Closed({ if ($null -ne $script:RefreshTimer) { $script:RefreshTimer.Stop() }; if ($null -ne $script:Mutex) { $script:Mutex.ReleaseMutex(); $script:Mutex.Dispose(); $script:Mutex = $null }; [Windows.Threading.Dispatcher]::CurrentDispatcher.InvokeShutdown() })
    return $window
}

try {
    Add-WpfAssemblies
    Initialize-DesktopIntegration
    if ($ValidateOnly) {
        # Construct all controls/templates without showing a window.
        $null = New-WidgetWindow
        Update-Panel
        Write-Output 'widget.ps1 validation OK'
        exit 0
    }
    if (-not (Acquire-SingleInstance)) { exit 0 }
    $script:WidgetSettings = Read-WidgetSettings
    $null = New-WidgetWindow
    try { Initialize-WidgetTray } catch {
        Write-WidgetEvent 'tray-error' $_.Exception.Message
        $script:WidgetSettings.show_edge_dock = $true
        $script:WidgetSettings.show_tray_icon = $false
        $script:TrayToggleItem.IsChecked = $false
        try { Save-WidgetSettings } catch { Write-WidgetEvent 'settings-save-error' $_.Exception.Message }
    }
    Write-WidgetEvent 'started' ('pid=' + $PID + ' tray=' + ($null -ne $script:TrayIcon -and $script:TrayIcon.Visible) + ' edge=' + $script:WidgetSettings.show_edge_dock)
    Update-Panel
    $script:RefreshTimer = [Windows.Threading.DispatcherTimer]::new()
    $script:RefreshTimer.Interval = [TimeSpan]::FromSeconds(2)
    $script:RefreshTimer.Add_Tick({ try { Update-Panel } catch { Write-WidgetEvent 'refresh-error' $_.Exception.Message } })
    $script:RefreshTimer.Start()
    if ($SmokeTestSeconds -gt 0) {
        $smokeTimer = [Windows.Threading.DispatcherTimer]::new(); $smokeTimer.Interval = [TimeSpan]::FromSeconds($SmokeTestSeconds); $smokeTimer.Add_Tick({ $smokeTimer.Stop(); $script:Window.Close() }); $smokeTimer.Start()
    }
    $script:Window.Show()
    Initialize-DesktopIntegration
    if (-not $script:WidgetSettings.show_edge_dock) { $script:Window.Hide() }
    if ($StartExpanded) { Set-Expanded $true }
    elseif ($StartPreview) { Set-DockMode 'preview' }
    if (-not [string]::IsNullOrWhiteSpace($ScreenshotPath)) {
        $captureTimer = [Windows.Threading.DispatcherTimer]::new(); $captureTimer.Interval = [TimeSpan]::FromMilliseconds(350); $captureTimer.Add_Tick({ $captureTimer.Stop(); Save-WidgetScreenshot $ScreenshotPath }); $captureTimer.Start()
    }
    [Windows.Threading.Dispatcher]::Run()
} catch {
    if ($ValidateOnly) { Write-Error ('widget.ps1 validation failed: ' + $_.Exception.Message); exit 1 }
    Write-WidgetEvent 'fatal-error' $_.Exception.Message
    Dispose-WidgetTray
    throw
}
