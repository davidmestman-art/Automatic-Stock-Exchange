@echo off
title NYSE Trading Engine
cd /d "%~dp0"

echo.
echo  =============================================
echo    NYSE Trading Engine  —  Starting up...
echo  =============================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Make sure Python is installed and in your PATH.
    echo  Download from https://python.org
    pause
    exit /b 1
)

echo  [1/3] Checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  WARNING: Some packages may not have installed correctly.
    echo  Try running:  pip install -r requirements.txt
    echo.
)

echo  [2/3] Starting dashboard server...
REM Start dashboard in a separate minimised window so this window can open the browser
start "NYSE Dashboard Server" /min python dashboard.py

echo  [3/3] Opening browser...
REM Wait 5 seconds for the server to start before opening the browser
timeout /t 5 /nobreak > nul
start http://localhost:8080

echo.
echo  Dashboard is running at http://localhost:8080
echo.
echo  TIP: If you have NGROK_AUTHTOKEN set in your .env file the public
echo       URL will be printed in the "NYSE Dashboard Server" window so
echo       you can access the dashboard from your phone on any network.
echo.
echo  Close the "NYSE Dashboard Server" window to stop the server.
echo  Press any key to close this launcher window...
pause > nul
