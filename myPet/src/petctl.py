#!/usr/bin/env python3
"""Set or clear the standalone pet's manual Codex-state override.

The file is replaced atomically so the bridge can never observe partial JSON.
Use ``auto`` to delete the override and return control to App Server/log linkage.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from codex_bridge import PET_STATES


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "runtime" / "pet-state.json"


def main() -> None:
    """Validate the requested state and atomically update runtime state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", choices=sorted(PET_STATES | {"auto"}))
    args = parser.parse_args()

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.state == "auto":
        try:
            STATE_PATH.unlink()
        except FileNotFoundError:
            pass
        print("Pet state: automatic Codex linkage")
        return

    # Write beside the target so ``replace`` stays on one filesystem and is atomic.
    temporary = STATE_PATH.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"state": args.state}) + "\n", encoding="utf-8")
    temporary.replace(STATE_PATH)
    print(f"Pet state: {args.state}")


if __name__ == "__main__":
    main()
