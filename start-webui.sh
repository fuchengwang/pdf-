#!/usr/bin/env bash
# 启动 pdf2zh-next Web UI（英译中，保留排版）
# 用法: ./start-webui.sh  或  bash start-webui.sh

set -e
cd "$(dirname "$0")"

export PDF2ZH_LANG_FROM="${PDF2ZH_LANG_FROM:-English}"
export PDF2ZH_LANG_TO="${PDF2ZH_LANG_TO:-Simplified Chinese}"

PORT="${PDF2ZH_SERVER_PORT:-7860}"
export PDF2ZH_SERVER_PORT="${PORT}"
UI_LANG="${PDF2ZH_UI_LANG:-zh}"

# PM2 托管时：先释放端口，禁止依赖「已在运行就退出」的逻辑
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
    echo "PDF 翻译 Web 已在运行，无需重复启动。"
    echo "  地址: http://localhost:${PORT}/"
    exit 0
  fi
fi

echo "正在启动 pdf2zh-next Web UI..."
echo "  翻译: ${PDF2ZH_LANG_FROM} → ${PDF2ZH_LANG_TO}"
echo "  地址: http://localhost:${PORT}/"
echo ""

exec .venv/bin/python run_webui.py \
  --server-port "$PORT" \
  --ui-lang "$UI_LANG"
