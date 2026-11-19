@echo off
title Install daily learning task
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Run start_dashboard.bat once first so .venv exists.
    pause
    exit /b 1
)
schtasks /Create /F /TN "GrokBotDailyLearning" /SC DAILY /ST 00:15 /RL LIMITED /TR "\"%PY%\" \"%~dp0learn_daily.py\""
if errorlevel 1 (
    echo.
    echo Could not register the task. It does not need administrator rights,
    echo but Task Scheduler has to allow the current user to create tasks.
    pause
    exit /b 1
)
echo.
echo Daily learning is scheduled at 00:15 local time for this Windows user.
echo It only replaces config.yaml while dry_run is true.
pause
