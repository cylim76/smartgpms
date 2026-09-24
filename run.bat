@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo smartGPMS environment is missing. Run setup.bat first.
  pause
  exit /b 1
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" /b ".venv\Scripts\pythonw.exe" "tools\launch_ui.py"
) else (
  start "smartGPMS" http://127.0.0.1:8765
)
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765
