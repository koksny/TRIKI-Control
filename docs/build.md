# Building & packaging

TRIKI Control is plain Python. You can run it straight from source, or build a single self-contained executable with PyInstaller.

## Requirements

- **Python 3.11 or newer.**
- The cap, if you want to actually test motion (the UI runs without it).
- A working Bluetooth LE adapter.

Runtime dependencies (`requirements.txt`):

- `bleak` for cross-platform BLE.
- `pywebview` for the desktop UI window.
- `pystray` for the tray icon in packaged builds.
- `Pillow` for icon/asset handling.

On **Linux**, `pywebview` additionally needs a system GUI backend (PyGObject + WebKit2GTK, or PyQt + QtWebEngine) that pip cannot install. See [linux.md](linux.md).

## Layout

The repository is intentionally flat *inside* a single source folder. Every application module is a top-level `triki_*.py` file under **`src/`**. The two files worth reading first:

- `src/triki_app.py`: the BLE connection loop, the local server, and the entire embedded UI (HTML/CSS/JS).
- `src/triki_motion_engine.py`: the Game-profile "tank" controller; most of the project's tuning lives here.

## Run from source

```bash
python -m venv .venv
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe src\triki_app.py
```

**macOS / Linux:**

```bash
source .venv/bin/activate
pip install -r requirements.txt
python src/triki_app.py
```

Running `src/triki_app.py` directly puts `src/` on Python's path automatically, so the `triki_*` modules resolve without any install step. The app starts a local server on `http://127.0.0.1:8766/` and opens a window pointing at it. If the window does not appear (or you prefer a browser), open that URL yourself. `/debug` is the diagnostics page; `/diagnostics` is the raw JSON feed.

## Build a standalone executable

Builds are driven by **PyInstaller spec files** in the repo root, which is the reliable path on every platform. Install the build dependencies first:

```bash
python -m pip install -r requirements-build.txt
```

Then build with the spec for your platform.

### Windows

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm TRIKI-Control.spec
```

This produces `dist\TRIKI-Control.exe`, a single file, no install. `TRIKI-Control-Debug.spec` builds the same app with a console window attached, which is handy when you want to watch the connection log live. The normal build includes the Windows icon, tray icon and OpenSSL DLLs needed by the embedded WebView runtime.

> Build from a dedicated Windows virtualenv (e.g. `.venv-windows`) to keep build tooling out of your run environment. Calling PyInstaller on the spec directly, as above, is the dependable route.

### macOS

```bash
python -m PyInstaller --noconfirm TRIKI-Control-macOS.spec
```

This produces a `.app` bundle in `dist/` carrying the Bluetooth usage strings CoreBluetooth requires. Remember that keyboard output needs **Accessibility** permission granted to the built app (not to your terminal) in System Settings > Privacy & Security > Accessibility.

### Linux

There is no single-file Linux executable, because the WebKit/Qt GUI backend has to come from the system and cannot be bundled. Run from source (above) after installing a backend per [linux.md](linux.md).

## Packaged icon assets

The packaged app icons live in `assets/triki-control-icon.*`. Windows builds use the `.ico`, macOS builds use the `.icns`, and the tray icon uses `assets/triki-control-icon-tray.png` as bundled data in the PyInstaller specs.

## Regenerating the cap art

The cap illustrations shown in the UI are embedded as data URIs in `src/triki_assets.py`. If you change the source PNGs in `assets/`, regenerate the module with:

```bash
python scripts/gen_triki_assets.py
```

## Tests

```bash
python -m pip install pytest
python -m pytest tests/
```

A `conftest.py` at the repo root puts `src/` on the path, so the tests `import triki_*` directly. The suite covers the current rotation-invariant Game/Doom controls, profile migration, local API, keyboard and mouse emitters, packaging helpers, and recorded-motion regressions. Platform-specific integration tests skip automatically when their native backend is unavailable.
