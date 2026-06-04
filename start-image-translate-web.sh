#!/usr/bin/env bash
# 图片翻译 Web（端口 10002，真 Chrome + Google 网页图译）
set -e
cd "$(dirname "$0")"

PORT="${IMAGE_TRANSLATE_PORT:-10002}"

if [ -n "${PM2_HOME:-}" ] || [ -n "${PM2_USAGE:-}" ]; then
  if PIDS=$(lsof -ti :"${PORT}" 2>/dev/null); then
    echo "PM2 启动：释放端口 ${PORT}"
    kill -TERM ${PIDS} 2>/dev/null || true
    sleep 0.5
  fi
else
  if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "图片翻译 Web 已在运行：http://localhost:${PORT}/"
    exit 0
  fi
fi

if ! .venv/bin/python -c "import playwright" 2>/dev/null; then
  echo "正在安装 playwright…"
  .venv/bin/python -m pip install playwright -q
  .venv/bin/playwright install chrome
fi

echo "正在启动图片翻译 Web…"
echo "  地址: http://localhost:${PORT}/"
echo "  首次请先点「打开浏览器登录」"
echo ""

exec .venv/bin/python image_translate_web.py
