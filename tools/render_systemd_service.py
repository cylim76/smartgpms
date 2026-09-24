"""Render the portable smartGPMS systemd unit during Linux setup."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy" / "smartgpms.service"


def _systemd_value(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("systemd values cannot contain control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _absolute_path(value: Path) -> Path:
    """Make a path absolute without dereferencing a virtualenv symlink."""
    return Path(os.path.abspath(value))


def _systemd_path(value: Path) -> str:
    """Escape a path for directives such as WorkingDirectory=."""
    text = str(_absolute_path(value))
    if "\n" in text or "\r" in text or "\0" in text:
        raise ValueError("systemd paths cannot contain control characters")
    output: list[str] = []
    for character in text:
        if character == " ":
            output.append("\\x20")
        elif character == "\t":
            output.append("\\x09")
        elif character == "\\":
            output.append("\\\\")
        elif character == "%":
            output.append("%%")
        else:
            output.append(character)
    return "".join(output)


def render_unit(
    *,
    service_user: str,
    service_group: str,
    working_directory: Path,
    data_directory: Path,
    python_executable: Path,
    host: str,
    port: int,
) -> str:
    values = {
        "@SERVICE_USER@": service_user,
        "@SERVICE_GROUP@": service_group,
        "@WORKING_DIRECTORY@": _systemd_path(working_directory),
        "@DATA_DIRECTORY@": _systemd_value(str(_absolute_path(data_directory))),
        "@PYTHON_EXECUTABLE@": _systemd_value(
            str(_absolute_path(python_executable))
        ),
        "@HOST@": _systemd_value(host),
        "@PORT@": str(port),
    }
    rendered = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if re.search(r"@[A-Z_]+@", rendered):
        raise ValueError("systemd template still contains an unresolved marker")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    content = render_unit(
        service_user=args.user,
        service_group=args.group,
        working_directory=args.working_directory,
        data_directory=args.data_directory,
        python_executable=args.python,
        host=args.host,
        port=args.port,
    )
    args.output.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
