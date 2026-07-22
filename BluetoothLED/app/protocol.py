"""Happy Lighting (Triones) / ELK-BLEDOM BLE command builders.

Derived from Happy+Lighting_1.351 APK (com.qh.blelight):
  - Write UUIDs: ffd9, ffe1, fff3
  - Notify UUIDs: ffd4, ffe2, fff4
  - Device name regex: Triones-|BRGlight|Triones+|Dream-|Light-|Triones~
  - Power ON/OFF byte arrays found in classes.dex: CC 23 33 / CC 24 33
  - Status query: EF 01 77
"""

from __future__ import annotations

import re
from enum import IntEnum

# GATT UUIDs embedded in the APK
WRITE_UUIDS = (
    "0000ffd9-0000-1000-8000-00805f9b34fb",  # Triones primary
    "0000ffe1-0000-1000-8000-00805f9b34fb",
    "0000fff3-0000-1000-8000-00805f9b34fb",  # ELK-BLEDOM style
    "0000fff5-0000-1000-8000-00805f9b34fb",
    "0000fff7-0000-1000-8000-00805f9b34fb",
    "0000fff9-0000-1000-8000-00805f9b34fb",
)

NOTIFY_UUIDS = (
    "0000ffd4-0000-1000-8000-00805f9b34fb",
    "0000ffe2-0000-1000-8000-00805f9b34fb",
    "0000fff4-0000-1000-8000-00805f9b34fb",
)

SERVICE_UUIDS = (
    "0000ffd5-0000-1000-8000-00805f9b34fb",
    "0000fff0-0000-1000-8000-00805f9b34fb",
    "0000ffd0-0000-1000-8000-00805f9b34fb",
)

# Device name patterns from APK (Triones: / Triones- / Triones+ / Triones~)
DEVICE_NAME_RE = re.compile(
    r"^(Triones[:\-+~]?|BRGlight|Dream-|Light-|ELK-BLE|MELK|LEDBLE|LED-|QHM-|Magic)",
    re.IGNORECASE,
)

# Triones:C67045841BD8 → C6:70:45:84:1B:D8
TRIONES_EMBEDDED_ADDR = re.compile(r"^Triones[:\-]([0-9A-Fa-f]{12})$", re.IGNORECASE)


def normalize_ble_address(value: str) -> str:
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", value or "")
    if len(hex_only) != 12:
        raise ValueError(f"BLEアドレスが不正です: {value}")
    parts = [hex_only[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(parts).upper()


def address_from_triones_name(name: str | None) -> str | None:
    if not name:
        return None
    m = TRIONES_EMBEDDED_ADDR.match(name.strip())
    if not m:
        return None
    return normalize_ble_address(m.group(1))


class BuiltInMode(IntEnum):
    SEVEN_COLOR_CROSS_FADE = 0x25
    RED_GRADUAL = 0x26
    GREEN_GRADUAL = 0x27
    BLUE_GRADUAL = 0x28
    YELLOW_GRADUAL = 0x29
    CYAN_GRADUAL = 0x2A
    PURPLE_GRADUAL = 0x2B
    WHITE_GRADUAL = 0x2C
    RED_GREEN_CROSS = 0x2D
    RED_BLUE_CROSS = 0x2E
    GREEN_BLUE_CROSS = 0x2F
    SEVEN_COLOR_STROBE = 0x30
    RED_STROBE = 0x31
    GREEN_STROBE = 0x32
    BLUE_STROBE = 0x33
    YELLOW_STROBE = 0x34
    CYAN_STROBE = 0x35
    PURPLE_STROBE = 0x36
    WHITE_STROBE = 0x37
    SEVEN_COLOR_JUMP = 0x38


MODE_LABELS_JA: dict[int, str] = {
    BuiltInMode.SEVEN_COLOR_CROSS_FADE: "7色フェード",
    BuiltInMode.RED_GRADUAL: "赤グラデーション",
    BuiltInMode.GREEN_GRADUAL: "緑グラデーション",
    BuiltInMode.BLUE_GRADUAL: "青グラデーション",
    BuiltInMode.YELLOW_GRADUAL: "黄グラデーション",
    BuiltInMode.CYAN_GRADUAL: "シアングラデーション",
    BuiltInMode.PURPLE_GRADUAL: "紫グラデーション",
    BuiltInMode.WHITE_GRADUAL: "白グラデーション",
    BuiltInMode.RED_GREEN_CROSS: "赤緑クロス",
    BuiltInMode.RED_BLUE_CROSS: "赤青クロス",
    BuiltInMode.GREEN_BLUE_CROSS: "緑青クロス",
    BuiltInMode.SEVEN_COLOR_STROBE: "7色ストロボ",
    BuiltInMode.RED_STROBE: "赤ストロボ",
    BuiltInMode.GREEN_STROBE: "緑ストロボ",
    BuiltInMode.BLUE_STROBE: "青ストロボ",
    BuiltInMode.YELLOW_STROBE: "黄ストロボ",
    BuiltInMode.CYAN_STROBE: "シアンストロボ",
    BuiltInMode.PURPLE_STROBE: "紫ストロボ",
    BuiltInMode.WHITE_STROBE: "白ストロボ",
    BuiltInMode.SEVEN_COLOR_JUMP: "7色ジャンプ",
}


def is_likely_led_device(name: str | None, address: str | None = None) -> bool:
    if name:
        cleaned = name.strip()
        if DEVICE_NAME_RE.match(cleaned):
            return True
        if "triones" in cleaned.lower():
            return True
    return False


def clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


# --- Triones protocol (primary in Happy Lighting APK) ---


def triones_power(on: bool) -> bytes:
    return bytes([0xCC, 0x23 if on else 0x24, 0x33])


def triones_color(r: int, g: int, b: int) -> bytes:
    return bytes([0x56, clamp_byte(r), clamp_byte(g), clamp_byte(b), 0x00, 0xF0, 0xAA])


def triones_white(intensity: int) -> bytes:
    return bytes([0x56, 0x00, 0x00, 0x00, clamp_byte(intensity), 0x0F, 0xAA])


def triones_mode(mode: int, speed: int) -> bytes:
    # speed: 0x01 = fastest, 0xFF = slowest (Triones convention)
    return bytes([0xBB, clamp_byte(mode), clamp_byte(speed), 0x44])


def triones_status_query() -> bytes:
    return bytes([0xEF, 0x01, 0x77])


def ui_speed_to_triones(ui_speed: int) -> int:
    """Map UI 1(slow)..100(fast) to Triones 0xFF..0x01."""
    ui_speed = max(1, min(100, int(ui_speed)))
    return clamp_byte(round(0xFF - (ui_speed - 1) * (0xFE / 99)))


# --- ELK-BLEDOM protocol (also referenced via fff* UUIDs in APK) ---


def elk_power(on: bool) -> bytes:
    # Common ELK-BLEDOM variant
    if on:
        return bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF])
    return bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])


def elk_color(r: int, g: int, b: int) -> bytes:
    return bytes(
        [0x7E, 0x00, 0x05, 0x03, clamp_byte(r), clamp_byte(g), clamp_byte(b), 0x00, 0xEF]
    )


def elk_brightness(percent: int) -> bytes:
    value = max(0, min(100, int(percent)))
    return bytes([0x7E, 0x04, 0x01, value, 0xFF, 0x00, 0xFF, 0x00, 0xEF])


def elk_effect(mode: int, speed: int) -> bytes:
    return bytes([0x7E, 0x00, 0x03, clamp_byte(mode), 0x03, 0x00, 0x00, 0x00, 0xEF])


def detect_protocol_from_name(name: str | None) -> str:
    """Return 'elk' or 'triones'."""
    if not name:
        return "triones"
    upper = name.upper()
    if upper.startswith(("ELK-", "MELK", "LEDBLE", "LED-")):
        return "elk"
    return "triones"
