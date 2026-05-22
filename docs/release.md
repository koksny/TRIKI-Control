# TRIKI Control Release Notes

## Current Alpha Builds

Version: `0.1.0-alpha.1`

This is the first usable public-style build of the TRIKI desktop remapper. It is still an alpha: BLE stability depends on adapter range and OS Bluetooth behavior, but the core controller flow is ready for broader testing.

## Included Apps

- `TRIKI-Control.exe`: normal standalone app with embedded WebView and tray behavior.
- `TRIKI-Control-Debug.exe`: same app with a console window for connection logs.
- `TRIKI Control.app`: macOS app bundle with Bluetooth privacy strings and Quartz key output.

The package also includes `README.md`, `CREDITS.md`, `LICENSE`, and the `docs` folder.

The standalone app window is fixed-size and sized to avoid normal vertical scrolling so the mapping layout stays predictable. Use the browser development mode if you need responsive layout testing.

## First-Run Flow

1. Start `TRIKI-Control.exe`.
2. Click `Pair TRIKI`.
3. Press the physical TRIKI pairing button once when prompted.
4. Wait for the Pair button to turn green and read `Connected`.
5. Optionally hold `Test LED` to confirm the app can send a command back to TRIKI.
6. Choose a profile or edit mappings, then focus the game/app you want to control.

On macOS, allow Bluetooth access on first launch and grant Accessibility permission to `TRIKI Control.app` before expecting mapped key output. The app can still pair, stream motion, read battery, and run LED tests before Accessibility is granted.

## Troubleshooting

- If pairing fails, keep TRIKI very close to the Bluetooth adapter and click `Pair TRIKI` again.
- If the app says connected but no keys arrive in a game, try Notepad first. If Notepad works but the game does not, the game is probably ignoring synthetic keyboard input. That game will need native TRIKI support or another game-specific integration path; this app is not planning a virtual gamepad driver.
- If battery shows `Battery --`, Windows did not expose the Battery Level characteristic during that session. The controller can still work.
- If movement is detected but wrong actions fire, export profiles before changing mappings so the configuration can be restored.
- On macOS, if diagnostics report `key_output.status = untrusted`, grant Accessibility permission in `System Settings > Privacy & Security > Accessibility`.
- On macOS, run the packaged app bundle for BLE testing. Bare command-line Python can be killed by macOS privacy checks if the host process lacks `NSBluetoothAlwaysUsageDescription`.

## Packaging

Build and package from the repo root:

```powershell
.\tools\build_windows_exe.ps1
.\tools\package_windows_release.ps1
```

To rebuild and package in one step:

```powershell
.\tools\package_windows_release.ps1 -Build
```

The zip is written to `release\TRIKI-Control-<version>-windows.zip`.

To also produce a direct-download Windows executable asset:

```powershell
.\tools\package_windows_release.ps1 -ExeAsset
```

This writes `release\TRIKI-Control-<version>-windows.exe`. The direct exe is the normal app only. The zip remains the complete package because it also includes the debug exe, README, credits, license, and docs.

Linux source-style package:

```bash
bash tools/package_linux_release.sh
```

The tarball is written to `release/TRIKI-Control-<version>-linux.tar.gz`.

macOS app bundle and zip package:

```bash
bash tools/build_macos_app.sh
bash tools/package_macos_release.sh
```

The zip is written to `release/TRIKI-Control-<version>-macos.zip`. The current macOS alpha is ad-hoc signed for local testing, not notarized.

## GitHub Release Upload

Do not commit packaged executables or archives to the source repository. Upload them as GitHub Release assets.

The `0.1.0-alpha.1` assets are:

```text
TRIKI-Control-0.1.0-alpha.1-windows.exe
TRIKI-Control-0.1.0-alpha.1-windows.zip
TRIKI-Control-0.1.0-alpha.1-macos.zip
TRIKI-Control-0.1.0-alpha.1-linux.tar.gz
```

After pushing `main` and the tag, create the GitHub release with:

```bash
gh release create v0.1.0-alpha.1 \
  ../release/TRIKI-Control-0.1.0-alpha.1-windows.exe \
  ../release/TRIKI-Control-0.1.0-alpha.1-windows.zip \
  ../release/TRIKI-Control-0.1.0-alpha.1-macos.zip \
  ../release/TRIKI-Control-0.1.0-alpha.1-linux.tar.gz \
  --title "TRIKI Control 0.1.0-alpha.1" \
  --notes-file docs/release.md \
  --prerelease
```

If the repository is pushed from `C:\Projects\triki\git-repo`, the `../release/...` paths point to the packaged artifacts already produced in this workspace.
