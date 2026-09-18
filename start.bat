@echo off
cd /d "%~dp0"

echo Starting ACC Connector...
echo.

if not exist ".env" (
    echo ERROR: .env file not found. Copy .env.example to .env and set SECRET_KEY.
    echo Databricks credentials are entered per-user in the Connect Databricks panel.
    pause
    exit /b 1
)

:: Stop any previous Flask instance on port 8000 so new code loads
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

:: Start Flask app in a new window so this script can return
start "ACC Connector" cmd /k "python app.py"

echo ACC Connector started on http://localhost:8000
echo Close the "ACC Connector" window to stop the server.
