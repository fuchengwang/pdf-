#!/usr/bin/env python3
"""
Google 网页版图片翻译（真 Chrome + 持久登录目录）。
默认后台最小化；前台模式可观看。不用无头 Chrome（Google 无头不译图）。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# 持久化 Chrome 配置（登录一次后 Cookie 保存在此）
PROFILE_DIR = Path(__file__).resolve().parent / ".chrome-google-translate"
# 源语言自动检测，目标语言简体中文；可用环境变量 GOOGLE_IMAGE_TRANSLATE_URL 覆盖
TRANSLATE_URL = os.environ.get(
    "GOOGLE_IMAGE_TRANSLATE_URL",
    "https://translate.google.com/?hl=zh-cn&sl=auto&tl=zh-CN&op=images",
)
def _default_log_file() -> Path:
    import tempfile

    return Path(tempfile.gettempdir()) / "image-translate-web.log"


LOG_FILE = Path(os.environ.get("IMAGE_TRANSLATE_LOG", str(_default_log_file())))
# 单次点击最长等多久（毫秒）；真正耗时多在等 Google 动画“稳定”
CLICK_MS = 1_500
WAIT_MS = 20_000

_logger = logging.getLogger("google_image_translate")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)


class ImageTranslateBackend(ABC):
    """图片翻译后端接口。"""

    @abstractmethod
    def translate(self, src: Path, dest: Path) -> Path:
        """把 src 译成图并保存到 dest。"""


class GoogleWebImageBackend(ImageTranslateBackend):
    """本机 Chrome + Google 图片翻译页。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        # False=后台（有界面但最小化）；True=前台可见。绝不用 headless，否则 Google 不译图
        self._show_browser = False
        self._current_show_browser: bool | None = None
        # 页面已就绪后不再整页刷新（避免每次多等 15–20 秒）
        self._images_ready = False
        # 本会话内已设过语言则不再点开语言菜单
        self._languages_configured = False

    def set_show_browser(self, show: bool) -> None:
        """切换前台观看 / 后台最小化（会重启浏览器）。"""
        if self._current_show_browser == show and self._context:
            return
        self.close()
        self._show_browser = show

    def open_for_login(self) -> None:
        """弹出可见 Chrome，供用户登录 Google。"""
        self.set_show_browser(True)
        self._ensure_browser()
        assert self._page
        _logger.info("打开登录页")
        self._init_images_page(self._page)

    def close(self) -> None:
        with self._lock:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
            self._context = None
            self._playwright = None
            self._page = None
            self._images_ready = False
            self._languages_configured = False

    def _fast_click(self, locator, *, timeout: int = CLICK_MS) -> None:
        """
        快速点击：force 跳过“等动画停稳”，no_wait_after 不傻等页面跳转。
        Playwright 默认每次 click 可能多等 1–3 秒，Google 页面特别明显。
        """
        locator.click(timeout=timeout, force=True, no_wait_after=True)

    def translate(self, src: Path, dest: Path) -> Path:
        src = src.resolve()
        dest = dest.resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            t0 = time.time()
            self._ensure_browser()
            assert self._page
            page = self._page
            _logger.info(
                "开始翻译: %s -> %s (前台=%s)",
                src,
                dest,
                self._show_browser,
            )

            self._prepare_images_page(page, t0)

            self._upload_image(page, src)
            _logger.info("已上传文件 (%.1fs)", time.time() - t0)

            self._wait_translation_ready(page)
            _logger.info("翻译完成 (%.1fs)", time.time() - t0)

            self._ensure_translated_view(page)
            self._save_download(page, dest)
            _logger.info("已保存 (%.1fs): %s", time.time() - t0, dest)
            return dest

    def _ensure_browser(self) -> None:
        """
        始终使用有界面 Chrome（Google 无头模式会返回未翻译原图）。
        后台模式：最小化窗口，不挡屏幕。
        """
        if (
            self._context
            and self._page
            and not self._page.is_closed()
            and self._current_show_browser == self._show_browser
        ):
            return

        if self._context:
            self.close()

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _logger.info(
            "启动 Chrome profile=%s 前台可见=%s",
            PROFILE_DIR,
            self._show_browser,
        )
        self._current_show_browser = self._show_browser
        self._playwright = sync_playwright().start()
        chrome_args = ["--disable-blink-features=AutomationControlled"]
        if not self._show_browser:
            chrome_args.extend(["--start-minimized", "--window-position=-3000,0"])

        self._context = self._playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            locale="zh-CN",
            viewport={"width": 1280, "height": 900},
            args=chrome_args,
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._context.set_default_timeout(WAIT_MS)
        self._page.set_default_navigation_timeout(12_000)
        self._page.set_default_timeout(CLICK_MS)
        if self._show_browser:
            self._page.bring_to_front()

    def _browse_button(self, page: Page):
        return page.get_by_role("button", name=re.compile(r"浏览文件|Browse", re.I))

    def _upload_area_ready(self, page: Page) -> bool:
        """上传区是否已显示（可跳过整页加载）。"""
        try:
            return self._browse_button(page).first.is_visible()
        except Exception:
            return False

    def _prepare_images_page(self, page: Page, t0: float) -> None:
        """首次完整配置；之后只刷新图片页（避免上一张残留导致下载原图）。"""
        if self._images_ready:
            page.goto(TRANSLATE_URL, wait_until="domcontentloaded")
            self._open_images_tab(page)
            _logger.info("刷新图片页 (%.1fs) url=%s", time.time() - t0, page.url)
            return

        self._init_images_page(page)
        _logger.info("首次加载页面完成 (%.1fs) url=%s", time.time() - t0, page.url)

    def _init_images_page(self, page: Page) -> None:
        """整页打开 + 图片标签 + 语言（仅首次或失效时）。"""
        page.goto(TRANSLATE_URL, wait_until="domcontentloaded")
        self._open_images_tab(page)
        self._dismiss_cookie_banner(page)
        self._set_languages_if_needed(page)
        self._images_ready = True

    def _clear_for_next_upload(self, page: Page) -> None:
        """译图页点「清除」，等「下载译文」消失后再传下一张。"""
        try:
            dl = self._download_button(page).first
            if dl.is_visible(timeout=500):
                self._fast_click(
                    page.get_by_role("button", name=re.compile(r"Clear|清除|清空", re.I)).first
                )
                dl.wait_for(state="hidden", timeout=5000)
        except (PlaywrightTimeout, Exception):
            pass
        try:
            self._browse_button(page).first.wait_for(state="visible", timeout=5000)
        except (PlaywrightTimeout, Exception):
            self._images_ready = False
            raise

    def _open_images_tab(self, page: Page) -> None:
        """必须进入「图片」标签；URL 常为 op=translate，不能单靠「浏览文件」判断。"""
        if "op=images" in page.url and self._upload_area_ready(page):
            return

        try:
            self._fast_click(page.locator('button[aria-label="图片翻译"]').first)
        except (PlaywrightTimeout, Exception):
            self._fast_click(page.get_by_role("button", name="图片").first)

        self._browse_button(page).first.wait_for(state="visible", timeout=5000)
        _logger.info("已进入图片标签 url=%s", page.url)

    def _set_languages_if_needed(self, page: Page) -> None:
        """仅当当前不是「检测语言 → 中文简体」时才点开菜单。"""
        if self._languages_configured:
            return

        if not self._target_is_zh_cn(page):
            self._pick_language_option(
                page,
                opener=re.compile(r"更多目标语言|More target languages", re.I),
                option=re.compile(r"中文（简体）|简体中文|Chinese \(Simplified\)", re.I),
                label="目标→中文简体",
            )
        else:
            _logger.info("目标语言已是中文简体，跳过")

        if not self._source_is_auto(page):
            self._pick_language_option(
                page,
                opener=re.compile(r"更多源语言|More source languages", re.I),
                option=re.compile(r"检测语言|Detect language", re.I),
                label="源→检测语言",
            )
        else:
            _logger.info("源语言已是检测语言，跳过")

        self._languages_configured = True

    def _target_is_zh_cn(self, page: Page) -> bool:
        """页面上是否已是中文简体目标语。"""
        try:
            return page.evaluate(
                """() => /中文[（(]简体[）)]|简体中文|Chinese \\(Simplified\\)/i.test(document.body.innerText)"""
            )
        except Exception:
            return False

    def _source_is_auto(self, page: Page) -> bool:
        try:
            return page.evaluate(
                """() => /检测语言|Detect language/i.test(document.body.innerText)"""
            )
        except Exception:
            return False

    def _pick_language_option(
        self,
        page: Page,
        *,
        opener: re.Pattern[str],
        option: re.Pattern[str],
        label: str,
    ) -> None:
        try:
            self._fast_click(page.get_by_role("button", name=opener).first)
            try:
                self._fast_click(page.get_by_role("option", name=option).first)
            except PlaywrightTimeout:
                self._fast_click(page.get_by_text(option).first)
            _logger.info("已设置语言: %s", label)
        except (PlaywrightTimeout, Exception) as e:
            _logger.warning("设置语言跳过 %s: %s", label, e)

    def _ensure_translated_view(self, page: Page) -> None:
        """关闭「显示原文」，避免下载到未译的原图。"""
        for name in (r"显示原文", r"Show original"):
            try:
                sw = page.get_by_role("switch", name=re.compile(name, re.I))
                if sw.count() and sw.first.is_checked():
                    self._fast_click(sw.first)
                    _logger.info("已关闭「显示原文」")
                    return
            except Exception:
                continue

    def _dismiss_cookie_banner(self, page: Page) -> None:
        for label in ("Accept all", "全部接受", "I agree", "同意"):
            try:
                self._fast_click(
                    page.get_by_role("button", name=re.compile(re.escape(label), re.I)).first
                )
                return
            except (PlaywrightTimeout, Exception):
                continue

    def _upload_image(self, page: Page, src: Path) -> None:
        """必须点「浏览文件」上传，hidden input 在文字页也会存在但不会产生译图。"""
        with page.expect_file_chooser(timeout=5000) as fc_info:
            self._fast_click(self._browse_button(page).first)
        fc_info.value.set_files(str(src))

    def _download_button(self, page: Page):
        """Google 中文版按钮文案为「下载译文」。"""
        return page.get_by_role(
            "button",
            name=re.compile(r"下载译文|Download translation|下载翻译", re.I),
        )

    def _wait_translation_ready(self, page: Page, timeout_ms: int = 45_000) -> None:
        """等待「下载译文」按钮（登录后通常 10 秒内）。"""
        try:
            self._download_button(page).first.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeout:
            debug = Path("/tmp/image-translate-debug.png")
            page.screenshot(path=str(debug), full_page=True)
            _logger.error("超时未出现下载译文，截图: %s url=%s", debug, page.url)
            raise TimeoutError(
                f"等待翻译超时（{timeout_ms // 1000}s）。调试截图：{debug}"
            ) from None

    def _save_download(self, page: Page, dest: Path) -> None:
        btn = self._download_button(page).first
        if not btn.is_visible():
            raise RuntimeError("未找到「下载译文」按钮，可能未生成译文。")
        with page.expect_download(timeout=15_000) as dl_info:
            self._fast_click(btn)
        dl_info.value.save_as(dest)
        _logger.info("已点击下载译文并保存")
        self._clear_for_next_upload(page)


_backend: GoogleWebImageBackend | None = None


def get_backend() -> GoogleWebImageBackend:
    global _backend
    if _backend is None:
        _backend = GoogleWebImageBackend()
    return _backend
