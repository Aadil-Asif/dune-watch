@echo off
REM Always-on Odyssey watcher. Double-click this, or run it from a terminal,
REM and leave the window open. It restarts the daemon if it ever crashes.
cd /d "%~dp0"

if defined NTFY_TOPIC goto run
if not exist ntfy_topic.txt goto notopic
set /p NTFY_TOPIC=<ntfy_topic.txt
goto run

:notopic
echo.
echo   No ntfy topic configured.
echo.
echo   Create a file called ntfy_topic.txt in this folder containing just
echo   your topic name on one line, or set NTFY_TOPIC in your environment.
echo   (ntfy_topic.txt is gitignored, so it won't leak to the public repo.)
echo.
pause
exit /b 1

:run
echo Odyssey IMAX 70mm watcher - notifying topic "%NTFY_TOPIC%"
echo Close this window to stop.
echo.

:loop
python daemon.py
echo.
echo Daemon exited with code %ERRORLEVEL%. Restarting in 30 seconds...
timeout /t 30 >nul
goto loop
