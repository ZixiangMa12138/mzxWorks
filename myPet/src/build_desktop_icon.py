#!/usr/bin/env python3
"""Build the launcher icon from the pet's lowest, vertical swing frame."""

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets" / "runtime" / "swing-interactive.png"
OUTPUT = ROOT / "assets" / "icons" / "swing-pet.png"
ICON_SIZE = 512
ART_MARGIN = 16
LOWEST_SWING_FRAME = 0


def main() -> int:
    """Crop transparent padding, enlarge the pet, and save a square PNG icon."""
    source = Image.open(SOURCE)
    source.seek(LOWEST_SWING_FRAME)
    frame = source.convert("RGBA")
    bounds = frame.getchannel("A").getbbox()
    if bounds is None:
        raise RuntimeError(f"Lowest swing frame is empty: {SOURCE}")

    artwork = frame.crop(bounds)
    available = ICON_SIZE - ART_MARGIN * 2
    scale = min(available / artwork.width, available / artwork.height)
    output_size = (
        max(1, round(artwork.width * scale)),
        max(1, round(artwork.height * scale)),
    )
    artwork = artwork.resize(output_size, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    position = ((ICON_SIZE - artwork.width) // 2, (ICON_SIZE - artwork.height) // 2)
    canvas.alpha_composite(artwork, position)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUTPUT, optimize=True)
    print(f"icon={OUTPUT} size={canvas.size} artwork={output_size} frame={LOWEST_SWING_FRAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
