import os
import time

import pytest
from fastapi import HTTPException

import app as app_module
from smartgpms.database import Database
from smartgpms.time_utils import business_now


def test_local_verification_returns_without_opening_das(tmp_path, monkeypatch):
    database = Database(tmp_path / "smartgpms.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100715",
            "container_no": "CAAU5328959",
            "business_stage": 4,
            "das_process_status": 5,
        }
    )
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.service, "note_interactive", lambda: None)

    def unexpected_gate_query(*_args, **_kwargs):
        raise AssertionError("local verification must not open DAS")

    monkeypatch.setattr(
        app_module.service.browser, "open_gate_detail", unexpected_gate_query
    )

    result = app_module.verify_local(
        app_module.VerifyPayload(containers=["CAAU5328959"])
    )

    assert result["rows"][0]["cpm_id"] == "100715"
    assert result["rows"][0]["gate_status"] == "pending"
    assert result["rows"][0]["gate_container_no"] == ""


def test_full_verification_refreshes_mapping_even_when_local_record_exists(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "smartgpms.sqlite3")
    for cpm_id in ("100", "200"):
        database.upsert_cpm(
            {
                "cpm_id": cpm_id,
                "container_no": "CAAU5328959",
                "business_stage": 4,
                "das_process_status": 5,
            }
        )
    new_record = database.cpm_by_id("200")
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.service, "note_interactive", lambda: None)
    monkeypatch.setattr(app_module.service, "log_activity", lambda *_a, **_k: None)
    calls = []

    def resolve(container_no):
        calls.append(container_no)
        return {"record": new_record, "gate": None, "gate_status": "unknown"}

    monkeypatch.setattr(app_module.service, "resolve_container", resolve)
    monkeypatch.setattr(
        app_module.service,
        "prepare_container",
        lambda _number: {**new_record, "container_ocr": None, "seal_ocr": None},
    )
    monkeypatch.setattr(
        app_module.service.browser,
        "open_gate_detail",
        lambda _number: {
            "container_no": "CAAU5328959",
            "seal_no": "CN123456",
            "gate_key": "gate-1",
        },
    )
    monkeypatch.setattr(app_module.service, "save_gate_detail", lambda gate: gate)

    result = app_module._verify(
        app_module.VerifyPayload(containers=["CAAU5328959"])
    )

    assert calls == ["CAAU5328959"]
    assert result["rows"][0]["cpm_id"] == "200"


def test_gate_date_validation_uses_planned_departure_calendar_date():
    today = business_now().strftime("%Y-%m-%d")

    assert app_module._gate_date_valid(
        {"planned_departure_at": f"{today} 08:30:00"}
    )
    assert not app_module._gate_date_valid(
        {"planned_departure_at": "2020-01-01 08:30:00"}
    )
    assert not app_module._gate_date_valid({"planned_departure_at": ""})


def test_comparison_fields_marks_only_the_mismatched_value():
    result = app_module._comparison_fields(
        {"container_no": "CAAU5328959", "seal_no": "CN123456"},
        {"observed_text": "CAAU5328959"},
        {"observed_text": "CN654321"},
    )

    assert result == {"container_matches": True, "seal_matches": False}


def test_pending_departure_ui_contract():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="departure-search"' in markup
    assert 'id="clear-departure-search"' in markup
    assert 'id="departure-list"' in markup
    assert 'addEventListener("dblclick"' in script
    assert 'class="departure-product"' in script
    assert 'setInterval(refreshPendingDepartures,60000)' in script
    assert '"/api/gate/pending-departures/ack"' in script


def test_application_icon_is_linked_and_contains_windows_sizes():
    from PIL import Image

    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    png_path = root / "static" / "assets" / "smartgpms-app.png"
    ico_path = root / "static" / "assets" / "smartgpms-app.ico"

    assert 'href="/static/assets/smartgpms-app.ico?v=1"' in markup
    assert 'href="/static/assets/smartgpms-app.png?v=1"' in markup
    with Image.open(png_path) as png:
        assert png.size == (512, 512)
    with Image.open(ico_path) as icon:
        assert icon.format == "ICO"
        assert (16, 16) in icon.info["sizes"]
        assert (256, 256) in icon.info["sizes"]


def test_photo_column_labels_use_business_wording():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")

    assert "<th>箱号照片</th>" in markup
    assert "<th>铅封照片</th>" in markup
    assert "<th>其他封箱照片</th>" in markup
    assert 'noData("无其他封箱照片")' in script


def test_left_panel_allocates_remaining_height_to_departure_list():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    styles = (root / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'styles.css?v=0.16.1' in markup
    assert ".input-panel>textarea{height:200px" in styles
    assert ".departure-panel{display:flex;min-height:150px;flex:1 1 auto" in styles
    assert ".departure-list{min-height:70px;max-height:none;flex:1 1 auto" in styles


def test_empty_install_has_blocking_initial_import_dialog_contract():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="initial-import-dialog"' in markup
    assert 'id="initial-import-count"' not in markup
    assert "最近 30 天" in markup
    assert "排除已经出厂" in markup
    assert 'id="initial-import-confirm"' in markup
    assert 'api("/api/initial-import"' in script
    assert 'JSON.stringify({confirm:true})' in script
    assert 'app.js?v=0.16.1' in markup
    assert "local-first.js" not in markup
    assert 'api("/api/verify/local"' in script
    assert 'api("/api/verify/gate"' in script
    assert "localRow.gate_needs_refresh" not in script


def test_result_row_has_confirmed_single_container_photo_refresh_action():
    root = app_module.BASE_DIR
    script = (root / "static" / "app.js").read_text(encoding="utf-8")
    styles = (root / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'class="photo-refresh-button"' in script
    assert 'title="重新获取监装照片"' in script
    assert '<path d="M21 3v5h-5"/>' in script
    assert 'message:"重新下载封箱照片。是否继续？"' in script
    assert 'api("/api/photos/refresh"' in script
    assert ".row-actions{display:flex" in styles
    assert ".photo-refresh-button.checking svg{animation:refresh-spin" in styles


def test_photo_refresh_endpoint_returns_rebuilt_row(tmp_path, monkeypatch):
    database = Database(tmp_path / "smartgpms.sqlite3")
    database.update_session("logged_in", "operator", "会话有效")
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(
        app_module.service,
        "refresh_container_photos",
        lambda container: {
            "record": {"container_no": container},
            "cpm_id": "101",
            "downloaded_photo_count": 4,
            "changed_cpm": True,
        },
    )
    monkeypatch.setattr(
        app_module,
        "verify_local",
        lambda _payload: {"rows": [{"input_container": "MSCU6639870"}]},
    )

    result = app_module.refresh_photos(
        app_module.PhotoRefreshPayload(container="MSCU6639870")
    )

    assert result["row"]["input_container"] == "MSCU6639870"
    assert result["message"] == "已采用最新监装记录并更新 4 张封箱照片"


def test_initial_import_endpoint_requires_login_and_confirmation(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "smartgpms.sqlite3")
    monkeypatch.setattr(app_module, "database", database)
    monkeypatch.setattr(app_module.service, "database", database)
    monkeypatch.setattr(app_module.service, "_startup_sync_pending", False)
    monkeypatch.setattr(app_module.service, "_background_resume_at", 0.0)

    with pytest.raises(HTTPException) as logged_out:
        app_module.configure_initial_import(app_module.InitialImportPayload())
    assert logged_out.value.status_code == 401

    database.update_session("logged_in", "operator", "会话有效")
    with pytest.raises(HTTPException) as invalid:
        app_module.configure_initial_import(
            app_module.InitialImportPayload(confirm=False)
        )
    assert invalid.value.status_code == 400

    result = app_module.configure_initial_import(app_module.InitialImportPayload())
    assert result["ok"] is True
    assert result["days"] == 30
    assert result["required"] is False


def test_print_spool_copy_does_not_inherit_expired_cache_timestamp(tmp_path):
    cached_pdf = tmp_path / "gatepass" / "cached.pdf"
    staged_pdf = tmp_path / "print-spool" / "token.pdf"
    cached_pdf.parent.mkdir()
    cached_pdf.write_bytes(b"%PDF-1.4\n")
    expired_time = time.time() - app_module.PENDING_PRINT_TTL_SECONDS - 60
    os.utime(cached_pdf, (expired_time, expired_time))

    app_module._stage_pending_pdf(cached_pdf, staged_pdf)

    assert staged_pdf.read_bytes() == cached_pdf.read_bytes()
    assert staged_pdf.stat().st_mtime > expired_time
    assert staged_pdf.stat().st_mtime >= time.time() - 5


def test_topbar_keeps_only_page_reload_and_polls_session_status():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="check-session"' not in markup
    assert 'id="reload-page"' in markup
    assert 'title="刷新页面"' in markup
    assert '$("#reload-page").addEventListener("click",()=>window.location.reload())' in script
    assert 'api("/api/session/status")' in script
    assert "setInterval(refreshSessionStatus,60000)" in script


def test_session_status_endpoint_reads_cached_database_state(tmp_path, monkeypatch):
    database = Database(tmp_path / "smartgpms.sqlite3")
    database.update_session("logged_in", "operator.one", "会话有效")
    monkeypatch.setattr(app_module, "database", database)

    result = app_module.session_status()

    assert result["status"] == "logged_in"
    assert result["username"] == "operator.one"


def test_result_legend_is_stacked_below_heading_compactly():
    root = app_module.BASE_DIR
    markup = (root / "static" / "index.html").read_text(encoding="utf-8")
    styles = (root / "static" / "styles.css").read_text(encoding="utf-8")

    assert markup.index("<h2>门证/监装照片核对</h2>") < markup.index(
        '<div class="legend">'
    )
    assert ".result-heading{align-items:flex-start" in styles
    assert "flex-direction:column" in styles
    assert ".result-heading .legend{gap:14px;font-size:11.5px" in styles


def test_server_mode_cannot_be_stopped_by_a_client_page(monkeypatch):
    monkeypatch.setattr(app_module, "RUN_MODE", "server")

    with pytest.raises(HTTPException) as error:
        app_module.desktop_shutdown()

    assert error.value.status_code == 404
