@echo off
setlocal
cd /d "%~dp0"
call START.bat
if errorlevel 1 exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1"
if errorlevel 1 goto failed
echo Setup complete. You can close this window.
pause
exit /b 0
:failed
echo Automatic startup could not be enabled. Read the error above.
pause
exit /b 1
