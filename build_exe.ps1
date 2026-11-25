# Build dist\HOTWire.exe (single file) with PyInstaller.
#
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# The .exe is unsigned: Windows Smart App Control (if on) will block it.
# On such laptops use install.bat instead.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = ".\.venv\Scripts\python.exe"

& $py -m pip install --quiet -r requirements.txt pyinstaller

# Qt modules HOTWire doesn't use (keeps the exe much smaller)
$exclude = @(
  "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
  "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
  "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtPdf",
  "PySide6.QtPdfWidgets", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtLocation",
  "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtBluetooth",
  "PySide6.QtNfc", "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp",
  "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
  "PySide6.QtStateMachine", "PySide6.QtHttpServer", "PySide6.QtWebSockets",
  "PySide6.QtSvgWidgets", "PySide6.QtUiTools", "PySide6.QtXml",
  # (pyqtgraph needs QtOpenGL, QtOpenGLWidgets, QtSvg and QtTest: keep those)
  "tkinter", "pytest"
) | ForEach-Object { "--exclude-module"; $_ }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name HOTWire `
  --icon assets\hotwire.ico `
  --add-data "examples\pit_checks.yaml;examples" `
  --add-data "assets\hotwire.ico;assets" `
  --collect-all ntcore --collect-all wpiutil --collect-all wpinet --collect-all native `
  @exclude `
  hotwire\__main__.py

Write-Host ("Built: " + (Resolve-Path dist\HOTWire.exe) + "  " + [math]::Round((Get-Item dist\HOTWire.exe).Length / 1MB, 1) + " MB") -ForegroundColor Green

# Release zip: the exe plus the current pit checks, so teams that already have an older
# pit_checks.yaml in their save folder can copy the new one over it.
$version = (& $py -c "import hotwire; print(hotwire.__version__)").Trim()
$stage = "dist\HOTWire-$version"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
Copy-Item dist\HOTWire.exe, examples\pit_checks.yaml $stage
@"
HOTWire $version

HOTWire.exe       run it, nothing to install (unsigned: "More info > Run anyway";
                  blocked where Smart App Control is on, use install.bat from the source there)
pit_checks.yaml   pit test checks for the current robot. HOTWire uses
                  <save folder>\pit_checks.yaml: copy this file over it to get the new checks
                  (your old one is replaced, so keep a copy if you changed it).
"@ | Set-Content -Encoding utf8 "$stage\README.txt"
$zip = "dist\HOTWire-$version.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "$stage\*" -DestinationPath $zip
Remove-Item -Recurse -Force $stage
Write-Host ("Release zip: " + (Resolve-Path $zip)) -ForegroundColor Green
Write-Host "Attach BOTH to the GitHub release: dist\HOTWire.exe and $zip"
