# Contributing

TRIKI Control is a small, practical desktop app for one specific Bluetooth motion cap. Contributions are welcome. The bar is just: keep it practical, keep it small, and keep it testable.

## Setup

```bash
python -m venv .venv
# Windows:  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS/Linux:  source .venv/bin/activate && pip install -r requirements.txt
python src/triki_app.py
```

The application modules live under `src/` as flat `triki_*.py` files, with no package install step. Full build and platform setup is in [docs/build.md](docs/build.md).

When changing screenshots or packaged icons, keep the source assets in `assets/` and run the relevant PyInstaller spec before publishing a release.

```bash
python -m pip install pytest
python -m pytest tests/
```

## Where help is most useful

- **BLE robustness:** fewer dropouts, faster reconnects, better behavior across adapters.
- **The motion engine** (`src/triki_motion_engine.py`): cleaner separation of the five controls, less ghosting, on a wider range of hands and surfaces. If you change tuning, validate against recorded sessions, not just by feel.
- **Platform output:** coverage and reliability of keyboard and mouse injection on Windows, macOS, and Linux.
- **Tests:** recorded-motion regressions, platform output fakes, and packaged-build smoke coverage.

## Ground rules

- Do not reintroduce a "directional joystick" without solving the heading problem ([docs/how-it-works.md](docs/how-it-works.md)). It cannot be faked in software on this hardware, which is exactly why the official games are single-axis too.
- Keep every control visible and rebindable. No hidden or removed bindings.
- This project ships nothing from Caps Apps / Żabka, only an independent, clean-room implementation of the public Bluetooth behavior. Keep it that way.

Open an issue to discuss anything substantial before a large PR.
