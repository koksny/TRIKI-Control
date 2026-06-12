import os
import sys

# The application modules live in src/; put it on the path so tests can
# `import triki_*` directly and PyInstaller / dev runs resolve the same way.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
