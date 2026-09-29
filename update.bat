@echo off
setlocal EnableExtensions
cd /d "%~dp0"

fltmc >nul 2>nul
if errorlevel 1 (
  echo Requesting administrator permission to update smartGPMS...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

where git >nul 2>nul
if errorlevel 1 goto :git_missing

sc query smartGPMS >nul 2>nul
if errorlevel 1 goto :service_missing

echo Stopping smartGPMS service...
sc query smartGPMS | findstr /C:"STOPPED" >nul
if errorlevel 1 (
  sc stop smartGPMS >nul
  if errorlevel 1 goto :stop_failed
  call :wait_for_state STOPPED 120
  if errorlevel 1 goto :stop_timeout
)

echo Pulling the latest smartGPMS code...
git pull --ff-only
set "PULL_EXIT=%ERRORLEVEL%"
if not "%PULL_EXIT%"=="0" goto :pull_failed

echo Starting smartGPMS service...
sc start smartGPMS >nul
if errorlevel 1 goto :start_failed
call :wait_for_state RUNNING 120
if errorlevel 1 goto :start_timeout

echo.
echo smartGPMS update completed successfully.
echo Open http://SERVER-IP:8765 and sign in again with OTP.
pause
exit /b 0

:wait_for_state
set "TARGET_STATE=%~1"
set /a "WAIT_LIMIT=%~2"
set /a "WAIT_COUNT=0"
:wait_loop
sc query smartGPMS | findstr /C:"%TARGET_STATE%" >nul
if not errorlevel 1 exit /b 0
set /a "WAIT_COUNT+=1"
if %WAIT_COUNT% GEQ %WAIT_LIMIT% exit /b 1
timeout /t 1 /nobreak >nul
goto :wait_loop

:pull_failed
echo.
echo Git pull failed. The existing service will be restarted without applying the update.
sc start smartGPMS >nul 2>nul
call :wait_for_state RUNNING 120
pause
exit /b %PULL_EXIT%

:git_missing
echo Git was not found. Install Git or add it to PATH, then try again.
pause
exit /b 1

:service_missing
echo The smartGPMS Windows service is not installed.
echo Run install_service.bat first.
pause
exit /b 1

:stop_failed
echo Failed to stop the smartGPMS service.
pause
exit /b 1

:stop_timeout
echo Timed out while waiting for the smartGPMS service to stop.
pause
exit /b 1

:start_failed
echo The update was downloaded, but the smartGPMS service could not be started.
echo Check data\service-logs and the Windows Event Viewer.
pause
exit /b 1

:start_timeout
echo Timed out while waiting for the smartGPMS service to start.
echo Check data\service-logs and the Windows Event Viewer.
pause
exit /b 1
