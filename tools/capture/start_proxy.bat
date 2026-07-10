@echo off
echo ============================================
echo   mitmproxy - WeChat Mini Program Capture
echo ============================================
echo.
echo Proxy:  127.0.0.1:8899
echo Panel:  http://127.0.0.1:8900/
echo.
echo 1. Set Windows proxy to 127.0.0.1:8899
echo 2. Open WeChat mini program
echo 3. Watch requests in browser panel
echo.
echo Close this window to stop.
echo ============================================
echo.

where mitmweb >nul 2>nul
if errorlevel 1 (
  echo mitmweb not found. Install with: python -m pip install mitmproxy
  exit /b 1
)

mitmweb ^
  --listen-port 8899 ^
  --web-port 8900 ^
  --web-host 127.0.0.1 ^
  --set block_global=false ^
  -s "%~dp0mitm_filter.py"
