from pathlib import Path

from tools import launch_ui


def test_centered_window_uses_ninety_percent_of_screen():
    assert launch_ui._centered_window_args((1920, 1080)) == [
        "--window-size=1728,972",
        "--window-position=96,54",
    ]


def test_centered_window_falls_back_to_maximized_without_screen_size():
    assert launch_ui._centered_window_args(None) == ["--start-maximized"]


def test_launch_arguments_use_an_isolated_ui_profile(tmp_path, monkeypatch):
    profile = tmp_path / "ui-browser-profile"
    monkeypatch.setattr(launch_ui, "UI_PROFILE_DIR", profile)
    monkeypatch.setattr(launch_ui, "_screen_size", lambda: (1600, 900))

    arguments = launch_ui._launch_arguments(Path("browser.exe"))

    assert f"--user-data-dir={profile}" in arguments
    assert "--disable-background-mode" in arguments
    assert "--window-size=1440,810" in arguments
    assert "--window-position=80,45" in arguments


def test_launch_arguments_always_restore_default_window_geometry(
    tmp_path, monkeypatch
):
    profile = tmp_path / "ui-browser-profile"
    profile.mkdir()
    (profile / ".window-initialized").touch()
    monkeypatch.setattr(launch_ui, "UI_PROFILE_DIR", profile)
    monkeypatch.setattr(launch_ui, "_screen_size", lambda: (1920, 1080))

    arguments = launch_ui._launch_arguments(Path("browser.exe"))

    assert "--window-size=1728,972" in arguments
    assert "--window-position=96,54" in arguments


def test_desktop_launcher_notifies_server_after_dedicated_window_closes(
    tmp_path, monkeypatch
):
    browser = tmp_path / "browser.exe"
    browser.touch()
    events = []

    class Process:
        @staticmethod
        def wait():
            events.append("closed")

    monkeypatch.setattr(launch_ui, "UI_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(launch_ui, "_wait_until_ready", lambda: True)
    monkeypatch.setattr(launch_ui, "_browser_candidates", lambda: [browser])
    monkeypatch.setattr(launch_ui, "_should_monitor_window", lambda: True)
    monkeypatch.setattr(
        launch_ui.subprocess, "Popen", lambda *_args, **_kwargs: Process()
    )
    monkeypatch.setattr(
        launch_ui, "_notify_desktop_closed", lambda: events.append("notified")
    )

    launch_ui.main()

    assert events == ["closed", "notified"]


def test_server_launcher_does_not_monitor_client_window(tmp_path, monkeypatch):
    browser = tmp_path / "browser.exe"
    browser.touch()
    events = []

    class Process:
        @staticmethod
        def wait():
            events.append("closed")

    monkeypatch.setattr(launch_ui, "UI_PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(launch_ui, "_wait_until_ready", lambda: True)
    monkeypatch.setattr(launch_ui, "_browser_candidates", lambda: [browser])
    monkeypatch.setattr(launch_ui, "_should_monitor_window", lambda: False)
    monkeypatch.setattr(
        launch_ui.subprocess, "Popen", lambda *_args, **_kwargs: Process()
    )

    launch_ui.main()

    assert events == []
