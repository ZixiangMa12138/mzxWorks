# Swing Cat (myPet)

[中文 README](README.md)

Swing Cat is a transparent desktop-pet project built from an original swinging GIF. The repository contains both a Codex-native sprite package and a feature-rich GTK standalone pet.

| Edition | Purpose | Entry point |
| --- | --- | --- |
| Codex-native pet | Loaded by the Codex/ChatGPT Desktop pet picker | `dist/swing-pet/` |
| Standalone desktop pet | Full interaction, affection system, and state bubble | `start-standalone.sh` |

The native package supplies standard Codex sprite animation. Clicking, dragging, jump transitions, affection, chains, and the key unlock sequence are implemented by the standalone GTK edition.

## Quick start

Run all commands from the repository root. No source path needs to be edited.

```bash
git clone <your-repository-url> myPet
cd myPet
python3 -m pip install -r requirements.txt
```

### Run without Codex

```bash
./start-standalone.sh --no-codex
```

This foreground mode is best for first-run troubleshooting. Exit with `Ctrl+C`, `Esc`, or the pet's right-click menu.

Useful options:

```bash
./start-standalone.sh --scale 1.4
./start-standalone.sh --initial-affection 12
./start-standalone.sh --diagnose
```

Use `--initial-affection 12` only to test the low-affection, explosion, and unlock flow quickly. Omit it for normal use.

For an idempotent background launch, use:

```bash
python3 src/autostart_pet.py
```

The PID, lock, log, and manual state are local, disposable files in `runtime/`; do not commit them. The standalone process is displayed as **PetCat**; use `ps -eo pid,comm,args | rg PetCat` to find it.

### Install a desktop shortcut

```bash
./install-desktop.sh
```

The installer uses `xdg-user-dir DESKTOP` for the desktop entry and installs an application-menu entry under `$XDG_DATA_HOME/applications` (default: `~/.local/share/applications`). A Desktop Entry must contain an absolute executable path, so the generated local file contains your clone location. Re-run the installer after moving or recloning the project. The tracked template `desktop/swing-pet.desktop.in` contains no personal path.

## Codex integration

The standalone edition enables `CodexBridge` by default. It reads only local Codex App Server/session information, never makes network requests, and maps activity to the bubble text and swing speed. Its priority is:

1. Manual `runtime/pet-state.json` override written by `petctl.py`;
2. local `codex app-server proxy`;
3. incremental reads from the newest JSONL under `~/.codex/sessions/`;
4. idle.

Start with automatic integration:

```bash
./start-standalone.sh
```

Test the visible states without a running task:

```bash
python3 src/petctl.py working
python3 src/petctl.py waiting
python3 src/petctl.py success
python3 src/petctl.py error
python3 src/petctl.py auto
```

`auto` removes the manual override and resumes automatic detection.

To start the standalone pet when a Codex session starts or resumes, add this command to your own Codex Hook configuration, replacing `<PROJECT_ROOT>` with the absolute path of your local clone:

```text
python3 <PROJECT_ROOT>/src/autostart_pet.py
```

The launcher immediately returns a hook response and uses a PID/lock check, so it does not block Codex or duplicate the pet when a desktop shortcut also runs. Hook locations, events, and trust requirements can differ by Codex version and workspace policy; add or verify the command in Codex's `/hooks` UI and trust it there.

> The standalone pet and the Codex-native pet are separate windows/runtimes. Integration synchronizes status text and swing speed for the standalone pet; it cannot inject its custom interactions into the native Codex sprite window.

### Install the Codex-native pet

Rebuild if needed, then install:

```bash
python3 src/build_pet.py
./install-local.sh
```

The installer copies `pet.json` and `spritesheet.webp` to `${CODEX_HOME:-$HOME/.codex}/pets/swing-pet/`. Restart Codex, choose the pet, and show it with `/pet` (or the app's Show pet control). Official documentation describes the desktop pet picker and `/pet`/`/pets` commands: [Pets documentation](https://learn.chatgpt.com/en/docs/pets).

## Requirements

- Linux, Python 3.10+, GTK 3, and a graphical session (X11 or a Wayland compositor with transparent-window support);
- PyGObject/GTK system packages; for Ubuntu/Debian: `sudo apt install python3-gi gir1.2-gtk-3.0`;
- Pillow, NumPy, and SciPy from `requirements.txt` for building/image processing;
- `swing-interactive.png`, `standing-transparent.png`, `jump-down.png`, and `jump-up.png` in `assets/runtime/` to run the standalone edition;
- Codex only when you want automatic status integration or the native pet package.

The standalone window cannot appear in a headless terminal or without a DISPLAY/Wayland session. `--diagnose` can still validate the assets.

## Repository layout

```text
myPet/
├── assets/source/       # User-supplied GIF and standing reference art
├── assets/runtime/      # Generated transparent APNG/PNG interaction assets
├── assets/icons/        # Launcher icon built from the lowest swing frame
├── desktop/             # Desktop Entry template with a path placeholder
├── dist/swing-pet/      # Installable local Codex v2 package
├── runtime/             # Local PID, lock, log, manual state (Git-ignored)
├── src/                 # Runtime, bridge, and asset builders
├── tests/               # State, drag, affection, and unlock tests
├── pet.config.json      # Atlas id, geometry, and source-cycle configuration
├── start-standalone.sh  # Foreground standalone launcher
├── install-desktop.sh   # Desktop/menu installer
└── install-local.sh     # Local Codex package installer
```

`qa/` contains reproducible visual-QA output. It can include local absolute paths and large previews, so it is not required for a release.

## How artwork and effects are made

| Item | Method | Implementation |
| --- | --- | --- |
| Swing and face | Original user art in `assets/source/`; builders only remove background, normalize crop/scale, and preserve alpha | `build_pet.py`, `build_interaction_assets.py` |
| Standing and jumps | The standing reference is separated from fixed swing geometry. Jump-up is an exact temporal reverse of jump-down for matching endpoints | `build_interaction_assets.py` |
| Swing motion | A closed period is extracted from the original GIF. Every frame shares coordinates, so the swing top stays fixed | `build_pet.py` |
| Launcher icon | The lowest point of the standalone swing is cropped and enlarged to 512px | `build_desktop_icon.py` |
| Heart | Drawn live with Cairo Bézier curves; it rises and fades | `HeartEffect` |
| Explosion, chains, lock, key | Drawn live with Cairo lines, dashes, arcs, rectangles, and star points; insertion, rotation, and shackle opening use time interpolation | `LockdownOverlay` |
| Bubble and red tint | GTK label/CSS for text; Pillow color mapping for low-affection tinting | `standalone_pet.py` |

Except for the user-provided artwork in `assets/source/`, most animation and effects can be regenerated from code. Confirm that you have redistribution rights for all source art before publishing the repository.

## Interaction rules

- Click: affection `+1`, with a stackable red heart near the head; maximum 1000.
- Drag: moves across monitors. Horizontal motion creates an opposite-direction swing lag; vertical motion holds the lowest pose. Holding without moving smoothly resumes swinging.
- Hover without movement for one second: the pet jumps down beside the swing; leaving jumps it back up.
- Dragging loses `-5` affection per full second down to 5; standing loses `-1` per second down to 5.
- Five seconds of normal swinging without click/drag loses `-1`, down to 0. Below 10 the pet reddens; at 5 and below the swing progressively speeds up.
- At 0 affection: an explosion starts the chain lockdown. Drag the randomly placed gold key to the central keyhole; it inserts, turns, opens the shackle, removes the chains, restores affection to 100, and resumes swinging.

## Development and rebuilding

Do not hand-edit final APNG/WebP outputs. After changing source art, crop logic, or atlas configuration, run:

```bash
python3 src/build_pet.py
python3 src/build_interaction_assets.py
python3 src/build_desktop_icon.py
python3 -m unittest discover -s tests -v
```

Key source files:

- `src/standalone_pet.py`: GTK window, state machine, input handling, effect layers, and animation scheduling.
- `src/codex_bridge.py`: local App Server first, session-log fallback, and state priority.
- `src/autostart_pet.py`: lock file and `/proc` verification to prevent duplicate pets; the background path also names the process `PetCat`.
- `src/build_pet.py`: transparent background cleanup, closed swing cycle extraction, and Codex v2 atlas build.
- `src/build_interaction_assets.py`: standing-art cleanup, swing/character layering, and jump transitions.

The source documents coordinate systems, interpolation, layer separation, and local Codex fallbacks with comments and docstrings. Keep the separation between read-only render ticks, input-driven state changes, and independent effect windows when extending the pet.

## Git hygiene and troubleshooting

Commit source, authorized source art, build configuration, validated runtime assets, and `dist/swing-pet/`. Do not commit `runtime/`, Python caches, machine-specific QA JSON, generated desktop entries, or personal Codex Hook configuration; `.gitignore` covers the local artifacts.

```bash
./start-standalone.sh --diagnose
python3 -m unittest discover -s tests -v
```

Without Codex, the pet remains usable and reports idle; `--no-codex` disables the bridge entirely. If the window does not appear, verify that the current graphical session exposes DISPLAY or Wayland variables, then inspect `runtime/standalone-pet.log`.
