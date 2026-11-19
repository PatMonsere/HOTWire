@echo off
rem Double-click to install or update HOTWire (runs install.ps1 without changing the system's script policy).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
