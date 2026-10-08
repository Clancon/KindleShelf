@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. Please check the message above.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 (
  echo.
  echo Kindle Shelf stopped because of an error.
  pause
)
