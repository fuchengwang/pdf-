#!/usr/bin/env python3
"""
PDF 拆页 Web 界面：上传/拖拽或填写本机路径，结果保存到原 PDF 同目录。
默认端口：10001
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import fitz
import gradio as gr

from pdf_split import (
    build_output_path,
    extract_pages,
    merge_bilingual_side_by_side,
    merge_pdfs_append,
    normalize_pages_spec,
    parse_page_list,
    remove_pages_from_source,
    split_each,
)
from platform_compat import (
    collect_files_by_name_size,
    gradio_allowed_path_candidates,
    is_temp_path,
    kill_port_processes,
    pick_best_by_mtime,
    port_busy_hint,
    search_roots,
)


def _zip_files(files: list[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return zip_path


def _resolve_split(
    pdf_file: str | None,
    pdf_path: str,
    save_dir: str,
) -> tuple[Path | None, Path | None, str | None]:
    """
    解析拆页用的源 PDF 与输出目录。
    与双语版一致：上传时按文件名在本机查找原文件，默认保存到该文件所在文件夹。
    """
    path_text = (pdf_path or "").strip()
    if path_text:
        src = Path(path_text).expanduser().resolve()
        if not src.is_file():
            return None, None, f"路径不存在或不是文件：{src}"
        if src.suffix.lower() != ".pdf":
            return None, None, "请填写 .pdf 文件路径。"
        return src, _output_dir(save_dir, src), None

    if not pdf_file:
        return None, None, "请上传 PDF 或填写本机 PDF 路径。"

    upload = Path(pdf_file).resolve()
    if not upload.exists():
        return None, None, "上传文件无效，请重新选择。"

    anchor = _find_original_pdf(upload)
    if anchor:
        return anchor, _output_dir(save_dir, anchor), None

    # 找不到本机原文件时，仅当用户指定了保存目录才可继续
    dir_text = (save_dir or "").strip()
    if dir_text:
        folder = Path(dir_text).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / upload.name
        shutil.copy2(upload, dest)
        return dest, folder, None

    return (
        None,
        None,
        f"未能定位「{upload.name}」在本机的位置（拖拽上传后系统找不到原文件）。"
        f"请填写「本机 PDF 路径」，或填写「保存目录」。",
    )


def _output_dir(save_dir: str, anchor: Path) -> Path:
    """未填保存目录时，使用 anchor（左侧 PDF）所在目录。"""
    if (save_dir or "").strip():
        folder = Path(save_dir).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    return anchor.parent


def _find_original_pdf(upload: Path) -> Path | None:
    """
    定位拖拽/上传对应的本机原 PDF。
    上传文件在临时目录时，必须在本机搜到真实路径；搜不到则返回 None。
    """
    if not upload.is_file():
        return None

    resolved = upload.resolve()
    name = upload.name
    size = upload.stat().st_size
    exclude = resolved if is_temp_path(resolved) else None
    matches = collect_files_by_name_size(name, size, exclude=exclude)

    if is_temp_path(resolved):
        if matches:
            return pick_best_by_mtime(matches, search_roots())
        return None

    if matches:
        if resolved in matches and len(matches) == 1:
            return resolved
        return pick_best_by_mtime(matches, search_roots())
    return resolved


def _ensure_pdf_in_dir(upload: Path, folder: Path) -> Path:
    """把上传副本落到目标目录，便于与输出文件放在同一文件夹。"""
    dest = (folder / upload.name).resolve()
    src = upload.resolve()
    if src == dest:
        return dest
    shutil.copy2(upload, dest)
    return dest


def _as_gradio_file(path: Path) -> str:
    """
    Gradio 预览/下载只能访问白名单目录内的文件。
    若输出在其它目录，复制一份到项目缓存再返回给界面。
    """
    resolved = path.resolve()
    for root in gradio_allowed_path_candidates():
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
        dir_text = (save_dir or "").strip()
        if dir_text:
            folder = Path(dir_text).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            left = _ensure_pdf_in_dir(left_upload, folder)
            right = _ensure_pdf_in_dir(right_upload, folder)
            return left, right, folder, None
        return (
            None,
            None,
            None,
            f"未能定位左 PDF「{left_upload.name}」在本机的位置（拖拽上传后系统找不到原文件）。"
            f"请填写「左 PDF 本机路径」，或填写「保存目录」。",
        )

    out_dir = _output_dir(save_dir, left_anchor)
    # 读写都用本机原 PDF，输出写到 out_dir，避免落在 Gradio 临时目录
    left = left_anchor
    right = _ensure_pdf_in_dir(right_upload, out_dir)
    return left, right, out_dir, None


def run_merge(
    left_file: str | None,
    left_path: str,
    right_file: str | None,
    right_path: str,
    save_dir: str,
) -> tuple[str | None, str]:
    """左 PDF 在前、右 PDF 在后，合并保存到左 PDF 所在目录。"""
    left, right, out_dir, err = _resolve_pair(
        left_file, left_path, right_file, right_path, save_dir
    )
    if err:
        return None, err
    assert left and right and out_dir

    out_path = out_dir / f"{left.stem}-合并.pdf"
    try:
        doc_l = fitz.open(left)
        n_l = doc_l.page_count
        doc_l.close()
        doc_r = fitz.open(right)
        n_r = doc_r.page_count
        doc_r.close()
        n = merge_pdfs_append(left, right, out_path)
        return (
            _as_gradio_file(out_path),
            f"合并完成（共 {n} 页：左 {n_l} + 右 {n_r}）：\n{out_path}\n\n"
            f"左：{left}\n右：{right}",
        )
    except Exception as e:
        return None, f"合并失败：{e}"


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
    remove_from_source: bool,
) -> tuple[str | None, str]:
    """拆页并保存到 PDF 所在目录（或指定保存目录），返回 (可选预览文件, 状态说明)。"""
    src, out_dir, err = _resolve_split(pdf_file, pdf_path, save_dir)
    if err:
        return None, err
    assert src is not None and out_dir is not None

    dir_note = f"\n\n源文件：{src}\n保存目录：{out_dir}"
    try:
        doc = fitz.open(src)
        total = doc.page_count
        doc.close()

        if mode == "每页单独导出":
            outs = split_each(src, out_dir)
            removed_note = ""
            if remove_from_source:
                left = remove_pages_from_source(src, list(range(1, total + 1)))
                removed_note = f"\n\n已从原 PDF 移出全部 {total} 页，原文件剩余 {left} 页"
            if len(outs) == 1:
                p = outs[0]
                return _as_gradio_file(p), f"已保存：\n{p}{removed_note}{dir_note}"
            zip_path = out_dir / f"{src.stem}-拆页.zip"
            _zip_files(outs, zip_path)
            lines = "\n".join(f"  · {f.name}" for f in outs[:5])
            if len(outs) > 5:
                lines += f"\n  · ... 共 {len(outs)} 个单页"
            return (
                _as_gradio_file(zip_path),
                f"已保存：\n{zip_path}\n\n单页文件：\n{lines}{removed_note}{dir_note}",
            )

        if mode == "连续范围":
            spec = normalize_pages_spec(page_range)
            if not spec:
                return None, "请填写页码范围，例如 1-3。"
            pages = parse_page_list(spec, total)
        else:
            spec = normalize_pages_spec(pages_text)
            if not spec:
                return None, "请填写页码，例如 4,7,18 或 4 7 18。"
            pages = parse_page_list(spec, total)

        out = build_output_path(src, pages, out_dir=out_dir)
        extract_pages(src, pages, out)
        msg = f"已保存：\n{out}\n\n页码：{pages}"
        if remove_from_source:
            left = remove_pages_from_source(src, pages)
            msg += f"\n\n已从原 PDF 移出上述页，原文件剩余 {left} 页"
        return _as_gradio_file(out), msg + dir_note
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
                    """
上传或拖入 PDF 即可。不填保存目录时，**保存到该 PDF 在本机所在文件夹**（按文件名+大小查找）。  
若找不到原文件，请填写「本机 PDF 路径」或「保存目录」。界面预览区可能是副本，以状态栏里的路径为准。
                    """
                )
                pdf_in = gr.File(
                    label="上传 PDF",
                    file_types=[".pdf"],
                    type="filepath",
                )
                pdf_path = gr.Textbox(
                    label="本机 PDF 路径（可选）",
                    placeholder="/Users/你/文件夹/书名.pdf",
                )
                save_dir = gr.Textbox(
                    label="保存目录（可选；不填则保存到 PDF 所在目录）",
                    placeholder="一般留空即可",
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
                remove_from_src = gr.Checkbox(
                    label="从原文件中移出已拆出的页（覆盖保存原 PDF）",
                    value=False,
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
                        remove_from_src,
                    ],
                    outputs=[pdf_out, status],
                )

            # ---------- 双语版（左右对照） ----------
            with gr.Tab("双语版（左右对照）"):
                gr.Markdown(
                    """
上传或拖入两个**页数相同**的 PDF。不填保存目录时，**保存到左侧 PDF 在本机的文件夹**（按文件名+大小查找）。  
找不到原文件时请填「左 PDF 本机路径」或「保存目录」。输出：`左文件名-双语版.pdf`
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

            # ---------- PDF 合并 ----------
            with gr.Tab("PDF 合并"):
                gr.Markdown(
                    """
上传或拖入两个 PDF：**左 PDF 全部页在前，右 PDF 接在后面**。  
不填保存目录时，**保存到左侧 PDF 在本机的文件夹**（按文件名+大小查找）。  
输出：`左文件名-合并.pdf`
                    """
                )
                with gr.Row():
                    merge_left_in = gr.File(
                        label="左 PDF（在前）",
                        file_types=[".pdf"],
                        type="filepath",
                    )
                    merge_right_in = gr.File(
                        label="右 PDF（在后）",
                        file_types=[".pdf"],
                        type="filepath",
                    )
                with gr.Row():
                    merge_left_path = gr.Textbox(
                        label="左 PDF 本机路径（可选）",
                        placeholder="/Users/你/第一部分.pdf",
                    )
                    merge_right_path = gr.Textbox(
                        label="右 PDF 本机路径（可选）",
                        placeholder="/Users/你/第二部分.pdf",
                    )
                save_dir_merge = gr.Textbox(
                    label="保存目录（可选；不填则保存到左侧 PDF 所在目录）",
                    placeholder="一般留空即可",
                )
                run_merge_btn = gr.Button("合并 PDF", variant="primary")
                status_merge = gr.Textbox(label="状态", interactive=False, lines=6)
                pdf_out_merge = gr.File(label="预览/下载")

                run_merge_btn.click(
                    run_merge,
                    inputs=[
                        merge_left_in,
                        merge_left_path,
                        merge_right_in,
                        merge_right_path,
                        save_dir_merge,
                    ],
                    outputs=[pdf_out_merge, status_merge],
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


def _under_pm2() -> bool:
    return bool(os.environ.get("PM2_HOME") or os.environ.get("PM2_USAGE"))


def _gradio_allowed_paths() -> list[str]:
    """Gradio 文件预览白名单；其它目录由 _as_gradio_file 复制到缓存。"""
    return [
        str(p.expanduser().resolve())
        for p in gradio_allowed_path_candidates()
        if p.exists()
    ]


def main() -> None:
    port = int(os.environ.get("PDF_SPLIT_PORT", "10001"))

    if _port_in_use(port):
        if _under_pm2():
            print(f"端口 {port} 被占用，PM2 启动前先释放旧进程…")
            kill_port_processes(port)
        elif _service_ok(port):
            print(f"PDF 拆页 Web 已在运行，无需重复启动：http://localhost:{port}/")
            sys.exit(0)
        else:
            print(f"端口 {port} 已被占用但服务无响应。请先结束旧进程：")
            print(f"  {port_busy_hint(port)}")
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
