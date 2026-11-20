#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.gemini/config/plugins/podo-stuff"

chmod +x "${SCRIPT_DIR}/scripts/podo-guard.py"
chmod +x "${SCRIPT_DIR}/scripts/podo-autopilot.py"
chmod +x "${SCRIPT_DIR}/install.sh"

mkdir -p "$(dirname "${TARGET_DIR}")"
rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"

# INVARIANT: Copy only runtime plugin files and keep tests/ and .podo/ excluded from user installs.
cp -a "${SCRIPT_DIR}/LICENSE" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/plugin.json" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/podo-config.json" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/hooks.json" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/README.md" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/assets" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/rules" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/scripts" "${TARGET_DIR}/"
cp -a "${SCRIPT_DIR}/skills" "${TARGET_DIR}/"

rm -rf "${TARGET_DIR}/tests" "${TARGET_DIR}/.podo"
find "${TARGET_DIR}/scripts" -name "__pycache__" -type d -exec rm -rf {} +
chmod +x "${TARGET_DIR}/scripts/podo-guard.py" "${TARGET_DIR}/scripts/podo-autopilot.py"
echo "[podo-stuff] Installed plugin to ${TARGET_DIR}"
