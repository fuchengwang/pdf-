#!/usr/bin/env bash
# 启动 PDF 拆页 Web（端口 10001，与 pdf2zh 7860 互不干扰）
set -e
cd "$(dirname "$0")"

PORT="${PDF_SPLIT_PORT:-10001}"

# PM2 托管时：先释放端口再启动，避免旧进程占端口导致崩溃重启循环
if [ -n "${PM2_HOME:-}" ] || [ -n "${PM2_USAGE:-}" ]; then
  if PIDS=$(lsof -ti :"${PORT}" 2>/dev/null); then
    echo "PM2 启动：释放端口 ${PORT}，结束旧进程 ${PIDS}"
    kill -TERM ${PIDS} 2>/dev/null || true
    sleep 0.5
    PIDS=$(lsof -ti :"${PORT}" 2>/dev/null || true)
    [ -n "${PIDS}" ] && kill -KILL ${PIDS} 2>/dev/null || true
  fi
else
  # 手动启动：服务已在运行则直接退出
  if curl -sf "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "PDF 拆页 Web 已在运行，无需重复启动。"
    echo "  地址: http://localhost:${PORT}/"
    exit 0
  fi
fi

echo "正在启动 PDF 拆页 Web..."
echo "  地址: http://localhost:${PORT}/"
echo "  停止: Ctrl+C"
echo ""

exec .venv/bin/python pdf_split_web.py
