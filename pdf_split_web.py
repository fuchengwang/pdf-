#!/usr/bin/env python3
"""
PDF 拆页 Web 界面：上传/拖拽或填写本机路径，结果保存到原 PDF 同目录。
默认端口：10001
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import fitz
import gradio as gr

from pdf_split import build_output_path, extract_pages, parse_page_list, split_each


def _normalize_pages_spec(text: str) -> str:
    """把空格、中文逗号等统一成 1,3,5-8 格式。"""
    return re.sub(r"[\s，、;；]+", ",", text.strip())


def _zip_files(files: list[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return zip_path


def _resolve_src(
    pdf_file: str | None,
    pdf_path: str,
    save_dir: str,
) -> tuple[Path | None, str | None]:
    """确定源 PDF 路径；输出目录 = 源文件所在目录。"""
    path_text = (pdf_path or "").strip()
    if path_text:
        src = Path(path_text).expanduser().resolve()
        if not src.is_file():
            return None, f"路径不存在或不是文件：{src}"
        if src.suffix.lower() != ".pdf":
            return None, "请填写 .pdf 文件路径。"
        return src, None

    if not pdf_file:
        return None, "请填写本机 PDF 路径，或上传 PDF 并填写「保存目录」。"

    upload = Path(pdf_file)
    if not upload.exists():
        return None, "上传文件无效，请重新选择。"

    dir_text = (save_dir or "").strip()
    if not dir_text:
        return None, "上传模式下请填写「保存目录」（即该 PDF 所在的文件夹路径）。"

    folder = Path(dir_text).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / upload.name
    shutil.copy2(upload, src)
    return src, None


def _output_dir(save_dir: str, anchor: Path) -> Path:
    """未填保存目录时，使用 anchor（左侧 PDF）所在目录。"""
    if (save_dir or "").strip():
        folder = Path(save_dir).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return anchor.parent


def _is_temp_upload(path: Path) -> bool:
    """Gradio 上传会先复制到临时目录。"""
    s = str(path).lower()
    return (
        "/gradio/" in s
        or "/tmp/" in s
        or "/var/folders/" in s
        or "/.cursor/" in s
        or "/temp/" in s
    )


def _search_roots() -> list[Path]:
    """在本机这些目录里查找上传前的原文件。"""
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop", home / "Documents", Path.cwd()]
    return [r for r in roots if r.is_dir()]


def _find_original_pdf(upload: Path) -> Path | None:
    """按文件名与大小定位本机上的原始 PDF（用于确定保存目录）。"""
    if not upload.is_file():
        return None

    resolved = upload.resolve()
    if not _is_temp_upload(resolved):
        return resolved

    name = upload.name
    size = upload.stat().st_size
    preferred = [str(r) for r in _search_roots()]

    try:
        out = subprocess.check_output(
            ["mdfind", f"kMDItemFSName == '{name}'"],
            text=True,
            timeout=15,
        )
        matches: list[Path] = []
        for line in out.strip().splitlines():
            p = Path(line.strip())
            try:
                if p.is_file() and p.suffix.lower() == ".pdf" and p.stat().st_size == size:
                    matches.append(p.resolve())
            except OSError:
                continue
        if matches:
            for p in matches:
                if any(str(root) in str(p) for root in preferred):
                    return p
            return matches[0]
    except Exception:
        pass

    for root in _search_roots():
        try:
            for p in root.rglob(name):
                if p.is_file() and p.stat().st_size == size:
                    return p.resolve()
        except OSError:
            continue
    return None


def _as_gradio_file(path: Path) -> str:
    """
    Gradio 预览/下载只能访问白名单目录内的文件。
    若输出在其它目录，复制一份到项目缓存再返回给界面。
    """
    resolved = path.resolve()
    for root in _gradio_allowed_paths():
        try:
            resolved.relative_to(Path(root))
            return str(resolved)
        except ValueError:
            continue

    cache = Path.cwd() / ".gradio_preview"
    cache.mkdir(exist_ok=True)
    dest = cache / resolved.name
    shutil.copy2(resolved, dest)
    return str(dest.resolve())


def _resolve_pair(
    left_file: str | None,
    left_path: str,
    right_file: str | None,
    right_path: str,
    save_dir: str,
) -> tuple[Path | None, Path | None, Path | None, str | None]:
    """解析左右两个 PDF；返回 (左, 右, 输出目录, 错误信息)。"""
    lp = (left_path or "").strip()
    rp = (right_path or "").strip()

    # 左：本机路径（推荐）
    if lp:
        left = Path(lp).expanduser().resolve()
        if not left.is_file():
            return None, None, None, f"左 PDF 不存在：{left}"
        if left.suffix.lower() != ".pdf":
            return None, None, None, "左文件须为 .pdf。"
        out_dir = _output_dir(save_dir, left)

        if rp:
            right = Path(rp).expanduser().resolve()
            if not right.is_file():
                return None, None, None, f"右 PDF 不存在：{right}"
        elif right_file:
            right = out_dir / Path(right_file).name
            shutil.copy2(right_file, right)
        else:
            return None, None, None, "请上传右 PDF 或填写右 PDF 路径。"

        if right.suffix.lower() != ".pdf":
            return None, None, None, "右文件须为 .pdf。"
        return left, right, out_dir, None

    # 仅上传：保存到左侧 PDF 在本机的所在目录
    if not left_file or not right_file:
        return None, None, None, "请上传两个 PDF，或填写左 PDF 本机路径。"

    left_upload = Path(left_file).resolve()
    right_upload = Path(right_file).resolve()

    left_anchor = _find_original_pdf(left_upload)
    if not left_anchor:
        return (
            None,
            None,
            None,
            f"未能定位左 PDF「{left_upload.name}」在本机的位置。"
            f"请填写「左 PDF 本机路径」，或填写「保存目录」。",
        )

    out_dir = _output_dir(save_dir, left_anchor)
    return left_upload, right_upload, out_dir, None


def merge_bilingual_side_by_side(left: Path, right: Path, out: Path) -> int:
    """左右并排合并：每对页合成一页（左原文、右译文）。返回总页数。"""
    doc_l = fitz.open(left)
    doc_r = fitz.open(right)
    if doc_l.page_count != doc_r.page_count:
        n_l, n_r = doc_l.page_count, doc_r.page_count
        doc_l.close()
        doc_r.close()
        raise ValueError(f"页数不一致：左 {n_l} 页，右 {n_r} 页，请使用相同页数的 PDF。")

    merged = fitz.open()
    for i in range(doc_l.page_count):
        r1 = doc_l[i].rect
        r2 = doc_r[i].rect
        h = max(r1.height, r2.height)
        w = r1.width + r2.width
        page = merged.new_page(width=w, height=h)
        page.show_pdf_page(fitz.Rect(0, 0, r1.width, h), doc_l, i)
        page.show_pdf_page(fitz.Rect(r1.width, 0, w, h), doc_r, i)

    merged.save(out, garbage=4, deflate=True)
    pages = merged.page_count
    merged.close()
    doc_l.close()
    doc_r.close()
    return pages


def run_bilingual(
    left_file: str | None,
    left_path: str,
    right_file: str | None,
    right_path: str,
    save_dir: str,
) -> tuple[str | None, str]:
    """生成左右对照双语版 PDF。"""
    left, right, out_dir, err = _resolve_pair(
        left_file, left_path, right_file, right_path, save_dir
    )
    if err:
        return None, err
    assert left and right and out_dir

    out_path = out_dir / f"{left.stem}-双语版.pdf"
    try:
        n = merge_bilingual_side_by_side(left, right, out_path)
        return (
            _as_gradio_file(out_path),
            f"双语版已生成（左右对照，共 {n} 页）：\n{out_path}\n\n"
            f"左：{left}\n右：{right}",
        )
    except Exception as e:
        return None, f"生成失败：{e}"


def run_split(
    pdf_file: str | None,
    pdf_path: str,
    save_dir: str,
    mode: str,
    pages_text: str,
    page_range: str,
) -> tuple[str | None, str]:
    """拆页并保存到源 PDF 同目录，返回 (可选预览文件, 状态说明)。"""
    src, err = _resolve_src(pdf_file, pdf_path, save_dir)
    if err:
        return None, err
    assert src is not None

    out_dir = src.parent
    try:
        doc = fitz.open(src)
        total = doc.page_count
        doc.close()

        if mode == "每页单独导出":
            outs = split_each(src)
            if len(outs) == 1:
                p = outs[0]
                return _as_gradio_file(p), f"已保存到原文件目录：\n{p}"
            zip_path = out_dir / f"{src.stem}-拆页.zip"
            _zip_files(outs, zip_path)
            lines = "\n".join(f"  · {f.name}" for f in outs[:5])
            if len(outs) > 5:
                lines += f"\n  · ... 共 {len(outs)} 个单页"
            return (
                _as_gradio_file(zip_path),
                f"已保存到原文件目录：\n{zip_path}\n\n单页文件：\n{lines}",
            )

        if mode == "连续范围":
            spec = _normalize_pages_spec(page_range)
            if not spec:
                return None, "请填写页码范围，例如 1-3。"
            pages = parse_page_list(spec, total)
        else:
            spec = _normalize_pages_spec(pages_text)
            if not spec:
                return None, "请填写页码，例如 4,7,18 或 4 7 18。"
            pages = parse_page_list(spec, total)

        out = build_output_path(src, pages)
        extract_pages(src, pages, out)
        return _as_gradio_file(out), f"已保存到原文件目录：\n{out}\n\n页码：{pages}"
    except Exception as e:
        return None, f"处理失败：{e}"


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="PDF 拆页", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            """
# PDF 工具
**结果保存到 PDF 所在文件夹**（或你指定的保存目录），不会只进浏览器「下载」目录。
            """
        )

        with gr.Tabs():
            # ---------- 拆页 ----------
            with gr.Tab("拆页"):
                gr.Markdown(
                    "推荐只填本机路径；上传时需填保存目录。"
                )
                pdf_path = gr.Textbox(
                    label="本机 PDF 完整路径（推荐）",
                    placeholder="/Users/你/文件夹/书名.pdf",
                )
                save_dir = gr.Textbox(
                    label="保存目录（上传时必填）",
                    placeholder="/Users/你/文件夹/",
                )
                pdf_in = gr.File(
                    label="或上传 PDF",
                    file_types=[".pdf"],
                    type="filepath",
                )
                mode = gr.Radio(
                    choices=["指定页码", "连续范围", "每页单独导出"],
                    value="指定页码",
                    label="拆页方式",
                )
                pages_text = gr.Textbox(
                    label="页码（指定页码模式）",
                    placeholder="例如：4,7,18  或  4 7 18",
                    visible=True,
                )
                page_range = gr.Textbox(
                    label="页码范围（连续范围模式）",
                    placeholder="例如：1-3",
                    visible=False,
                )
                run_btn = gr.Button("开始拆页", variant="primary")
                status = gr.Textbox(label="状态", interactive=False, lines=6)
                pdf_out = gr.File(label="预览/下载")

                def on_mode_change(m: str):
                    return (
                        gr.update(visible=m == "指定页码"),
                        gr.update(visible=m == "连续范围"),
                    )

                mode.change(
                    on_mode_change, inputs=mode, outputs=[pages_text, page_range]
                )
                run_btn.click(
                    run_split,
                    inputs=[
                        pdf_in,
                        pdf_path,
                        save_dir,
                        mode,
                        pages_text,
                        page_range,
                    ],
                    outputs=[pdf_out, status],
                )

            # ---------- 双语版（左右对照） ----------
            with gr.Tab("双语版（左右对照）"):
                gr.Markdown(
                    """
上传两个**页数相同**的 PDF 即可。不填保存目录时，**保存到左侧 PDF 所在文件夹**（程序会按文件名在本机查找）。  
输出：`左文件名-双语版.pdf`
                    """
                )
                with gr.Row():
                    left_in = gr.File(
                        label="左 PDF（原文，如英文）",
                        file_types=[".pdf"],
                        type="filepath",
                    )
                    right_in = gr.File(
                        label="右 PDF（译文，如中文）",
                        file_types=[".pdf"],
                        type="filepath",
                    )
                with gr.Row():
                    left_path = gr.Textbox(
                        label="左 PDF 本机路径（可选）",
                        placeholder="/Users/你/原文.pdf",
                    )
                    right_path = gr.Textbox(
                        label="右 PDF 本机路径（可选）",
                        placeholder="/Users/你/译文.pdf",
                    )
                save_dir_dual = gr.Textbox(
                    label="保存目录（可选；不填则保存到左侧 PDF 所在目录）",
                    placeholder="一般留空即可",
                )
                run_dual_btn = gr.Button("生成双语版", variant="primary")
                status_dual = gr.Textbox(label="状态", interactive=False, lines=6)
                pdf_out_dual = gr.File(label="预览/下载")

                run_dual_btn.click(
                    run_bilingual,
                    inputs=[
                        left_in,
                        left_path,
                        right_in,
                        right_path,
                        save_dir_dual,
                    ],
                    outputs=[pdf_out_dual, status_dual],
                )

    return demo


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _service_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _kill_port_processes(port: int) -> None:
    """结束占用指定端口的进程（PM2 重启前清理旧实例）"""
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
    except subprocess.CalledProcessError:
        return
    if not out:
        return
    for pid in out.split("\n"):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
    except subprocess.CalledProcessError:
        return
    for pid in out.split("\n"):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _under_pm2() -> bool:
    return bool(os.environ.get("PM2_HOME") or os.environ.get("PM2_USAGE"))


def _gradio_allowed_paths() -> list[str]:
    """
    Gradio 的 File 输出只能引用这些目录下的文件。
    输出可能在下载/桌面等用户目录，须加入白名单；其它目录由 _as_gradio_file 复制到缓存预览。
    """
    home = Path.home()
    candidates = [
        Path.cwd(),
        Path(os.environ.get("TMPDIR", "/tmp")),
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]
    return [str(p.expanduser().resolve()) for p in candidates if p.exists()]


def main() -> None:
    port = int(os.environ.get("PDF_SPLIT_PORT", "10001"))

    if _port_in_use(port):
        if _under_pm2():
            print(f"端口 {port} 被占用，PM2 启动前先释放旧进程…")
            _kill_port_processes(port)
        elif _service_ok(port):
            print(f"PDF 拆页 Web 已在运行，无需重复启动：http://localhost:{port}/")
            sys.exit(0)
        else:
            print(f"端口 {port} 已被占用但服务无响应。请先结束旧进程：")
            print(f"  lsof -i :{port}")
            sys.exit(1)

    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        show_error=True,
        inbrowser=False,
        allowed_paths=_gradio_allowed_paths(),
    )


if __name__ == "__main__":
    main()
