@echo off
title SOL SMC Bot Dashboard
cd /d "%~dp0"

rem --- first run: create the virtual environment and install requirements ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment .venv ...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Could not create .venv. Install Python 3.11+ from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup, then run this file again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example - add your Bybit API key there before going live.
)

call ".venv\Scripts\activate.bat"
echo Starting dashboard at http://127.0.0.1:8080 ...
python dashboard.py %*
echo.
echo Dashboard closed. (A bot you started keeps running in the background - reopen the dashboard to stop it.)
pause
