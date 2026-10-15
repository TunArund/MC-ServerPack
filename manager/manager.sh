#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：需要 Python 3.10 或更高版本" >&2
  exit 127
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "错误：需要 Python 3.10 或更高版本" >&2
  exit 1
fi

cd "$ROOT_DIR"
PYTHON="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi
exec "$PYTHON" -m lib.cli "$@"
