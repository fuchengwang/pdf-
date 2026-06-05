#!/usr/bin/env python3
"""启动 pdf2zh 翻译中控台（默认端口 10003）。等价于 python translate_console.py"""

from translate_console import DEFAULT_PORT, run_server

import os

if __name__ == "__main__":
    port = int(os.environ.get("PDF2ZH_CONSOLE_PORT", DEFAULT_PORT))
    run_server(port)
