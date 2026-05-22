#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "macOS app builds must run on Darwin." >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Expected Python at $PYTHON. Create .venv and install requirements first." >&2
  exit 1
fi

cd "$ROOT"
rm -rf "$ROOT/dist/TRIKI Control.app" "$ROOT/dist/TRIKI Control"
"$PYTHON" -m PyInstaller --clean --noconfirm "$ROOT/TRIKI-Control-macOS.spec"

if command -v codesign >/dev/null 2>&1; then
  find -H "$ROOT/dist/TRIKI Control.app" -exec xattr -c {} + 2>/dev/null || true
  codesign --force --deep --sign - "$ROOT/dist/TRIKI Control.app"
fi

echo "Built $ROOT/dist/TRIKI Control.app"
