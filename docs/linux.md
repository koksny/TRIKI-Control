# Linux Setup

Linux support is currently focused on the desktop app plus keyboard/media output through `/dev/uinput`.

The BLE, parser, classifier, profiles, and local web UI are shared with the Windows build. The Linux-specific part is the key output backend: TRIKI creates a virtual keyboard through the kernel uinput interface when the first mapped action is emitted.

## Development Run

From the repo root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
python -m triki_control.app
```

Open the printed local URL if the browser does not open automatically.

## uinput Permissions

Most Linux desktops require explicit permission before a normal user can create a uinput device.

One common setup is:

```bash
sudo groupadd -f input
sudo usermod -aG input "$USER"
printf 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"\n' | sudo tee /etc/udev/rules.d/99-triki-uinput.rules
sudo modprobe uinput
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Log out and back in after changing groups. Some distributions use a different input group policy; keep the rule aligned with the distro's normal input-device permissions.

## Smoke Test

Safe dry-run, no key emitted:

```bash
python -m triki_control.linux_smoke --json
```

Real uinput test:

```bash
python -m triki_control.linux_smoke --json --emit --key space
```

The real test creates the virtual keyboard and sends one key press. Focus a text editor first if you want to see visible input.

If the smoke report says `missing`, load the kernel module with `sudo modprobe uinput`. If it says `permission`, fix the udev rule or group membership.

## WSL

WSL is useful for import and dry-run checks:

```bash
python3 -m triki_control.linux_smoke --json
python3 -m unittest tests.test_triki_linux_smoke
```

WSL usually does not provide a real desktop input stack or `/dev/uinput` suitable for end-to-end key injection, so the `--emit` test should be treated as a native Linux-desktop check.

## Diagnostics

Print environment diagnostics:

```bash
python -m triki_control.diagnostics --json
```

The running app also exposes the same information at:

```text
http://127.0.0.1:8766/diagnostics
```

Use this output when reporting Linux setup issues. It includes Python version, platform, dependency imports, config path, `/dev/uinput` status, and suggested fixes.

## Package

Build a source-style Linux archive:

```bash
bash tools/package_linux_release.sh
```

The archive is written to `release/TRIKI-Control-<version>-linux.tar.gz` and includes `triki-control`, `src/triki_control`, `pyproject.toml`, `requirements.txt`, `README.md`, `CREDITS.md`, `LICENSE`, and `docs`.
