@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=C:\Users\lg\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=py"
if not exist ".venv\Scripts\python.exe" %PYTHON_EXE% -m venv .venv
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo Installation complete. Run run.bat to start smartGPMS.
pause
