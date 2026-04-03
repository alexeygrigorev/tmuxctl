#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASHRC_PATH="${HOME}/.bashrc"
PATH_LINE="export PATH=\"${REPO_DIR}/.venv/bin:\$PATH\""

touch "${BASHRC_PATH}"

if grep -Fqx "${PATH_LINE}" "${BASHRC_PATH}"; then
  echo "PATH already configured in ${BASHRC_PATH}"
  exit 0
fi

printf '\n%s\n' "${PATH_LINE}" >> "${BASHRC_PATH}"
echo "Added ${REPO_DIR}/.venv/bin to PATH in ${BASHRC_PATH}"
