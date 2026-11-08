@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run the installer first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m xiaojiu_brain --config "%~dp0private\config.json" stop
if errorlevel 1 pause
