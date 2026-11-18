@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run the installer first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -I -X utf8 -m xiaojiu_brain --config "%~dp0private\config.json" stop
set "result=%errorlevel%"
if errorlevel 1 pause
exit /b %result%
