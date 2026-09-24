import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_desktop_runner_exits_after_graceful_shutdown_request(tmp_path):
    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "SMARTGPMS_DATA_DIR": str(tmp_path / "data"),
            "SMARTGPMS_HOST": "127.0.0.1",
            "SMARTGPMS_PORT": str(port),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(root / "tools" / "run_desktop.py")],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    health_url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (OSError, TimeoutError, urllib.error.URLError):
                if process.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            raise AssertionError("desktop runner did not become ready")
        assert process.poll() is None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/desktop/shutdown",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
