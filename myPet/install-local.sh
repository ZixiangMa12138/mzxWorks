#!/usr/bin/env bash
set -euo pipefail

# Read the package id from configuration so changing the pet id does not require
# editing this installer. The installed pair must always stay in one directory.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pet_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["id"])' "$project_dir/pet.config.json")"
source_dir="$project_dir/dist/$pet_id"
target_dir="${CODEX_HOME:-$HOME/.codex}/pets/$pet_id"

# Refuse partial packages: Codex needs both metadata and the referenced atlas.
if [[ ! -f "$source_dir/pet.json" || ! -f "$source_dir/spritesheet.webp" ]]; then
  echo "Pet package is missing. Run: python3 src/build_pet.py" >&2
  exit 1
fi

# Copy only finalized distribution files; source and QA artifacts remain local.
mkdir -p "$target_dir"
cp "$source_dir/pet.json" "$target_dir/pet.json"
cp "$source_dir/spritesheet.webp" "$target_dir/spritesheet.webp"
echo "Installed $pet_id to $target_dir"
echo "Restart Codex, then use OpenAI > Show pet."
