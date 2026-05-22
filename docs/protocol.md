# TRIKI Protocol Notes

## Device Identity

- Device name: `Triki`
- Model: `CAP001`
- Producer: Caps Apps

TRIKI Control is an independent open-source desktop remapper. It is not the official Android app.

Current evidence shows TRIKI exposes a Nordic UART style BLE service.

Known UUIDs:

```text
NUS service: 6e400001-b5a3-f393-e0a9-e50e24dcca9e
RX/write:    6e400002-b5a3-f393-e0a9-e50e24dcca9e
TX/notify:   6e400003-b5a3-f393-e0a9-e50e24dcca9e
LED/write:   6e400004-b5a3-f393-e0a9-e50e24dcca9e
```

Current stream start command:

```text
20 10 00 d0 07 34 00 03
```

LED test command:

```text
01 = LED on while held
00 = LED off on release
```

The LED characteristic uses write-with-response. Android captures from the official app showed repeated writes to handle `0x0012` with `01` at button hold and `00` at release; GATT discovery maps that handle to `6e400004-b5a3-f393-e0a9-e50e24dcca9e`.

The parser in `triki_protocol.py` converts notification bytes into six signed channels. Current classifier names those channels `a` through `f`; the exact physical sensor mapping is still research-derived.

Current production gestures:

- `rotate-cw`: rotate the cap itself clockwise on the table.
- `rotate-ccw`: rotate the cap itself counterclockwise on the table.
- `scrub-cw`: move the cap around a clockwise circle on the table without intentionally rotating the cap itself.
- `scrub-ccw`: move the cap around a counterclockwise circle on the table without intentionally rotating the cap itself.
- `back-forth`: slide the cap side to side in a mostly straight line on the table.
- `lift`: lift and stamp/set the cap back down.
- `flip-over`: flip the cap over.

The classifier intentionally treats strong `c`-axis spin as rotate, even when
there is some lateral motion. Scrub requires signed lateral loop evidence; this
keeps ordinary rotate steering from accidentally firing scrub mappings.

Legacy labels `swirl-cw`, `swirl-ccw`, `shake`, and `slide-back-forth` are normalized to the canonical scrub/back-forth names when reading configs or calibration data.

Rejected or research-only gestures:

- tap gestures
- toss/catch
- twist patterns
- edge press/lift

Those gestures were too unreliable for the first app mapping set.
