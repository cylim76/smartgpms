from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import uvicorn

CONTROLLED_RESTART_EXIT_CODE = 75


def prepare_project_import_path(root: Path) -> None:
    """Make direct-script and ``python -m`` service launches behave identically."""
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    os.chdir(root)


def main() -> None:
    """Run the single-session smartGPMS backend under a service manager."""
    os.environ["SMARTGPMS_RUN_MODE"] = "server"
    host = os.environ.get("SMARTGPMS_HOST", "0.0.0.0")
    port = int(os.environ.get("SMARTGPMS_PORT", "8765"))
    root = Path(__file__).resolve().parents[1]
    prepare_project_import_path(root)
    from app import app, service

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            workers=1,
            access_log=False,
        )
    )

    def wait_for_controlled_restart() -> None:
        service.restart_requested.wait()
        if service.restart_requested.is_set():
            server.should_exit = True

    threading.Thread(
        target=wait_for_controlled_restart,
        name="smartgpms-update-restart",
        daemon=True,
    ).start()
    server.run()
    if service.restart_requested.is_set():
        raise SystemExit(CONTROLLED_RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
