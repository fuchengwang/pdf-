#!/usr/bin/env python3
"""
启动 pdf2zh-next 的 Web UI（跳过全量 warmup）。

官方 CLI 入口会在启动前执行 babeldoc 资源全量预热，网络不稳定时可能卡住很久。
该脚本直接调用 GUI 启动函数，让页面先可用；翻译时再按需获取资源。
"""

from __future__ import annotations

import argparse
import os

from pdf2zh_next.gui import setup_gui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start pdf2zh-next Web UI quickly")
    parser.add_argument("--server-port", type=int, default=7860, help="Web UI 端口")
    parser.add_argument("--ui-lang", default="zh", help="界面语言")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 环境变量优先（工作台通过 PM2 注入 PDF2ZH_SERVER_PORT）
    port = int(os.environ.get("PDF2ZH_SERVER_PORT", args.server_port))
    os.environ["PDF2ZH_UI_LANG"] = args.ui_lang

    # PM2 托管时不自动弹浏览器，由工作台点击打开
    under_pm2 = bool(os.environ.get("PM2_HOME") or os.environ.get("PM2_USAGE"))

    setup_gui(
        auth_file=None,
        welcome_page=None,
        server_port=port,
        inbrowser=not under_pm2,
    )


if __name__ == "__main__":
    main()
