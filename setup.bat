@echo off
title Grok Bot setup
cd /d "%~dp0"
echo Checking for Python 3.11 through 3.14...
set "PY="
where py >nul 2>nul && (
    for %%V in (3.14 3.13 3.12 3.11) do (
        py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V" && goto :found
    )
)
where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,11) and sys.version_info<(3,15) else 1)" >nul 2>nul && set "PY=python" && goto :found
echo.
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH". If Smart App Control blocks the installer,
echo see the README section "Smart App Control".
pause
exit /b 1

:found
echo Using %PY%
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Could not create .venv.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo pip could not finish. If Windows Smart App Control blocked a DLL inside a package,
    echo open the README section "Smart App Control". The dashboard itself adds no extra compiled packages.
    pause
    exit /b 1
)
if not exist ".env" copy ".env.example" ".env" >nul
powershell -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Grok Bot.lnk'); $s.TargetPath='%~dp0start_dashboard.bat'; $s.WorkingDirectory='%~dp0'; $s.Save()"
echo.
echo Setup finished. A Desktop shortcut named Grok Bot opens the dashboard.
echo Dry run stays on until you change it in Settings and type GO LIVE.
set /p AUTO=Register the dashboard to start when you sign in to Windows? (y/N) 
if /I "%AUTO%"=="y" call "%~dp0install_autostart.bat"
pause
