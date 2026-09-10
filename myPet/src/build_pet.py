#!/usr/bin/env python3
"""Build the Codex v2 atlas and full-rate standalone swing animation.

The source GIF contains many disposal/delta frames, so this pipeline first
decodes composited frames, removes only edge-connected bright background,
computes one global crop, and applies one scale/offset to every frame. Shared
geometry is the key invariant that keeps both rope anchors fixed on screen.

The final 8×11 atlas targets Codex's v2 renderer. The separate runtime APNG keeps
the measured full swing cadence for :mod:`standalone_pet`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "pet.config.json"

# Codex v2 animation rows. The first nine rows have unused transparent cells.
ROW_SPECS = (
    ("idle", 6),
    ("running-right", 8),
    ("running-left", 8),
    ("waving", 4),
    ("jumping", 5),
    ("failed", 8),
    ("waiting", 6),
    ("running", 6),
    ("review", 6),
    ("look-directions-a", 8),
    ("look-directions-b", 8),
)

# Keyframes from one measured 22-frame source cycle. End points are held longer by
# Codex's built-in state timings, while the middle frames pass through quickly.
STATE_PHASES = {
    "idle": (0, 4, 7, 11, 15, 19),
    "running-right": (0, 3, 6, 9, 11, 14, 17, 20),
    "running-left": (0, 3, 6, 9, 11, 14, 17, 20),
    "waving": (0, 7, 11, 19),
    "jumping": (0, 5, 9, 14, 19),
    "failed": (0, 3, 6, 9, 11, 14, 17, 20),
    "waiting": (0, 4, 7, 11, 15, 19),
    "running": (0, 4, 7, 11, 15, 19),
    "review": (0, 4, 7, 11, 15, 19),
    "look-directions-a": (0, 3, 6, 9, 11, 14, 17, 20),
    "look-directions-b": (0, 3, 6, 9, 11, 14, 17, 20),
}

STATE_DURATIONS = {
    "idle": (280, 110, 110, 140, 140, 320),
    "running-right": (120, 120, 120, 120, 120, 120, 120, 220),
    "running-left": (120, 120, 120, 120, 120, 120, 120, 220),
    "waving": (140, 140, 140, 280),
    "jumping": (140, 140, 140, 140, 280),
    "failed": (140, 140, 140, 140, 140, 140, 140, 240),
    "waiting": (150, 150, 150, 150, 150, 260),
    "running": (120, 120, 120, 120, 120, 220),
    "review": (150, 150, 150, 150, 150, 280),
}


def load_config() -> dict:
    """Load the single source of truth for pet id, cycle and atlas geometry."""
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def extract_foreground(frame: Image.Image) -> Image.Image:
    """Remove the dithered edge-connected background without erasing white body areas."""
    gray = ImageOps.grayscale(frame.convert("RGBA")).filter(ImageFilter.MedianFilter(3))
    gray = ImageEnhance.Contrast(gray).enhance(1.10)
    values = np.asarray(gray, dtype=np.uint8)

    traversable = values >= 202
    seeds = np.zeros_like(traversable, dtype=bool)
    seeds[0, :] = traversable[0, :]
    seeds[-1, :] = traversable[-1, :]
    seeds[:, 0] = traversable[:, 0]
    seeds[:, -1] = traversable[:, -1]
    exterior = ndimage.binary_propagation(seeds, mask=traversable)

    foreground = ndimage.binary_closing(
        ~exterior, structure=np.ones((2, 2), dtype=bool), iterations=1
    )
    alpha = ndimage.gaussian_filter(foreground.astype(np.float32), sigma=0.55)
    alpha = np.clip((alpha - 0.04) / 0.92, 0.0, 1.0)
    alpha_image = Image.fromarray(np.round(alpha * 255).astype(np.uint8), mode="L")

    clean_gray = ImageOps.autocontrast(gray, cutoff=(0.2, 0.2))
    clean_gray = clean_gray.filter(
        ImageFilter.UnsharpMask(radius=0.85, percent=115, threshold=3)
    )
    return Image.merge("RGBA", (clean_gray, clean_gray, clean_gray, alpha_image))


def read_source_frames(path: Path) -> tuple[list[Image.Image], list[int]]:
    """Decode every GIF frame after foreground extraction and retain durations."""
    source = Image.open(path)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for index in range(source.n_frames):
        source.seek(index)
        frames.append(extract_foreground(source.convert("RGBA")))
        durations.append(int(source.info.get("duration", 40)))
    return frames, durations


def get_global_bbox(frames: list[Image.Image]) -> tuple[int, int, int, int]:
    """Return one shared crop box so every frame keeps the source coordinate system."""
    boxes = [frame.getchannel("A").getbbox() for frame in frames]
    visible = [box for box in boxes if box is not None]
    if not visible:
        raise ValueError("Source animation contains no visible foreground")
    left = min(box[0] for box in visible)
    top = min(box[1] for box in visible)
    right = max(box[2] for box in visible)
    bottom = max(box[3] for box in visible)
    return left, top, right, bottom


def fit_to_cell(
    frame: Image.Image,
    global_bbox: tuple[int, int, int, int],
    width: int,
    height: int,
    padding: int,
) -> Image.Image:
    """Place a frame with one shared crop, scale, and offset for the full animation."""
    cropped = frame.crop(global_bbox)
    source_width, source_height = cropped.size
    scale = min(
        (width - padding * 2) / source_width,
        (height - padding * 2) / source_height,
    )
    target_size = (
        max(1, round(source_width * scale)),
        max(1, round(source_height * scale)),
    )
    cropped = cropped.resize(target_size, Image.Resampling.LANCZOS)
    cell = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    x = (width - cropped.width) // 2
    y = (height - cropped.height) // 2
    cell.alpha_composite(cropped, (x, y))
    return cell


def clear_hidden_rgb(image: Image.Image) -> Image.Image:
    """Codex requires fully transparent pixels to contain zeroed RGB channels."""
    pixels = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
    pixels[pixels[:, :, 3] == 0, :3] = 0
    return Image.fromarray(pixels, mode="RGBA")


def make_preview(
    frames: list[Image.Image],
    durations: list[int],
    global_bbox: tuple[int, int, int, int],
    output: Path,
) -> None:
    """Write a lossless full-source preview using the production geometry."""
    previews = []
    for frame in frames:
        cell = fit_to_cell(frame, global_bbox, 192, 208, 4)
        previews.append(cell)
    previews[0].save(
        output,
        save_all=True,
        append_images=previews[1:],
        duration=durations,
        loop=0,
        lossless=True,
        method=6,
    )


def make_runtime_apng(
    frames: list[Image.Image],
    durations: list[int],
    global_bbox: tuple[int, int, int, int],
    cycle_start: int,
    cycle_frames: int,
    output: Path,
) -> None:
    """Write one closed motion cycle whose final pose matches its first pose."""
    cycle_end = cycle_start + cycle_frames
    loop_frames = frames[cycle_start:cycle_end]
    loop_durations = durations[cycle_start:cycle_end]
    if len(loop_frames) != cycle_frames:
        raise ValueError(
            f"Requested source cycle {cycle_start}:{cycle_end}, "
            f"but only {len(frames)} source frames exist"
        )
    runtime_frames = [
        fit_to_cell(frame, global_bbox, 192, 208, 4) for frame in loop_frames
    ]
    # The source endpoint is already within a tenth of a pixel; copying the
    # first cell makes the encoded loop seam pixel-identical as well.
    runtime_frames[-1] = runtime_frames[0].copy()
    runtime_frames[0].save(
        output,
        save_all=True,
        append_images=runtime_frames[1:],
        duration=loop_durations,
        loop=0,
        disposal=0,
        blend=0,
        optimize=False,
    )


def make_state_preview(
    cells: list[Image.Image], durations: tuple[int, ...], output: Path
) -> None:
    """Encode one sampled Codex state row as a small QA animation."""
    cells[0].save(
        output,
        save_all=True,
        append_images=cells[1:],
        duration=durations,
        loop=0,
        lossless=True,
        method=6,
    )


def main() -> None:
    """Assemble assets, QA previews, package manifest and machine-readable report."""
    config = load_config()
    canvas = config["canvas"]
    cell_width = int(canvas["cellWidth"])
    cell_height = int(canvas["cellHeight"])
    columns = int(canvas["columns"])
    rows = int(canvas["rows"])
    padding = int(canvas["padding"])

    source_path = ROOT / config["sourceGif"]
    frames, durations = read_source_frames(source_path)
    global_bbox = get_global_bbox(frames)
    cycle_start = int(config.get("sourceCycleStart", 0))
    cycle_frames = int(config.get("sourceCycleFrames", 22))
    atlas = Image.new(
        "RGBA",
        (columns * cell_width, rows * cell_height),
        (0, 0, 0, 0),
    )

    selected_frames: dict[str, list[int]] = {}
    state_cells: dict[str, list[Image.Image]] = {}
    for row, (state, used_columns) in enumerate(ROW_SPECS):
        phases = STATE_PHASES[state]
        if len(phases) != used_columns:
            raise ValueError(f"{state} expects {used_columns} phases, got {len(phases)}")
        indices = [cycle_start + (phase % cycle_frames) for phase in phases]
        selected_frames[state] = indices
        state_cells[state] = []
        for column, frame_index in enumerate(indices):
            cell = fit_to_cell(
                frames[frame_index], global_bbox, cell_width, cell_height, padding
            )
            state_cells[state].append(cell.copy())
            atlas.alpha_composite(cell, (column * cell_width, row * cell_height))

    # V2 reserves idle row column 6 as the neutral/front look fallback. It is not
    # part of the six-frame idle animation, so use the centered source pose.
    neutral_index = cycle_start + 11
    neutral_cell = fit_to_cell(
        frames[neutral_index], global_bbox, cell_width, cell_height, padding
    )
    atlas.alpha_composite(neutral_cell, (6 * cell_width, 0))

    dist_dir = ROOT / "dist" / config["id"]
    qa_dir = ROOT / "qa"
    runtime_assets_dir = ROOT / "assets" / "runtime"
    dist_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    runtime_assets_dir.mkdir(parents=True, exist_ok=True)

    atlas = clear_hidden_rgb(atlas)
    spritesheet_path = dist_dir / "spritesheet.webp"
    atlas.save(spritesheet_path, lossless=True, method=6, exact=True)

    manifest = {
        "id": config["id"],
        "displayName": config["displayName"],
        "description": config["description"],
        "spriteVersionNumber": 2,
        "spritesheetPath": "spritesheet.webp",
    }
    (dist_dir / "pet.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    make_preview(frames, durations, global_bbox, qa_dir / "swing-preview.webp")
    make_runtime_apng(
        frames,
        durations,
        global_bbox,
        cycle_start,
        cycle_frames,
        runtime_assets_dir / "swing-transparent.png",
    )
    for state, state_durations in STATE_DURATIONS.items():
        make_state_preview(
            state_cells[state], state_durations, qa_dir / f"{state}-preview.webp"
        )
    atlas.resize((768, 1144), Image.Resampling.LANCZOS).save(
        qa_dir / "atlas-preview.png"
    )

    summary = {
        "ok": True,
        "sourceFrames": len(frames),
        "sourceDurationMs": sum(durations),
        "sourceCycleStart": cycle_start,
        "sourceCycleFrames": cycle_frames,
        "runtimeLoopDurationMs": sum(
            durations[cycle_start : cycle_start + cycle_frames]
        ),
        "runtimeLoopFirstFrame": cycle_start,
        "runtimeLoopLastFrame": cycle_start + cycle_frames - 1,
        "globalBBox": list(global_bbox),
        "selectedFrames": selected_frames,
        "neutralFrame": neutral_index,
        "atlasSize": list(atlas.size),
        "cellSize": [cell_width, cell_height],
        "rows": [{"state": name, "usedColumns": count} for name, count in ROW_SPECS],
        "prototypeNote": "v0.2 uses one measured source cycle and shared geometry so the top anchors remain fixed.",
    }
    (qa_dir / "build-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Built {spritesheet_path}")
    print(f"Source motion: {len(frames)} frames / {sum(durations)} ms")


if __name__ == "__main__":
    main()
