#!/usr/bin/env bash
# 安装 Finder 右键「服务」（菜单底部，与 QQ/Hammerspoon 同级）
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

echo "生成并安装 PDF 服务…"
.venv/bin/python build_finder_workflows.py 2>/dev/null || python3 build_finder_workflows.py

SERVICES="$HOME/Library/Services"
mkdir -p "$SERVICES"

for wf in finder-services/*.workflow; do
  name="$(basename "$wf")"
  rm -rf "$SERVICES/$name"
  cp -R "$wf" "$SERVICES/"
  mode="$(plutil -extract workflowMetaData.presentationMode raw "$SERVICES/$name/Contents/document.wflow" 2>/dev/null || echo "?")"
  echo "  已安装：$name （presentationMode=$mode）"
done

LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREG" ]; then
  "$LSREG" -f -R "$SERVICES" 2>/dev/null || true
  "$LSREG" -f -R "$ROOT/finder-apps" 2>/dev/null || true
fi
killall Finder 2>/dev/null || true
killall pbs 2>/dev/null || true

echo ""
echo "=========================================="
echo "  菜单位置：右键最底部「服务」区域"
echo "  （与 QQ 闪传、Hammerspoon 同级，不是「快速操作」）"
echo "=========================================="
echo ""
echo "  若仍看不到，请勾选："
echo "  系统设置 → 键盘 → 键盘快捷键 → 服务 → 文件和文件夹"
echo "       ☑ PDF 合并  ☑ PDF 双语版  ☑ PDF 拆页  ☑ ⭐️PDF翻译"
echo ""
echo "  日志：~/Library/Logs/pdf-finder-tools.log"
echo ""
