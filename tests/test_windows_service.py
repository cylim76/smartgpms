from pathlib import Path
from xml.etree import ElementTree


def test_windows_entry_points_are_limited_to_desktop_and_service_management():
    root = Path(__file__).resolve().parents[1]

    assert (root / "run_win.bat").is_file()
    assert (root / "install_service.bat").is_file()
    assert (root / "uninstall_service.bat").is_file()
    assert not (root / "run_server.bat").exists()


def test_winsw_configuration_runs_one_hidden_server_worker():
    root = Path(__file__).resolve().parents[1]
    config = ElementTree.parse(root / "deploy" / "windows" / "smartGPMS.xml")
    service = config.getroot()
    env = {
        item.attrib["name"]: item.attrib["value"]
        for item in service.findall("env")
    }

    assert service.findtext("id") == "smartGPMS"
    assert service.findtext("executable") == (
        r"%BASE%\..\.venv\Scripts\python.exe"
    )
    assert service.findtext("arguments") == r'"%BASE%\..\tools\run_service.py"'
    assert service.findtext("workingdirectory") == r"%BASE%\.."
    assert service.findtext("hidewindow") == "true"
    assert service.findtext("startmode") == "Automatic"
    assert service.findtext("delayedAutoStart") == "true"
    assert env == {
        "SMARTGPMS_RUN_MODE": "server",
        "SMARTGPMS_HOST": "0.0.0.0",
        "SMARTGPMS_PORT": "8765",
        "SMARTGPMS_DATA_DIR": r"%BASE%\..\data",
    }


def test_windows_service_scripts_pin_and_verify_winsw_and_manage_firewall():
    root = Path(__file__).resolve().parents[1]
    install = (root / "install_service.bat").read_text(encoding="utf-8")
    uninstall = (root / "uninstall_service.bat").read_text(encoding="utf-8")

    assert "releases/download/v2.12.0/WinSW-x64.exe" in install
    assert "Get-FileHash -Algorithm SHA256" in install
    assert "smartGPMS TCP 8765" in install
    assert '"%SERVICE_EXE%" install' in install
    assert '"%SERVICE_EXE%" start' in install
    assert '"%SERVICE_EXE%" uninstall' in uninstall
    assert "firewall delete rule" in uninstall


def test_windows_service_runner_forces_single_server_mode(monkeypatch):
    from tools import run_service

    captured = {}
    monkeypatch.setattr(
        run_service.uvicorn,
        "run",
        lambda target, **kwargs: captured.update(target=target, **kwargs),
    )
    monkeypatch.setenv("SMARTGPMS_HOST", "10.0.0.8")
    monkeypatch.setenv("SMARTGPMS_PORT", "9876")

    run_service.main()

    assert captured == {
        "target": "app:app",
        "host": "10.0.0.8",
        "port": 9876,
        "workers": 1,
        "access_log": False,
        "app_dir": str(Path(run_service.__file__).resolve().parents[1]),
    }
