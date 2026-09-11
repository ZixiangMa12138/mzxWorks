#!/usr/bin/env python3
"""Run the interactive transparent desktop edition of Swing Pet.

The module has four deliberately separate responsibilities:

* load APNG/GIF frames without losing alpha or per-frame timing;
* render the pet, status bubble and short-lived heart effect in GTK windows;
* translate pointer input into physically continuous swing/hover animations;
* maintain affinity and the zero-affinity explosion/chain/key state machine.

Codex activity arrives from :mod:`codex_bridge` on a background thread. The
bridge callback is always marshalled onto GTK's main loop with ``GLib.idle_add``;
all widgets and animation state therefore remain single-threaded.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import gi
from PIL import Image, ImageSequence

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

# Audio is an optional enhancement for the standalone GTK pet. Import it
# separately so a machine without GStreamer can still run every visual feature.
try:  # pragma: no cover - availability depends on the desktop installation
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: E402
except (ImportError, ValueError):  # pragma: no cover
    Gst = None

from codex_bridge import BridgeUpdate, CodexBridge


# Linux exposes both a short task name (``comm``) and a command line. The shell
# launchers set argv[0] to this value, while this helper updates ``comm`` so
# system monitors consistently show the pet as PetCat instead of python3.
PROCESS_NAME = "PetCat"


def set_process_name() -> None:
    """Best-effort Linux ``PR_SET_NAME`` update without an extra dependency."""
    if os.name != "posix":
        return
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(15, PROCESS_NAME.encode(), 0, 0, 0)
    except (AttributeError, OSError):
        # The window remains usable on non-Linux POSIX hosts where prctl is not
        # available; the launcher still supplies a readable command line.
        pass


ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "petcat"
SETTINGS_PATH = CONFIG_ROOT / "settings.json"
RUNTIME_ASSETS = ROOT / "assets" / "runtime"
AUDIO_ASSETS = ROOT / "assets" / "audio"
DEFAULT_ASSET = RUNTIME_ASSETS / "swing-interactive.png"
DEFAULT_STANDING_ASSET = RUNTIME_ASSETS / "standing-transparent.png"
DEFAULT_JUMP_DOWN_ASSET = RUNTIME_ASSETS / "jump-down.png"
DEFAULT_JUMP_UP_ASSET = RUNTIME_ASSETS / "jump-up.png"
AUDIO_LANDING = AUDIO_ASSETS / "landing-zhedia.mp3"
AUDIO_DRAG = AUDIO_ASSETS / "drag-wocao.mp3"
AUDIO_EXPLOSION = AUDIO_ASSETS / "explosion.wav"
AUDIO_CHAINS = AUDIO_ASSETS / "chains-metal.wav"
AUDIO_UNLOCK = AUDIO_ASSETS / "unlock.wav"
AUDIO_ROTOR = AUDIO_ASSETS / "danger-rotor.mp3"
AUDIO_VOLUME = 0.52
AUDIO_EXPLOSION_VOLUME = 0.943
DRAG_VOICE_GAP_SECONDS = 0.5
AUDIO_VOLUME_RAMP_PER_SECOND = 0.18

# Codex changes playback speed, never frame order. This preserves the source
# swing's fixed top anchors and pixel-identical loop seam in every app state.
STATE_SPEED = {
    "idle": 1.0,
    "working": 1.45,
    "waiting": 0.55,
    "success": 1.9,
    "error": 0.30,
}

# Indices measured from the closed 18-frame runtime cycle. Each apex is followed
# by a duplicate hold frame, so playback resumes with a natural direction reversal.
DRAG_POSE_INDEX = {
    "bottom": 0,
    "right_apex": 6,
    "left_apex": 13,
}
DRAG_START_THRESHOLD = 4.0
DRAG_AXIS_THRESHOLD = 1.25
DRAG_CYCLE_PERIOD = 17
DRAG_TRANSITION_FRAME_MS = 32
DRAG_IDLE_RESUME_MS = 110
TICK_IDLE_MS = 33
TICK_ACTIVE_MS = 22
TICK_INTERACTIVE_MS = 16
DRAG_ALERT_TEXT = "WOC ！！！"
LANDING_TEXT = "咋滴啊？"

# Affinity timing is based on ``time.monotonic`` so wall-clock changes cannot
# award or remove points. Dragging and standing have a floor of 5, while the
# ordinary five-second idle decay may continue to zero and enter lockdown.
AFFECTION_DEFAULT = 100
AFFECTION_MAX = 1000
AFFECTION_DECAY_SECONDS = 5.0
AFFECTION_DRAG_LOSS = 5
AFFECTION_STANDING_LOSS = 1
AFFECTION_ACTION_PERIOD_SECONDS = 1.0
AFFECTION_ACTION_FLOOR = 5
HOVER_DISMOUNT_SECONDS = 1.0
LOW_AFFECTION_THRESHOLD = 10
DANGER_AFFECTION_THRESHOLD = 5
DANGER_SPEED_STEP_SECONDS = 0.5
DANGER_SPEED_STEP = 0.15
HEART_DURATION_SECONDS = 1.05
HEART_HEAD_OFFSET_X = 20.0
HEART_HEAD_Y_RATIO = 0.18
HEART_WINDOW_CENTER_X = 28.0
LOCK_HIT_RADIUS = 72.0

STATE_INFO_TEXT = {
    "idle": "空闲中",
    "working": "工作中",
    "waiting": "思考中",
    "success": "空闲中",
    "error": "思考中",
}

STATE_LABEL = {
    "idle": "Codex 空闲 · 悠闲荡秋千",
    "working": "Codex 工作中 · 加速摆动",
    "waiting": "Codex 等待确认 · 慢速摆动",
    "success": "Codex 已完成 · 开心加速",
    "error": "Codex 遇到问题 · 缓慢摆动",
}


def load_sound_setting(path: Path = SETTINGS_PATH, default: bool = True) -> bool:
    """Load the persisted sound switch, tolerating absent or damaged config."""
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("sound_enabled")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return default
    return value if isinstance(value, bool) else default


def save_sound_setting(enabled: bool, path: Path = SETTINGS_PATH) -> None:
    """Atomically persist the sound switch outside the Git working tree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"sound_enabled": bool(enabled)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def drag_pose_for_delta(dx: float, dy: float) -> str | None:
    """Map pointer velocity to the physically lagging swing pose."""
    if abs(dx) >= DRAG_AXIS_THRESHOLD:
        return "right_apex" if dx < 0 else "left_apex"
    if abs(dy) >= DRAG_AXIS_THRESHOLD:
        return "bottom"
    return None


def alert_markup(text: str) -> str:
    """Return the shared high-visibility style for drag and landing messages."""
    return f'<span foreground="#FFE600" size="xx-large" weight="bold">{text}</span>'


def info_markup(text: str, affection: int, alert: bool = False) -> str:
    """Build the popup text while keeping affinity readable but unobtrusive."""
    escaped = GLib.markup_escape_text(text)
    color = "#FFE600" if alert else "#FFFFFF"
    size = "xx-large" if alert else "large"
    return (
        f'<span foreground="{color}" size="{size}" weight="bold">{escaped}</span>'
        f'\n<span foreground="#FFFFFF" size="small">亲密度 {affection}</span>'
    )


def clamp_affection(value: int) -> int:
    """Clamp every external or calculated affinity value to its storage range."""
    return max(0, min(AFFECTION_MAX, int(value)))


def low_affection_blocks_motion(affection: int) -> bool:
    """Allow only recovery clicks while affinity is in the 1..5 danger range."""
    return 0 < affection <= DANGER_AFFECTION_THRESHOLD


def low_affection_tint(affection: int) -> float:
    """Return ten visibly distinct red-tint levels for affinity 10 through 1."""
    if affection <= 0:
        return 1.0
    if affection > LOW_AFFECTION_THRESHOLD:
        return 0.0
    return (LOW_AFFECTION_THRESHOLD + 1 - affection) / LOW_AFFECTION_THRESHOLD


def danger_speed_multiplier(affection: int, elapsed: float) -> float:
    """Smoothly add one speed step per half-second at affinity 5 through 1.

    A cubic smoothstep blends each 0.15 increment into the next one. This keeps
    the requested half-second acceleration rhythm but removes instantaneous
    multiplier jumps that look like a pause. The multiplier intentionally has
    no cap because ordinary decay eventually reaches zero and starts lockdown.
    """
    if affection <= 0 or affection > DANGER_AFFECTION_THRESHOLD:
        return 1.0
    step_position = max(0.0, elapsed) / DANGER_SPEED_STEP_SECONDS
    completed_steps = math.floor(step_position)
    fraction = step_position - completed_steps
    eased_fraction = fraction * fraction * (3.0 - 2.0 * fraction)
    return 1.0 + (completed_steps + eased_fraction) * DANGER_SPEED_STEP


def danger_rotor_volume(affection: int) -> float:
    """Map affinity 5..1 to a progressively louder but still moderate rotor."""
    if affection <= 0 or affection > DANGER_AFFECTION_THRESHOLD:
        return 0.0
    danger_progress = (DANGER_AFFECTION_THRESHOLD - affection) / max(
        1, DANGER_AFFECTION_THRESHOLD - 1
    )
    # Both endpoints are 15% louder than the originally approved 0.14..0.60
    # curve, while remaining below GStreamer's unity-gain value of 1.0.
    return 0.161 + danger_progress * 0.529


def approach_volume(current: float, target: float, elapsed: float, rate: float) -> float:
    """Move an audio level toward its target without an audible step change."""
    distance = target - current
    maximum_step = max(0.0, elapsed) * max(0.0, rate)
    if abs(distance) <= maximum_step:
        return target
    return current + math.copysign(maximum_step, distance)


def elapsed_periods(now: float, deadline: float, period: float) -> int:
    """Count periodic events due at ``now``, including the deadline itself."""
    if now < deadline:
        return 0
    return int((now - deadline) // period) + 1


def timed_affection_loss(
    elapsed: float, carry: float, points_per_period: int
) -> tuple[int, float]:
    """Convert elapsed action time into whole-second affinity-loss steps."""
    total = max(0.0, elapsed) + max(0.0, carry)
    steps = int(total // AFFECTION_ACTION_PERIOD_SECONDS)
    return steps * points_per_period, total - steps * AFFECTION_ACTION_PERIOD_SECONDS


def decrease_to_floor(current: int, loss: int, floor: int = AFFECTION_ACTION_FLOOR) -> int:
    """Decrease affinity without raising values that are already below the floor."""
    if current <= floor:
        return current
    return max(floor, current - max(0, loss))


def hover_dismount_ready(
    now: float,
    idle_since: float | None,
    pointer_inside: bool,
    drag_candidate: bool,
    animation_name: str,
) -> bool:
    """Only dismount after one uninterrupted second with no pointer action."""
    return (
        idle_since is not None
        and pointer_inside
        and not drag_candidate
        and animation_name == "swing"
        and now - idle_since >= HOVER_DISMOUNT_SECONDS
    )


def key_hits_lock(x: float, y: float, width: float, height: float) -> bool:
    """Return whether the key tip overlaps the screen-center keyhole."""
    return math.hypot(x - width / 2, y - height / 2) <= LOCK_HIT_RADIUS


def centered_popup_x(root_x: float, visible_center_x: float, popup_width: int) -> int:
    """Center a popup over visible artwork instead of its transparent canvas."""
    return round(root_x + visible_center_x - popup_width / 2)


def heart_effect_origin(
    pet_x: float,
    pet_y: float,
    head_center_x: float,
    pet_height: float,
    scale: float,
) -> tuple[int, int]:
    """Place the heart just right of the head while allowing sprite overlap."""
    return (
        round(pet_x + head_center_x + HEART_HEAD_OFFSET_X * scale - HEART_WINDOW_CENTER_X),
        round(pet_y + pet_height * HEART_HEAD_Y_RATIO),
    )


def window_state_hides_auxiliary(state: int) -> bool:
    """Return whether the owner is withdrawn/iconified by Show Desktop."""
    hidden_states = int(Gdk.WindowState.WITHDRAWN | Gdk.WindowState.ICONIFIED)
    return bool(int(state) & hidden_states)


def should_resume_held_drag(idle_ms: float, has_transition: bool) -> bool:
    """Resume normal motion while the button remains held but the pointer is still."""
    return idle_ms >= DRAG_IDLE_RESUME_MS and not has_transition


def shortest_cycle_path(current: int, target: int) -> list[int]:
    """Return the shortest continuous GIF phase path, excluding current."""
    current %= DRAG_CYCLE_PERIOD
    target %= DRAG_CYCLE_PERIOD
    forward = (target - current) % DRAG_CYCLE_PERIOD
    backward = (current - target) % DRAG_CYCLE_PERIOD
    if forward <= backward:
        return [(current + step) % DRAG_CYCLE_PERIOD for step in range(1, forward + 1)]
    return [(current - step) % DRAG_CYCLE_PERIOD for step in range(1, backward + 1)]


@dataclass(frozen=True)
class AnimationFrame:
    """One decoded frame plus data needed for hit-testing and tint caching.

    ``pixbuf`` is the normal display object, ``rgba`` is retained for generating
    the ten low-affinity red variants, and ``alpha`` provides cheap per-pixel
    pointer hit-testing without reading mutable GDK memory.
    """
    pixbuf: GdkPixbuf.Pixbuf
    duration_ms: int
    rgba: bytes
    alpha: bytes
    width: int
    height: int
    visible_center_x: float


def image_to_pixbuf(image: Image.Image) -> GdkPixbuf.Pixbuf:
    """Create an immutable GTK pixbuf whose backing bytes stay reference-counted."""
    rgba = image.convert("RGBA")
    raw = GLib.Bytes.new(rgba.tobytes())
    return GdkPixbuf.Pixbuf.new_from_bytes(
        raw,
        GdkPixbuf.Colorspace.RGB,
        True,
        8,
        rgba.width,
        rgba.height,
        rgba.width * 4,
    )


def load_animation(path: Path, scale: float) -> list[AnimationFrame]:
    """Decode every animation frame, preserve duration/alpha, and scale once."""
    source = Image.open(path)
    frames: list[AnimationFrame] = []
    for frame in ImageSequence.Iterator(source):
        rgba = frame.convert("RGBA")
        if scale != 1.0:
            size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
            rgba = rgba.resize(size, Image.Resampling.LANCZOS)
        duration = max(20, int(frame.info.get("duration", source.info.get("duration", 40))))
        alpha = rgba.getchannel("A")
        visible_bounds = alpha.getbbox()
        visible_center_x = (
            (visible_bounds[0] + visible_bounds[2]) / 2
            if visible_bounds is not None
            else rgba.width / 2
        )
        frames.append(
            AnimationFrame(
                image_to_pixbuf(rgba),
                duration,
                rgba.tobytes(),
                alpha.tobytes(),
                rgba.width,
                rgba.height,
                visible_center_x,
            )
        )
    if not frames:
        raise RuntimeError(f"No animation frames found in {path}")
    return frames


class AudioManager:
    """Play short interaction sounds through reusable GStreamer channels.

    Voice, impact, rotor, and chain sounds use separate channels. Completion is
    read from each GStreamer bus so repeated speech never truncates itself.
    """

    def __init__(self, enabled: bool = True, volume: float = AUDIO_VOLUME) -> None:
        self.available = Gst is not None
        self.enabled = enabled and self.available
        self.volume = volume
        self.players: dict[str, object] = {}
        self.playing: set[str] = set()
        self.looping: set[str] = set()
        self.paths: dict[str, Path] = {}
        self.channel_volumes: dict[str, float] = {}
        self.volume_targets: dict[str, float] = {}
        self.last_volume_tick = time.monotonic()
        self.ready_at: dict[str, float] = {}
        self.end_gaps: dict[str, float] = {}
        if self.enabled:
            Gst.init(None)

    def set_enabled(self, enabled: bool) -> None:
        """Apply the menu sound switch immediately without rebuilding the pet."""
        requested = bool(enabled)
        if not requested:
            self.stop_all()
            self.enabled = False
            return
        self.enabled = self.available
        if self.enabled:
            Gst.init(None)
            self.last_volume_tick = time.monotonic()

    def _player(self, channel: str):
        """Return one reusable playbin per logical sound channel."""
        player = self.players.get(channel)
        if player is None:
            player = Gst.ElementFactory.make("playbin", f"petcat-{channel}")
            if player is None:
                self.enabled = False
                return None
            # Queue the same URI before a rotor reaches EOS. Playbin performs
            # this hand-off gaplessly; the EOS seek below remains only a safety
            # fallback for backends that do not emit about-to-finish.
            player.connect("about-to-finish", self._on_about_to_finish, channel)
            self.players[channel] = player
        return player

    def _on_about_to_finish(self, player, channel: str) -> None:
        """Queue another copy of an active loop without stopping its pipeline."""
        path = self.paths.get(channel)
        if channel in self.looping and path is not None:
            player.set_property("uri", path.resolve().as_uri())

    def _start(self, channel: str, path: Path, volume: float | None = None) -> bool:
        """Start a channel without changing its loop or cooldown policy."""
        player = self._player(channel)
        if player is None:
            return False
        player.set_state(Gst.State.NULL)
        effective_volume = (
            self.channel_volumes.get(channel, self.volume) if volume is None else volume
        )
        self.channel_volumes[channel] = effective_volume
        player.set_property("volume", effective_volume)
        player.set_property("uri", path.resolve().as_uri())
        player.set_state(Gst.State.PLAYING)
        self.paths[channel] = path
        self.playing.add(channel)
        return True

    def play(self, channel: str, path: Path, volume: float | None = None) -> None:
        """Start a local file on a named channel, replacing any prior sound."""
        if not self.enabled or not path.is_file():
            return
        self.looping.discard(channel)
        self.volume_targets.pop(channel, None)
        self.channel_volumes[channel] = self.volume if volume is None else volume
        self.ready_at.pop(channel, None)
        self.end_gaps.pop(channel, None)
        self._start(channel, path, volume)

    def request(self, channel: str, path: Path, gap: float = 0.0) -> bool:
        """Play only after the prior clip ends and its post-play gap elapses.

        Calling this every render tick is safe: an active clip is never cut off.
        If callers stop requesting after a drag release, the current clip still
        reaches EOS naturally but no later clip is started.
        """
        if not self.enabled or not path.is_file():
            return False
        now = time.monotonic()
        self._poll_channel(channel, now)
        if channel in self.playing or now < self.ready_at.get(channel, 0.0):
            return False
        self.end_gaps[channel] = max(0.0, gap)
        return self._start(channel, path)

    def start_loop(self, channel: str, path: Path, volume: float) -> None:
        """Keep an effect looping and ramp toward new volume without restarting."""
        if not self.enabled or not path.is_file():
            return
        self.looping.add(channel)
        self.paths[channel] = path
        self.volume_targets[channel] = volume
        if channel not in self.playing:
            # A newly entered danger stage begins quietly at its affinity-5
            # level. Later affinity changes update only the target and tick()
            # performs the audible transition continuously.
            self.channel_volumes[channel] = volume
            self._start(channel, path, volume)

    def _poll_channel(self, channel: str, now: float) -> None:
        """Consume a non-blocking EOS/error message for one channel."""
        player = self.players.get(channel)
        if player is None or channel not in self.playing:
            return
        message = player.get_bus().pop_filtered(Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if message is None:
            return
        if message.type == Gst.MessageType.EOS and channel in self.looping:
            # Seek on the existing pipeline instead of tearing it down. This
            # avoids an audible gap when the danger rotor wraps to its first
            # sample and preserves the current affinity-driven volume.
            player.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            player.set_state(Gst.State.PLAYING)
            return
        player.set_state(Gst.State.NULL)
        self.playing.discard(channel)
        if message.type == Gst.MessageType.EOS:
            self.ready_at[channel] = now + self.end_gaps.get(channel, 0.0)
        else:
            self.looping.discard(channel)

    def tick(self, now: float) -> None:
        """Poll completion, restart loops, and smoothly approach target levels."""
        if not self.enabled:
            return
        elapsed = max(0.0, now - self.last_volume_tick)
        self.last_volume_tick = now
        for channel, target in tuple(self.volume_targets.items()):
            current = self.channel_volumes.get(channel, target)
            updated = approach_volume(
                current, target, elapsed, AUDIO_VOLUME_RAMP_PER_SECOND
            )
            self.channel_volumes[channel] = updated
            player = self.players.get(channel)
            if player is not None and channel in self.playing:
                player.set_property("volume", updated)
        for channel in tuple(self.playing):
            self._poll_channel(channel, now)
        for channel in tuple(self.looping):
            if channel not in self.playing and channel in self.paths:
                self._start(channel, self.paths[channel])

    def stop(self, channel: str) -> None:
        """Stop one sound immediately, used when chain movement has completed."""
        player = self.players.get(channel)
        if player is not None:
            player.set_state(Gst.State.NULL)
        self.playing.discard(channel)
        self.looping.discard(channel)
        self.ready_at.pop(channel, None)
        self.end_gaps.pop(channel, None)
        self.channel_volumes.pop(channel, None)
        self.volume_targets.pop(channel, None)

    def stop_all(self) -> None:
        """Release audio devices before the GTK process exits."""
        for player in self.players.values():
            player.set_state(Gst.State.NULL)
        self.players.clear()
        self.playing.clear()
        self.looping.clear()
        self.paths.clear()
        self.channel_volumes.clear()
        self.volume_targets.clear()
        self.ready_at.clear()
        self.end_gaps.clear()


class HeartEffect(Gtk.Window):
    """A click-through heart that rises beside and partly over the pet's head."""

    # This effect deliberately has no image asset: drawing it with Cairo keeps
    # it sharp at every pet scale and makes each click independent/stackable.

    def __init__(self, owner: "PetWindow") -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.owner = owner
        self.started = time.monotonic()
        pet_x, pet_y = owner.get_position()
        pet_height = owner.get_allocated_height() or owner.frames[0].height
        self.base_x, self.base_y = heart_effect_origin(
            pet_x,
            pet_y,
            owner.info_anchor_x,
            pet_height,
            owner.scale,
        )
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_app_paintable(True)
        self.set_default_size(56, 72)
        # Transiency makes the short-lived heart obey Show Desktop together with
        # the main pet instead of remaining as an independent top-level window.
        self.set_transient_for(owner)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.connect("draw", self._draw)
        self.connect("realize", self._make_click_through)
        self.show_all()
        GLib.timeout_add(16, self._tick)

    def _make_click_through(self, *_args) -> None:
        """Prevent the decorative heart from stealing pointer events."""
        gdk_window = self.get_window()
        if gdk_window is not None:
            gdk_window.set_pass_through(True)

    def _tick(self) -> bool:
        """Move upward on a small arc and self-destruct after one second."""
        progress = (time.monotonic() - self.started) / HEART_DURATION_SECONDS
        if progress >= 1.0:
            self.destroy()
            self.owner._forget_heart(self)
            return False
        self.move(
            self.base_x + round(math.sin(progress * math.pi) * 8),
            self.base_y - round(progress * 66),
        )
        self.queue_draw()
        return True

    def _draw(self, _widget: Gtk.Widget, cr) -> bool:
        """Draw a scalable Bézier heart with fade-in and fade-out."""
        progress = min(1.0, (time.monotonic() - self.started) / HEART_DURATION_SECONDS)
        alpha = min(1.0, progress * 5.0) * (1.0 - progress) ** 0.55
        scale = 0.72 + 0.36 * math.sin(progress * math.pi)
        cr.set_operator(1)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)
        cr.translate(28, 28)
        cr.scale(scale, scale)
        cr.move_to(0, 19)
        cr.curve_to(-28, 2, -21, -19, -8, -19)
        cr.curve_to(-2, -19, 0, -14, 0, -10)
        cr.curve_to(0, -14, 3, -19, 9, -19)
        cr.curve_to(23, -19, 28, 2, 0, 19)
        cr.close_path()
        cr.set_source_rgba(1.0, 0.03, 0.12, alpha)
        cr.fill_preserve()
        cr.set_line_width(2.2)
        cr.set_source_rgba(0.58, 0.0, 0.04, alpha)
        cr.stroke()
        return True


class LockdownOverlay(Gtk.Window):
    """Full-monitor chains, draggable key, and animated padlock release."""

    # Chains, lock, key, and explosion are procedural Cairo artwork rather than
    # bitmap files. Keeping the overlay self-contained also lets it cover the
    # monitor that currently owns the pet when it has been dragged cross-screen.

    EXPLOSION_SECONDS = 0.85
    CHAINS_IN_SECONDS = 1.35
    KEY_UNLOCK_SECONDS = 1.15
    CHAINS_OUT_SECONDS = 0.85

    def __init__(self, owner: "PetWindow") -> None:
        super().__init__(type=Gtk.WindowType.POPUP)
        self.owner = owner
        self.phase = "explosion"
        self.phase_started = time.monotonic()
        self.chain_progress = 0.0
        self.key_dragging = False
        self.key_offset = (0.0, 0.0)
        self.key_position = (100.0, 90.0)
        self.key_insert_start = self.key_position

        pet_x, pet_y = owner.get_position()
        pet_width = owner.get_allocated_width() or owner.frames[0].width
        pet_height = owner.get_allocated_height() or owner.frames[0].height
        screen = owner.get_screen()
        monitor = screen.get_monitor_at_point(
            pet_x + pet_width // 2, pet_y + pet_height // 2
        )
        self.workarea = screen.get_monitor_workarea(monitor)

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.set_default_size(self.workarea.width, self.workarea.height)
        self.move(self.workarea.x, self.workarea.y)
        self.resize(self.workarea.width, self.workarea.height)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("key-press-event", self._on_key)
        self.show_all()
        self.present()
        GLib.timeout_add(16, self._tick)

    def _pet_rect_local(self) -> tuple[float, float, float, float]:
        """Convert the pet's root coordinates into this monitor-local overlay."""
        pet_x, pet_y = self.owner.get_position()
        width = self.owner.get_allocated_width() or self.owner.frames[0].width
        height = self.owner.get_allocated_height() or self.owner.frames[0].height
        return (
            pet_x - self.workarea.x,
            pet_y - self.workarea.y,
            float(width),
            float(height),
        )

    def _pet_head_local(self) -> tuple[float, float]:
        """Return the explosion origin near the visible head rather than window top."""
        x, y, width, height = self._pet_rect_local()
        return x + width * 0.5, y + height * 0.34

    def _randomize_key(self) -> None:
        """Place the key away from both the lock and pet so dragging is required."""
        center = (self.workarea.width / 2, self.workarea.height / 2)
        pet_x, pet_y, pet_w, pet_h = self._pet_rect_local()
        candidates: list[tuple[float, float]] = []
        for _ in range(40):
            x = random.uniform(70, max(71, self.workarea.width - 70))
            y = random.uniform(80, max(81, self.workarea.height - 80))
            far_from_lock = math.hypot(x - center[0], y - center[1]) > 150
            outside_pet = not (
                pet_x - 90 <= x <= pet_x + pet_w + 90
                and pet_y - 90 <= y <= pet_y + pet_h + 90
            )
            if far_from_lock and outside_pet:
                self.key_position = (x, y)
                return
            candidates.append((x, y))
        self.key_position = candidates[-1] if candidates else (100.0, 100.0)

    def _set_phase(self, phase: str) -> None:
        """Enter one lockdown phase and reset its monotonic animation clock."""
        previous_phase = self.phase
        self.phase = phase
        self.phase_started = time.monotonic()
        if phase == "chains_in":
            self.owner.audio.play("chains", AUDIO_CHAINS)
        elif previous_phase == "chains_in":
            # The metal collision belongs only to moving chains. Stop it at the
            # exact phase boundary even if the source file has audio remaining.
            self.owner.audio.stop("chains")
        if phase == "locked":
            self.chain_progress = 1.0
            self._randomize_key()
        self.queue_draw()

    def _tick(self) -> bool:
        """Advance explosion → chain-in → locked → key-turn → chain-out."""
        elapsed = time.monotonic() - self.phase_started
        if self.phase == "explosion" and elapsed >= self.EXPLOSION_SECONDS:
            self._set_phase("chains_in")
        elif self.phase == "chains_in":
            self.chain_progress = min(1.0, elapsed / self.CHAINS_IN_SECONDS)
            if self.chain_progress >= 1.0:
                self._set_phase("locked")
        elif self.phase == "key_unlock" and elapsed >= self.KEY_UNLOCK_SECONDS:
            self._set_phase("chains_out")
        elif self.phase == "chains_out":
            self.chain_progress = max(0.0, 1.0 - elapsed / self.CHAINS_OUT_SECONDS)
            if self.chain_progress <= 0.0:
                self.owner._finish_unlock()
                self.destroy()
                return False
        self.queue_draw()
        return True

    def _on_key(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        """Keep an emergency Escape route even while the overlay blocks input."""
        if event.keyval == Gdk.KEY_Escape:
            self.owner.destroy()
            return True
        return False

    def _on_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        """Begin dragging when the pointer hits the key bow or shaft."""
        if event.button != 1 or self.phase != "locked":
            return True
        key_x, key_y = self.key_position
        if key_x - 78 <= event.x <= key_x + 18 and abs(event.y - key_y) <= 34:
            self.key_dragging = True
            self.key_offset = (event.x - key_x, event.y - key_y)
        return True

    def _on_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        """Move the key while clamping all of its artwork inside the monitor."""
        if not self.key_dragging or self.phase != "locked":
            return True
        self.key_position = (
            max(82.0, min(self.workarea.width - 22.0, event.x - self.key_offset[0])),
            max(38.0, min(self.workarea.height - 38.0, event.y - self.key_offset[1])),
        )
        self.queue_draw()
        return True

    def _on_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        """Insert the key only when its released tip overlaps the keyhole."""
        if event.button != 1 or not self.key_dragging:
            return True
        self.key_dragging = False
        key_x, key_y = self.key_position
        if key_hits_lock(key_x, key_y, self.workarea.width, self.workarea.height):
            self.key_insert_start = (key_x, key_y)
            self.owner.audio.play("mechanism", AUDIO_UNLOCK)
            self._set_phase("key_unlock")
        return True

    @staticmethod
    def _draw_chain_segment(cr, start: tuple[float, float], end: tuple[float, float]) -> None:
        """Render one growing chain half as dark cable plus bright dashed links."""
        cr.set_line_cap(1)
        cr.move_to(*start)
        cr.line_to(*end)
        cr.set_line_width(15)
        cr.set_source_rgba(0.05, 0.06, 0.07, 0.92)
        cr.stroke_preserve()
        cr.set_dash([13, 8], 0)
        cr.set_line_width(7)
        cr.set_source_rgba(0.70, 0.73, 0.77, 1.0)
        cr.stroke()
        cr.set_dash([], 0)

    @staticmethod
    def _draw_lock(
        cr, x: float, y: float, alpha: float = 1.0, unlock_progress: float = 0.0
    ) -> None:
        """Draw a recognizable heavy padlock whose shackle visibly springs open."""
        # The shackle pivots from its left mounting point during the final part
        # of the key turn, making the successful unlock readable at a glance.
        cr.save()
        cr.translate(x - 30, y - 23)
        cr.rotate(-0.52 * unlock_progress)
        cr.set_line_cap(1)
        cr.set_line_join(1)
        cr.set_line_width(14)
        cr.set_source_rgba(0.12, 0.13, 0.15, alpha)
        cr.move_to(0, 4)
        cr.line_to(0, -31)
        cr.curve_to(0, -70, 60, -70, 60, -31)
        cr.line_to(60, 4)
        cr.stroke()
        cr.set_line_width(7)
        cr.set_source_rgba(0.76, 0.78, 0.82, alpha)
        cr.move_to(0, 3)
        cr.line_to(0, -31)
        cr.curve_to(0, -60, 60, -60, 60, -31)
        cr.line_to(60, 3)
        cr.stroke()
        cr.restore()

        # A broad rounded-looking body, double outline and classic keyhole make
        # the silhouette unambiguously read as a padlock even on a large screen.
        cr.set_line_join(1)
        cr.rectangle(x - 46, y - 27, 92, 76)
        cr.set_source_rgba(0.97, 0.66, 0.04, alpha)
        cr.fill_preserve()
        cr.set_line_width(7)
        cr.set_source_rgba(0.20, 0.13, 0.01, alpha)
        cr.stroke()
        cr.rectangle(x - 35, y - 17, 70, 9)
        cr.set_source_rgba(1.0, 0.84, 0.24, 0.72 * alpha)
        cr.fill()
        cr.arc(x, y + 6, 9, 0, 2 * math.pi)
        cr.set_source_rgba(0.10, 0.09, 0.08, alpha)
        cr.fill()
        cr.move_to(x - 5, y + 10)
        cr.line_to(x + 5, y + 10)
        cr.line_to(x + 8, y + 29)
        cr.line_to(x - 8, y + 29)
        cr.close_path()
        cr.fill()

    @staticmethod
    def _draw_key(cr, x: float, y: float, rotation: float = 0.0) -> None:
        """Draw a large traditional gold key whose tip is the drag anchor."""
        cr.save()
        cr.translate(x, y)
        cr.rotate(rotation)
        cr.set_line_cap(1)
        cr.set_line_join(1)
        cr.set_line_width(11)
        cr.set_source_rgba(0.98, 0.70, 0.08, 1.0)
        cr.move_to(-48, 0)
        cr.line_to(-3, 0)
        cr.stroke()
        cr.arc(-63, 0, 19, 0, 2 * math.pi)
        cr.set_line_width(10)
        cr.stroke()
        cr.set_line_width(5)
        cr.set_source_rgba(0.30, 0.18, 0.01, 1.0)
        cr.move_to(-48, 0)
        cr.line_to(-3, 0)
        cr.stroke()
        cr.set_source_rgba(0.98, 0.70, 0.08, 1.0)
        cr.move_to(-18, 1)
        cr.line_to(-18, 17)
        cr.line_to(-8, 17)
        cr.line_to(-8, 7)
        cr.line_to(0, 7)
        cr.line_to(0, -6)
        cr.close_path()
        cr.fill_preserve()
        cr.set_line_width(3)
        cr.set_source_rgba(0.30, 0.18, 0.01, 1.0)
        cr.stroke()
        cr.restore()

    def _draw_explosion(self, cr, elapsed: float) -> None:
        """Draw a short expanding starburst over the pet's head."""
        progress = min(1.0, elapsed / self.EXPLOSION_SECONDS)
        cx, cy = self._pet_head_local()
        radius = 18 + 92 * math.sin(progress * math.pi)
        alpha = max(0.0, 1.0 - progress ** 1.6)
        points = 20
        cr.move_to(cx + radius, cy)
        for index in range(1, points + 1):
            angle = index * math.pi * 2 / points
            spoke = radius if index % 2 == 0 else radius * 0.46
            cr.line_to(cx + math.cos(angle) * spoke, cy + math.sin(angle) * spoke)
        cr.close_path()
        cr.set_source_rgba(1.0, 0.24 + 0.55 * progress, 0.0, alpha)
        cr.fill()

    def _draw(self, _widget: Gtk.Widget, cr) -> bool:
        """Composite the current lockdown phase over a transparent monitor window."""
        cr.set_operator(1)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)
        now = time.monotonic()
        elapsed = now - self.phase_started
        if self.phase == "explosion":
            self._draw_explosion(cr, elapsed)
            return True

        width, height = self.workarea.width, self.workarea.height
        center = (width / 2, height / 2)
        progress = self.chain_progress
        for corner in ((0.0, 0.0), (width, height), (width, 0.0), (0.0, height)):
            end = (
                corner[0] + (center[0] - corner[0]) * progress,
                corner[1] + (center[1] - corner[1]) * progress,
            )
            self._draw_chain_segment(cr, corner, end)

        unlock_progress = 1.0 if self.phase == "chains_out" else 0.0
        key_x = key_y = key_rotation = None
        if self.phase == "key_unlock":
            t = min(1.0, elapsed / self.KEY_UNLOCK_SECONDS)
            insert_t = min(1.0, t / 0.28)
            insert_eased = insert_t * insert_t * (3.0 - 2.0 * insert_t)
            turn_t = max(0.0, min(1.0, (t - 0.25) / 0.48))
            turn_eased = turn_t * turn_t * (3.0 - 2.0 * turn_t)
            open_t = max(0.0, min(1.0, (t - 0.62) / 0.38))
            unlock_progress = open_t * open_t * (3.0 - 2.0 * open_t)
            start_x, start_y = self.key_insert_start
            key_x = start_x + (center[0] - start_x) * insert_eased
            key_y = start_y + (center[1] + 6 - start_y) * insert_eased
            key_rotation = turn_eased * math.pi / 2

        if self.phase in {"locked", "key_unlock", "chains_out"}:
            self._draw_lock(
                cr, *center, alpha=max(0.0, progress), unlock_progress=unlock_progress
            )
        if self.phase == "locked":
            self._draw_key(cr, *self.key_position)
        elif self.phase == "key_unlock":
            self._draw_key(cr, key_x, key_y, key_rotation)
        return True


class PetWindow(Gtk.Window):
    """Main pet window and owner of every interactive runtime state.

    ``animation_name`` selects the source sequence, while ``frame_index`` keeps
    the exact phase inside that sequence. Hover, drag, affinity and Codex status
    are orthogonal flags; keeping them separate avoids visual jumps when one
    interaction interrupts another.
    """
    def __init__(
        self,
        animations: dict[str, list[AnimationFrame]],
        scale: float,
        initial_affection: int = AFFECTION_DEFAULT,
        audio_enabled: bool = True,
    ) -> None:
        super().__init__(title="Swing Pet")
        self.animations = animations
        self.animation_name = "swing"
        self.frames = animations[self.animation_name]
        self.scale = scale
        self.sound_enabled = bool(audio_enabled)
        self.audio = AudioManager(enabled=audio_enabled)
        # Keep overlays anchored to the lowest/vertical swing pose. Individual
        # animation frames move inside the wide transparent canvas, but UI must
        # remain stable while only the pet swings underneath it.
        self.info_anchor_x = animations["swing"][DRAG_POSE_INDEX["bottom"]].visible_center_x
        self.frame_index = 0
        self.frame_elapsed = 0.0
        self.last_tick = time.monotonic()
        self._tick_source: int | None = None
        self.pointer_inside = False
        self.pending_hover = False
        self.pending_return = False
        self.drag_candidate = False
        self.dragging = False
        self.drag_alert_active = False
        self.drag_start_root = (0.0, 0.0)
        self.drag_last_root = (0.0, 0.0)
        self.drag_origin = (0, 0)
        self.drag_window_position: tuple[int, int] | None = None
        # GDK can emit hundreds of motion events per second. Keep only the most
        # recent root coordinate; the render tick consumes it at a bounded rate.
        self.pending_drag_root: tuple[float, float] | None = None
        self.drag_target_pose: str | None = None
        self.drag_transition_path: list[int] = []
        self.drag_transition_elapsed = 0.0
        self.last_drag_motion = time.monotonic()
        self.suppress_hover_until_leave = False
        self.pet_state = "idle"
        self.bridge_connected = False
        self.bridge_detail = "Codex bridge is starting"
        now = time.monotonic()
        self.affection = clamp_affection(initial_affection)
        # This clock spans the complete 5..1 danger stage. Increasing affinity
        # above 5 cancels it; dropping into the range later starts a fresh ramp.
        self.danger_speed_started: float | None = (
            now if 0 < self.affection <= DANGER_AFFECTION_THRESHOLD else None
        )
        self.next_affection_decay = now + AFFECTION_DECAY_SECONDS
        self.drag_decay_carry = 0.0
        self.standing_decay_carry = 0.0
        self.hover_idle_since: float | None = None
        self.hearts: list[HeartEffect] = []
        self.lockdown: LockdownOverlay | None = None
        self.locked_out = False
        self.info_requested_visible = False
        self.owner_temporarily_hidden = False
        self._info_popup_size: tuple[int, int] | None = None
        self._last_info_markup: str | None = None
        self.tint_cache: dict[tuple[str, int, int], GdkPixbuf.Pixbuf] = {}

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.stick()
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(self.frames[0].width, self.frames[0].height)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)

        self.info_popup, self.info_label = self._build_info_popup()
        self.image = Gtk.Image.new_from_pixbuf(self.frames[0].pixbuf)
        self.add(self.image)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.VISIBILITY_NOTIFY_MASK
        )
        self.connect("draw", self._draw_transparent)
        self.connect("button-press-event", self._on_button_press)
        self.connect("button-release-event", self._on_button_release)
        self.connect("motion-notify-event", self._on_pointer_motion)
        self.connect("enter-notify-event", self._on_pointer_enter)
        self.connect("leave-notify-event", self._on_pointer_leave)
        self.connect("key-press-event", self._on_key_press)
        self.connect("configure-event", self._on_configure)
        self.connect("window-state-event", self._on_window_state)
        self.connect("visibility-notify-event", self._on_visibility_notify)
        self.connect("map-event", self._on_map)
        self.connect("unmap-event", self._on_unmap)
        self.connect("realize", self._place_bottom_right)
        self.connect("destroy", self._on_destroy)
        self._schedule_tick(0)
        self._refresh_info()
        self._sync_danger_audio()

    def _next_tick_delay_ms(self) -> int:
        """Choose the cheapest cadence that still preserves visible motion.

        The source swing is normally about 25 FPS, so polling at 30 FPS is
        sufficient while idle. Faster Codex states, jump transitions, and live
        dragging use a tighter cadence only for as long as they need it.
        """
        if self.locked_out:
            return 100
        if self.owner_temporarily_hidden:
            return 250
        if self.drag_candidate or self.dragging or self.drag_transition_path:
            return TICK_INTERACTIVE_MS
        if self.animation_name in {"jump_down", "jump_up"}:
            return TICK_INTERACTIVE_MS
        if self.animation_name == "swing":
            speed = STATE_SPEED[self.pet_state]
            if self.danger_speed_started is not None:
                speed *= danger_speed_multiplier(
                    self.affection, time.monotonic() - self.danger_speed_started
                )
            return TICK_ACTIVE_MS if speed > 1.15 else TICK_IDLE_MS
        return TICK_IDLE_MS

    def _schedule_tick(self, delay_ms: int | None = None) -> None:
        """Schedule exactly one one-shot tick, avoiding permanent 60 Hz wakeups."""
        if self._tick_source is not None:
            return
        delay = self._next_tick_delay_ms() if delay_ms is None else delay_ms
        self._tick_source = GLib.timeout_add(max(0, delay), self._run_scheduled_tick)

    def _run_scheduled_tick(self) -> bool:
        """Run a state update, then select the next adaptive wakeup interval."""
        self._tick_source = None
        if self._tick():
            self._schedule_tick()
        return False

    def _build_info_popup(self) -> tuple[Gtk.Window, Gtk.Label]:
        """Create the click-through status bubble shown above the pet."""
        popup = Gtk.Window(type=Gtk.WindowType.POPUP)
        popup.set_decorated(False)
        popup.set_resizable(False)
        popup.set_keep_above(True)
        popup.set_skip_taskbar_hint(True)
        popup.set_skip_pager_hint(True)
        popup.set_accept_focus(False)
        popup.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        # Make the bubble a real child in the window-manager hierarchy so
        # Super/Start+D cannot leave it behind after hiding the pet window.
        popup.set_transient_for(self)
        popup.set_name("pet-info-popup")

        label = Gtk.Label()
        label.set_name("pet-info-label")
        label.set_justify(Gtk.Justification.CENTER)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(8)
        label.set_margin_bottom(8)
        popup.add(label)

        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"#pet-info-popup { background-color: rgba(28, 28, 30, 0.94); "
            b"border-radius: 10px; } #pet-info-label { color: #ffffff; font-size: 14px; "
            b"font-weight: bold; } #pet-info-label.drag-alert { color: #ffe600; "
            b"font-size: 22px; font-weight: 900; } #pet-info-label.standing-quip { "
            b"color: #ffffff; font-size: 18px; font-weight: bold; }"
        )
        popup.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        label.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        popup.connect("realize", self._make_popup_click_through)
        return popup, label

    def _make_popup_click_through(self, popup: Gtk.Window) -> None:
        """Let clicks reach the pet even when the bubble overlaps its window."""
        gdk_window = popup.get_window()
        if gdk_window is not None:
            gdk_window.set_pass_through(True)

    def _position_info_popup(self) -> bool:
        """Follow the pet and clamp the bubble to the pet's current monitor."""
        if not self.info_popup.get_visible():
            return False
        if self.dragging and self.drag_window_position is not None:
            root_x, root_y = self.drag_window_position
        else:
            root_x, root_y = self.get_position()
        pet_width = self.get_allocated_width() or self.frames[0].width
        pet_height = self.get_allocated_height() or self.frames[0].height
        if self._info_popup_size is None:
            popup_size = self.info_popup.get_preferred_size()[1]
            self._info_popup_size = (popup_size.width, popup_size.height)
        popup_width, popup_height = self._info_popup_size
        # The APNG uses a wide transparent canvas so the standing pose has room.
        # Use one calibrated visual anchor rather than the current frame bounds;
        # the bubble follows window dragging but never oscillates with the GIF.
        x = centered_popup_x(root_x, self.info_anchor_x, popup_width)
        y = root_y - popup_height - round(10 * self.scale)

        # Clamp against the monitor currently containing the pet, not the primary
        # monitor. This keeps the bubble attached when crossing screen boundaries.
        screen = self.get_screen()
        monitor = screen.get_monitor_at_point(
            root_x + pet_width // 2, root_y + pet_height // 2
        )
        workarea = screen.get_monitor_workarea(monitor)
        x = max(workarea.x + 4, min(x, workarea.x + workarea.width - popup_width - 4))
        y = max(workarea.y + 4, min(y, workarea.y + workarea.height - popup_height - 4))
        self.info_popup.move(x, y)
        return False

    def _on_configure(self, *_args) -> bool:
        """Reposition the detached popup after window-manager movement."""
        if self.info_popup.get_visible() and not self.dragging:
            GLib.idle_add(self._position_info_popup)
        return False

    def _restore_info_if_requested(self) -> bool:
        """Restore the bubble only after the owner is mapped and visible again."""
        if (
            self.info_requested_visible
            and not self.owner_temporarily_hidden
            and not self.locked_out
            and self.get_mapped()
        ):
            self.info_popup.show_all()
            self._position_info_popup()
        return False

    def _set_owner_temporarily_hidden(self, hidden: bool) -> None:
        """Synchronize auxiliary windows with desktop/window-manager visibility."""
        self.owner_temporarily_hidden = hidden
        if hidden:
            self.info_popup.hide()
            for heart in self.hearts:
                heart.hide()
        else:
            GLib.idle_add(self._restore_info_if_requested)

    def _on_window_state(self, _widget: Gtk.Widget, event: Gdk.EventWindowState) -> bool:
        """Hide the bubble when Show Desktop withdraws or iconifies the pet."""
        self._set_owner_temporarily_hidden(
            window_state_hides_auxiliary(event.new_window_state)
        )
        return False

    def _on_visibility_notify(
        self, _widget: Gtk.Widget, event: Gdk.EventVisibility
    ) -> bool:
        """Cover desktop environments that obscure rather than iconify windows."""
        hidden = event.state == Gdk.VisibilityState.FULLY_OBSCURED
        if hidden or not window_state_hides_auxiliary(self.get_window().get_state()):
            self._set_owner_temporarily_hidden(hidden)
        return False

    def _on_map(self, *_args) -> bool:
        """Restore requested UI after the pet returns from Show Desktop."""
        self._set_owner_temporarily_hidden(False)
        self._schedule_tick(0)
        return False

    def _on_unmap(self, *_args) -> bool:
        """Hide every auxiliary window before the owner leaves the desktop."""
        self._set_owner_temporarily_hidden(True)
        return False

    def _show_info(self) -> None:
        """Show and immediately attach the status popup to the current position."""
        self.info_requested_visible = True
        if self.owner_temporarily_hidden or self.locked_out or not self.get_mapped():
            return
        if not self.info_popup.get_visible():
            self.info_popup.show_all()
        self._position_info_popup()

    def _hide_info(self) -> None:
        """Hide the bubble while the full-screen lockdown owns presentation."""
        self.info_requested_visible = False
        self.info_popup.hide()

    def _on_destroy(self, *_args) -> None:
        """Destroy auxiliary windows before stopping the GTK event loop."""
        self.audio.stop_all()
        for heart in list(self.hearts):
            heart.destroy()
        self.hearts.clear()
        if self.lockdown is not None:
            self.lockdown.destroy()
            self.lockdown = None
        self.info_popup.destroy()
        Gtk.main_quit()

    def _forget_heart(self, heart: HeartEffect) -> None:
        """Remove a completed transient effect from the ownership list."""
        if heart in self.hearts:
            self.hearts.remove(heart)

    def _spawn_heart(self) -> None:
        """Create one independent heart so rapid clicks may overlap naturally."""
        heart = HeartEffect(self)
        self.hearts.append(heart)

    def _set_audio_enabled(self, enabled: bool) -> None:
        """Apply and persist the right-click sound preference immediately."""
        self.sound_enabled = bool(enabled)
        self.audio.set_enabled(self.sound_enabled)
        try:
            save_sound_setting(self.sound_enabled)
        except OSError:
            # A read-only or unavailable config directory should not make the
            # desktop pet unusable; the switch still applies for this process.
            pass
        self._sync_danger_audio()

    def _show_affection_dialog(self, *_args) -> None:
        """Prompt for an exact 0..1000 affinity value from the context menu."""
        self.hover_idle_since = None
        dialog = Gtk.Dialog(
            title="设置亲密度",
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
        dialog.add_buttons(
            "取消",
            Gtk.ResponseType.CANCEL,
            "确定",
            Gtk.ResponseType.OK,
        )
        content = dialog.get_content_area()
        content.set_spacing(10)
        content.set_border_width(14)
        label = Gtk.Label(label=f"请输入亲密度（0–{AFFECTION_MAX}）：")
        label.set_xalign(0.0)
        spin = Gtk.SpinButton.new_with_range(0, AFFECTION_MAX, 1)
        spin.set_value(self.affection)
        spin.set_numeric(True)
        spin.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        content.pack_start(label, False, False, 0)
        content.pack_start(spin, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        selected = int(spin.get_value())
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            # Manual adjustment starts a fresh timing period, avoiding an
            # immediate decay caused by carry accumulated before the dialog.
            self.next_affection_decay = time.monotonic() + AFFECTION_DECAY_SECONDS
            self.drag_decay_carry = 0.0
            self.standing_decay_carry = 0.0
            self._set_affection(selected)

    def _record_interaction(self, affection_delta: int = 0) -> None:
        """Reset ordinary idle decay and optionally apply a click reward."""
        self.next_affection_decay = time.monotonic() + AFFECTION_DECAY_SECONDS
        if affection_delta:
            self._set_affection(self.affection + affection_delta)

    def _sync_danger_audio(self) -> None:
        """Start, stop, or smoothly retune the low-affinity rotor loop."""
        volume = danger_rotor_volume(self.affection)
        if volume > 0.0 and not self.locked_out:
            self.audio.start_loop("rotor", AUDIO_ROTOR, volume)
        else:
            self.audio.stop("rotor")

    def _cancel_low_affection_motion(self) -> None:
        """Cancel drag/hover state and return directly to a safe swing pose.

        This intentionally does not play jump-up: once affinity reaches 5 the
        only accepted interaction is a recovery click, so even a transition
        animation must not continue as an indirect hover response.
        """
        self.drag_candidate = False
        self.dragging = False
        self.drag_alert_active = False
        self.pending_drag_root = None
        self.drag_window_position = None
        self.drag_target_pose = None
        self.drag_transition_path.clear()
        self.drag_transition_elapsed = 0.0
        self.pending_hover = False
        self.pending_return = False
        self.hover_idle_since = None
        self.drag_decay_carry = 0.0
        self.standing_decay_carry = 0.0
        if self.animation_name != "swing":
            self.animation_name = "swing"
            self.frames = self.animations["swing"]
            self.frame_index = DRAG_POSE_INDEX["bottom"]
            self.frame_elapsed = 0.0
            self._display_frame()

    def _set_affection(self, value: int) -> None:
        """Store affinity without synchronously redrawing a moving swing frame."""
        value = clamp_affection(value)
        if value == self.affection:
            return
        was_in_danger = 0 < self.affection <= DANGER_AFFECTION_THRESHOLD
        self.affection = value
        is_in_danger = 0 < self.affection <= DANGER_AFFECTION_THRESHOLD
        if low_affection_blocks_motion(self.affection):
            self._cancel_low_affection_motion()
        if is_in_danger and not was_in_danger:
            self.danger_speed_started = time.monotonic()
        elif not is_in_danger:
            self.danger_speed_started = None
        self._sync_danger_audio()
        # Keep tints keyed by affection instead of clearing them on each point
        # loss. More importantly, do not redraw the active swing frame here:
        # the next scheduled GIF frame picks up the new tint within 40 ms. This
        # avoids a Python pixel loop and GTK popup relayout at every affinity
        # decrement, which previously made the half-second danger ramp hitch.
        if self.animation_name != "swing":
            self._display_frame()
        self._refresh_info(reposition=False)
        if self.affection == 0 and not self.locked_out:
            self._begin_lockdown()

    def _tinted_pixbuf(self, frame: AnimationFrame) -> GdkPixbuf.Pixbuf:
        """Return a cached red-tinted frame using Pillow's C-level point tables."""
        tint = low_affection_tint(self.affection)
        if tint <= 0.0:
            return frame.pixbuf
        key = (self.animation_name, self.frame_index, self.affection)
        cached = self.tint_cache.get(key)
        if cached is not None:
            return cached
        red_mix = 0.72 * tint
        darken = 1.0 - 0.72 * tint
        red_table = [min(255, round(value + (255 - value) * red_mix)) for value in range(256)]
        dark_table = [round(value * darken) for value in range(256)]
        image = Image.frombytes("RGBA", (frame.width, frame.height), frame.rgba)
        red, green, blue, alpha = image.split()
        image = Image.merge(
            "RGBA",
            (red.point(red_table), green.point(dark_table), blue.point(dark_table), alpha),
        )
        pixbuf = image_to_pixbuf(image)
        self.tint_cache[key] = pixbuf
        return pixbuf

    def _display_frame(self, index: int | None = None) -> None:
        """Display one frame through the affinity-aware rendering path."""
        if index is not None:
            self.frame_index = index
        self.image.set_from_pixbuf(self._tinted_pixbuf(self.frames[self.frame_index]))

    def _begin_lockdown(self) -> None:
        """Freeze at the vertical swing pose and hand control to the overlay."""
        self.locked_out = True
        # Stop the escalating rotor before the louder explosion begins, keeping
        # the transition distinct and preventing the two low-frequency effects
        # from masking each other.
        self.audio.stop("rotor")
        self.drag_candidate = False
        self.dragging = False
        self.drag_alert_active = False
        self.drag_transition_path.clear()
        self.hover_idle_since = None
        self.pending_hover = False
        self.pending_return = False
        self.animation_name = "swing"
        self.frames = self.animations["swing"]
        self.frame_index = DRAG_POSE_INDEX["bottom"]
        self.frame_elapsed = 0.0
        self._display_frame()
        self._hide_info()
        self.audio.play("impact", AUDIO_EXPLOSION, AUDIO_EXPLOSION_VOLUME)
        self.lockdown = LockdownOverlay(self)

    def _finish_unlock(self) -> None:
        """Restore affinity 100 and resume from the bottom of the swing cycle."""
        self.lockdown = None
        self.locked_out = False
        self.audio.stop("rotor")
        self.affection = AFFECTION_DEFAULT
        self.danger_speed_started = None
        self.tint_cache.clear()
        self.next_affection_decay = time.monotonic() + AFFECTION_DECAY_SECONDS
        self.drag_decay_carry = 0.0
        self.standing_decay_carry = 0.0
        self.animation_name = "swing"
        self.frames = self.animations["swing"]
        self.frame_index = DRAG_POSE_INDEX["bottom"]
        self.frame_elapsed = 0.0
        self.last_tick = time.monotonic()
        self._display_frame()
        self._refresh_info()
        self._show_info()

    def _draw_transparent(self, _widget: Gtk.Widget, cr) -> bool:
        """Clear the undecorated window to alpha before GTK paints the image."""
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(1)
        cr.paint()
        cr.set_operator(2)
        return False

    def _place_bottom_right(self, *_args) -> None:
        """Choose a deterministic initial position inside the primary work area."""
        screen = self.get_screen()
        monitor = screen.get_primary_monitor()
        geometry = screen.get_monitor_workarea(monitor)
        margin = round(24 * self.scale)
        self.move(
            geometry.x + geometry.width - self.frames[0].width - margin,
            geometry.y + geometry.height - self.frames[0].height - margin,
        )

    def _on_button_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        """Start a possible left drag or open the right-click status menu."""
        if self.locked_out:
            return True
        if self.owner_temporarily_hidden:
            # Keep the state machine alive at a low cadence while the desktop
            # hides the pet, but avoid decoding/committing invisible frames.
            return True
        if event.button == 1:
            if not self._point_hits_visible_pet(event.x, event.y):
                return False
            self.pending_hover = False
            self.hover_idle_since = None
            if low_affection_blocks_motion(self.affection):
                # Reward immediately without arming a drag gesture. Repeated
                # clicks can recover above 5, at which point normal interaction
                # becomes available again on the next press.
                self._record_interaction(1)
                self._spawn_heart()
                self._refresh_info()
                self._show_info()
                return True
            self._record_interaction()
            self.drag_candidate = True
            self.dragging = False
            self.drag_decay_carry = 0.0
            self.drag_start_root = (event.x_root, event.y_root)
            self.drag_last_root = self.drag_start_root
            self.drag_origin = self.get_position()
            return True
        if event.button == 3:
            self.hover_idle_since = None
            menu = Gtk.Menu()
            status = Gtk.MenuItem(label=STATE_LABEL[self.pet_state])
            status.set_sensitive(False)
            menu.append(status)
            affection = Gtk.MenuItem(label=f"亲密度：{self.affection} / {AFFECTION_MAX}")
            affection.set_sensitive(False)
            menu.append(affection)
            set_affection = Gtk.MenuItem(label="设置亲密度…")
            set_affection.connect("activate", self._show_affection_dialog)
            menu.append(set_affection)
            audio_item = Gtk.CheckMenuItem(label="开启音效")
            audio_item.set_active(self.sound_enabled)
            audio_item.connect(
                "toggled", lambda item: self._set_audio_enabled(item.get_active())
            )
            menu.append(audio_item)
            menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="退出宠物")
            quit_item.connect("activate", lambda *_: self.destroy())
            menu.append(quit_item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _on_button_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        """Resolve the gesture as click or drag and restart hover-idle timing."""
        if event.button != 1 or not self.drag_candidate:
            return False
        was_dragging = self.dragging
        self.drag_candidate = False
        self.dragging = False
        self.drag_alert_active = False
        self.drag_window_position = None
        self.pending_drag_root = None
        if was_dragging:
            # Finish any remaining interpolation path, then resume the GIF from
            # that exact phase instead of snapping to a predetermined first frame.
            self.frame_elapsed = 0.0
            self.last_tick = time.monotonic()
            self.suppress_hover_until_leave = False
        else:
            self.drag_transition_path.clear()
            self.drag_target_pose = None
            self._record_interaction(1)
            self._spawn_heart()
        self.drag_decay_carry = 0.0
        self.hover_idle_since = time.monotonic() if self.pointer_inside else None
        self._refresh_info()
        self._show_info()
        return True

    def _update_drag(self, root_x: float, root_y: float) -> bool:
        """Move the window and select a physically lagging swing target pose."""
        total_dx = root_x - self.drag_start_root[0]
        total_dy = root_y - self.drag_start_root[1]
        if not self.dragging and (total_dx * total_dx + total_dy * total_dy) ** 0.5 < DRAG_START_THRESHOLD:
            return True
        if not self.dragging:
            self.dragging = True
            self.drag_alert_active = True
            self.pending_hover = False
            self.pending_return = False
            self.suppress_hover_until_leave = True
            self.hover_idle_since = None
            self._record_interaction()
            self.drag_target_pose = None
            self.drag_transition_path.clear()
            self.drag_transition_elapsed = 0.0
            self.last_drag_motion = time.monotonic()
            self.audio.request("drag-voice", AUDIO_DRAG, DRAG_VOICE_GAP_SECONDS)
            self._refresh_info()
            self._show_info()

        new_position = (
            self.drag_origin[0] + round(total_dx),
            self.drag_origin[1] + round(total_dy),
        )
        self.drag_window_position = new_position
        self.move(*new_position)
        self._position_info_popup()
        step_dx = root_x - self.drag_last_root[0]
        step_dy = root_y - self.drag_last_root[1]
        self.drag_last_root = (root_x, root_y)

        # Any meaningful horizontal component wins for a diagonal drag. Moving the
        # window left makes the swing lag to the right, and vice versa.
        pose = drag_pose_for_delta(step_dx, step_dy)
        if pose is None:
            return True
        self.last_drag_motion = time.monotonic()
        self._record_interaction()
        if not self.drag_alert_active:
            self.drag_alert_active = True
            self._refresh_info()
        self._show_drag_pose(pose)
        return True

    def _show_drag_pose(self, pose: str) -> None:
        """Queue the shortest continuous path from current phase to drag target."""
        if pose == self.drag_target_pose:
            return
        if self.animation_name != "swing":
            self.animation_name = "swing"
            self.frames = self.animations["swing"]
            self.frame_index = DRAG_POSE_INDEX["bottom"]
            self._display_frame()
        self.drag_target_pose = pose
        self.drag_transition_path = shortest_cycle_path(
            self.frame_index, DRAG_POSE_INDEX[pose]
        )
        self.drag_transition_elapsed = 0.0
        self.frame_elapsed = 0.0
        self.last_tick = time.monotonic()

    def _advance_drag_transition(self, elapsed_ms: float) -> bool:
        """Consume queued intermediate frames without skipping the physical arc."""
        self.drag_transition_elapsed += elapsed_ms
        changed = False
        while (
            self.drag_transition_path
            and self.drag_transition_elapsed >= DRAG_TRANSITION_FRAME_MS
        ):
            self.drag_transition_elapsed -= DRAG_TRANSITION_FRAME_MS
            self.frame_index = self.drag_transition_path.pop(0)
            changed = True
        if changed:
            self._display_frame()
        return changed

    def _on_key_press(self, _widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        """Allow Escape to close the standalone pet."""
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _point_hits_visible_pet(self, x: float, y: float) -> bool:
        """Reject transparent-window clicks using the current frame alpha mask."""
        frame = self.frames[self.frame_index]
        px, py = int(x), int(y)
        if px < 0 or py < 0 or px >= frame.width or py >= frame.height:
            return False
        return frame.alpha[py * frame.width + px] >= 28

    def _on_pointer_enter(self, _widget: Gtk.Widget, event: Gdk.EventCrossing) -> bool:
        """Start the one-second dismount timer only over visible pet pixels."""
        if self.locked_out:
            return False
        self.pointer_inside = True
        if self._point_hits_visible_pet(event.x, event.y):
            self._show_info()
            self.hover_idle_since = (
                None
                if low_affection_blocks_motion(self.affection)
                else time.monotonic()
            )
        return False

    def _on_pointer_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        """Reset stationary hover timing or route motion into active dragging."""
        if self.locked_out:
            return True
        self.pointer_inside = True
        if low_affection_blocks_motion(self.affection):
            self._cancel_low_affection_motion()
            if self._point_hits_visible_pet(event.x, event.y):
                self._show_info()
            return False
        if self.drag_candidate:
            # Do not move a pair of top-level windows for every raw event. The
            # latest pointer position is applied by the adaptive render tick.
            self.pending_drag_root = (event.x_root, event.y_root)
            return True
        hit = self._point_hits_visible_pet(event.x, event.y)
        if hit:
            self._show_info()
            self.pending_hover = False
            self.hover_idle_since = time.monotonic()
        if self.animation_name == "swing":
            if not hit:
                self.pending_hover = False
                self.hover_idle_since = None
        elif self.animation_name == "standing" and not hit:
            # Transparent pixels do not count as hovering the pet, even though
            # GTK still reports them as part of the undecorated window.
            self.pointer_inside = False
            self._start_animation("jump_up")
        return False

    def _on_pointer_leave(self, _widget: Gtk.Widget, _event: Gdk.EventCrossing) -> bool:
        """Cancel hover work and return a standing pet to the swing."""
        if self.drag_candidate:
            return False
        self.pointer_inside = False
        self.pending_hover = False
        self.hover_idle_since = None
        self.suppress_hover_until_leave = False
        if self.animation_name == "standing":
            self._start_animation("jump_up")
        elif self.animation_name == "jump_down":
            self.pending_return = True
        return False

    def _request_dismount(self) -> None:
        """Dismount at the bottom pose; otherwise defer until the loop boundary."""
        if (
            self.locked_out
            or low_affection_blocks_motion(self.affection)
            or self.suppress_hover_until_leave
            or self.drag_candidate
        ):
            return
        if self.animation_name != "swing":
            return
        if self.frame_index == 0:
            self._start_animation("jump_down")
        else:
            self.pending_hover = True

    def _start_animation(self, name: str) -> None:
        """Switch sequences atomically and reset per-sequence elapsed state."""
        self.animation_name = name
        self.frames = self.animations[name]
        self.frame_index = 0
        self.frame_elapsed = 0.0
        self.standing_decay_carry = 0.0
        self._display_frame()
        self._refresh_info()
        if name == "standing":
            # Speak only after landing, at the same moment the “咋滴啊？” bubble
            # appears. Jump frames themselves remain uninterrupted.
            self.audio.play("voice", AUDIO_LANDING)
        if self.info_popup.get_visible():
            self._position_info_popup()

    def apply_bridge_update(self, update: BridgeUpdate) -> bool:
        """Apply a bridge message on GTK's main thread and refresh status text."""
        self.pet_state = update.state
        self.bridge_connected = update.connected
        self.bridge_detail = update.detail
        self._refresh_info()
        return False

    def _refresh_info(self, reposition: bool = True) -> None:
        """Select alert/standing/status copy and include current affinity."""
        style = self.info_label.get_style_context()
        style.remove_class("drag-alert")
        style.remove_class("standing-quip")
        if self.drag_alert_active:
            style.add_class("drag-alert")
            markup = info_markup(DRAG_ALERT_TEXT, self.affection, alert=True)
        elif self.animation_name == "standing":
            style.add_class("drag-alert")
            markup = info_markup(LANDING_TEXT, self.affection, alert=True)
        else:
            markup = info_markup(STATE_INFO_TEXT[self.pet_state], self.affection)
        if markup != self._last_info_markup:
            self._last_info_markup = markup
            self._info_popup_size = None
            self.info_label.set_markup(markup)
        if reposition and self.info_popup.get_visible() and self._info_popup_size is None:
            self.info_popup.resize(1, 1)
            GLib.idle_add(self._position_info_popup)

    def _tick(self) -> bool:
        """Run the 60 Hz state machine for affinity, dragging and frame playback.

        Ordering matters: affinity may enter lockdown and stop normal rendering;
        timed drag/standing decay runs before animation; queued drag transition
        frames run before ordinary GIF timing; completed one-shot sequences then
        select their next stable animation.
        """
        now = time.monotonic()
        self.audio.tick(now)
        elapsed_seconds = max(0.0, now - self.last_tick)
        elapsed_ms = elapsed_seconds * 1000.0
        self.last_tick = now
        if self.affection == 0 and not self.locked_out:
            self._begin_lockdown()
        if self.locked_out:
            return True

        if self.pending_drag_root is not None and self.drag_candidate:
            root_x, root_y = self.pending_drag_root
            self.pending_drag_root = None
            self._update_drag(root_x, root_y)

        if self.dragging:
            # EOS, rather than an arbitrary one-second timer, gates repetition.
            # Releasing the mouse simply stops future requests; GStreamer still
            # plays the current utterance to completion.
            self.audio.request("drag-voice", AUDIO_DRAG, DRAG_VOICE_GAP_SECONDS)
            loss, self.drag_decay_carry = timed_affection_loss(
                elapsed_seconds, self.drag_decay_carry, AFFECTION_DRAG_LOSS
            )
            self._record_interaction()
            if loss:
                self._set_affection(decrease_to_floor(self.affection, loss))
            self.standing_decay_carry = 0.0
        elif self.animation_name == "standing":
            # Standing has its own faster decay and must not also accumulate the
            # ordinary five-second idle decay. Both action decays stop at 5.
            self.next_affection_decay = now + AFFECTION_DECAY_SECONDS
            loss, self.standing_decay_carry = timed_affection_loss(
                elapsed_seconds, self.standing_decay_carry, AFFECTION_STANDING_LOSS
            )
            if loss:
                self._set_affection(decrease_to_floor(self.affection, loss))
        else:
            self.standing_decay_carry = 0.0
            decay_steps = elapsed_periods(
                now, self.next_affection_decay, AFFECTION_DECAY_SECONDS
            )
            if decay_steps:
                self.next_affection_decay += decay_steps * AFFECTION_DECAY_SECONDS
                self._set_affection(self.affection - decay_steps)
                if self.locked_out:
                    return True

        if not low_affection_blocks_motion(self.affection) and hover_dismount_ready(
            now,
            self.hover_idle_since,
            self.pointer_inside,
            self.drag_candidate,
            self.animation_name,
        ):
            self.hover_idle_since = None
            self._request_dismount()

        drag_transition_advanced = False
        if self.dragging:
            drag_transition_advanced = self._advance_drag_transition(elapsed_ms)
            idle_ms = (now - self.last_drag_motion) * 1000.0
            if not should_resume_held_drag(idle_ms, bool(self.drag_transition_path)):
                return True
            # The pointer is still but the button remains down. Resume the GIF from
            # the exact held phase and stop showing the drag-only WOC message.
            self.drag_target_pose = None
            self.drag_alert_active = False
            self._refresh_info()
        if self.drag_transition_path:
            if not drag_transition_advanced:
                self._advance_drag_transition(elapsed_ms)
            if not self.drag_transition_path:
                self.drag_target_pose = None
                self.frame_elapsed = 0.0
            return True
        speed = STATE_SPEED[self.pet_state] if self.animation_name == "swing" else 1.0
        if self.animation_name == "swing" and self.danger_speed_started is not None:
            # Multiply the existing Codex-state speed while preserving the exact
            # GIF frame order, closed loop seam, and fixed rope anchor geometry.
            speed *= danger_speed_multiplier(
                self.affection, now - self.danger_speed_started
            )
        self.frame_elapsed += elapsed_ms * speed

        changed = False
        while self.frame_elapsed >= self.frames[self.frame_index].duration_ms:
            self.frame_elapsed -= self.frames[self.frame_index].duration_ms
            next_index = self.frame_index + 1
            if next_index < len(self.frames):
                self.frame_index = next_index
            elif self.animation_name == "swing":
                self.frame_index = 0
                if self.pending_hover and self.pointer_inside:
                    self.pending_hover = False
                    self._start_animation("jump_down")
                    return True
            elif self.animation_name == "jump_down":
                if self.pending_return or not self.pointer_inside:
                    self.pending_return = False
                    self._start_animation("jump_up")
                else:
                    self._start_animation("standing")
                return True
            elif self.animation_name == "jump_up":
                self._start_animation("swing")
                return True
            else:
                self.frame_index = 0
                self.frame_elapsed = 0.0
                break
            changed = True
        if changed:
            self._display_frame()
        return True


def diagnose(paths: tuple[Path, ...]) -> int:
    """Validate that every required animation exists, decodes and has alpha."""
    ok = True
    for path in paths:
        source = Image.open(path)
        frames = 0
        duration = 0
        has_alpha = True
        for frame in ImageSequence.Iterator(source):
            rgba = frame.convert("RGBA")
            frames += 1
            duration += int(frame.info.get("duration", source.info.get("duration", 40)))
            has_alpha = has_alpha and rgba.getchannel("A").getextrema()[0] < 255
        print(f"asset={path} frames={frames} duration_ms={duration} transparent={str(has_alpha).lower()}")
        ok = ok and frames >= 1 and has_alpha
    return 0 if ok else 1


def main() -> int:
    """Parse CLI options, load assets, start optional Codex bridge, and run GTK."""
    set_process_name()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", type=Path, default=DEFAULT_ASSET)
    parser.add_argument("--standing", type=Path, default=DEFAULT_STANDING_ASSET)
    parser.add_argument("--jump-down", type=Path, default=DEFAULT_JUMP_DOWN_ASSET)
    parser.add_argument("--jump-up", type=Path, default=DEFAULT_JUMP_UP_ASSET)
    parser.add_argument("--scale", type=float, default=1.15)
    parser.add_argument(
        "--initial-affection",
        type=int,
        default=int(os.environ.get("SWING_PET_INITIAL_AFFECTION", AFFECTION_DEFAULT)),
        help="initial affinity, 0-1000 (or set SWING_PET_INITIAL_AFFECTION)",
    )
    parser.add_argument("--no-codex", action="store_true", help="play without Codex linkage")
    parser.add_argument("--mute", action="store_true", help="disable interaction audio")
    parser.add_argument("--diagnose", action="store_true", help="verify assets without opening a window")
    args = parser.parse_args()

    required_assets = (args.asset, args.standing, args.jump_down, args.jump_up)
    missing = [str(path) for path in required_assets if not path.is_file()]
    if missing:
        parser.error(
            "interaction asset not found: " + ", ".join(missing)
            + "; run python3 src/build_interaction_assets.py"
        )
    if not 0.4 <= args.scale <= 3.0:
        parser.error("--scale must be between 0.4 and 3.0")
    if not 0 <= args.initial_affection <= AFFECTION_MAX:
        parser.error(f"--initial-affection must be between 0 and {AFFECTION_MAX}")
    if args.diagnose:
        return diagnose(required_assets)

    animations = {
        "swing": load_animation(args.asset, args.scale),
        "standing": load_animation(args.standing, args.scale),
        "jump_down": load_animation(args.jump_down, args.scale),
        "jump_up": load_animation(args.jump_up, args.scale),
    }
    # The right-click preference survives restarts. ``--mute`` remains a
    # one-launch override and does not rewrite that persisted preference.
    audio_enabled = load_sound_setting() and not args.mute
    window = PetWindow(
        animations,
        args.scale,
        args.initial_affection,
        audio_enabled=audio_enabled,
    )
    bridge: CodexBridge | None = None
    if not args.no_codex:
        bridge = CodexBridge(
            on_update=lambda update: GLib.idle_add(window.apply_bridge_update, update),
            manual_state_path=ROOT / "runtime" / "pet-state.json",
        )
        bridge.start()
    window.show_all()
    window._show_info()
    try:
        Gtk.main()
    finally:
        if bridge:
            bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
