@echo off
setlocal EnableExtensions
cd /d "%~dp0"

fltmc >nul 2>nul
if errorlevel 1 (
  echo Requesting administrator permission to uninstall smartGPMS service...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "SERVICE_EXE=%~dp0service\smartGPMS.exe"
if not exist "%SERVICE_EXE%" (
  echo WinSW service wrapper was not found. Checking Windows service registry...
  sc query smartGPMS >nul 2>nul
  if errorlevel 1 goto :already_removed
  sc stop smartGPMS >nul 2>nul
  sc delete smartGPMS
) else (
  "%SERVICE_EXE%" stop >nul 2>nul
  "%SERVICE_EXE%" uninstall
)

netsh advfirewall firewall delete rule name="smartGPMS TCP 8765" >nul 2>nul
echo.
echo smartGPMS Windows service and its firewall rule were removed.
echo Business data, cached photos, logs, and the local run environment were preserved.
pause
exit /b 0

:already_removed
netsh advfirewall firewall delete rule name="smartGPMS TCP 8765" >nul 2>nul
echo smartGPMS Windows service is not installed.
pause
exit /b 0
