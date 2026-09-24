import time
from pathlib import Path

import playwright.sync_api
import pytest

from smartgpms.config import AppConfig
from smartgpms.das_browser import DasBrowser, GatePassNotFound


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

    def launch_persistent_context(self, *_args, **_kwargs):
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
