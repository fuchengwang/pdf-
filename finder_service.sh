#!/usr/bin/env bash
# Finder「服务」入口：由 Automator 调用，统一收集路径并写日志
set -eo pipefail

SUBCMD="${1:?缺少子命令 merge|bilingual|split|translate}"
shift

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG="${HOME}/Library/Logs/pdf-finder-tools.log"
CACHE="${HOME}/Library/Caches/pdf-finder-selection.txt"
PY="${ROOT}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi

# 安全打印数组（避免 set -u 下空数组报错）
_join_args() {
  local out="" a
  for a in "$@"; do
    out+="${out:+ }$a"
  done
  printf '%s' "$out"
}

# Automator 未传参时，尝试从 Finder 选中项取路径
_finder_selection() {
  /usr/bin/osascript 2>/dev/null <<'APPLESCRIPT' || true
tell application "Finder"
  set sel to selection
  if (count of sel) is 0 then return ""
  set lines to ""
  repeat with f in sel
    set lines to lines & (POSIX path of (f as alias)) & linefeed
  end repeat
  return lines
end tell
APPLESCRIPT
}

mkdir -p "$(dirname "$LOG")"
{
  echo "======== $(date '+%Y-%m-%d %H:%M:%S') $SUBCMD ========"
  echo "cwd=$(pwd)"
  echo "shell argc=$# argv=[$*]"

  args=("$@")

  # 1) 读 Automator AppleScript 写入的缓存（Python 解析，兼容粘成一行的情况）
  if [ ${#args[@]} -eq 0 ] && [ -f "$CACHE" ]; then
    echo "cache_size=$(wc -c <"$CACHE" | tr -d ' ')"
    export PDF_FINDER_CACHE="$CACHE" PDF_FINDER_ROOT="$ROOT"
    while IFS= read -r line; do
      [ -n "$line" ] && args+=("$line")
    done < <("$PY" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["PDF_FINDER_ROOT"])
from finder_pdf_tools import collect_pdf_paths
raw = Path(os.environ["PDF_FINDER_CACHE"]).read_text(encoding="utf-8", errors="replace")
for p in collect_pdf_paths([], raw):
    print(p)
PY
    )
    if [ ${#args[@]} -gt 0 ]; then
      rm -f "$CACHE"
      echo "from_cache=[$(_join_args "${args[@]}")]"
    else
      echo "cache_read_empty raw=$(head -c 200 "$CACHE" | tr '\n' '|')"
    fi
  fi

  # 2) 读 stdin
  stdin_payload=""
  if [ ${#args[@]} -eq 0 ] && [ ! -t 0 ]; then
    stdin_payload="$(cat)"
    echo "stdin=[$stdin_payload]"
  fi

  # 3) 读 Finder 选中（需自动化权限，作最后兜底）
  if [ ${#args[@]} -eq 0 ] && [ -z "$stdin_payload" ]; then
    sel="$(_finder_selection)"
    echo "finder_selection=[$sel]"
    if [ -n "$sel" ]; then
      while IFS= read -r line; do
        [ -n "$line" ] && args+=("$line")
      done <<<"$sel"
    fi
  fi

  if [ ! -x "$PY" ]; then
    /usr/bin/osascript -e 'display dialog "找不到 Python，请检查项目 .venv" buttons {"好"}' 2>/dev/null || true
    exit 1
  fi

  if [ ${#args[@]} -gt 0 ]; then
    "$PY" "${ROOT}/finder_pdf_tools.py" service "$SUBCMD" -- "${args[@]}" </dev/null
  elif [ -n "$stdin_payload" ]; then
    printf '%s' "$stdin_payload" | "$PY" "${ROOT}/finder_pdf_tools.py" service "$SUBCMD" --
  else
    "$PY" "${ROOT}/finder_pdf_tools.py" service "$SUBCMD" --
  fi
  ec=$?
  echo "exit=$ec"
  exit "$ec"
} >>"$LOG" 2>&1
