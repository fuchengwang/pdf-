#!/usr/bin/env python3
"""
Finder 右键 / 快速操作入口：合并、双语版、拆页（复用 pdf_split 逻辑）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

# 翻译中控台默认地址（与 translate_console.py 一致）
TRANSLATE_CONSOLE_URL = os.environ.get(
    "PDF2ZH_CONSOLE_URL", "http://127.0.0.1:10003"
)

LOG_PATH = Path.home() / "Library/Logs/pdf-finder-tools.log"

from pdf_split import (
    build_output_path,
    extract_pages,
    merge_bilingual_side_by_side,
    merge_pdfs_append,
    normalize_pages_spec,
    parse_page_list,
    remove_pages_from_source,
)


def _applescript_quote(text: str) -> str:
    """转成 AppleScript 安全字符串（避免换行/引号导致 osascript 失败）。"""
    one_line = text.replace("\r\n", "\n").replace("\n", " ").replace("\r", " ")
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _notify(title: str, message: str) -> None:
    """macOS 通知中心提示。"""
    subprocess.run(
        [
            "osascript",
            "-e",
            f"display notification {_applescript_quote(message[:200])} with title {_applescript_quote(title)}",
        ],
        check=False,
    )


def _log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def _alert(message: str, title: str = "PDF 工具") -> None:
    """错误弹窗（macOS 26 上 display alert 会语法报错，改用 display dialog）。"""
    _log(f"ALERT [{title}] {message}")
    subprocess.run(
        [
            "osascript",
            "-e",
            "display dialog "
            f"{_applescript_quote(message)} "
            f"with title {_applescript_quote(title)} "
            'buttons {"好"} default button "好" with icon stop',
        ],
        check=False,
    )
    _notify(title, message[:200])


def _decode_path(text: str) -> str:
    text = text.strip().strip('"').strip("'")
    if text.startswith("file://"):
        text = unquote(urlparse(text).path)
    return text


def _split_path_blob(blob: str) -> list[str]:
    """
    把一段文本拆成若干路径。
    修复缓存里两条 PDF 路径粘成一行（如 a.pdf/Users/.../b.pdf）的情况。
    """
    lines: list[str] = []
    for chunk in re.split(r"[\n\r\0]+", blob):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 多条路径缺少换行，在 .pdf/ 或 .pdf\ 处切开
        if re.search(r"\.pdf[/\\]", chunk, re.I):
            parts = re.split(r"(?<=\.pdf)(?=[/\\])", chunk, flags=re.I)
            for part in parts:
                part = part.strip()
                if part:
                    lines.append(part)
        else:
            lines.append(chunk)
    return lines


def collect_pdf_paths(argv: list[str], stdin_text: str = "") -> list[Path]:
    """
    从 Automator 参数解析 PDF 路径。
    常见情况：多个路径分多行放在 $1，或 file:/// 前缀。
    """
    blobs: list[str] = []
    for arg in argv:
        if arg in ("--", "merge", "bilingual", "split", "service"):
            continue
        blobs.append(arg)
    if stdin_text.strip():
        blobs.append(stdin_text)

    paths: list[Path] = []
    seen: set[str] = set()
    for blob in blobs:
        for line in _split_path_blob(blob):
            line = _decode_path(line)
            if not line:
                continue
            p = Path(line).expanduser()
            try:
                key = str(p.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            if p.is_file() and p.suffix.lower() == ".pdf":
                seen.add(key)
                paths.append(p.resolve())
    return paths


def _ensure_pdf(path: Path) -> Path:
    p = path.expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    if p.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF：{p.name}")
    return p


def cmd_merge(left: Path, right: Path) -> tuple[Path, int]:
    """左 PDF 在前、右 PDF 在后，保存到左文件所在目录。"""
    left = _ensure_pdf(left)
    right = _ensure_pdf(right)
    out = left.parent / f"{left.stem}-合并.pdf"
    n = merge_pdfs_append(left, right, out)
    return out, n


def cmd_bilingual(left: Path, right: Path) -> tuple[Path, int]:
    """左右对照双语版，保存到左文件所在目录。"""
    left = _ensure_pdf(left)
    right = _ensure_pdf(right)
    out = left.parent / f"{left.stem}-双语版.pdf"
    n = merge_bilingual_side_by_side(left, right, out)
    return out, n


def _ask_split_options() -> tuple[str, bool] | None:
    """
    弹出对话框：输入页码、是否从原文件移出。
    取消则返回 None。
    """
    script = r'''
on escape_quotes(t)
    set AppleScript's text item delimiters to "\""
    set parts to text items of t
    set AppleScript's text item delimiters to "\\\""
    set t to parts as string
    set AppleScript's text item delimiters to ""
    return t
end escape_quotes

try
    set dlg1 to display dialog "请输入要拆出的页码（指定页码模式）" & return & return & "例如：4,7,18  或  1-3" default answer "" buttons {"取消", "下一步"} default button "下一步" with title "PDF 拆页"
    if button returned of dlg1 is "取消" then return "CANCEL"
    set pagesText to text returned of dlg1

    set dlg2 to display dialog "是否从原 PDF 中移出已拆出的页？" & return & "（会覆盖保存原文件）" buttons {"取消", "不移除", "移出"} default button "不移除" with title "PDF 拆页"
    if button returned of dlg2 is "取消" then return "CANCEL"
    set removeFlag to "0"
    if button returned of dlg2 is "移出" then set removeFlag to "1"

    return pagesText & tab & removeFlag
on error number -128
    return "CANCEL"
end try
'''
    out = subprocess.check_output(["osascript", "-e", script], text=True).strip()
    if out == "CANCEL" or not out:
        return None
    parts = out.split("\t", 1)
    pages_text = parts[0].strip()
    remove = len(parts) > 1 and parts[1] == "1"
    if not pages_text:
        _alert("未填写页码。")
        return None
    return pages_text, remove


def cmd_split_interactive(pdf: Path) -> Path | None:
    """拆页：对话框输入页码，输出到 PDF 所在目录。"""
    pdf = _ensure_pdf(pdf)
    opts = _ask_split_options()
    if opts is None:
        return None
    pages_text, remove = opts

    doc_pages = __import__("fitz").open(pdf)
    total = doc_pages.page_count
    doc_pages.close()

    spec = normalize_pages_spec(pages_text)
    pages = parse_page_list(spec, total)
    out = build_output_path(pdf, pages)
    extract_pages(pdf, pages, out)
    if remove:
        left = remove_pages_from_source(pdf, pages)
        _notify("PDF 拆页", f"已保存；原文件剩余 {left} 页")
    return out


def cmd_split_cli(pdf: Path, pages_text: str, remove: bool) -> Path:
    """命令行拆页（供脚本直接传参）。"""
    pdf = _ensure_pdf(pdf)
    doc = __import__("fitz").open(pdf)
    total = doc.page_count
    doc.close()
    pages = parse_page_list(normalize_pages_spec(pages_text), total)
    out = build_output_path(pdf, pages)
    extract_pages(pdf, pages, out)
    if remove:
        remove_pages_from_source(pdf, pages)
    return out


def enqueue_translate(paths: list[Path]) -> list[dict]:
    """把 PDF 提交到翻译中控台队列（POST /api/jobs）。"""
    added: list[dict] = []
    for pdf in paths:
        payload = json.dumps({"path": str(pdf)}).encode("utf-8")
        req = urllib.request.Request(
            f"{TRANSLATE_CONSOLE_URL.rstrip('/')}/api/jobs",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(
                "翻译中控台未运行，请先启动：\n"
                f"  {TRANSLATE_CONSOLE_URL}/\n"
                f"（项目目录执行 ./start-translate-console.sh）\n"
                f"详情：{e}"
            ) from e
        for item in data.get("added", []):
            added.append(item)
        for err in data.get("errors", []):
            raise ValueError(err)
    return added


def run_service(action: str, paths: list[Path]) -> None:
    """Finder 服务统一入口。"""
    _log(f"service {action} paths={[str(p) for p in paths]}")
    if action == "translate":
        if not paths:
            raise ValueError(
                "「PDF 翻译」需要选中至少 1 个 PDF。"
                "若刚右键运行，请检查自动化权限（访达 → 隐私与安全性）。"
            )
        items = enqueue_translate(paths)
        names = "、".join(i.get("name", "") for i in items[:3])
        extra = f" 等 {len(items)} 个" if len(items) > 3 else ""
        _notify("PDF 翻译", f"已加入队列：{names}{extra}")
        print(json.dumps(items, ensure_ascii=False))
    elif action == "merge":
        if len(paths) != 2:
            raise ValueError(
                "「PDF 合并」需要恰好选中 2 个 PDF，"
                f"当前识别到 {len(paths)} 个。"
                "请先选中左 PDF，再按住 Cmd 选中右 PDF。"
            )
        out, n = cmd_merge(paths[0], paths[1])
        _notify("PDF 合并", f"已保存（{n} 页）：{out.name}")
        print(out)
    elif action == "bilingual":
        if len(paths) != 2:
            extra = ""
            if len(paths) == 0:
                extra = (
                    " 若刚右键运行：请到「系统设置 → 隐私与安全性 → 自动化」，"
                    "允许「Automator」或「文件夹操作」控制「访达」。"
                )
            raise ValueError(
                "「PDF 双语版」需要恰好选中 2 个 PDF，"
                f"当前识别到 {len(paths)} 个。"
                "请先选中左/原文 PDF，再按住 Cmd 选中右/译文 PDF。"
                + extra
            )
        out, n = cmd_bilingual(paths[0], paths[1])
        _notify("PDF 双语版", f"已保存（{n} 页）：{out.name}")
        print(out)
    elif action == "split":
        if len(paths) != 1:
            raise ValueError(
                "「PDF 拆页」需要只选中 1 个 PDF，"
                f"当前识别到 {len(paths)} 个。"
            )
        out = cmd_split_interactive(paths[0])
        if out is None:
            sys.exit(0)
        _notify("PDF 拆页", f"已保存：{out.name}")
        print(out)
    else:
        raise ValueError(f"未知操作：{action}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Finder / 命令行 PDF 快捷工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_service = sub.add_parser("service", help="Finder 服务调用")
    p_service.add_argument(
        "action",
        choices=["merge", "bilingual", "split", "translate"],
    )
    p_service.add_argument("paths", nargs="*", default=[])

    p_merge = sub.add_parser("merge", help="合并两个 PDF（左在前）")
    p_merge.add_argument("left", type=Path)
    p_merge.add_argument("right", type=Path)

    p_bi = sub.add_parser("bilingual", help="生成双语对照版")
    p_bi.add_argument("left", type=Path)
    p_bi.add_argument("right", type=Path)

    p_split = sub.add_parser("split", help="拆出指定页")
    p_split.add_argument("pdf", type=Path)
    p_split.add_argument("--pages", default="", help="页码；不填则弹出对话框")
    p_split.add_argument(
        "--remove-from-source",
        action="store_true",
        help="从原文件移出已拆页",
    )
    p_split.add_argument(
        "--dialog",
        action="store_true",
        help="强制使用对话框输入页码",
    )

    args = parser.parse_args()
    try:
        if args.cmd == "service":
            stdin_text = ""
            if not sys.stdin.isatty():
                stdin_text = sys.stdin.read()
            paths = collect_pdf_paths(list(args.paths), stdin_text)
            run_service(args.action, paths)
        elif args.cmd == "merge":
            out, n = cmd_merge(args.left, args.right)
            msg = f"已保存（共 {n} 页）：\n{out.name}"
            _notify("PDF 合并", msg)
            print(out)
        elif args.cmd == "bilingual":
            out, n = cmd_bilingual(args.left, args.right)
            msg = f"已保存（共 {n} 页）：\n{out.name}"
            _notify("PDF 双语版", msg)
            print(out)
        elif args.cmd == "split":
            if args.dialog or not args.pages:
                out = cmd_split_interactive(args.pdf)
                if out is None:
                    sys.exit(0)
            else:
                out = cmd_split_cli(args.pdf, args.pages, args.remove_from_source)
            msg = f"已保存：\n{out.name}"
            _notify("PDF 拆页", msg)
            print(out)
    except Exception as e:
        _alert(str(e))
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
