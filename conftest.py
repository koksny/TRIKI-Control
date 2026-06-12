import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# The application modules live in src/; put it on the path so tests can
# `import triki_*` directly and PyInstaller / dev runs resolve the same way.
sys.path.insert(0, os.fspath(ROOT / "src"))
