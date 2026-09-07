@echo off
setlocal

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_proxy.ps1" -NoPanel %*
if errorlevel 1 (
  echo.
  echo Capture startup failed.
  pause
)

exit /b %ERRORLEVEL%
