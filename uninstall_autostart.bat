@echo off
schtasks /Delete /TN "GrokBotDashboard" /F
if errorlevel 1 (
    echo No GrokBotDashboard logon task was found.
) else (
    echo Removed the logon task.
)
pause
