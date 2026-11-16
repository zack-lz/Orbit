@echo off
setlocal
cd /d "%~dp0"
title Orbit Bridge
if exist ".venv\Scripts\python.exe" goto run
py -3.11 -c "import struct; assert struct.calcsize('P') == 8" >nul 2>&1
if not errorlevel 1 goto setup
if exist "%LocalAppData%\Python\pythoncore-3.11-64\python.exe" goto managedpython
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" goto localpython
echo Python 3.11 64-bit is required for the Windows media bridge.
echo Install Python 3.11 from https://www.python.org/downloads/windows/
echo Or run: winget install -e --id Python.Python.3.11 --scope user
echo Then close this window and double-click START.bat again.
start "" "https://www.python.org/downloads/release/python-3119/"
pause
exit /b 1
:localpython
"%LocalAppData%\Programs\Python\Python311\python.exe" -m venv .venv
if errorlevel 1 goto failed
goto install
:managedpython
"%LocalAppData%\Python\pythoncore-3.11-64\python.exe" -m venv .venv
if errorlevel 1 goto failed
goto install
:setup
py -3.11 -m venv .venv
if errorlevel 1 goto failed
:install
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
echo ready> .venv\remote-ready
:run
if not exist ".venv\remote-ready" goto install
.venv\Scripts\python.exe -c "import aiohttp, qrcode, yaml" >nul 2>&1
if errorlevel 1 goto install
.venv\Scripts\python.exe setup_claude.py
.venv\Scripts\python.exe background.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Setup or startup failed. Keep this window open and share the error text.
pause
exit /b 1
