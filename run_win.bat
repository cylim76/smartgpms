@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo smartGPMS environment is missing. Run setup_win.bat first.
  pause
  exit /b 1
)
set "SMARTGPMS_RUN_MODE=desktop"
set "SMARTGPMS_HOST=127.0.0.1"
if not defined SMARTGPMS_PORT set "SMARTGPMS_PORT=8765"
if exist ".venv\Scripts\pythonw.exe" (
  start "" /b ".venv\Scripts\pythonw.exe" "tools\launch_ui.py"
) else (
  start "smartGPMS" http://127.0.0.1:8765
)
".venv\Scripts\python.exe" "tools\run_desktop.py"
