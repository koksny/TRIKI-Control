# Linux notes

Linux works, but it needs two things pip cannot give you: a GUI backend for the window, and permission to write keystrokes.

## GUI backend for pywebview

`pywebview` renders the UI in a system web view. On Linux it needs one of:

- **GTK:** PyGObject + WebKit2GTK (e.g. `python3-gi`, `gir1.2-webkit2-4.1`), or
- **Qt:** PyQt + QtWebEngine.

Install one through your distro's package manager. Without a backend, the app still runs its local server, so you can open `http://127.0.0.1:8766/` in any browser as a fallback.

## Keyboard output via uinput

Keystrokes are written to **`/dev/uinput`**, which is root-only by default. Grant access without running the whole app as root:

```bash
sudo modprobe uinput
sudo usermod -aG input "$USER"   # then log out and back in
# or add a udev rule giving your user rw on /dev/uinput
```

If output does nothing while the Output toggle is ON, this is almost always the cause. Check that your user can read and write `/dev/uinput`.

## Bluetooth

`bleak` uses BlueZ over D-Bus. Make sure the Bluetooth service is running and the adapter is powered before pairing. No special permissions beyond the usual desktop Bluetooth access are required.
