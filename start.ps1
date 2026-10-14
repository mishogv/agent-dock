param([switch]$NoWidget)
$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { throw 'Python 3.10+ must be on PATH.' }
$taskPython = & $pythonCommand.Source -c "import pathlib, sys; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $taskPython)) { throw 'pythonw.exe is missing.' }
if ($NoWidget) {
    Start-Process -FilePath $taskPython -ArgumentList ('"{0}" --watch' -f (Join-Path $PSScriptRoot 'usage.py')) -WindowStyle Hidden
    Start-Process -FilePath $taskPython -ArgumentList ('"{0}" --watch' -f (Join-Path $PSScriptRoot 'telemetry.py')) -WindowStyle Hidden
} else {
    Start-Process -FilePath $taskPython -ArgumentList ('"{0}"' -f (Join-Path $PSScriptRoot 'launcher.py')) -WindowStyle Hidden
}
