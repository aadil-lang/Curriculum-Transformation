#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

python3.12 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/pip" install -r "${ROOT_DIR}/requirements.txt"
"${VENV_DIR}/bin/playwright" install chromium
"${VENV_DIR}/bin/python" "${ROOT_DIR}/main.py" bootstrap

echo "Bootstrap complete."
echo "Activate with: source ${VENV_DIR}/bin/activate"
