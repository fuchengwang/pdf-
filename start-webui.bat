@echo off
REM pdf2zh-next Web UI（端口 7860）
cd /d "%~dp0"
set PORT=%PDF2ZH_SERVER_PORT%
if not defined PORT set PORT=7860

curl -sf "http://127.0.0.1:%PORT%/" >nul 2>&1
if %ERRORLEVEL%==0 (
  echo PDF 翻译 Web 已在运行：http://localhost:%PORT%/
  exit /b 0
)

if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=python
)

echo 正在启动 pdf2zh-next Web UI...
echo   地址: http://localhost:%PORT%/
echo.

%PY% run_webui.py --server-port %PORT% --ui-lang zh
