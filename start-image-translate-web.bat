@echo off
REM 图片翻译 Web（端口 10002，需本机 Google Chrome）
cd /d "%~dp0"
set PORT=%IMAGE_TRANSLATE_PORT%
if not defined PORT set PORT=10002

curl -sf "http://127.0.0.1:%PORT%/" >nul 2>&1
if %ERRORLEVEL%==0 (
  echo 图片翻译 Web 已在运行：http://localhost:%PORT%/
  exit /b 0
)

if exist ".venv\Scripts\python.exe" (
  set PY=.venv\Scripts\python.exe
) else (
  set PY=python
)

%PY% -c "import playwright" 2>nul
if errorlevel 1 (
  echo 正在安装 playwright...
  %PY% -m pip install playwright -q
  %PY% -m playwright install chrome
)

echo 正在启动图片翻译 Web...
echo   地址: http://localhost:%PORT%/
echo   首次请先点「打开浏览器登录」
echo.

%PY% image_translate_web.py
