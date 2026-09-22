from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(
        viewport={"width": 1600, "height": 900}, device_scale_factor=1
    )
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    page.locator("#login-card").wait_for(state="visible")
    assert page.locator("#containers").is_visible()
    assert page.locator("#username").is_visible()
    page.screenshot(path=str(ROOT / "data" / "ui-smoke.png"), full_page=True)
    page.evaluate("""() => {
      setSession('logged_in');
      render(Array.from({length: 6}, (_, i) => ({
        input_container: 'CMAU4338290', gate_container_no: 'CMAU4338290', gate_seal_no: 'FX49497582',
        business_stage: 4, print_status: i > 3 ? 1 : 0, last_printed_at: i > 3 ? '2026-09-22 21:08:30' : '',
        verdict: ['match','match','mismatch','review','match','review'][i],
        message: ['箱号和铅封号均一致','','门证与照片识别内容不一致','照片尚未识别','','找不到照片'][i]
      })));
    }""")
    page.screenshot(path=str(ROOT / "data" / "ui-results-smoke.png"), full_page=True)
    browser.close()
