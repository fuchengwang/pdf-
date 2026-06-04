#!/usr/bin/env python3
"""
简单 PDF 拆页工具：输出到原文件同目录，文件名清晰。

用法示例：
  # 抽出第 4、7、18 页
  python pdf_split.py "book.pdf" 4 7 18

  # 抽出第 1～3 页（连续范围）
  python pdf_split.py "book.pdf" --range 1-3

  # 按页码列表（支持 1,3,5-8）
  python pdf_split.py "book.pdf" --pages 1,3,5-8

  # 每一页单独成一个 PDF
  python pdf_split.py "book.pdf" --each

  # 一次处理多个 PDF，各自拆成单页
  python pdf_split.py a.pdf b.pdf --each
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz


def parse_page_list(spec: str, total: int) -> list[int]:
    """解析页码：1,3,5-8 -> [1,3,5,6,7,8]（1 起算）。"""
    pages: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    # 去重且保持顺序
    seen: set[int] = set()
    ordered: list[int] = []
    for p in pages:
        if p < 1 or p > total:
            raise ValueError(f"页码 {p} 超出范围（共 {total} 页）")
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    if not ordered:
        raise ValueError("未指定有效页码")
    return ordered


def is_consecutive(pages: list[int]) -> bool:
    return len(pages) > 1 and pages == list(range(pages[0], pages[-1] + 1))


def build_output_path(src: Path, pages: list[int], each_index: int | None = None) -> Path:
    """根据页码生成清晰文件名。"""
    stem = src.stem
    parent = src.parent

    if each_index is not None:
        # 单页：书名-第004页.pdf（位数按总页数对齐）
        return parent / f"{stem}-第{each_index:03d}页.pdf"

    if len(pages) == 1:
        return parent / f"{stem}-第{pages[0]}页.pdf"
    if is_consecutive(pages):
        if pages[0] == 1 and len(pages) <= 10:
            return parent / f"{stem}-前{len(pages)}页.pdf"
        return parent / f"{stem}-第{pages[0]}-{pages[-1]}页.pdf"
    label = "-".join(str(p) for p in pages)
    return parent / f"{stem}-第{label}页.pdf"


def extract_pages(src: Path, pages: list[int], out: Path) -> None:
    doc = fitz.open(src)
    new = fitz.open()
    for p in pages:
        idx = p - 1
        new.insert_pdf(doc, from_page=idx, to_page=idx)
    new.save(out, garbage=4, deflate=True)
    new.close()
    doc.close()


def split_each(src: Path) -> list[Path]:
    doc = fitz.open(src)
    total = doc.page_count
    width = len(str(total))
    outputs: list[Path] = []
    for i in range(total):
        out = src.parent / f"{src.stem}-第{i + 1:0{width}d}页.pdf"
        new = fitz.open()
        new.insert_pdf(doc, from_page=i, to_page=i)
        new.save(out, garbage=4, deflate=True)
        new.close()
        outputs.append(out)
    doc.close()
    return outputs


def process_file(src: Path, pages: list[int] | None, each: bool) -> None:
    if not src.is_file():
        raise FileNotFoundError(f"找不到文件: {src}")
    if src.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF 文件: {src}")

    if each:
        outs = split_each(src)
        print(f"[完成] {src.name} -> {len(outs)} 个单页文件（同目录）")
        for o in outs[:3]:
            print(f"       {o.name}")
        if len(outs) > 3:
            print(f"       ... 共 {len(outs)} 个")
        return

    doc = fitz.open(src)
    total = doc.page_count
    doc.close()

    if pages is None:
        raise ValueError("请指定页码，或使用 --each / --range / --pages")

    out = build_output_path(src, pages)
    extract_pages(src, pages, out)
    print(f"[完成] {src.name} 第 {pages} 页 -> {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="简单 PDF 拆页（输出到原目录）")
    parser.add_argument("pdfs", nargs="+", type=Path, help="一个或多个 PDF 路径")
    parser.add_argument("pages_pos", nargs="*", type=int, help="页码，如 4 7 18")
    parser.add_argument("--range", dest="page_range", metavar="N-M", help="连续页，如 1-3")
    parser.add_argument("--pages", metavar="LIST", help="页码列表，如 1,3,5-8")
    parser.add_argument("--each", action="store_true", help="每一页单独导出为一个 PDF")
    args = parser.parse_args()

    # 若最后一个参数是 .pdf，则全是文件；否则需区分（argparse 已分开）
    pdf_files = [p.expanduser().resolve() for p in args.pdfs]

    for pdf in pdf_files:
        pages: list[int] | None = None
        if args.each:
            process_file(pdf, None, each=True)
            continue

        doc = fitz.open(pdf)
        total = doc.page_count
        doc.close()

        if args.page_range:
            if not re.fullmatch(r"\d+-\d+", args.page_range):
                parser.error("--range 格式应为 1-3")
            a, b = map(int, args.page_range.split("-"))
            pages = parse_page_list(f"{a}-{b}", total)
        elif args.pages:
            pages = parse_page_list(args.pages, total)
        elif args.pages_pos:
            pages = parse_page_list(",".join(str(x) for x in args.pages_pos), total)
        else:
            parser.error(f"请为 {pdf.name} 指定页码，或使用 --each")

        process_file(pdf, pages, each=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
