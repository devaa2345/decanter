@echo off
title Sovereign Scents - Decanter Bot
cd /d "%~dp0"

echo ============================================
echo   Sovereign Scents - Decanter Bot
echo ============================================
echo.

where python >nul 2>nul
if not errorlevel 1 (
    set PYTHON_CMD=python
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set PYTHON_CMD=py
    ) else (
        echo ERROR: Python was not found on PATH.
        echo Install Python from https://python.org and try again.
        echo.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo WARNING: No .env file found in this folder.
    echo The bot will still start, but replies/analytics/dashboard login
    echo will not work until CHATMITRA_API_TOKEN, SUPABASE_* etc. are set.
    echo.
)

echo Checking dependencies (this is quick if already installed)...
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies. See the output above.
    pause
    exit /b 1
)

echo.
echo Starting the server on http://127.0.0.1:8000
echo The dashboard will open in your browser in a few seconds.
echo.
echo Keep this window open while you're using the bot.
echo Close this window, or press Ctrl+C, to stop the server.
echo ============================================
echo.

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000/dashboard/index.html'"

%PYTHON_CMD% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo ============================================
echo   Server stopped.
echo ============================================
pause
