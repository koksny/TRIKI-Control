# macOS notes

TRIKI Control can run on macOS from source or from the packaged `.app` build.

## First run

1. Open `TRIKI Control.app`.
2. Allow Bluetooth access when macOS asks.
3. Click `Pair TRIKI`, then press the physical TRIKI pairing button once.
4. Grant Accessibility permission in System Settings before enabling keyboard or mouse output.

The app needs Bluetooth for the cap connection and Accessibility for sending the
mapped keyboard and mouse actions to games or other apps.

## From source

Install the Python requirements, then run:

```bash
python src/triki_app.py --ui browser
```

The source run uses the same profiles and action mappings as the packaged app.
