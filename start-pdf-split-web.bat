@echo off
REM PDF 拆页 Web（端口 10001）
cd /d "%~dp0"
set PORT=%PDF_SPLIT_PORT%
if not defined PORT set PORT=10001

curl -sf "http://127.0.0.1:%PORT%/" >nul 2>&1
if %ERRORLEVEL%==0 (
  echo PDF 拆页 Web 已在运行：http://localhost:%PORT%/
  exit /b 0
)

echo 正在启动 PDF 拆页 Web...
echo   地址: http://localhost:%PORT%/
echo.

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" pdf_split_web.py
) else (
  python pdf_split_web.py
)
