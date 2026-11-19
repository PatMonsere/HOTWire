# HOTWire installer for a team Windows laptop.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#
# Creates .venv (Python 3.13), installs the requirements and adds
# "HOTWire" shortcuts to the Start menu and desktop. Run it again to update
# after a `git pull`.
#
# Why no .exe? Windows Smart App Control blocks unsigned executables and
# DLLs. HOTWire runs on the signed python.org Python instead.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = $null
try { & py -3.13 -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-3.13") } } catch {}
if (-not $py) {
    Write-Host "Python 3.13 not found." -ForegroundColor Red
    Write-Host "Install it from python.org (or: winget install Python.Python.3.13), then run this again."
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    & $py[0] $py[1] -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
& .\.venv\Scripts\python.exe -c "import PySide6, paramiko, websocket, msgpack, ruamel.yaml, numpy; print('Libraries OK')"
& .\.venv\Scripts\python.exe -c "from hotwire.nt4 import make_client; print('NetworkTables client:', make_client('auto', 5810)[1])"

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("Desktop"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "HOTWire.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m hotwire"
    $lnk.WorkingDirectory = $root
    $lnk.Description = "HOTWire - FRC 67 pit tool"
    $lnk.Save()
}
Write-Host "Installed. Start HOTWire from the Start menu or the desktop shortcut." -ForegroundColor Green
