#!/usr/bin/env python3
"""批量测试 pics 目录图片翻译：耗时 + 是否与原图不同。"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

from PIL import Image
import numpy as np

# 直接调后端，避免 Gradio 排队
from google_image_translate import GoogleWebImageBackend

ROOT = Path(__file__).resolve().parent
PICS = ROOT / "pics"
OUT = PICS / "译图输出"
ASSETS = Path(
    "/Users/fucheng/.cursor/projects/Users-fucheng-Codes-pdf/assets"
    "/translated_image_zh-CN__12_-c8fbf89c-a8b4-43f7-816d-5a424177f611.png"
)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _compare_images(src: Path, out: Path) -> dict:
    """对比原图与译图：是否同一文件、像素差异比例。"""
    same_file = _md5(src) == _md5(out)
    img_s = Image.open(src).convert("RGB")
    img_o = Image.open(out).convert("RGB")
    if img_s.size != img_o.size:
        return {
            "same_file": same_file,
            "same_size": False,
            "size_src": img_s.size,
            "size_out": img_o.size,
            "diff_ratio": 1.0,
        }
    diff = np.abs(np.asarray(img_s, dtype=np.int16) - np.asarray(img_o, dtype=np.int16))
    ratio = float((diff > 8).mean())
    return {
        "same_file": same_file,
        "same_size": True,
        "size_src": img_s.size,
        "size_out": img_o.size,
        "diff_ratio": round(ratio, 4),
    }


def _verdict(cmp: dict) -> str:
    if cmp["same_file"]:
        return "未翻译（文件与原图相同）"
    if cmp["diff_ratio"] < 0.01:
        return "疑似未翻译（像素几乎相同）"
    if cmp["diff_ratio"] < 0.02:
        return "轻微变化（流程图线条占多数时属正常）"
    return "已翻译（与原图有明显差异，文字区已变）"


def main() -> int:
    sources: list[Path] = sorted(PICS.glob("*.png")) + sorted(PICS.glob("*.jpg"))
    # 狼群图为中文，目标语 zh-CN 不会出译文，批量测试只测 pics 内英文图

    if not sources:
        print("pics 目录下没有图片")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    backend = GoogleWebImageBackend()
    # 批量测试用后台最小化；若失败可改为 True
    backend.set_show_browser(False)

    rows: list[str] = []
    print(f"共 {len(sources)} 张，输出目录：{OUT}\n")

    try:
        for i, src in enumerate(sources, 1):
            dest = OUT / f"{src.stem}-译图.png"
            print(f"[{i}/{len(sources)}] {src.name} …", flush=True)
            t0 = time.time()
            try:
                backend.translate(src, dest)
                elapsed = time.time() - t0
                cmp = _compare_images(src, dest)
                verdict = _verdict(cmp)
                rows.append(
                    f"| {src.name} | {elapsed:.1f}s | {verdict} | "
                    f"diff={cmp['diff_ratio']} | {dest.name} |"
                )
                print(f"  ✓ {elapsed:.1f}s  {verdict}\n")
            except Exception as e:
                elapsed = time.time() - t0
                rows.append(f"| {src.name} | {elapsed:.1f}s | 失败: {e} | - | - |")
                print(f"  ✗ {elapsed:.1f}s  {e}\n")
    finally:
        backend.close()

    print("\n## 汇总\n")
    print("| 文件 | 耗时 | 结果 | diff比例 | 输出 |")
    print("|------|------|------|----------|------|")
    for r in rows:
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
