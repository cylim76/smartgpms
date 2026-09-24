"""Run the single-user Windows desktop instance with graceful window shutdown."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SMARTGPMS_RUN_MODE"] = "desktop"

import uvicorn

import app as app_module


def main() -> None:
    host = os.environ.get("SMARTGPMS_HOST", "127.0.0.1")
    port = int(os.environ.get("SMARTGPMS_PORT", "8765"))
    server = uvicorn.Server(
        uvicorn.Config(
            app_module.app,
            host=host,
            port=port,
            workers=1,
        )
    )
    app_module.configure_desktop_shutdown(
        lambda: setattr(server, "should_exit", True)
    )
    server.run()


if __name__ == "__main__":
    main()
