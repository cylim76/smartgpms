from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import AppConfig
from .das_parser import (
    find_cpm_id,
    parse_cpm_detail,
    parse_cpm_search_rows,
    parse_gate_detail,
    parse_gate_export_rows,
    parse_gate_search_rows,
)
from .time_utils import business_now

LOGGER = logging.getLogger(__name__)


class DasBrowserError(RuntimeError):
    pass


class LoginRequired(DasBrowserError):
    pass


class DasUnavailable(DasBrowserError):
    pass


class GatePassNotFound(DasBrowserError):
    pass


class DasBrowser:
    """Owns one isolated Playwright/Edge profile used only by smartGPMS."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.RLock()
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._current_gate_container = ""
        self._current_gate_opened_at = 0.0
        self._current_gate_record: dict[str, str] = {}
        self._sso_username = ""

    @staticmethod
    def _is_closed_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "target page, context or browser has been closed",
                "browser has been closed",
                "context has been closed",
                "page has been closed",
                "target closed",
            )
        )

    def _discard(self) -> None:
        """Forget a dead Playwright session even when its cleanup methods fail."""
        context, playwright = self._context, self._playwright
        self._context = self._playwright = self._page = None
        self._current_gate_container = ""
        self._current_gate_opened_at = 0.0
        self._current_gate_record = {}
        self._sso_username = ""
        if context is not None:
            try:
                context.close()
            except Exception:
                LOGGER.debug("Unable to close stale browser context", exc_info=True)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                LOGGER.debug("Unable to stop stale Playwright instance", exc_info=True)

    def _ensure(self, *, headless: bool = True):
        with self._lock:
            if self._context is not None:
                try:
                    pages = [page for page in self._context.pages if not page.is_closed()]
                    if self._page not in pages:
                        self._page = pages[0] if pages else self._context.new_page()
                    self._page.set_default_timeout(20_000)
                    return self._context
                except Exception:  # noqa: BLE001 - stale contexts must be rebuilt
                    self._discard()
            try:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                    self.config.base_dir / ".playwright-browsers"
                )
                from playwright.sync_api import Error as PlaywrightError
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise DasBrowserError(
                    "尚未安装 Playwright，请先运行 setup_win.bat 或 ./setup_linux.sh"
                ) from exc
            self.config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._playwright = sync_playwright().start()
                options = {
                    "headless": headless,
                    "accept_downloads": True,
                    "viewport": {"width": 1440, "height": 960},
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                }
                if os.name == "nt":
                    try:
                        self._context = (
                            self._playwright.chromium.launch_persistent_context(
                                str(self.config.browser_profile_dir),
                                channel="msedge",
                                **options,
                            )
                        )
                    except PlaywrightError:
                        self._context = (
                            self._playwright.chromium.launch_persistent_context(
                                str(self.config.browser_profile_dir),
                                **options,
                            )
                        )
                else:
                    self._context = self._playwright.chromium.launch_persistent_context(
                        str(self.config.browser_profile_dir),
                        **options,
                    )
            except PlaywrightError as browser_error:
                self._discard()
                raise DasBrowserError(
                    f"无法启动 smartGPMS 独立浏览器：{browser_error}"
                ) from browser_error
            self._page = next(
                (page for page in self._context.pages if not page.is_closed()), None
            ) or self._context.new_page()
            self._page.set_default_timeout(20_000)
            return self._context

    @property
    def page(self):
        self._ensure()
        return self._page

    def close(self) -> None:
        with self._lock:
            self._discard()

    def logout(self) -> None:
        """Remove persisted SSO cookies before closing the background browser."""
        with self._lock:
            if self._context is not None:
                try:
                    self._context.clear_cookies()
                except Exception:
                    LOGGER.debug("Unable to clear SSO cookies during logout", exc_info=True)
            self._discard()

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

    @staticmethod
    def _response_status(response: Any | None) -> int:
        try:
            return int(getattr(response, "status", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _sso_page_matches_username(source: str, username: str) -> bool:
        lowered = source.lower()
        principal = username.strip().lower().split("\\")[-1].split("@")[0]
        if not principal:
            return False
        if principal in lowered:
            return True
        compact_source = re.sub(r"[^a-z0-9]", "", lowered[:200_000])
        compact_principal = re.sub(r"[^a-z0-9]", "", principal)
        return len(compact_principal) >= 5 and compact_principal in compact_source

    @staticmethod
    def _looks_das_unavailable(url: str, source: str) -> bool:
        text = f"{url} {source[:20000]}".lower()
        return any(
            marker in text
            for marker in (
                "chrome-error://",
                "err_connection_refused",
                "err_connection_timed_out",
                "service unavailable",
                "http error 500",
                "http error 502",
                "http error 503",
                "system maintenance",
                "系统维护",
                "服务不可用",
                "점검 중",
            )
        )

    @staticmethod
    def _looks_like_gate_page(url: str, source: str) -> bool:
        text = source[:100_000].lower()
        return "das.china.lge.com" in url.lower() and any(
            marker in text
            for marker in (
                'id="dgmain"',
                "id='dgmain'",
                "r_egt_tmgerpview.aspx",
                "lbl_container_no",
            )
        )

    def _clear_sso_cookies(self) -> None:
        if self._context is not None:
            self._context.clear_cookies()
        self._sso_username = ""

    @staticmethod
    def _has_sso_login_form(page) -> bool:
        return all(
            page.locator(selector).count()
            for selector in ("#USER", "#LDAPPASSWORD", "#OTPPASSWORD", "#loginSsobtn")
        )

    def _submit_sso_form(
        self, page, username: str, password: str, otp: str
    ) -> None:
        if not password or not otp:
            raise LoginRequired("当前需要重新登录 SSO，请输入密码和 OTP")
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
        source = page.content()
        if self._looks_logged_out(page.url, source) or self._has_sso_login_form(page):
            self._sso_username = ""
            raise LoginRequired("SSO 登录未成功或 OTP 已失效")
        self._sso_username = username.strip()

    def _prepare_sso_session(
        self, page, username: str, password: str, otp: str
    ) -> bool:
        """Return True when a credential form was submitted, False when reused."""
        response = page.goto(
            self.config.sso_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        status = self._response_status(response)
        if status >= 400:
            raise DasBrowserError(f"SSO 登录页面当前无法访问（HTTP {status}）")
        if self._has_sso_login_form(page):
            self._submit_sso_form(page, username, password, otp)
            return True

        source = page.content()
        known_same_user = bool(
            self._sso_username
            and self._sso_username.casefold() == username.strip().casefold()
        )
        page_matches_user = self._sso_page_matches_username(source, username)
        if known_same_user or page_matches_user:
            self._sso_username = username.strip()
            return False

        # A persistent browser profile can contain a session belonging to a
        # different or unknown user. Clear it before applying supplied credentials.
        self._clear_sso_cookies()
        response = page.goto(
            self.config.sso_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        status = self._response_status(response)
        if status >= 400:
            raise DasBrowserError(f"SSO 登录页面当前无法访问（HTTP {status}）")
        if not self._has_sso_login_form(page):
            raise DasBrowserError("SSO 登录页面未正常显示，请稍后重试")
        self._submit_sso_form(page, username, password, otp)
        return True

    def _open_das_after_sso(self, page) -> None:
        message = "SSO登录成功，但DAS系统当前无法访问，请稍后重试。"
        try:
            login_response = page.goto(
                self.config.das_login_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            login_source = page.content()
            if self._looks_logged_out(page.url, login_source):
                raise LoginRequired("SSO 登录未成功或 OTP 已失效")
            login_status = self._response_status(login_response)
            if login_status >= 400 or self._looks_das_unavailable(
                page.url, login_source
            ):
                raise DasUnavailable(message)

            gate_response = page.goto(
                self.config.gate_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            gate_source = page.content()
            if self._looks_logged_out(page.url, gate_source):
                raise LoginRequired("SSO 登录未成功或 OTP 已失效")
            gate_status = self._response_status(gate_response)
            if gate_status >= 400 or self._looks_das_unavailable(
                page.url, gate_source
            ) or not self._looks_like_gate_page(page.url, gate_source):
                raise DasUnavailable(message)
        except (DasUnavailable, LoginRequired):
            raise
        except Exception as exc:
            if self._is_closed_error(exc):
                raise
            raise DasUnavailable(message) from exc

    def login(self, username: str, password: str, otp: str) -> dict[str, str | bool]:
        if not username:
            raise DasBrowserError("用户名不能为空")
        with self._lock:
            try:
                page = self.page
                self._prepare_sso_session(page, username, password, otp)
                self._open_das_after_sso(page)
                return {"ok": True, "url": page.url, "title": page.title()}
            except (DasBrowserError, LoginRequired):
                raise
            except Exception as exc:
                if self._is_closed_error(exc):
                    self._discard()
                    raise DasBrowserError(
                        "smartGPMS 后台浏览器已退出并完成重置，请重新点击登录"
                    ) from exc
                raise

    def check_session(self) -> dict[str, str | bool]:
        with self._lock:
            page = None
            try:
                page = self.page
                self._open_das_after_sso(page)
                return {
                    "logged_in": True,
                    "url": page.url,
                    "title": page.title(),
                    "message": "会话有效",
                }
            except LoginRequired:
                return {
                    "logged_in": False,
                    "url": getattr(page, "url", "") if page is not None else "",
                    "title": "",
                    "message": "需要重新登录",
                }
            except DasUnavailable as exc:
                return {
                    "logged_in": False,
                    "url": getattr(page, "url", "") if page is not None else "",
                    "title": "",
                    "message": str(exc),
                }
            except Exception as exc:  # noqa: BLE001 - session probes must return a status, not crash the worker
                closed = self._is_closed_error(exc)
                if closed:
                    self._discard()
                return {
                    "logged_in": False,
                    "url": getattr(page, "url", "") if page is not None else "",
                    "title": "",
                    "message": (
                        "后台浏览器已退出，已完成重置，请重新登录"
                        if closed
                        else str(exc)
                    ),
                }

    def refresh_das(self):
        with self._lock:
            page = self.page
            self._open_das_after_sso(page)
            return page

    def _save_excel_export(self, page, prefix: str) -> str:
        """Download a WebForms Excel export into app-owned storage and read it."""
        button = page.locator("#btnExcel")
        if not button.count():
            raise DasBrowserError("DAS 页面中未找到 Excel 导出按钮")
        self.config.sync_export_dir.mkdir(parents=True, exist_ok=True)
        stamp = business_now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.config.sync_export_dir / f"{prefix}_{stamp}.xls"
        try:
            with page.expect_download(timeout=180_000) as download_info:
                button.first.click()
            download = download_info.value
            failure = download.failure()
            if failure:
                raise DasBrowserError(f"DAS Excel 明细下载失败：{failure}")
            download.save_as(str(target))
            if not target.is_file() or target.stat().st_size < 100:
                raise DasBrowserError("DAS Excel 明细下载结果为空")
            return target.read_text(encoding="utf-8", errors="replace")
        finally:
            target.unlink(missing_ok=True)

    def cpm_export_snapshot(self, start_date: str, end_date: str) -> list[dict]:
        """Export and parse every CPM row in the requested date range."""
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            page = self.refresh_das()
            page.goto(
                self.config.cpm_index_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            if self._looks_logged_out(page.url, page.content()):
                raise LoginRequired("打开 DAS 照片列表页时会话已失效")
            for selector, value in (
                ("#txt_Date1", start_date),
                ("#txt_Date2", end_date),
            ):
                field = page.locator(selector)
                if field.count():
                    field.first.fill(value)
            container_field = page.locator("#txtCntrNo")
            if container_field.count():
                container_field.first.fill("")
            search = page.locator("#btnSearch")
            if not search.count():
                raise DasBrowserError("DAS 照片列表页中未找到查询按钮")
            try:
                with page.expect_response(
                    lambda response: (
                        "R_CPM_SEARCH.aspx" in response.url
                        and response.request.method == "POST"
                    ),
                    timeout=90_000,
                ):
                    search.first.click()
            except PlaywrightTimeoutError:
                LOGGER.warning("Timed out waiting for CPM export query response")
                page.wait_for_timeout(1000)
            rows = parse_cpm_search_rows(
                self._save_excel_export(page, "CPM_LIST"), limit=None
            )
            if not rows:
                raise DasBrowserError("DAS 监装 Excel 已下载，但没有解析到记录")
            self._page = page
            return rows

    def find_cpm_by_container(
        self, container_no: str, limit: int = 50
    ) -> list[dict]:
        """Query the DAS photo-download page by container number."""
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            wanted = re.sub(r"[^A-Z0-9]", "", container_no.upper())
            if not re.fullmatch(r"[A-Z]{4}\d{7}", wanted):
                raise DasBrowserError(f"箱号格式不正确：{container_no}")
            page = self.refresh_das()
            page.goto(
                self.config.cpm_index_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            source = page.content()
            if self._looks_logged_out(page.url, source):
                raise LoginRequired("打开 DAS 照片列表页时会话已失效")
            container_field = page.locator("#txtCntrNo")
            button = page.locator("#btnSearch")
            if not container_field.count() or not button.count():
                raise DasBrowserError(
                    "DAS 照片列表页中未找到箱号输入框 txtCntrNo 或查询按钮 btnSearch"
                )
            container_field.first.fill(wanted)
            # User-triggered lookup is intentionally not limited to the 30-day
            # maintenance window. Exact container filtering keeps the result small.
            start_date = page.locator("#txt_Date1")
            end_date = page.locator("#txt_Date2")
            if start_date.count():
                start_date.first.fill("20000101")
            if end_date.count():
                end_date.first.fill(business_now().strftime("%Y%m%d"))
            response_source = ""
            try:
                with page.expect_response(
                    lambda response: (
                        "R_CPM_SEARCH.aspx" in response.url
                        and response.request.method == "POST"
                    ),
                    timeout=90_000,
                ) as response_info:
                    button.first.click()
                response_source = response_info.value.text()
            except PlaywrightTimeoutError:
                LOGGER.warning(
                    "Timed out waiting for container CPM query response; using DOM"
                )
            rows = parse_cpm_search_rows(response_source, limit)
            if not rows:
                page.wait_for_timeout(1000)
                rows = parse_cpm_search_rows(page.content(), limit)
            self._page = page
            return [
                row
                for row in rows
                if re.sub(
                    r"[^A-Z0-9]", "", str(row.get("container_no", "")).upper()
                )
                == wanted
            ]

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
                raise DasBrowserError(f"监装照片详情读取失败 HTTP {response.status}")
            source = response.text()
            if self._looks_logged_out(response.url, source):
                raise LoginRequired("读取监装照片详情前 DAS 会话已失效")
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

    def gate_pass_snapshot(self, application_date: str | None = None) -> list[dict[str, str]]:
        """Return every gate-pass row on all pages for one business date."""
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            date_value = application_date or business_now().strftime("%Y%m%d")
            page = self.refresh_das()
            for selector in ("#txt_BeginDate", "#txt_EndDate"):
                field = page.locator(selector)
                if field.count():
                    field.first.fill(date_value)
            container_field = page.locator("#txt_s_container_no")
            if container_field.count():
                container_field.first.fill("")
            button = page.locator("#btnSearch")
            if not button.count():
                raise DasBrowserError("DAS 门证页面未找到查询按钮")
            grid = page.locator("#dgMain")
            before = grid.inner_html() if grid.count() else ""
            button.first.click()
            try:
                page.wait_for_function(
                    "before => (document.querySelector('#dgMain')?.innerHTML || '') !== before",
                    arg=before,
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1000)

            records: list[dict[str, str]] = []
            visited_pages: set[int] = set()
            while True:
                records.extend(parse_gate_search_rows(page.content(), page.url))
                next_button = page.locator("#btnnext")
                if not next_button.count():
                    break
                paging_text = next_button.first.evaluate(
                    "el => el.parentElement?.innerText || ''"
                )
                paging = re.search(r"(\d+)\s*/\s*(\d+)", paging_text)
                current = int(paging.group(1)) if paging else 1
                total = int(paging.group(2)) if paging else 1
                if current >= total or current in visited_pages:
                    break
                visited_pages.add(current)
                before = grid.inner_html() if grid.count() else ""
                next_button.first.click()
                try:
                    page.wait_for_function(
                        "before => (document.querySelector('#dgMain')?.innerHTML || '') !== before",
                        arg=before,
                        timeout=30_000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise DasBrowserError("DAS 门证列表翻页超时") from exc
            self._page = page
            return records

    def gate_pass_export_snapshot(
        self, start_date: str, end_date: str
    ) -> list[dict[str, str]]:
        """Export and parse every gate-pass row in the requested date range."""
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            page = self.refresh_das()
            for selector, value in (
                ("#txt_BeginDate", start_date),
                ("#txt_EndDate", end_date),
            ):
                field = page.locator(selector)
                if field.count():
                    field.first.fill(value)
            container_field = page.locator("#txt_s_container_no")
            if container_field.count():
                container_field.first.fill("")
            button = page.locator("#btnSearch")
            if not button.count():
                raise DasBrowserError("DAS 门证页面未找到查询按钮")
            grid = page.locator("#dgMain")
            before = grid.inner_html() if grid.count() else ""
            button.first.click()
            try:
                page.wait_for_function(
                    "before => (document.querySelector('#dgMain')?.innerHTML || '') !== before",
                    arg=before,
                    timeout=90_000,
                )
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1000)
            rows = parse_gate_export_rows(
                self._save_excel_export(page, "GERP_GATEPASS_LIST")
            )
            if not rows:
                raise DasBrowserError("DAS 门证 Excel 已下载，但没有解析到记录")
            self._page = page
            return rows

    def open_gate_detail(
        self, container_no: str, gate_pass_no: str = ""
    ) -> dict[str, str]:
        with self._lock:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            page = self.refresh_das()
            current = business_now()
            start_date = (current - timedelta(days=60)).strftime("%Y%m%d")
            end_date = current.strftime("%Y%m%d")
            self._query_container(page, container_no, start_date, end_date)
            source = page.content()
            wanted = re.sub(r"[^A-Z0-9]", "", container_no.upper())
            candidates = [
                row
                for row in parse_gate_search_rows(source, page.url)
                if row.get("container_no") == wanted
                and (not gate_pass_no or row.get("gate_pass_no") == gate_pass_no)
                and (row.get("status_url") or row.get("event_target"))
            ]
            if not candidates:
                raise GatePassNotFound(f"未找到箱号 {container_no} 的门证")
            selected = max(
                candidates,
                key=lambda row: (
                    row.get("planned_departure_at", ""),
                    int(row.get("sequence_no", "0"))
                    if row.get("sequence_no", "").isdigit()
                    else 0,
                    row.get("gate_pass_no", ""),
                ),
            )
            event_target = selected["event_target"]
            popup = None
            status_url = str(selected.get("status_url", "")).strip()
            if status_url:
                page.goto(
                    status_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            else:
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
            detail_source = detail_page.content()
            values = parse_gate_detail(detail_source)
            actual = re.sub(
                r"[^A-Z0-9]", "", str(values.get("container_no", "")).upper()
            )
            if actual != wanted:
                raise DasBrowserError(
                    f"DAS 门证详情页箱号校验失败：期望 {wanted}，实际 {actual or '无'}"
                )
            detail_path = urlparse(detail_page.url).path.lower()
            if not detail_path.endswith("/r_egt_gerptmreservedetail.aspx"):
                raise DasBrowserError(
                    f"DAS 未进入门证详情页，已停止生成 PDF：{detail_page.url}"
                )
            values = {
                **selected,
                **values,
                "cpm_id": find_cpm_id(detail_source, detail_page.url)
                or find_cpm_id(source, page.url),
                "event_target": event_target,
                "url": detail_page.url,
            }
            if popup and page != popup:
                page.close()
            self._page = detail_page
            self._current_gate_container = re.sub(
                r"[^A-Z0-9]", "", str(values.get("container_no", "")).upper()
            )
            self._current_gate_opened_at = time.monotonic()
            self._current_gate_record = {
                key: str(value) for key, value in values.items()
            }
            return values

    def current_gate_detail(
        self, container_no: str, max_age_seconds: float = 180.0
    ) -> dict[str, str] | None:
        """Reuse a just-verified gate page only while it is still visibly valid."""
        wanted = re.sub(r"[^A-Z0-9]", "", container_no.upper())
        with self._lock:
            if (
                not wanted
                or self._current_gate_container != wanted
                or time.monotonic() - self._current_gate_opened_at > max_age_seconds
                or self._page is None
                or self._page.is_closed()
            ):
                return None
            try:
                source = self._page.content()
                values = parse_gate_detail(source)
                actual = re.sub(
                    r"[^A-Z0-9]", "", str(values.get("container_no", "")).upper()
                )
                if actual != wanted:
                    return None
                values = {
                    **self._current_gate_record,
                    **values,
                    "cpm_id": find_cpm_id(source, self._page.url),
                    "event_target": "",
                    "url": self._page.url,
                    "reused": "true",
                }
                return values
            except Exception:
                LOGGER.debug("Unable to reuse current gate detail page", exc_info=True)
                return None

    @staticmethod
    def _query_container(
        page,
        container_no: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
        current = business_now()
        dates = {
            "#txt_BeginDate": start_date
            or (current - timedelta(days=60)).strftime("%Y%m%d"),
            "#txt_EndDate": end_date or current.strftime("%Y%m%d"),
        }
        for selector, date_value in dates.items():
            date_field = page.locator(selector)
            if date_field.count():
                date_field.first.fill(date_value)
        wanted = re.sub(r"[^A-Z0-9]", "", container_no.upper())
        grid = page.locator("#dgMain")
        before = grid.inner_html() if grid.count() else ""

        def wait_for_result() -> None:
            try:
                page.wait_for_function(
                    "state => {const grid=document.querySelector('#dgMain');const text=(grid?.innerText||'').replace(/[^A-Z0-9]/gi,'');return text.includes(state.container)||(grid?.innerHTML||'')!==state.before}",
                    arg={"container": wanted, "before": before},
                    timeout=10_000,
                )
            except PlaywrightTimeoutError:
                # An unchanged empty result is still a valid completed query;
                # open_gate_detail will classify it as GatePassNotFound.
                pass

        buttons = page.locator("input[type=submit], input[type=button], button")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            label = " ".join(
                filter(None, [button.get_attribute("value"), button.inner_text()])
            ).lower()
            if re.search(r"query|search|查询|检索", label):
                button.click()
                wait_for_result()
                return
        field.press("Enter")
        wait_for_result()

    def export_current_gate_pdf(self, target: Path) -> dict[str, str | bool | int]:
        with self._lock:
            page = self.page
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.emulate_media(media="print")
                page.pdf(
                    path=str(target),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                    margin={
                        "top": "0mm",
                        "right": "0mm",
                        "bottom": "0mm",
                        "left": "0mm",
                    },
                )
            except Exception as exc:
                if target.is_file():
                    target.unlink(missing_ok=True)
                raise DasBrowserError(f"门证 PDF 生成失败：{exc}") from exc
            finally:
                try:
                    page.emulate_media(media="screen")
                except Exception:
                    LOGGER.debug("Unable to restore screen media", exc_info=True)
            if not target.is_file() or target.stat().st_size == 0:
                target.unlink(missing_ok=True)
                raise DasBrowserError("门证 PDF 生成失败：文件为空")
            return {
                "ok": True,
                "message": "门证 PDF 已生成，请在打印窗口中完成打印",
                "size": target.stat().st_size,
            }

    def download_gate_pdf(
        self, container_no: str, gate_pass_no: str, target: Path
    ) -> dict[str, Any]:
        """Open, verify and export one gate pass without releasing the browser."""
        with self._lock:
            detail = self.open_gate_detail(container_no, gate_pass_no)
            result = self.export_current_gate_pdf(target)
            return {
                **result,
                "detail": detail,
                "verified_container_no": str(detail.get("container_no", "")),
                "source_url": str(detail.get("url", "")),
            }
