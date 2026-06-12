# Building & packaging

TRIKI Control is plain Python. You can run it straight from source, or build a single self-contained executable with PyInstaller.

## Requirements

- **Python 3.11 or newer.**
- The cap, if you want to actually test motion (the UI runs without it).
- A working Bluetooth LE adapter.

Runtime dependencies (`requirements.txt`):

- `bleak` — cross-platform BLE.
- `pywebview` — the desktop UI window.
- `pystray` — tray icon in packaged builds.
- `Pillow` — icon/asset handling.

On **Linux**, `pywebview` additionally needs a system GUI backend (PyGObject + WebKit2GTK, or PyQt + QtWebEngine) that pip cannot install — see [linux.md](linux.md).

## Run from source

```bash
python -m venv .venv
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe triki_app.py
```

**macOS / Linux:**

```bash
source .venv/bin/activate
pip install -r requirements.txt
python triki_app.py
```

The app starts a local server on `http://127.0.0.1:8766/` and opens a window pointing at it. If the window doesn't appear (or you prefer a browser), open that URL yourself. `/debug` is the diagnostics page; `/diagnostics` is the raw JSON feed.

The repo is intentionally **flat** — every module is a top-level `triki_*.py`. The two files worth reading first:

- `triki_app.py` — the BLE connection loop, the local server, and the entire embedded UI (HTML/CSS/JS).
- `triki_motion_engine.py` — the Game-profile "tank" controller; most of the project's tuning lives here.

## Build a standalone executable

Builds are driven by **PyInstaller spec files** in the repo root — this is the reliable path on every platform. Install the build dependencies first:

```bash
python -m pip install -r requirements-build.txt
```

Then build with the spec for your platform.

### Windows

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm TRIKI-Control.spec
```

This produces `dist\TRIKI-Control.exe` — a single file, no install. `TRIKI-Control-Debug.spec` builds the same app with a console window attached, which is handy when you want to watch the connection log live.

> Build from a dedicated Windows virtualenv (e.g. `.venv-windows`) to keep build tooling out of your run environment. Calling PyInstaller on the spec directly, as above, is the dependable route.

### macOS

```bash
python -m PyInstaller --noconfirm TRIKI-Control-macOS.spec
```

This produces a `.app` bundle in `dist/` carrying the Bluetooth usage strings CoreBluetooth requires. Remember that keyboard output needs **Accessibility** permission granted to the built app (not to your terminal) in System Settings → Privacy & Security → Accessibility.

### Linux

There's no single-file Linux executable — the WebKit/Qt GUI backend has to come from the system, so it can't be bundled. Run from source (above) after installing a backend per [linux.md](linux.md).

## Regenerating the cap art

The cap illustrations shown in the UI are embedded as data URIs in `triki_assets.py`. If you change the source PNGs in `assets/`, regenerate the module with:

```bash
python scripts/gen_triki_assets.py
```

## Tests

```bash
python -m pip install pytest
python -m pytest tests/
```

Heads up: the test suite is mid-migration. A chunk of it still targets an earlier control scheme (a directional/WASD-style design that predates the rotation-invariant engine) and those cases fail by design until they're rewritten against the current engine. The protocol, action-mapping, and key-emitter tests are the trustworthy ones today.
