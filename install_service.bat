@echo off
setlocal EnableExtensions
cd /d "%~dp0"

fltmc >nul 2>nul
if errorlevel 1 (
  echo Requesting administrator permission to install smartGPMS service...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo smartGPMS environment is missing. Run setup_win.bat first.
  pause
  exit /b 1
)

set "SERVICE_DIR=%~dp0service"
set "SERVICE_EXE=%SERVICE_DIR%\smartGPMS.exe"
set "SERVICE_XML=%SERVICE_DIR%\smartGPMS.xml"
set "WINSW_URL=https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
set "WINSW_SHA256=05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA"

if not exist "%SERVICE_DIR%" mkdir "%SERVICE_DIR%"
copy /y "deploy\windows\smartGPMS.xml" "%SERVICE_XML%" >nul

if not exist "%SERVICE_EXE%" (
  echo Downloading WinSW 2.12.0 from the official GitHub release...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; for($attempt=1; $attempt -le 3; $attempt++){ try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 90 -Uri '%WINSW_URL%' -OutFile '%SERVICE_EXE%'; exit 0 } catch { if($attempt -eq 3){ Write-Error $_; exit 1 }; Start-Sleep -Seconds (2 * $attempt) } }"
  if errorlevel 1 goto :download_failed
)

for /f "tokens=*" %%H in ('powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '%SERVICE_EXE%').Hash"') do set "ACTUAL_SHA256=%%H"
if /i not "%ACTUAL_SHA256%"=="%WINSW_SHA256%" (
  echo WinSW checksum verification failed. The service was not installed.
  del /q "%SERVICE_EXE%" >nul 2>nul
  pause
  exit /b 1
)

sc query smartGPMS >nul 2>nul
if not errorlevel 1 (
  echo Existing smartGPMS service detected. Stopping and refreshing it...
  "%SERVICE_EXE%" stop >nul 2>nul
  "%SERVICE_EXE%" uninstall >nul 2>nul
)

netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if not errorlevel 1 goto :port_in_use

"%SERVICE_EXE%" install
if errorlevel 1 goto :install_failed

netsh advfirewall firewall delete rule name="smartGPMS TCP 8765" >nul 2>nul
netsh advfirewall firewall add rule name="smartGPMS TCP 8765" dir=in action=allow protocol=TCP localport=8765 profile=domain,private >nul

"%SERVICE_EXE%" start
if errorlevel 1 goto :start_failed

echo.
echo smartGPMS Windows service is installed and started.
echo Open http://SERVER-IP:8765 from a trusted LAN client.
echo Service commands: sc query smartGPMS ^| sc start smartGPMS ^| sc stop smartGPMS
echo Logs: data\service-logs
echo The service uses its own Windows DPAPI identity; sign in once after installation.
pause
exit /b 0

:download_failed
echo.
echo Failed to download WinSW. Check the network and try again.
del /q "%SERVICE_EXE%" >nul 2>nul
pause
exit /b 1

:install_failed
echo.
echo Failed to install the smartGPMS Windows service.
pause
exit /b 1

:start_failed
echo.
echo The service was installed but could not start.
echo Check data\service-logs and the Windows Event Viewer.
pause
exit /b 1

:port_in_use
echo.
echo TCP port 8765 is already in use. Close run_win.bat or the conflicting program,
echo then run install_service.bat again.
pause
exit /b 1
