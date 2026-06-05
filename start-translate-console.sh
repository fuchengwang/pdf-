#!/usr/bin/env bash
# 启动 PDF 翻译中控台（端口 10003，单线程队列）
set -e
cd "$(dirname "$0")"

PORT="${PDF2ZH_CONSOLE_PORT:-10003}"

# PM2 托管时：先释放端口再启动
if [ -n "${PM2_HOME:-}" ] || [ -n "${PM2_USAGE:-}" ]; then
  if PIDS=$(lsof -ti :"${PORT}" 2>/dev/null); then
    echo "PM2 启动：释放端口 ${PORT}，结束旧进程 ${PIDS}"
    kill -TERM ${PIDS} 2>/dev/null || true
    sleep 0.5
    PIDS=$(lsof -ti :"${PORT}" 2>/dev/null || true)
    [ -n "${PIDS}" ] && kill -KILL ${PIDS} 2>/dev/null || true
  fi
else
  if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "翻译中控台已在运行。"
    echo "  地址: http://localhost:${PORT}/"
    exit 0
  fi
fi

echo "正在启动 PDF 翻译中控台…"
echo "  地址: http://localhost:${PORT}/"
echo "  设置: 与 pdf2zh Web UI 共用 ~/.config/pdf2zh/config.v3.toml"
echo "  停止: Ctrl+C"
echo ""

exec .venv/bin/python translate_console.py
