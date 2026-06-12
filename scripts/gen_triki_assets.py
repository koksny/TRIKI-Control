"""Regenerate triki_assets.py from the cap PNGs in assets/.

The end-user UI (triki_app.build_html) embeds the cap art as base64 data: URIs
so the live-cap visualization works inside the one-file PyInstaller EXE with no
runtime file-path dependence. Run this whenever the source PNGs change:

    .venv-windows/Scripts/python.exe scripts/gen_triki_assets.py

Only the three faces used by the UI are inlined; the 1.4 MB sprite-source sheet
is intentionally skipped (it is not referenced by the page).
"""

from __future__ import annotations

import base64
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
OUTPUT = REPO_ROOT / "triki_assets.py"

# (constant name, source PNG filename)
CAPS = [
    ("CAP_FRONT_DATA_URI", "triki-cap-front.png"),
    ("CAP_SIDE_DATA_URI", "triki-cap-side.png"),
    ("CAP_REVERSE_DATA_URI", "triki-cap-reverse.png"),
]

HEADER = '''"""Base64 data-URI copies of the TRIKI cap art used by the end-user UI.

Generated build artifact (run scripts/gen_triki_assets.py to regenerate). The
three crown-cap PNGs in assets/ are inlined as data: URIs so build_html() can
embed them directly into the page. This keeps the live-cap visualization working
with zero runtime file-path dependence -- crucial for the one-file PyInstaller
EXE (no sys._MEIPASS handling, no .spec datas, no HTTP /assets route needed) and
fully offline inside the embedded WebView. The 1.4 MB sprite-source sheet is NOT
inlined; it is not referenced by the UI.
"""

from __future__ import annotations

'''

CHUNK = 76


def build_source() -> str:
    parts = [HEADER]
    for const_name, filename in CAPS:
        raw = (ASSETS_DIR / filename).read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        full = "data:image/png;base64," + b64
        pieces = [full[i : i + CHUNK] for i in range(0, len(full), CHUNK)]
        parts.append(f"{const_name} = (\n")
        parts.append("".join(f"    {piece!r}\n" for piece in pieces))
        parts.append(")\n\n")
    return "".join(parts)


def main() -> None:
    source = build_source()
    OUTPUT.write_text(source, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(source)} bytes)")


if __name__ == "__main__":
    main()
