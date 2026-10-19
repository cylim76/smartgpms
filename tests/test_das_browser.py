import time
from pathlib import Path
from types import SimpleNamespace

import playwright.sync_api
import pytest

import smartgpms.das_browser as das_browser_module
from smartgpms.config import AppConfig
from smartgpms.das_browser import (
    DasBrowser,
    DasBrowserError,
    DasUnavailable,
    GatePassNotFound,
    LoginRequired,
)


class FakePage:
    def __init__(self, goto_error: Exception | None = None):
        self.goto_error = goto_error
        self.timeout = None
        self.url = "about:blank"

    def is_closed(self):
        return False

    def set_default_timeout(self, timeout):
        self.timeout = timeout

    def goto(self, *_args, **_kwargs):
        if self.goto_error:
            raise self.goto_error


class FakeContext:
    def __init__(self, pages=None, pages_error: Exception | None = None):
        self._pages = pages or []
        self.pages_error = pages_error
        self.closed = False

    @property
    def pages(self):
        if self.pages_error:
            raise self.pages_error
        return self._pages

    def new_page(self):
        page = FakePage()
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def launch_persistent_context(self, *_args, **_kwargs):
        self.calls.append((_args, _kwargs))
        return self.context


class FakePlaywright:
    def __init__(self, context):
        self.chromium = FakeChromium(context)
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    def start(self):
        return self.playwright


def test_ensure_rebuilds_a_stale_browser_context(tmp_path, monkeypatch):
    browser = DasBrowser(AppConfig(tmp_path))
    stale_context = FakeContext(
        pages_error=RuntimeError("Target page, context or browser has been closed")
    )
    stale_playwright = FakePlaywright(stale_context)
    new_page = FakePage()
    new_context = FakeContext([new_page])
    new_playwright = FakePlaywright(new_context)
    browser._context = stale_context
    browser._playwright = stale_playwright

    monkeypatch.setattr(
        playwright.sync_api,
        "sync_playwright",
        lambda: FakeStarter(new_playwright),
    )

    assert browser._ensure() is new_context
    assert browser.page is new_page
    assert stale_context.closed
    assert stale_playwright.stopped
    assert new_page.timeout == 20_000


def test_linux_browser_uses_playwright_chromium_without_edge_channel(
    tmp_path, monkeypatch
):
    browser = DasBrowser(AppConfig(tmp_path))
    page = FakePage()
    context = FakeContext([page])
    fake_playwright = FakePlaywright(context)
    monkeypatch.setattr(
        playwright.sync_api,
        "sync_playwright",
        lambda: FakeStarter(fake_playwright),
    )
    monkeypatch.setattr(
        das_browser_module,
        "os",
        SimpleNamespace(name="posix", environ={}),
    )

    assert browser._ensure() is context
    options = fake_playwright.chromium.calls[0][1]
    assert "channel" not in options
    assert options["headless"] is True
    assert options["locale"] == "zh-CN"
    assert options["timezone_id"] == "Asia/Shanghai"


def test_session_check_discards_browser_after_target_closed(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))
    page = FakePage(RuntimeError("Target page, context or browser has been closed"))
    context = FakeContext([page])
    playwright = FakePlaywright(context)
    browser._page = page
    browser._context = context
    browser._playwright = playwright

    result = browser.check_session()

    assert not result["logged_in"]
    assert result["message"] == "后台浏览器已退出，已完成重置，请重新登录"
    assert browser._context is None
    assert browser._page is None
    assert context.closed
    assert playwright.stopped


def test_logout_clears_persistent_cookies_before_closing_browser(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class LogoutContext(FakeContext):
        cookies_cleared = False

        def clear_cookies(self):
            self.cookies_cleared = True

    context = LogoutContext([FakePage()])
    playwright = FakePlaywright(context)
    browser._context = context
    browser._playwright = playwright
    browser._page = context.pages[0]

    browser.logout()

    assert context.cookies_cleared
    assert context.closed
    assert playwright.stopped
    assert browser._context is None


def test_login_reuses_matching_sso_session_then_reports_das_maintenance(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class MissingLocator:
        @staticmethod
        def count():
            return 0

    class ExistingSessionPage(FakePage):
        def __init__(self):
            super().__init__()
            self.source = "<html>QINGLONG.LIN portal</html>"

        def goto(self, url, **_kwargs):
            self.url = url
            if "das.china.lge.com" in url:
                raise RuntimeError("net::ERR_CONNECTION_REFUSED")
            return SimpleNamespace(status=200)

        @staticmethod
        def locator(_selector):
            return MissingLocator()

        def content(self):
            return self.source

    page = ExistingSessionPage()
    context = FakeContext([page])
    browser._page = page
    browser._context = context
    browser._playwright = FakePlaywright(context)

    with pytest.raises(
        DasUnavailable, match="SSO登录成功，但DAS系统当前无法访问"
    ):
        browser.login("qinglong.lin", "", "")

    assert browser._sso_username == "qinglong.lin"
    assert browser._context is context


def test_unknown_persistent_sso_user_is_cleared_before_form_login(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class LoginLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector
            self.value = ""

        def count(self):
            return int(self.page.form_visible)

        def fill(self, value):
            self.value = value

        @staticmethod
        def evaluate(_script):
            return None

        def click(self):
            if self.selector == "#loginSsobtn":
                self.page.form_visible = False
                self.page.source = "<html>qinglong.lin portal</html>"

    class LoginPage(FakePage):
        def __init__(self):
            super().__init__()
            self.form_visible = False
            self.source = "<html>different.user portal</html>"
            self.fields = {}

        def goto(self, url, **_kwargs):
            self.url = url
            if context.cookies_cleared:
                self.form_visible = True
                self.source = '<input id="USER"><input id="LDAPPASSWORD">'
            return SimpleNamespace(status=200)

        def locator(self, selector):
            return self.fields.setdefault(selector, LoginLocator(self, selector))

        def content(self):
            return self.source

        @staticmethod
        def wait_for_timeout(_timeout):
            return None

        @staticmethod
        def wait_for_load_state(*_args, **_kwargs):
            return None

    class CookieContext(FakeContext):
        cookies_cleared = False

        def clear_cookies(self):
            self.cookies_cleared = True

    page = LoginPage()
    context = CookieContext([page])
    browser._page = page
    browser._context = context
    browser._playwright = FakePlaywright(context)

    submitted = browser._prepare_sso_session(
        page, "qinglong.lin", "password", "123456"
    )

    assert submitted is True
    assert context.cookies_cleared
    assert page.fields["#USER"].value == "qinglong.lin"
    assert page.fields["#LDAPPASSWORD"].value == "password"
    assert page.fields["#OTPPASSWORD"].value == "123456"
    assert browser._sso_username == "qinglong.lin"


def test_visible_sso_form_still_requires_password_and_otp(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    with pytest.raises(LoginRequired, match="请输入密码和 OTP"):
        browser._submit_sso_form(object(), "qinglong.lin", "", "")


def test_das_maintenance_page_is_not_treated_as_success(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class MaintenancePage(FakePage):
        def __init__(self):
            super().__init__()
            self.source = "<html>系统维护</html>"

        def goto(self, url, **_kwargs):
            self.url = url
            return SimpleNamespace(status=503)

        def content(self):
            return self.source

    page = MaintenancePage()

    with pytest.raises(DasUnavailable, match="DAS系统当前无法访问"):
        browser._open_das_after_sso(page)

    context = FakeContext([page])
    browser._page = page
    browser._context = context
    browser._playwright = FakePlaywright(context)
    state = browser.check_session()
    assert state["logged_in"] is False
    assert state["message"] == "SSO登录成功，但DAS系统当前无法访问，请稍后重试。"
    assert browser._context is context


def test_http_200_maintenance_page_is_not_treated_as_das_success(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class MaintenancePage(FakePage):
        def __init__(self):
            super().__init__()
            self.source = "<html><h1>Temporarily offline</h1></html>"

        def goto(self, url, **_kwargs):
            self.url = url
            return SimpleNamespace(status=200)

        def content(self):
            return self.source

    with pytest.raises(DasUnavailable, match="DAS系统当前无法访问"):
        browser._open_das_after_sso(MaintenancePage())


def test_gate_query_without_detail_link_is_not_found(tmp_path, monkeypatch):
    browser = DasBrowser(AppConfig(tmp_path))

    class EmptyGatePage:
        url = "http://das/gate"

        @staticmethod
        def content():
            return "<html><table id='dgMain'></table></html>"

    page = EmptyGatePage()
    monkeypatch.setattr(browser, "refresh_das", lambda: page)
    monkeypatch.setattr(browser, "_query_container", lambda *_args: None)

    with pytest.raises(GatePassNotFound, match="未找到箱号"):
        browser.open_gate_detail("CAAU6040388")


def test_photo_page_lookup_fills_container_and_parses_cpm_rows(tmp_path, monkeypatch):
    browser = DasBrowser(AppConfig(tmp_path))
    response_html = """
    <table id="dgMain"><tr><th>序号</th><th>检查序号</th></tr>
    <tr><td>1</td><td>100256</td><td>TRHU5107393</td>
      <td><a href="V_CPM_DETAIL.aspx?cpm_id=100256">5.铅封确认</a></td>
      <td>2026-09-18 15:59:13</td><td>2026-09-22 19:51:13</td>
      <td>空调</td><td>整箱</td></tr></table>
    """

    class Locator:
        def __init__(self):
            self.value = ""
            self.clicked = False

        @property
        def first(self):
            return self

        @staticmethod
        def count():
            return 1

        def fill(self, value):
            self.value = value

        def click(self):
            self.clicked = True

    class Response:
        url = "http://das/Manage/cpm/R_CPM_SEARCH.aspx"
        request = type("Request", (), {"method": "POST"})()

        @staticmethod
        def text():
            return response_html

    class ResponseInfo:
        value = Response()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class SearchPage(FakePage):
        def __init__(self):
            super().__init__()
            self.url = "http://das/Manage/cpm/R_CPM_SEARCH.aspx"
            self.fields = {
                selector: Locator()
                for selector in ("#txtCntrNo", "#txt_Date1", "#txt_Date2", "#btnSearch")
            }

        @staticmethod
        def content():
            return "<html></html>"

        def locator(self, selector):
            return self.fields[selector]

        @staticmethod
        def expect_response(predicate, **_kwargs):
            assert predicate(Response())
            return ResponseInfo()

    page = SearchPage()
    monkeypatch.setattr(browser, "refresh_das", lambda: page)

    rows = browser.find_cpm_by_container("trhu5107393")

    assert rows[0]["cpm_id"] == "100256"
    assert rows[0]["das_process_status"] == 5
    assert page.fields["#txtCntrNo"].value == "TRHU5107393"
    assert page.fields["#txt_Date1"].value == "20000101"
    assert page.fields["#btnSearch"].clicked


def test_current_gate_detail_reuses_only_matching_recent_page(tmp_path):
    browser = DasBrowser(AppConfig(tmp_path))

    class GatePage(FakePage):
        url = "http://das/gate/detail"

        @staticmethod
        def content():
            return (
                '<span id="lbl_container_no">CAAU6040388</span>'
                '<span id="lbl_seal_no">M8597437</span>'
            )

    page = GatePage()
    browser._page = page
    browser._current_gate_container = "CAAU6040388"
    browser._current_gate_opened_at = time.monotonic()

    result = browser.current_gate_detail("CAAU6040388")

    assert result is not None
    assert result["container_no"] == "CAAU6040388"
    assert result["seal_no"] == "M8597437"
    assert browser.current_gate_detail("FCIU7546349") is None


def test_export_current_gate_pdf_uses_print_media(tmp_path):
    class PdfPage(FakePage):
        def __init__(self):
            super().__init__()
            self.media = []
            self.pdf_options = None

        def emulate_media(self, *, media):
            self.media.append(media)

        def pdf(self, **options):
            self.pdf_options = options
            Path(options["path"]).write_bytes(b"%PDF-1.4\nsmartgpms\n")

    browser = DasBrowser(AppConfig(tmp_path))
    page = PdfPage()
    context = FakeContext([page])
    browser._page = page
    browser._context = context
    browser._playwright = FakePlaywright(context)
    target = tmp_path / "print-spool" / "gate.pdf"

    result = browser.export_current_gate_pdf(target)

    assert result["ok"]
    assert result["size"] == target.stat().st_size
    assert page.media == ["print", "screen"]
    assert page.pdf_options["print_background"] is True
    assert page.pdf_options["prefer_css_page_size"] is True


def test_open_gate_detail_rejects_a_non_gate_detail_page(tmp_path, monkeypatch):
    browser = DasBrowser(AppConfig(tmp_path))

    class WrongPage(FakePage):
        def __init__(self):
            super().__init__()
            self.url = "http://das/eGate/tm/R_EGT_TMGERPVIEW.aspx"

        def goto(self, url, **_kwargs):
            self.url = url

        @staticmethod
        def content():
            return (
                '<span id="lbl_container_no">FFAU2997569</span>'
                '<span id="lbl_seal_no">SEAL123</span>'
            )

    page = WrongPage()
    monkeypatch.setattr(browser, "refresh_das", lambda: page)
    monkeypatch.setattr(browser, "_query_container", lambda *_args: None)
    monkeypatch.setattr(
        "smartgpms.das_browser.parse_gate_search_rows",
        lambda *_args: [
            {
                "container_no": "FFAU2997569",
                "gate_pass_no": "PASS1",
                "planned_departure_at": "2026-10-19 08:00:00",
                "sequence_no": "1",
                "event_target": "",
                "status_url": "http://das/Manage/cpm/R_CPM_SEARCH.aspx",
            }
        ],
    )

    with pytest.raises(DasBrowserError, match="未进入门证详情页"):
        browser.open_gate_detail("FFAU2997569", "PASS1")


def test_download_gate_pdf_returns_verified_detail_metadata(tmp_path, monkeypatch):
    browser = DasBrowser(AppConfig(tmp_path))
    target = tmp_path / "gate.pdf"
    calls = []
    detail = {
        "container_no": "FFAU2997569",
        "gate_pass_no": "PASS1",
        "url": "http://das/eGate/tm/R_EGT_GERPTMRESERVEDETAIL.aspx?Seqno=1",
    }

    def open_detail(container_no, gate_pass_no):
        calls.append(("open", container_no, gate_pass_no))
        return detail

    def export_pdf(path):
        calls.append(("export", path))
        path.write_bytes(b"%PDF-1.4\nverified gate pass\n")
        return {"ok": True, "message": "generated", "size": path.stat().st_size}

    monkeypatch.setattr(browser, "open_gate_detail", open_detail)
    monkeypatch.setattr(browser, "export_current_gate_pdf", export_pdf)

    result = browser.download_gate_pdf("FFAU2997569", "PASS1", target)

    assert [call[0] for call in calls] == ["open", "export"]
    assert result["detail"] == detail
    assert result["verified_container_no"] == "FFAU2997569"
    assert "R_EGT_GERPTMRESERVEDETAIL.aspx" in result["source_url"]


def test_excel_export_waits_for_download_and_removes_temporary_file(tmp_path):
    content = "<table><tr><td>complete export</td></tr></table>"

    class Download:
        @staticmethod
        def failure():
            return None

        @staticmethod
        def save_as(path):
            Path(path).write_text(content * 4, encoding="utf-8")

    class DownloadInfo:
        value = Download()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Button:
        first = None

        def __init__(self):
            self.first = self

        @staticmethod
        def count():
            return 1

        @staticmethod
        def click():
            return None

    class ExportPage:
        @staticmethod
        def locator(selector):
            assert selector == "#btnExcel"
            return Button()

        @staticmethod
        def expect_download(**options):
            assert options["timeout"] == 180_000
            return DownloadInfo()

    browser = DasBrowser(AppConfig(tmp_path))

    source = browser._save_excel_export(ExportPage(), "CPM_LIST")

    assert source == content * 4
    assert list(browser.config.sync_export_dir.glob("*.xls")) == []
