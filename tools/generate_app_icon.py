"""Generate the smartGPMS GP application icon in PNG and Windows ICO formats."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "static" / "assets"
ICON_COLOR = "#03a69c"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = []
    if os.name == "nt":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend((fonts / "arialbd.ttf", fonts / "segoeuib.ttf"))
    candidates.extend(
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)


def render_icon(size: int = 512) -> Image.Image:
    scale = size / 512
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = round(20 * scale)
    radius = round(142 * scale)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=radius,
        fill=ICON_COLOR,
    )
    font = _font(round(224 * scale))
    text = "GP"
    bounds = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    position = (
        round((size - width) / 2 - bounds[0]),
        round((size - height) / 2 - bounds[1] - 4 * scale),
    )
    draw.text(position, text, font=font, fill="white")
    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    icon = render_icon()
    icon.save(ASSET_DIR / "smartgpms-app.png", format="PNG", optimize=True)
    icon.save(
        ASSET_DIR / "smartgpms-app.ico",
        format="ICO",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )


if __name__ == "__main__":
    main()
