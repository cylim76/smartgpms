@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\setup_environment.py"
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 "tools\setup_environment.py"
  ) else (
    python "tools\setup_environment.py"
  )
)
if errorlevel 1 (
  echo.
  echo Installation failed. Check the messages above.
  pause
  exit /b 1
)
echo.
echo Installation complete. Run run.bat to start smartGPMS.
pause
