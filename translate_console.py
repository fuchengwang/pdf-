#!/usr/bin/env python3
"""
pdf2zh 翻译中控台：单线程队列 + 简单 Web 页面（默认端口 10003）。

- 无任务时显示「当前无任务」
- 翻译设置沿用 ~/.config/pdf2zh/config.v3.toml（与 Web UI 7860 共用）
- Finder 右键可通过 POST /api/jobs 入队
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pdf2zh_next.config import ConfigManager
from pdf2zh_next.const import DEFAULT_CONFIG_FILE

# 与 Web UI 相同：读写 ~/.config/pdf2zh/config.v3.toml
_config_manager = ConfigManager()


def _load_settings():
    """
    加载 pdf2zh 配置。库内部会 parse_args()，需临时清空 argv，
    避免与本脚本的 --port 等参数冲突。
    """
    argv = sys.argv[:]
    sys.argv = ["translate_console"]
    try:
        return _config_manager.initialize_config()
    finally:
        sys.argv = argv
from pdf2zh_next.high_level import do_translate_async_stream

from platform_compat import kill_port_processes

# 默认监听端口，工作台 PM2 可注入 PDF2ZH_CONSOLE_PORT
DEFAULT_PORT = 10003

# 中控台输出：仅保留纯翻译版，命名为「原文件名.中文版.pdf」
CHINESE_PDF_SUFFIX = ".中文版.pdf"

# 队列数据库放在用户目录，重启后任务记录仍在
DB_DIR = Path.home() / "Library/Application Support/pdf2zh-translate-console"
DB_PATH = DB_DIR / "queue.db"

# pdf2zh Web UI 地址（仅展示链接，设置在此修改）
WEBUI_URL = os.environ.get("PDF2ZH_WEBUI_URL", "http://127.0.0.1:7860")

logger = logging.getLogger("translate_console")


# ---------------------------------------------------------------------------
# 数据库：FIFO 队列 + 历史记录
# ---------------------------------------------------------------------------


class JobStore:
    """SQLite 任务存储（线程安全）。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    output_mono TEXT NOT NULL DEFAULT '',
                    output_dual TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.commit()

    def add_job(self, file_path: Path) -> int:
        """将 PDF 加入排队（pending）。"""
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO jobs (file_path, file_name, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (str(file_path), file_path.name, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [job_id]
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", vals)
            self._conn.commit()

    def fetch_next_pending(self) -> dict[str, Any] | None:
        """取最早的一条 pending 任务。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_by_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ({placeholders})
                ORDER BY id ASC
                """,
                statuses,
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_history(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('done', 'failed', 'cancelled')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def delete_job(self, job_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE id = ? AND status IN ('pending', 'paused')",
                (job_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_status(self, job_id: int, old: str, new: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ? AND status = ?",
                (new, job_id, old),
            )
            self._conn.commit()
            return cur.rowcount > 0


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _elapsed_seconds(started_at: str) -> int:
    if not started_at:
        return 0
    try:
        start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
        return max(0, int((datetime.now() - start).total_seconds()))
    except ValueError:
        return 0


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _apply_console_output_prefs(settings: Any) -> None:
    """
    中控台专用输出偏好：
    - 只要纯翻译版（no_dual）
    - 开启自动术语提取（翻译前用 LLM 抽术语，翻译时强制统一译法，提升质量）
    - 不保存 .glossary.csv 到磁盘（完成后会清理）
    """
    settings.pdf.no_dual = True
    settings.pdf.no_mono = False
    settings.translation.no_auto_extract_glossary = False
    settings.translation.save_auto_extracted_glossary = False


def _chinese_pdf_path(source: Path) -> Path:
    """目标文件名：原文件名.中文版.pdf"""
    return source.parent / f"{source.stem}{CHINESE_PDF_SUFFIX}"


def _safe_unlink(path: Path | str | None) -> None:
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        return
    try:
        p.unlink()
        logger.info("已删除：%s", p)
    except OSError as e:
        logger.warning("删除失败 %s: %s", p, e)


def _cleanup_extra_outputs(source: Path, keep: Path) -> None:
    """删除同一次翻译留下的双语版、术语表、带 .mono/.dual 的中间文件。"""
    stem = source.stem
    for p in source.parent.iterdir():
        if not p.is_file():
            continue
        if p.resolve() in {keep.resolve(), source.resolve()}:
            continue
        if not p.name.startswith(stem):
            continue
        name = p.name
        if (
            ".dual.pdf" in name
            or ".glossary." in name
            or ".mono.pdf" in name
            or (".no_watermark." in name and p.suffix.lower() == ".pdf")
        ):
            _safe_unlink(p)


def _finalize_mono_output(
    source: Path,
    mono: Path | str | None,
    dual: Path | str | None,
    glossary: Path | str | None,
) -> Path | None:
    """
    将翻译结果整理为唯一的「原文件名.中文版.pdf」。
    删除双语版、术语表及 babeldoc 默认长文件名产物。
    """
    if not mono:
        return None
    mono_path = Path(mono)
    if not mono_path.is_file():
        return None

    target = _chinese_pdf_path(source)
    if mono_path.resolve() != target.resolve():
        if target.exists():
            _safe_unlink(target)
        mono_path.rename(target)
        logger.info("已重命名：%s → %s", mono_path.name, target.name)
    else:
        target = mono_path

    _safe_unlink(dual)
    _safe_unlink(glossary)
    _cleanup_extra_outputs(source, target)
    return target


# ---------------------------------------------------------------------------
# 翻译 Worker：同时只跑 1 个任务
# ---------------------------------------------------------------------------


class TranslateWorker:
    """后台线程：从队列取任务，调用 pdf2zh 翻译。"""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._current_task: asyncio.Task | None = None
        self._current_job_id: int | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="translate-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.cancel_current()

    def cancel_current(self) -> bool:
        """取消正在翻译的任务。"""
        with self._lock:
            loop = self._loop
            task = self._current_task
            job_id = self._current_job_id
        if not loop or not task or job_id is None:
            return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    def current_job_id(self) -> int | None:
        with self._lock:
            return self._current_job_id

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        while not self._stop.is_set():
            job = self.store.fetch_next_pending()
            if not job:
                time.sleep(0.4)
                continue
            try:
                loop.run_until_complete(self._translate_one(job))
            except Exception as e:
                logger.exception("worker error: %s", e)
                time.sleep(1)

    async def _translate_one(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        file_path = Path(job["file_path"])

        with self._lock:
            self._current_job_id = job_id

        self.store.update_job(
            job_id,
            status="running",
            started_at=_now_iso(),
            progress=0,
            stage="准备中",
            error="",
        )

        try:
            await self._do_translate(job_id, file_path)
        except asyncio.CancelledError:
            self.store.update_job(
                job_id,
                status="cancelled",
                finished_at=_now_iso(),
                stage="已取消",
            )
            logger.info("job %s cancelled", job_id)
        except Exception as e:
            self.store.update_job(
                job_id,
                status="failed",
                error=str(e),
                finished_at=_now_iso(),
                stage="失败",
            )
            logger.exception("job %s failed", job_id)
        finally:
            with self._lock:
                self._current_task = None
                self._current_job_id = None

    async def _do_translate(self, job_id: int, file_path: Path) -> None:
        if not file_path.is_file():
            raise FileNotFoundError(f"文件不存在：{file_path}")
        if file_path.suffix.lower() != ".pdf":
            raise ValueError("仅支持 PDF 文件")

        # 每次翻译前重新加载配置，与 Web UI 修改保持同步
        settings = _load_settings()
        settings.report_interval = 0.2
        _apply_console_output_prefs(settings)
        out = settings.translation.output
        if not out or str(out) in ("null", "None", ""):
            settings.translation.output = str(file_path.parent)

        task = asyncio.create_task(self._stream_translate(job_id, settings, file_path))
        with self._lock:
            self._current_task = task
        await task

    async def _stream_translate(self, job_id: int, settings: Any, file_path: Path) -> None:
        async for event in do_translate_async_stream(settings, file_path):
            etype = event.get("type")
            if etype in ("progress_start", "progress_update", "progress_end"):
                stage = event.get("stage", "")
                part_i = event.get("part_index", 0)
                part_n = event.get("total_parts", 0)
                cur = event.get("stage_current", 0)
                total = event.get("stage_total", 0)
                desc = f"{stage} ({part_i}/{part_n}, {cur}/{total})"
                self.store.update_job(
                    job_id,
                    progress=float(event.get("overall_progress", 0)),
                    stage=desc,
                )
            elif etype == "finish":
                result = event["translate_result"]
                final = _finalize_mono_output(
                    file_path,
                    result.mono_pdf_path,
                    result.dual_pdf_path,
                    getattr(result, "auto_extracted_glossary_path", None),
                )
                self.store.update_job(
                    job_id,
                    status="done",
                    progress=100,
                    stage="完成",
                    output_mono=str(final) if final else "",
                    output_dual="",
                    finished_at=_now_iso(),
                )
                return
            elif etype == "error":
                msg = event.get("error", "未知错误")
                details = event.get("details", "")
                full = f"{msg}" + (f" ({details})" if details else "")
                self.store.update_job(
                    job_id,
                    status="failed",
                    error=full,
                    finished_at=_now_iso(),
                    stage="失败",
                )
                return


def _settings_summary() -> dict[str, Any]:
    """读取当前 pdf2zh 配置摘要（完整设置在 Web UI 修改）。"""
    try:
        settings = _load_settings()
        engine = "未知"
        if settings.translate_engine_settings:
            engine = settings.translate_engine_settings.translate_engine_type
        out = settings.translation.output
        if not out or str(out) in ("null", "None"):
            out = "（与源 PDF 同目录）"
        return {
            "lang_in": settings.translation.lang_in,
            "lang_out": settings.translation.lang_out,
            "engine": engine,
            "output": str(out),
            "config_path": str(DEFAULT_CONFIG_FILE),
            "webui_url": WEBUI_URL,
        }
    except Exception as e:
        return {
            "lang_in": "?",
            "lang_out": "?",
            "engine": "?",
            "output": "?",
            "config_path": str(DEFAULT_CONFIG_FILE),
            "webui_url": WEBUI_URL,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# HTTP API + 内嵌 Web 页面
# ---------------------------------------------------------------------------

STORE: JobStore | None = None
WORKER: TranslateWorker | None = None


class ConsoleHandler(BaseHTTPRequestHandler):
    """简单 REST + 单页 HTML。"""

    server_version = "pdf2zh-console/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._send_html(INDEX_HTML)
            elif path == "/api/status":
                self._send_json(200, _build_status())
            else:
                self._send_json(404, {"error": "not found"})
        except Exception as e:
            logger.exception("GET %s failed", self.path)
            self._send_json(500, {"error": str(e)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/jobs":
                self._handle_add_job()
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                self._handle_cancel(_parse_job_id(path, "/cancel"))
                return
            if path.startswith("/api/jobs/") and path.endswith("/remove"):
                self._handle_remove(_parse_job_id(path, "/remove"))
                return
            if path.startswith("/api/jobs/") and path.endswith("/pause"):
                self._handle_pause(_parse_job_id(path, "/pause"))
                return
            if path.startswith("/api/jobs/") and path.endswith("/resume"):
                self._handle_resume(_parse_job_id(path, "/resume"))
                return
            self._send_json(404, {"error": "not found"})
        except Exception as e:
            logger.exception("POST %s failed", self.path)
            self._send_json(500, {"error": str(e)})

    def _handle_add_job(self) -> None:
        assert STORE is not None
        data = self._read_json_body()
        paths: list[str] = []
        if "path" in data and data["path"]:
            paths.append(str(data["path"]))
        if "paths" in data and isinstance(data["paths"], list):
            paths.extend(str(p) for p in data["paths"] if p)

        if not paths:
            self._send_json(400, {"error": "缺少 path 或 paths"})
            return

        added = []
        errors = []
        for p in paths:
            fp = Path(p).expanduser()
            try:
                fp = fp.resolve()
            except OSError:
                errors.append(f"无效路径：{p}")
                continue
            if not fp.is_file():
                errors.append(f"文件不存在：{fp}")
                continue
            if fp.suffix.lower() != ".pdf":
                errors.append(f"不是 PDF：{fp.name}")
                continue
            jid = STORE.add_job(fp)
            added.append({"id": jid, "path": str(fp), "name": fp.name})

        self._send_json(200, {"added": added, "errors": errors})

    def _handle_cancel(self, job_id: int | None) -> None:
        assert STORE is not None and WORKER is not None
        if job_id is None:
            self._send_json(400, {"error": "无效任务 ID"})
            return
        job = STORE.get_job(job_id)
        if not job:
            self._send_json(404, {"error": "任务不存在"})
            return
        if job["status"] == "running":
            if WORKER.current_job_id() == job_id:
                WORKER.cancel_current()
                self._send_json(200, {"ok": True, "action": "cancelling"})
            else:
                self._send_json(409, {"error": "任务状态不一致"})
            return
        if job["status"] in ("pending", "paused"):
            STORE.update_job(job_id, status="cancelled", finished_at=_now_iso())
            self._send_json(200, {"ok": True, "action": "cancelled"})
            return
        self._send_json(400, {"error": f"无法取消状态：{job['status']}"})

    def _handle_remove(self, job_id: int | None) -> None:
        assert STORE is not None
        if job_id is None:
            self._send_json(400, {"error": "无效任务 ID"})
            return
        job = STORE.get_job(job_id)
        if not job:
            self._send_json(404, {"error": "任务不存在"})
            return
        if job["status"] in ("pending", "paused"):
            STORE.delete_job(job_id)
            self._send_json(200, {"ok": True})
            return
        self._send_json(400, {"error": "仅可移除排队中的任务"})

    def _handle_pause(self, job_id: int | None) -> None:
        assert STORE is not None
        if job_id is None:
            self._send_json(400, {"error": "无效任务 ID"})
            return
        if STORE.set_status(job_id, "pending", "paused"):
            self._send_json(200, {"ok": True})
        else:
            self._send_json(400, {"error": "仅可暂停排队中的任务"})

    def _handle_resume(self, job_id: int | None) -> None:
        assert STORE is not None
        if job_id is None:
            self._send_json(400, {"error": "无效任务 ID"})
            return
        if STORE.set_status(job_id, "paused", "pending"):
            self._send_json(200, {"ok": True})
        else:
            self._send_json(400, {"error": "仅可恢复已暂停的排队任务"})


def _parse_job_id(path: str, suffix: str) -> int | None:
    # /api/jobs/123/cancel
    mid = path.removeprefix("/api/jobs/").removesuffix(suffix)
    try:
        return int(mid)
    except ValueError:
        return None


def _build_status() -> dict[str, Any]:
    assert STORE is not None
    running = STORE.list_by_status(("running",))
    current = None
    if running:
        j = running[0]
        current = {
            **j,
            "elapsed_sec": _elapsed_seconds(j.get("started_at", "")),
            "elapsed_text": _format_duration(_elapsed_seconds(j.get("started_at", ""))),
        }
    queue = STORE.list_by_status(("pending", "paused"))
    history = STORE.list_history()
    idle = current is None and len(queue) == 0
    return {
        "idle": idle,
        "idle_message": "当前无任务",
        "current": current,
        "queue": queue,
        "history": history,
        "settings": _settings_summary(),
    }


# 内嵌单页：每 2 秒轮询 /api/status
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF 翻译中控台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #f5f6f8; color: #1a1a1a; }
  h1 { font-size: 1.35rem; margin: 0 0 8px; }
  .sub { color: #666; font-size: 0.9rem; margin-bottom: 20px; }
  .card { background: #fff; border-radius: 10px; padding: 16px 18px;
          margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .card h2 { font-size: 1rem; margin: 0 0 12px; }
  .empty { color: #888; text-align: center; padding: 28px 0; }
  .bar { height: 8px; background: #e8eaed; border-radius: 4px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: #2563eb; width: 0%; transition: width .3s; }
  .meta { font-size: 0.85rem; color: #555; margin-top: 8px; }
  .err { color: #b91c1c; font-size: 0.85rem; margin-top: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eee; }
  th { color: #666; font-weight: 600; }
  .btn { border: none; background: #e5e7eb; color: #111; padding: 4px 10px;
         border-radius: 6px; cursor: pointer; font-size: 0.8rem; margin-right: 4px; }
  .btn:hover { background: #d1d5db; }
  .btn-danger { background: #fee2e2; color: #991b1b; }
  .btn-danger:hover { background: #fecaca; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; }
  .tag-pending { background: #dbeafe; color: #1d4ed8; }
  .tag-paused { background: #fef3c7; color: #b45309; }
  .tag-running { background: #dcfce7; color: #15803d; }
  .tag-done { background: #e5e7eb; color: #374151; }
  .tag-failed { background: #fee2e2; color: #991b1b; }
  .tag-cancelled { background: #f3f4f6; color: #6b7280; }
  a { color: #2563eb; }
  .settings dt { color: #666; font-size: 0.8rem; }
  .settings dd { margin: 0 0 8px; font-size: 0.9rem; }
</style>
</head>
<body>
  <h1>PDF 翻译中控台</h1>
  <p class="sub">单线程队列 · 设置与 <a id="webui-link" href="#" target="_blank">pdf2zh Web UI</a> 共用 config.v3.toml</p>

  <div class="card" id="current-card">
    <h2>当前任务</h2>
    <div id="current-body" class="empty">加载中…</div>
  </div>

  <div class="card">
    <h2>排队 <span id="queue-count"></span></h2>
    <div id="queue-body" class="empty">加载中…</div>
  </div>

  <div class="card">
    <h2>历史记录</h2>
    <div id="history-body" class="empty">加载中…</div>
  </div>

  <div class="card">
    <h2>当前设置（只读）</h2>
    <dl class="settings" id="settings-body"></dl>
  </div>

<script>
const STATUS_LABEL = {
  pending: '排队中', paused: '已暂停', running: '翻译中',
  done: '完成', failed: '失败', cancelled: '已取消'
};

function tag(status) {
  return `<span class="tag tag-${status}">${STATUS_LABEL[status] || status}</span>`;
}

async function api(path, method='GET') {
  const r = await fetch(path, { method });
  return r.json();
}

function renderCurrent(d) {
  const el = document.getElementById('current-body');
  if (!d.current) {
    el.className = 'empty';
    el.innerHTML = d.idle ? d.idle_message : '等待下一个任务…';
    return;
  }
  const c = d.current;
  el.className = '';
  el.innerHTML = `
    <div><strong>${c.file_name}</strong> ${tag('running')}</div>
    <div class="meta">${c.file_path}</div>
    <div class="bar" style="margin-top:12px"><i style="width:${c.progress || 0}%"></i></div>
    <div class="meta">进度 ${(c.progress||0).toFixed(1)}% · ${c.stage || ''} · 已运行 ${c.elapsed_text || ''}</div>
    <button class="btn btn-danger" onclick="cancelJob(${c.id})">取消翻译</button>
  `;
}

function renderQueue(list) {
  const el = document.getElementById('queue-body');
  document.getElementById('queue-count').textContent = list.length ? `(${list.length})` : '';
  if (!list.length) {
    el.className = 'empty';
    el.textContent = '队列为空';
    return;
  }
  el.className = '';
  el.innerHTML = `<table><thead><tr><th>文件</th><th>状态</th><th>操作</th></tr></thead><tbody>` +
    list.map(j => `<tr>
      <td title="${j.file_path}">${j.file_name}</td>
      <td>${tag(j.status)}</td>
      <td>
        ${j.status==='pending' ? `<button class="btn" onclick="pauseJob(${j.id})">暂停</button>` : ''}
        ${j.status==='paused' ? `<button class="btn" onclick="resumeJob(${j.id})">恢复</button>` : ''}
        <button class="btn btn-danger" onclick="removeJob(${j.id})">移除</button>
      </td>
    </tr>`).join('') + '</tbody></table>';
}

function renderHistory(list) {
  const el = document.getElementById('history-body');
  if (!list.length) {
    el.className = 'empty';
    el.textContent = '暂无记录';
    return;
  }
  el.className = '';
  el.innerHTML = `<table><thead><tr><th>文件</th><th>状态</th><th>时间</th><th>说明</th></tr></thead><tbody>` +
    list.map(j => `<tr>
      <td title="${j.file_path}">${j.file_name}</td>
      <td>${tag(j.status)}</td>
      <td>${j.finished_at || j.started_at || j.created_at}</td>
      <td>${j.error || j.stage || (j.output_mono ? j.output_mono.split('/').pop() : '')}</td>
    </tr>`).join('') + '</tbody></table>';
}

function renderSettings(s) {
  const el = document.getElementById('settings-body');
  const link = document.getElementById('webui-link');
  link.href = s.webui_url;
  el.innerHTML = `
    <dt>翻译引擎</dt><dd>${s.engine}</dd>
    <dt>源语言 → 目标语言</dt><dd>${s.lang_in} → ${s.lang_out}</dd>
    <dt>输出目录</dt><dd>${s.output}</dd>
    <dt>配置文件</dt><dd><code>${s.config_path}</code></dd>
    <dt>修改设置</dt><dd>请打开 <a href="${s.webui_url}" target="_blank">pdf2zh Web UI</a> 保存后，新任务自动生效。</dd>
  `;
}

async function refresh() {
  try {
    const d = await api('/api/status');
    renderCurrent(d);
    renderQueue(d.queue || []);
    renderHistory(d.history || []);
    renderSettings(d.settings || {});
  } catch (e) {
    console.error(e);
  }
}

async function cancelJob(id) { await api(`/api/jobs/${id}/cancel`, 'POST'); refresh(); }
async function removeJob(id) { await api(`/api/jobs/${id}/remove`, 'POST'); refresh(); }
async function pauseJob(id) { await api(`/api/jobs/${id}/pause`, 'POST'); refresh(); }
async function resumeJob(id) { await api(`/api/jobs/${id}/resume`, 'POST'); refresh(); }

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def run_server(port: int) -> None:
    """启动 HTTP 服务与翻译 Worker。"""
    global STORE, WORKER
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # PM2 托管时先释放端口，避免重启循环
    under_pm2 = bool(os.environ.get("PM2_HOME") or os.environ.get("PM2_USAGE"))
    if under_pm2:
        kill_port_processes(port)

    STORE = JobStore(DB_PATH)
    WORKER = TranslateWorker(STORE)
    WORKER.start()

    server = ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)
    logger.info("翻译中控台已启动：http://127.0.0.1:%s/", port)
    logger.info("配置：%s", DEFAULT_CONFIG_FILE)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在停止…")
    finally:
        if WORKER:
            WORKER.stop()
        server.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("PDF2ZH_CONSOLE_PORT", DEFAULT_PORT))
    run_server(port)
