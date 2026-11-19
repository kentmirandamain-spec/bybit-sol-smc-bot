@echo off
cd /d "%~dp0"
schtasks /Create /F /TN "GrokBotDashboard" /SC ONLOGON /RL LIMITED /TR "cmd /c start /min \"\" \"%~dp0start_dashboard.bat\""
if errorlevel 1 (
    echo Could not register the logon task.
    pause
    exit /b 1
)
echo The dashboard will open minimized when you sign in to Windows.
echo The bot starts too only if autostart_bot is true in config.yaml. Dry run stays dry run.
pause
