#!/usr/bin/env bash
set -euo pipefail

# Anchor execution to the repository instead of the caller's working directory.
# ``exec`` intentionally replaces this shell so Ctrl+C and exit codes reach the
# Python GUI directly. ``exec -a`` gives process viewers a friendly argv[0],
# while standalone_pet.py also updates Linux's short comm name. Every CLI
# option is forwarded unchanged via "$@".
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec -a PetCat python3 "$project_dir/src/standalone_pet.py" "$@"
