from __future__ import annotations

import sys
from pathlib import Path


OPENSSL_DLL_PATTERNS = (
    "libssl-*.dll",
    "libcrypto-*.dll",
    "libssl*.dll",
    "libcrypto*.dll",
)


def _python_prefixes() -> list[Path]:
    prefixes = []
    seen = set()
    for value in (
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
    ):
        path = Path(value)
        key = str(path.resolve()).casefold()
        if key not in seen:
            prefixes.append(path)
            seen.add(key)
    return prefixes


def collect_windows_openssl_binaries(
    *, prefixes: list[Path] | None = None
) -> list[tuple[str, str]]:
    entries = []
    seen = set()

    for prefix in prefixes or _python_prefixes():
        for directory in (prefix / "Library" / "bin", prefix / "DLLs"):
            if not directory.is_dir():
                continue
            for pattern in OPENSSL_DLL_PATTERNS:
                for dll_path in sorted(directory.glob(pattern)):
                    key = str(dll_path.resolve()).casefold()
                    if key in seen:
                        continue
                    entries.append((str(dll_path), "."))
                    seen.add(key)

    return entries
