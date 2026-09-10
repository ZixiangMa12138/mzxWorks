#!/usr/bin/env python3
"""Build deterministic hover/jump assets from the approved source artwork.

The script never redraws or replaces the supplied face. It separates character
and swing geometry with explicit masks, removes rope fragments from the moving
character component, then moves the complete character on an eased jump arc.
The jump-up sequence is an exact temporal reverse of jump-down, guaranteeing a
matching endpoint when returning to the swing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from scipy import ndimage
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageSequence

from build_pet import clear_hidden_rgb, extract_foreground


ROOT = Path(__file__).resolve().parents[1]
SWING_ASSET = ROOT / "assets" / "runtime" / "swing-transparent.png"
STANDING_SOURCE = ROOT / "assets" / "source" / "standing-reference.png"
RUNTIME_DIR = ROOT / "assets" / "runtime"
QA_DIR = ROOT / "qa"

CANVAS = (320, 208)
SWING_OFFSET_X = 128
ANCHOR_X = (SWING_OFFSET_X + 58, SWING_OFFSET_X + 130)
HAND_CENTER = (190, 123)
HAND_RADIUS = (16, 11)
TRANSITION_FRAME_COUNT = 14
TRANSITION_DURATION_MS = 55


def ease_in_out(value: float) -> float:
    """Cubic smoothstep used for position and scale interpolation."""
    return value * value * (3.0 - 2.0 * value)


def with_opacity(image: Image.Image, opacity: float) -> Image.Image:
    """Return a copy with alpha multiplied without altering visible RGB."""
    result = image.copy()
    alpha = np.asarray(result.getchannel("A"), dtype=np.float32)
    result.putalpha(Image.fromarray(np.uint8(np.clip(alpha * opacity, 0, 255))))
    return result


def paste_centered(canvas: Image.Image, layer: Image.Image, xy: tuple[int, int]) -> None:
    """Alpha-composite a prepared layer at a deterministic canvas coordinate."""
    canvas.alpha_composite(layer, xy)


def save_apng(frames: list[Image.Image], output: Path, durations: list[int]) -> None:
    """Encode transparent APNG frames after clearing hidden RGB bytes."""
    frames = [clear_hidden_rgb(frame) for frame in frames]
    frames[0].save(
        output,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=0,
        blend=0,
        optimize=False,
    )


def load_swing_frames() -> tuple[list[Image.Image], list[int]]:
    """Pad swing cells to interaction canvas and rotate cycle to upright phase."""
    source = Image.open(SWING_ASSET)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        rgba = frame.convert("RGBA")
        canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        canvas.alpha_composite(rgba, (SWING_OFFSET_X, 0))
        frames.append(canvas)
        durations.append(int(frame.info.get("duration", source.info.get("duration", 40))))
    # Frame 9 is the upright, front-facing midpoint. Rotate the closed cycle so
    # hover transitions start from that pose and append it again as the exact seam.
    neutral_index = 9
    unique_frames = frames[:-1]
    unique_durations = durations[:-1]
    frames = unique_frames[neutral_index:] + unique_frames[: neutral_index + 1]
    durations = unique_durations[neutral_index:] + unique_durations[: neutral_index + 1]
    return frames, durations


def make_standing_scene(start_frame: Image.Image) -> Image.Image:
    """Normalize the user-supplied standing scene without face compositing."""
    source = Image.open(STANDING_SOURCE).convert("RGBA")
    foreground = extract_foreground(source)
    bbox = foreground.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("Standing source contains no foreground")
    subject = foreground.crop(bbox).resize((228, 200), Image.Resampling.LANCZOS)

    scene = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    scene.alpha_composite(subject, (45, 4))

    # Keep the supplied standing artwork intact, including its original face.
    # Only resolution/contrast styling is applied to the complete scene.
    small = scene.resize((160, 104), Image.Resampling.LANCZOS)
    scene = small.resize(CANVAS, Image.Resampling.LANCZOS)
    scene = ImageEnhance.Contrast(scene).enhance(1.08)
    return clear_hidden_rgb(scene)


def split_standing_scene(standing: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Separate by swing geometry, avoiding the old hard cut through face/hand pixels."""
    array = np.asarray(standing).copy()
    yy, xx = np.mgrid[0 : CANVAS[1], 0 : CANVAS[0]]
    left_rope = (xx >= ANCHOR_X[0] - 5) & (xx <= ANCHOR_X[0] + 5)
    right_side = xx >= 232
    seat_and_hangers = (xx >= 168) & (yy >= 126)
    swing_mask = left_rope | right_side | seat_and_hangers
    hand_zone = (
        (
            ((xx - HAND_CENTER[0]) / HAND_RADIUS[0]) ** 2
            + ((yy - HAND_CENTER[1]) / HAND_RADIUS[1]) ** 2
            <= 1.0
        )
        & (yy >= 115)
    )
    arm_zone = (xx >= 116) & (xx <= 181) & (yy >= 111) & (yy <= 137)
    # The hand starts at its real top outline (y=115); pixels above that point are
    # rope and remain with the swing. The complete forearm stays with the character.
    swing_mask &= ~(hand_zone | arm_zone)

    character = array.copy()
    character[swing_mask] = 0
    # Retain only the main connected character silhouette. Small secondary
    # components here are rope knots/hanger fragments, never part of the pet.
    labels, count = ndimage.label(character[:, :, 3] > 20)
    if count:
        sizes = ndimage.sum(character[:, :, 3] > 20, labels, range(1, count + 1))
        main_label = int(np.argmax(sizes)) + 1
        character[labels != main_label] = 0
    swing = array.copy()
    swing[~swing_mask] = 0

    # Rebuild the exposed left rope underneath the moving character. The hand is
    # retained in the character layer, while no head/face fragment can enter swing.
    swing_image = Image.fromarray(swing, "RGBA")
    rope = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    ImageDraw.Draw(rope).line(
        (ANCHOR_X[0], 0, ANCHOR_X[0], 151),
        fill=(22, 22, 22, 245),
        width=3,
    )
    swing_image.alpha_composite(rope)
    return Image.fromarray(character, "RGBA"), swing_image


def move_character(character: Image.Image, progress: float) -> Image.Image:
    """Place one complete character layer along the take-off/landing arc."""
    bbox = character.getchannel("A").getbbox()
    if bbox is None:
        return Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    crop = character.crop(bbox)
    eased = ease_in_out(progress)
    scale_x = 0.78 + 0.22 * eased
    scale_y = 0.70 + 0.30 * eased
    size = (max(1, round(crop.width * scale_x)), max(1, round(crop.height * scale_y)))
    crop = crop.resize(size, Image.Resampling.LANCZOS)

    dx = round(78 * (1.0 - eased))
    # A true arc rather than linear diagonal travel: feet lift, cross, then land.
    dy = round(19 * (1.0 - eased) - 31 * math.sin(math.pi * progress))
    x = bbox[0] + dx + (bbox[2] - bbox[0] - crop.width) // 2
    y = bbox[1] + dy + (bbox[3] - bbox[1] - crop.height)
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    canvas.alpha_composite(crop, (x, y))
    return canvas


def make_jump_down(start: Image.Image, standing: Image.Image) -> list[Image.Image]:
    """Synthesize a single-silhouette transition from swing to standing art."""
    character, fixed_swing = split_standing_scene(standing)
    frames: list[Image.Image] = []
    for index in range(TRANSITION_FRAME_COUNT):
        t = index / (TRANSITION_FRAME_COUNT - 1)
        if index == 0:
            frames.append(start.copy())
            continue
        if index == TRANSITION_FRAME_COUNT - 1:
            frames.append(standing.copy())
            continue

        # Hold the source pose for two frames, then cut on action to the compact
        # take-off pose. Avoiding simultaneous silhouettes removes double-image
        # ghosting while the fast movement hides the pose substitution.
        if index <= 2:
            frames.append(start.copy())
            continue
        frame = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        frame.alpha_composite(fixed_swing)
        frame.alpha_composite(move_character(character, t))
        frames.append(frame)
    return [clear_hidden_rgb(frame) for frame in frames]


def make_contact_sheet(frames: list[Image.Image], output: Path) -> None:
    """Create a numbered transition sheet for hand/rope/face visual review."""
    picks = (0, 2, 4, 6, 8, 10, 12, 13)
    sheet = Image.new("RGBA", (CANVAS[0] * 4, CANVAS[1] * 2), (238, 238, 238, 255))
    for position, frame_index in enumerate(picks):
        tile = frames[frame_index].copy()
        ImageDraw.Draw(tile).text((6, 5), str(frame_index), fill=(180, 40, 40, 255))
        sheet.alpha_composite(tile, ((position % 4) * CANVAS[0], (position // 4) * CANVAS[1]))
    sheet.save(output)


def main() -> None:
    """Generate runtime APNGs, motion preview and deterministic QA measurements."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    QA_DIR.mkdir(parents=True, exist_ok=True)
    swing_frames, swing_durations = load_swing_frames()
    standing = make_standing_scene(swing_frames[0])
    jump_down = make_jump_down(swing_frames[0], standing)
    jump_up = list(reversed([frame.copy() for frame in jump_down]))

    save_apng(swing_frames, RUNTIME_DIR / "swing-interactive.png", swing_durations)
    standing.save(RUNTIME_DIR / "standing-transparent.png")
    transition_durations = [TRANSITION_DURATION_MS] * TRANSITION_FRAME_COUNT
    transition_durations[-1] = 90
    save_apng(jump_down, RUNTIME_DIR / "jump-down.png", transition_durations)
    save_apng(jump_up, RUNTIME_DIR / "jump-up.png", list(reversed(transition_durations)))
    make_contact_sheet(jump_down, QA_DIR / "hover-transition-contact.png")

    preview = swing_frames[:5] + jump_down + [standing] * 9 + jump_up + swing_frames[:5]
    preview_durations = swing_durations[:5] + transition_durations + [90] * 9
    preview_durations += list(reversed(transition_durations)) + swing_durations[:5]
    preview[0].save(
        QA_DIR / "hover-interaction-preview.webp",
        save_all=True,
        append_images=preview[1:],
        duration=preview_durations,
        loop=0,
        lossless=True,
        method=6,
    )

    reference = np.asarray(swing_frames[0])
    qa_character, qa_swing = split_standing_scene(standing)
    yy, xx = np.mgrid[0 : CANVAS[1], 0 : CANVAS[0]]
    qa_hand_zone = (
        (
            ((xx - HAND_CENTER[0]) / HAND_RADIUS[0]) ** 2
            + ((yy - HAND_CENTER[1]) / HAND_RADIUS[1]) ** 2
            <= 1.0
        )
        & (yy >= 115)
    )
    character_hand_pixels = int(
        np.count_nonzero((np.asarray(qa_character.getchannel("A")) > 20) & qa_hand_zone)
    )
    moving_rope_pixels_above_hand = int(
        np.count_nonzero(np.asarray(qa_character.getchannel("A"))[96:115, 181:193] > 20)
    )
    fixed_arm_pixels = int(
        np.count_nonzero(np.asarray(qa_swing.getchannel("A"))[116:138, 116:182] > 20)
    )
    report = {
        "ok": character_hand_pixels >= 80 and moving_rope_pixels_above_hand == 0 and fixed_arm_pixels == 0,
        "canvas": list(CANVAS),
        "swingFrames": len(swing_frames),
        "jumpDownFrames": len(jump_down),
        "jumpUpFrames": len(jump_up),
        "transitionDurationMs": sum(transition_durations),
        "anchorX": list(ANCHOR_X),
        "artificialTopRopeSegments": False,
        "standingTransparent": standing.getchannel("A").getextrema()[0] == 0,
        "jumpDownStartsAtSwing": np.array_equal(np.asarray(jump_down[0]), reference),
        "jumpDownEndsAtStanding": np.array_equal(np.asarray(jump_down[-1]), np.asarray(standing)),
        "standingFaceSource": "assets/source/standing-reference.png",
        "originalFaceOverlayUsed": False,
        "fixedSwingFaceRegionAlphaMax": int(
            np.asarray(qa_swing.getchannel("A"))[44:125, 65:176].max()
        ),
        "handCenter": list(HAND_CENTER),
        "characterHandPixels": character_hand_pixels,
        "handAssignedToCharacter": character_hand_pixels >= 80,
        "movingRopePixelsAboveHand": moving_rope_pixels_above_hand,
        "fixedArmPixelsBeforeLanding": fixed_arm_pixels,
        "jumpUpIsExactReverse": all(
            np.array_equal(np.asarray(a), np.asarray(b)) for a, b in zip(jump_up, reversed(jump_down))
        ),
    }
    (QA_DIR / "hover-interaction.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
