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

echo  [1/4] Stopping any previous server on port 8080...
REM Kill previous "NYSE Dashboard Server" window if it exists
taskkill /fi "WINDOWTITLE eq NYSE Dashboard Server" /f >nul 2>&1
REM Also free the port in case it was left occupied
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8080 " ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo  [2/4] Checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  WARNING: Some packages may not have installed correctly.
    echo  Try running:  pip install -r requirements.txt
    echo.
)

echo  [3/4] Starting dashboard server...
REM Start dashboard in a separate minimised window so this window can open the browser
start "NYSE Dashboard Server" /min python dashboard.py

echo  [4/4] Opening browser...
REM Wait 6 seconds for the server to start before opening the browser
timeout /t 6 /nobreak >nul
start http://localhost:8080

echo.
echo  Dashboard is running at http://localhost:8080
echo.
echo  ─── Make it public (access from phone / share with anyone) ────────────────
echo.
echo   OPTION A — ngrok  (already built in, free):
echo     1. Sign up free at https://ngrok.com
echo     2. Copy your authtoken from the ngrok dashboard
echo     3. Add this line to your .env file:
echo            NGROK_AUTHTOKEN=your_token_here
echo     4. Re-run start.bat — a public URL appears in the dashboard header.
echo.
echo   OPTION B — Deploy to the cloud (runs 24/7 without your PC):
echo     Easiest: https://railway.app  (free tier, connect GitHub, auto-deploys)
echo     Also:    https://render.com   (similar, free tier)
echo.
echo  ───────────────────────────────────────────────────────────────────────────
echo.
echo  Close the "NYSE Dashboard Server" window to stop the server.
echo  Press any key to close this launcher window...
pause >nul
