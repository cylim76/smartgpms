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

    assert 'styles.css?v=0.12.3' in markup
    assert ".input-panel>textarea{height:200px" in styles
    assert ".departure-panel{display:flex;min-height:150px;flex:1 1 auto" in styles
    assert ".departure-list{min-height:70px;max-height:none;flex:1 1 auto" in styles
