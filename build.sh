#!/usr/bin/env bash
# Builds dist/NOMAD-<version>-linux-x86_64: standalone executable for Linux.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m pytest tests/
python3 -m PyInstaller --noconfirm --clean --onefile --name NOMAD-1.24.2-linux-x86_64 --add-data "nomad/data:nomad/data" main.py
echo "Built dist/NOMAD-1.24.2-linux-x86_64"
