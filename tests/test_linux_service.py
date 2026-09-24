from pathlib import Path

from tools.render_systemd_service import render_unit


def test_systemd_unit_uses_resolved_installation_and_data_paths(tmp_path):
    project = tmp_path / "smart gpms"
    data = tmp_path / "service data"
    python = project / ".venv" / "bin" / "python"

    unit = render_unit(
        service_user="lucas",
        service_group="developers",
        working_directory=project,
        data_directory=data,
        python_executable=python,
        host="0.0.0.0",
        port=8765,
    )

    assert "User=lucas" in unit
    assert "Group=developers" in unit
    project_value = str(project.absolute()).replace("\\", "\\\\").replace(" ", "\\x20")
    data_value = str(data.resolve()).replace("\\", "\\\\")
    python_value = str(python.absolute()).replace("\\", "\\\\")
    assert f"WorkingDirectory={project_value}" in unit
    assert f'Environment="SMARTGPMS_DATA_DIR={data_value}"' in unit
    assert 'Environment="SMARTGPMS_RUN_MODE=server"' in unit
    assert f'ExecStart="{python_value}" -m uvicorn' in unit
    assert '--host "0.0.0.0" --port 8765 --workers 1' in unit
    assert not any(
        marker in unit
        for marker in ("@SERVICE_USER@", "@WORKING_DIRECTORY@", "@PORT@")
    )


def test_linux_setup_registers_systemd_without_legacy_run_scripts():
    root = Path(__file__).resolve().parents[1]
    setup = (root / "setup_linux.sh").read_text(encoding="utf-8")

    assert "systemctl enable smartgpms.service" in setup
    assert "/etc/systemd/system/smartgpms.service" in setup
    assert not (root / "run.sh").exists()
    assert not (root / "run_server.sh").exists()
