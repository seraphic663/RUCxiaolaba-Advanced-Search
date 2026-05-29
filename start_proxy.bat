@echo off
echo ============================================
echo   mitmproxy - WeChat Mini Program Capture
echo ============================================
echo.
echo Proxy:  127.0.0.1:8899
echo Panel:  http://127.0.0.1:8900/?token=xlb123
echo.
echo 1. Set Windows proxy to 127.0.0.1:8899
echo 2. Open WeChat mini program
echo 3. Watch requests in browser panel
echo.
echo Close this window to stop.
echo ============================================
echo.

set "MITM_PATH=C:\Users\31572\AppData\Roaming\Python\Python312\Scripts"

"%MITM_PATH%\mitmweb.exe" ^
  --listen-port 8899 ^
  --web-port 8900 ^
  --web-host 127.0.0.1 ^
  --set web_password=xlb123 ^
  --set block_global=false ^
  -s "%~dp0mitm_filter.py"
