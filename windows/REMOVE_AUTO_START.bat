@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Remove
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Could not remove automatic startup. Read the error above.
pause
exit /b 1
