#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASHRC_PATH="${HOME}/.bashrc"
PATH_LINE="export PATH=\"${REPO_DIR}/.venv/bin:\$PATH\""
ALIAS_LINE="alias tl='t l'"

touch "${BASHRC_PATH}"

if grep -Fqx "${PATH_LINE}" "${BASHRC_PATH}"; then
  echo "PATH already configured in ${BASHRC_PATH}"
else
  printf '\n%s\n' "${PATH_LINE}" >> "${BASHRC_PATH}"
  echo "Added ${REPO_DIR}/.venv/bin to PATH in ${BASHRC_PATH}"
fi

if grep -Fqx "${ALIAS_LINE}" "${BASHRC_PATH}"; then
  echo "tl alias already configured in ${BASHRC_PATH}"
else
  printf '%s\n' "${ALIAS_LINE}" >> "${BASHRC_PATH}"
  echo "Added tl alias to ${BASHRC_PATH}"
fi
