"""Start the collectors and WPF without a console or hidden-window inheritance."""
import os
from pathlib import Path
import subprocess
import sys
from config import app_state_dir

root = Path(__file__).resolve().parent
state = app_state_dir()
state.mkdir(parents=True, exist_ok=True)
flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
pythonw = Path(sys.executable).with_name('pythonw.exe')
with (state / 'startup.log').open('a', encoding='utf-8') as log:
    for collector in ('usage.py', 'telemetry.py'):
        subprocess.Popen([str(pythonw), str(root / collector), '--watch'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=log, creationflags=flags)
    subprocess.Popen(['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                      '-File', str(root / 'widget.ps1')], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, creationflags=flags)
