import subprocess

from smartgpms.updater import GitRevisionMonitor


def _git(path, *arguments):
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )


def test_revision_monitor_accepts_valid_locally_pulled_code(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "smartGPMS Test")
    source = tmp_path / "sample.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "sample.py")
    _git(tmp_path, "commit", "-m", "first")
    monitor = GitRevisionMonitor(tmp_path)
    previous = monitor.current_revision()

    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "sample.py")
    _git(tmp_path, "commit", "-m", "second")
    current = monitor.current_revision()

    result = monitor.validate_revision(previous, current)
    assert result.valid
    assert "1 个 Python 文件" in result.message


def test_revision_monitor_rejects_dependency_change_until_setup_runs(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "smartGPMS Test")
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi\n", encoding="utf-8")
    _git(tmp_path, "add", "requirements.txt")
    _git(tmp_path, "commit", "-m", "first")
    monitor = GitRevisionMonitor(tmp_path)
    previous = monitor.current_revision()

    requirements.write_text("fastapi\nuvicorn\n", encoding="utf-8")
    _git(tmp_path, "add", "requirements.txt")
    _git(tmp_path, "commit", "-m", "second")

    result = monitor.validate_revision(previous, monitor.current_revision())
    assert not result.valid
    assert "安装脚本" in result.message
