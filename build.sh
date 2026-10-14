#!/usr/bin/env bash
# Builds dist/NOMAD-<version>-linux-<arch>: standalone executable for Linux.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -z "$PYTHON" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON="python3"
    else
        PYTHON="/usr/bin/python3"
    fi
fi

ARCH=$(uname -m)
VERSION=$($PYTHON -c "import nomad; print(nomad.__version__)")
NAME="NOMAD-${VERSION}-linux-${ARCH}"

echo "Building ${NAME} using ${PYTHON}..."
$PYTHON -m PyInstaller --noconfirm --clean --onefile --name "${NAME}" --add-data "nomad/data:nomad/data" --hidden-import "PyQt5.QtSvg" main.py
echo "Built dist/${NAME}"
