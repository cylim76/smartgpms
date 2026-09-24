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
