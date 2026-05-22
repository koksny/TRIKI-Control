# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

from triki_macos_package import macos_info_plist


datas = []
binaries = []
hiddenimports = []

for package in (
    "bleak",
    "webview",
    "pystray",
    "PIL",
    "objc",
    "Foundation",
    "CoreBluetooth",
    "Quartz",
    "Cocoa",
    "WebKit",
    "Security",
    "UniformTypeIdentifiers",
    "dispatch",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports


a = Analysis(
    ["triki_desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["android"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TRIKI Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TRIKI Control",
)

app = BUNDLE(
    coll,
    name="TRIKI Control.app",
    icon=None,
    bundle_identifier="com.koksny.triki.control",
    info_plist=macos_info_plist(),
)
