#!/usr/bin/env python3
"""
图片翻译 Web（端口 10002）：上传图片 → 保存译图到指定目录与文件名。
后端默认：Google 网页图片翻译（真 Chrome 持久配置，单张串行）。
"""

from __future__ import annotations

import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

import gradio as gr

from google_image_translate import get_backend
from platform_compat import (
    gradio_allowed_path_candidates,
    kill_port_processes,
    port_busy_hint,
)

PORT = int(os.environ.get("IMAGE_TRANSLATE_PORT", "10002"))


def _safe_filename(name: str, fallback_stem: str) -> str:
    """去掉非法字符，保证可作为文件名。"""
    name = (name or "").strip()
    if not name:
        return f"{fallback_stem}-译图.png"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        name += ".png"
    return name


def _resolve_output_path(
    image_file: str | None,
    save_dir: str,
    output_name: str,
) -> tuple[Path | None, str | None]:
    """根据上传文件与表单，确定译图完整保存路径。"""
    if not image_file:
        return None, "请先上传图片。"

    src = Path(image_file).resolve()
    if not src.is_file():
        return None, "上传文件无效。"

    dir_text = (save_dir or "").strip()
    if dir_text:
        out_dir = Path(dir_text).expanduser().resolve()
    else:
        out_dir = Path.home() / "Downloads"
    out_dir.mkdir(parents=True, exist_ok=True)

    dest = out_dir / _safe_filename(output_name, src.stem)
    return dest, None


def _as_gradio_file(path: Path) -> str:
    """Gradio 预览用：优先返回原路径，否则复制到项目缓存。"""
    resolved = path.resolve()
    allowed_roots = gradio_allowed_path_candidates()
    for root in allowed_roots:
        if root.exists():
            try:
                resolved.relative_to(root.resolve())
                return str(resolved)
            except ValueError:
                continue
    cache = Path.cwd() / ".gradio_preview"
    cache.mkdir(exist_ok=True)
    import shutil

    dest = cache / resolved.name
    shutil.copy2(resolved, dest)
    return str(dest.resolve())


def open_login_browser() -> str:
    """打开可见 Chrome，在 Google 图片翻译页登录一次即可。"""
    try:
        backend = get_backend()
        backend.set_show_browser(True)
        backend.open_for_login()
        return (
            "已打开 Chrome（Google 图片翻译页）。\n"
            "请在此窗口完成 Google 登录，并确认能看到「图片」翻译界面。\n"
            "登录成功后可直接「开始翻译」；"
            "平时翻译在后台无界面 Chrome 中完成，不会反复弹窗。"
        )
    except Exception as e:
        return f"打开浏览器失败：{e}\n请确认已安装 Google Chrome，并执行：playwright install chrome"


def run_translate(
    image_file: str | None,
    save_dir: str,
    output_name: str,
    show_browser: bool,
) -> tuple[str | None, str]:
    """接收图片，译好后保存到指定路径。"""
    dest, err = _resolve_output_path(image_file, save_dir, output_name)
    if err or not dest:
        return None, err or "路径无效"
    assert dest is not None

    src = Path(image_file).resolve()
    try:
        backend = get_backend()
        # 勾选时弹出 Chrome 前台；默认后台无界面
        backend.set_show_browser(show_browser)
        out = backend.translate(src, dest)
        mode = "前台浏览器" if show_browser else "后台最小化"
        return (
            _as_gradio_file(out),
            f"翻译完成（{mode}），已保存：\n{out}",
        )
    except Exception as e:
        return None, (
            f"翻译失败：{e}\n\n"
            "建议：先点「打开浏览器登录」完成 Google 登录；"
            "若页面改版，请把报错信息发给我以便调整选择器。"
        )


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="图片翻译", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
# 图片翻译（Google 网页版）

上传一张图片，译图保存到你指定的**文件夹**和**文件名**（一次只处理一张，更稳定）。  
首次使用请先点 **「打开浏览器登录」**。默认在**后台最小化**翻译（不挡屏幕）；勾选可**弹出 Chrome 观看**。两者都会真正译成中文，不用无头模式。
            """
        )
        image_in = gr.Image(label="上传图片", type="filepath")
        save_dir = gr.Textbox(
            label="保存目录",
            placeholder="留空则保存到「下载」文件夹，例如 C:\\Users\\你\\Downloads\\译图",
        )
        output_name = gr.Textbox(
            label="输出文件名（可选）",
            placeholder="留空则：原文件名-译图.png",
        )
        show_browser = gr.Checkbox(
            label="显示浏览器窗口（前台运行，可观看上传与翻译过程）",
            value=False,
        )
        with gr.Row():
            login_btn = gr.Button("打开浏览器登录", variant="secondary")
            run_btn = gr.Button("开始翻译", variant="primary")
        status = gr.Textbox(label="状态", interactive=False, lines=8)
        image_out = gr.Image(label="译图预览")

        login_btn.click(open_login_browser, outputs=status)
        run_btn.click(
            run_translate,
            inputs=[image_in, save_dir, output_name, show_browser],
            outputs=[image_out, status],
        )
    return demo


def _gradio_allowed_paths() -> list[str]:
    return [
        str(p.expanduser().resolve())
        for p in gradio_allowed_path_candidates()
        if p.exists()
    ]


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _service_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> None:
    if _port_in_use(PORT):
        if os.environ.get("PM2_HOME") or os.environ.get("PM2_USAGE"):
            kill_port_processes(PORT)
        elif _service_ok(PORT):
            print(f"图片翻译 Web 已在运行：http://localhost:{PORT}/")
            sys.exit(0)
        else:
            print(f"端口 {PORT} 被占用，请先结束旧进程：{port_busy_hint(PORT)}")
            sys.exit(1)

    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=PORT,
        show_error=True,
        inbrowser=False,
        allowed_paths=_gradio_allowed_paths(),
    )


if __name__ == "__main__":
    main()
