@echo off
title Remove daily learning task
schtasks /Delete /TN "GrokBotDailyLearning" /F
if errorlevel 1 (
    echo No GrokBotDailyLearning task was removed. It may not have been installed.
) else (
    echo Removed the daily learning task. The bot and dashboard are unchanged.
)
pause
