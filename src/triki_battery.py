from __future__ import annotations


BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"


def normalize_battery_percent(percent: int | None) -> int | None:
    if percent is None:
        return None
    return max(0, min(100, int(percent)))


def battery_status(percent: int | None, message: str = "") -> str:
    if percent is None:
        return "unavailable" if "unavailable" in message.lower() else "unknown"
    if percent <= 10:
        return "critical"
    if percent <= 25:
        return "low"
    if percent <= 50:
        return "medium"
    return "ok"


def battery_snapshot(percent: int | None, message: str = "") -> dict:
    normalized = normalize_battery_percent(percent)
    detail = message or (
        "Battery level unknown."
        if normalized is None
        else "Battery level read from BLE."
    )
    return {
        "percent": normalized,
        "status": battery_status(normalized, detail),
        "label": "Battery --" if normalized is None else f"{normalized}%",
        "message": detail,
    }
