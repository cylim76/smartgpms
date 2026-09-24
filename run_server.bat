@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo smartGPMS environment is missing. Run setup.bat first.
  pause
  exit /b 1
)
if not defined SMARTGPMS_HOST set "SMARTGPMS_HOST=0.0.0.0"
if not defined SMARTGPMS_PORT set "SMARTGPMS_PORT=8765"
".venv\Scripts\python.exe" -m uvicorn app:app --host %SMARTGPMS_HOST% --port %SMARTGPMS_PORT% --workers 1
