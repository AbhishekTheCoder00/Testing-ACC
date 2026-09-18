@echo off
echo Stopping ACC Connector...

:: Kill any python process running app.py on port 8000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo Killing PID %%a
    taskkill /PID %%a /F >nul 2>&1
)

:: Also close the named window if it's still open
taskkill /FI "WINDOWTITLE eq ACC Connector" /F >nul 2>&1

echo Done.
