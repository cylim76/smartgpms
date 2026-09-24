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
