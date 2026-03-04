"""Helpers for listing serial ports, with ESP32-device filtering on Linux."""

import sys

# USB Vendor IDs of chips commonly found on ESP32 dev boards
_ESP32_VIDS: frozenset[int] = frozenset({
    0x1A86,  # QinHeng: CH340, CH341, CH9102
    0x10C4,  # Silicon Labs: CP2102 / CP210x
    0x0403,  # FTDI: FT232
    0x303A,  # Espressif: native USB (ESP32-S3, C3, C6, H2)
})

# Description substrings — fallback when the driver doesn't expose VID (Windows)
_ESP32_DESC_KEYWORDS: tuple[str, ...] = (
    "ch340", "ch341", "ch9102",
    "cp210", "cp2102", "cp2104",
    "ft232", "ftdi",
    "usb serial", "usb-serial",
    "espressif",
)


def _is_esp32_port(p) -> bool:
    if p.vid in _ESP32_VIDS:
        return True
    desc = (p.description or "").lower()
    return any(kw in desc for kw in _ESP32_DESC_KEYWORDS)


def list_esp32_ports():
    """Return serial ports that look like ESP32 devices.

    On Windows all COM ports are returned (drivers often don't expose VID).
    On Linux/macOS only ports matching known ESP32 VIDs/descriptions are returned.
    """
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
    except Exception:
        return []

    if sys.platform == "win32":
        return sorted(ports, key=lambda p: p.device)

    return sorted(
        (p for p in ports if _is_esp32_port(p)),
        key=lambda p: p.device,
    )
