#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
BUILD=0

for arg in "$@"; do
  case "$arg" in
    --build)
      BUILD=1
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PYTHON" ]]; then
  echo "Expected Python at $PYTHON. Create .venv and install requirements first." >&2
  exit 1
fi

if [[ "$BUILD" == "1" ]]; then
  PYTHON="$PYTHON" bash "$ROOT/tools/build_macos_app.sh"
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m triki_control.macos_package \
  --root "$ROOT" \
  --release-dir "$ROOT/release" \
  --app-path "$ROOT/dist/TRIKI Control.app"
