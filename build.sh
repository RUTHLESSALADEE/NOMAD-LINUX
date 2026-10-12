#!/usr/bin/env bash
# Builds dist/NOMAD-<version>-linux-x86_64: standalone executable for Linux.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="/usr/bin/python3"
VERSION=$($PYTHON -c "import nomad; print(nomad.__version__)")
NAME="NOMAD-${VERSION}-linux-x86_64"

echo "Building ${NAME} using ${PYTHON}..."
$PYTHON -m PyInstaller --noconfirm --clean --onefile --name "${NAME}" --add-data "nomad/data:nomad/data" main.py
echo "Built dist/${NAME}"
