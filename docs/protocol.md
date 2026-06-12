# BLE protocol notes

Everything here was derived independently from observing the cap over standard Bluetooth, from packet captures and GATT discovery, not from any official software. It is accurate enough to drive the device reliably; the exact physical meaning of a couple of fields is still best-effort.

## Device identity

- Advertised name: `Triki`
- Model: `CAP001`
- Producer: Caps Apps (distributed by Żabka)

The cap exposes a **Nordic UART Service (NUS)**-style profile.

```text
NUS service:  6e400001-b5a3-f393-e0a9-e50e24dcca9e
RX  (write):  6e400002-b5a3-f393-e0a9-e50e24dcca9e
TX  (notify): 6e400003-b5a3-f393-e0a9-e50e24dcca9e
LED (write):  6e400004-b5a3-f393-e0a9-e50e24dcca9e
```

Motion data arrives as notifications on the TX characteristic. You start the stream by writing a start command to RX; you control the LED by writing to the LED characteristic.

## Starting the motion stream

Several start commands were observed in captures, differing in stream rate and a trailing profile byte:

```text
20 10 00 d0 07 d0 00 03   # ~208 Hz  (high-rate motion)
20 10 00 d0 07 68 00 03   # ~106 Hz  (active motion)
20 10 00 d0 07 68 00 01   # ~106 Hz  (flip profile)
20 10 00 d0 07 34 00 03   # ~53 Hz   (smooth steering)
```

The little-endian field in the middle (`0x0034`, `0x0068`, `0x00d0`) appears to request the rate; the final byte appears to select a device-side input profile. TRIKI Control uses the **~53 Hz steering** command, which matches the official app's late-session steering capture and gives a clean, low-jitter stream for the motion engine.

In practice delivery is **bursty**: notifications arrive in clumps, inter-sample gaps reach ~100 ms at the 99th percentile, and brief multi-second dropouts happen (frequently when a finger covers the antenna mid-motion). The engine is built to tolerate this. See [how-it-works.md](how-it-works.md).

## The six motion channels

Each notification decodes (in `src/triki_protocol.py`) into **six signed channels**. The motion engine treats them as a 6-axis IMU:

- `values[0..2]`: **gyroscope** (angular rate around the three axes).
- `values[3..5]`: **accelerometer** (force along the three axes; gravity plus motion).

At rest, lying flat, the accelerometer reads roughly `(24, 0, -2050)` in raw units, the large negative `z` being "down". There is **no magnetometer**; the consequences of that absence are the whole subject of [how-it-works.md](how-it-works.md), and they are why the cap's official games each stick to a single axis.

The gyro bias is stable and device-specific (around `(12, -31, -22)` raw on the unit this was tuned on); the engine re-learns it live rather than hard-coding it, because the *first* packet after a connection can be an outlier that would otherwise poison the estimate.

## LED test command

The LED characteristic uses **write-with-response**:

```text
01  # LED on  (sent while the Test LED button is held)
00  # LED off (sent on release)
```

This is the simplest end-to-end check that the app can both reach the cap and write back to it.
