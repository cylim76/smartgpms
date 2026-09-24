from __future__ import annotations

import os
from pathlib import Path

import uvicorn


def main() -> None:
    """Run the single-session smartGPMS backend under a service manager."""
    os.environ["SMARTGPMS_RUN_MODE"] = "server"
    host = os.environ.get("SMARTGPMS_HOST", "0.0.0.0")
    port = int(os.environ.get("SMARTGPMS_PORT", "8765"))
    root = Path(__file__).resolve().parents[1]
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        workers=1,
        access_log=False,
        app_dir=str(root),
    )


if __name__ == "__main__":
    main()
