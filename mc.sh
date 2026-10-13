#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Linux Release 自带独立可执行文件；源码包没有该文件，继续使用 Python 环境。
if [ -x "$ROOT/mc" ]; then
  exec "$ROOT/mc" "$@"
fi

if [ -n "${MC_PYTHON:-}" ]; then
  candidates=("$MC_PYTHON")
else
  candidates=("$ROOT/.venv/bin/python" "$ROOT/.venv/Scripts/python.exe" python3 python)
fi

python_found=0
for candidate in "${candidates[@]}"; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1 || continue
  python_found=1
  if ! "$candidate" -c 'import yaml' >/dev/null 2>&1; then
    continue
  fi
  exec "$candidate" -m lib "$@"
done

if [ "$python_found" = 1 ]; then
  echo "错误：Python 依赖不完整；请按 README 在项目内创建虚拟环境" >&2
  exit 2
fi
echo "错误：找不到 Python 3.10+；可通过 MC_PYTHON 指定解释器" >&2
exit 2
