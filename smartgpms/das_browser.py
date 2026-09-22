from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

from .config import AppConfig
from .das_parser import find_gate_postback, parse_cpm_detail, parse_gate_detail


class DasBrowserError(RuntimeError):
    pass


class LoginRequired(DasBrowserError):
    pass


class DasBrowser:
    """Owns one isolated Playwright/Edge profile used only by smartGPMS."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.RLock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    def _ensure(self, *, headless: bool = False):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DasBrowserError("尚未安装 Playwright，请先运行 setup.bat") from exc
        self.config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.config.browser_profile_dir),
                channel="msedge",
                headless=headless,
                args=["--start-minimized"],
                accept_downloads=True,
                viewport={"width": 1440, "height": 960},
            )
        except PlaywrightError as edge_error:
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(self.config.browser_profile_dir),
                    headless=headless,
                    args=["--start-minimized"],
                    accept_downloads=True,
                    viewport={"width": 1440, "height": 960},
                )
            except PlaywrightError as fallback_error:
                self._playwright.stop()
                self._playwright = None
                raise DasBrowserError(
                    f"无法启动 smartGPMS 独立浏览器：{edge_error}; {fallback_error}"
                ) from fallback_error
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        self._page.set_default_timeout(20_000)
        return self._context

    @property
    def page(self):
        self._ensure()
        return self._page

    def close(self) -> None:
        with self._lock:
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()
            self._context = self._playwright = self._page = None

    @staticmethod
    def _looks_logged_out(url: str, source: str) -> bool:
        text = (url + " " + source[:12000]).lower()
        return any(
            marker in text
            for marker in (
                "logoutmsgtype=sessionlost",
                "sso.lge.com/eplogin",
                'id="ldappassword"',
                'name="ldappassword"',
                "id='ldappassword'",
                "name='ldappassword'",
            )
        )

    def login(self, username: str, password: str, otp: str) -> dict[str, str | bool]:
        if not username or not password or not otp:
            raise DasBrowserError("用户名、密码和 OTP 均不能为空")
        with self._lock:
            page = self.page
            page.goto(
                self.config.sso_url, wait_until="domcontentloaded", timeout=60_000
            )
            page.locator("#USER").fill(username)
            page.locator("#USER").evaluate(
                "el => el.dispatchEvent(new Event('blur', {bubbles:true}))"
            )
            page.wait_for_timeout(1500)
            page.locator("#LDAPPASSWORD").fill(password)
            otp_field = page.locator("#OTPPASSWORD")
            otp_field.evaluate("el => el.style.display='block'")
            otp_field.fill(otp)
            page.locator("#loginSsobtn").click()
            page.wait_for_timeout(1200)
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            try:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(500)
            page.goto(
                self.config.das_login_url, wait_until="domcontentloaded", timeout=60_000
            )
            page.goto(
                self.config.gate_url, wait_until="domcontentloaded", timeout=60_000
            )
            source = page.content()
            if self._looks_logged_out(page.url, source):
                raise LoginRequired("SSO 登录未成功或 OTP 已失效")
            return {"ok": True, "url": page.url, "title": page.title()}

    def check_session(self) -> dict[str, str | bool]:
        with self._lock:
            page = self.page
            try:
                page.goto(
                    self.config.das_login_url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                page.goto(
                    self.config.gate_url, wait_until="domcontentloaded", timeout=45_000
                )
                source = page.content()
                logged_in = not self._looks_logged_out(page.url, source)
                return {
                    "logged_in": logged_in,
                    "url": page.url,
                    "title": page.title(),
                    "message": "会话有效" if logged_in else "需要重新登录",
                }
            except Exception as exc:  # noqa: BLE001 - session probes must return a status, not crash the worker
                return {
                    "logged_in": False,
                    "url": getattr(page, "url", ""),
                    "title": "",
                    "message": str(exc),
                }

    def refresh_das(self):
        with self._lock:
            page = self.page
            page.goto(
                self.config.das_login_url, wait_until="domcontentloaded", timeout=45_000
            )
            page.goto(
                self.config.gate_url, wait_until="domcontentloaded", timeout=45_000
            )
            source = page.content()
            if self._looks_logged_out(page.url, source):
                raise LoginRequired("DAS 会话已失效")
            return page

    def fetch_cpm_detail(self, cpm_id: str, refresh: bool = True) -> dict:
        with self._lock:
            if refresh:
                self.refresh_das()
            context = self._ensure()
            url = self.config.cpm_detail_url.format(cpm_id=cpm_id)
            response = context.request.get(url, timeout=45_000)
            if response.status == 404:
                return parse_cpm_detail("", url, cpm_id).as_dict()
            if not response.ok:
                raise DasBrowserError(f"CPM 详情读取失败 HTTP {response.status}")
            source = response.text()
            if self._looks_logged_out(response.url, source):
                raise LoginRequired("读取 CPM 详情前 DAS 会话已失效")
            return parse_cpm_detail(source, response.url, cpm_id).as_dict()

    def download(self, url: str, target: Path) -> dict[str, str | int]:
        with self._lock:
            context = self._ensure()
            response = context.request.get(url, timeout=60_000)
            if not response.ok:
                raise DasBrowserError(f"照片下载失败 HTTP {response.status}")
            body = response.body()
            content_type = str(response.headers.get("content-type", "")).lower()
            if "text/html" in content_type or self._looks_logged_out(
                response.url, body[:12000].decode("utf-8", errors="ignore")
            ):
                raise LoginRequired("下载照片时 DAS 会话已失效")
            if not body:
                raise DasBrowserError("照片下载结果为空")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            return {
                "path": str(target),
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }

    def open_gate_detail(self, container_no: str) -> dict[str, str]:
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            page = self.refresh_das()
            self._query_container(page, container_no)
            source = page.content()
            event_target = find_gate_postback(source, container_no)
            if not event_target:
                raise DasBrowserError(
                    f"查询结果中未找到箱号 {container_no} 的查看详细链接"
                )
            popup = None
            try:
                with page.expect_popup(timeout=10_000) as popup_info:
                    page.evaluate(
                        "target => window.__doPostBack(target, '')", event_target
                    )
                popup = popup_info.value
                popup.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1000)
            detail_page = popup or page
            values = parse_gate_detail(detail_page.content())
            if not values["container_no"]:
                raise DasBrowserError("DAS 回发后未取得门证详情页")
            values.update({"event_target": event_target, "url": detail_page.url})
            if popup and page != popup:
                page.close()
            self._page = detail_page
            return values

    @staticmethod
    def _query_container(page, container_no: str) -> None:
        candidates = [
            "#txt_s_container_no",
            "input[id*='container' i]",
            "input[name*='container' i]",
            "input[id*='cntr' i]",
            "input[name*='cntr' i]",
        ]
        field = next(
            (
                loc.first
                for selector in candidates
                if (loc := page.locator(selector)).count()
            ),
            None,
        )
        if field is None:
            raise DasBrowserError(
                "尚未识别门证页面的箱号查询框，需要登录后采集一次 DOM"
            )
        field.fill(container_no)
        buttons = page.locator("input[type=submit], input[type=button], button")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            label = " ".join(
                filter(None, [button.get_attribute("value"), button.inner_text()])
            ).lower()
            if re.search(r"query|search|查询|检索", label):
                button.click()
                page.wait_for_function(
                    "container => (document.querySelector('#dgMain')?.innerText || '').replace(/[^A-Z0-9]/gi,'').includes(container)",
                    arg=re.sub(r"[^A-Z0-9]", "", container_no.upper()),
                    timeout=45_000,
                )
                return
        field.press("Enter")
        page.wait_for_function(
            "container => (document.querySelector('#dgMain')?.innerText || '').replace(/[^A-Z0-9]/gi,'').includes(container)",
            arg=re.sub(r"[^A-Z0-9]", "", container_no.upper()),
            timeout=45_000,
        )

    def print_current_gate(self) -> dict[str, str | bool]:
        with self._lock:
            page = self.page
            for selector in (
                "#btnPrint",
                "input[id*='print' i]",
                "button:has-text('打印')",
                "input[value*='打印']",
            ):
                locator = page.locator(selector)
                if locator.count():
                    locator.first.evaluate("element => element.click()")
                    return {"ok": True, "message": "已调用 DAS 原打印按钮"}
            raise DasBrowserError("未识别到 DAS 打印按钮，需要登录后采集详情页 DOM")
