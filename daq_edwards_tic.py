"""
daq_edwards_tic.py

Device plugin for the Edwards TIC (Turbo Instrument Controller).
Reads Pirani and wide-range gauge (Pirani/CCG) pressures via RS-232.

Plugin protocol (required by daq_core)
---------------------------------------
  MODULE_NAME   : str        — dataset name written inside beads/data/ in the H5 file
  DEVICE_NAME   : str        — human-readable label for log messages and the GUI
  CONFIG_FIELDS : list[dict] — describes the GUI fields
  DEFAULTS      : dict       — attribute values written when the device is unavailable
  read(config)  : dict       — read live values; raises on any error
  test(config)  : (bool,str) — try read(); return (success, message) for the GUI

Serial protocol
---------------
The Edwards TIC uses a simple ASCII query/response protocol over RS-232:
  Query:    ?V<id>\r
  Response: =V<id> <value>\r   (success)
            *V<id> <code>\r    (error / out-of-range)

Pressure values are returned in mbar.

Parameter IDs
-------------
The IDs below are correct for a standard TIC with an active Pirani gauge on
input 1 and a wide-range gauge (WRG / Pirani+CCG) on input 2.  If your gauge
numbering differs, update PARAM_PIRANI and PARAM_WIDE_RANGE to match your TIC
manual (section "RS232 parameter list").

  913 — Pirani gauge pressure (input 1)
  914 — Wide-range gauge pressure (input 2)
"""

from __future__ import annotations

import re

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------

MODULE_NAME = "TIC"
DEVICE_NAME = "Edwards TIC"


# ---------------------------------------------------------------------------
# GUI configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "port",
        "label":   "COM port",
        "type":    "text",
        "default": "COM3",
    },
    {
        "key":     "baudrate",
        "label":   "Baud rate",
        "type":    "text",
        "default": "9600",
    },
]


# ---------------------------------------------------------------------------
# Parameter IDs
# Verify against your TIC manual if gauge inputs differ from the defaults.
# ---------------------------------------------------------------------------

PARAM_PIRANI     = 914   # Pirani gauge (APGX),        input 2 — TIC returns Pa
PARAM_WIDE_RANGE = 913   # Wide-range gauge (WRG),     input 1 — TIC returns Pa


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

KEY_APGX = "APGX"
KEY_WRG  = "WRG"

DEFAULTS: dict = {
    KEY_APGX: 0.0,
    KEY_WRG:  0.0,
}


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def _query(ser, param_id: int) -> float:
    """
    Send one ?V<id> query and parse the numeric response.

    Raises
    ------
    IOError   if the TIC returns an error code or an unrecognised response
    ValueError if the value field cannot be converted to float
    """
    cmd = f"?V{param_id}\r"
    ser.reset_input_buffer()
    ser.write(cmd.encode("ascii"))

    raw = ser.read_until(b"\r").decode("ascii", errors="replace").strip()

    if not raw:
        raise IOError(f"No response to ?V{param_id} — check COM port and cable")

    # Error response: *Vnnn <code>
    if raw.startswith("*"):
        raise IOError(f"TIC error for parameter {param_id}: {raw!r}")

    # Success response: =Vnnn <value>[;<extra fields>...]
    # The TIC may return semicolon-delimited fields; pressure is always first.
    match = re.match(r"=V\d+\s+([\S]+)", raw)
    if not match:
        raise IOError(f"Unexpected TIC response for parameter {param_id}: {raw!r}")

    first_field = match.group(1).split(";")[0]
    # TIC returns pressure in Pascals; convert to mbar (1 mbar = 100 Pa)
    return float(first_field) / 100.0


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Open a serial session to the TIC and read both gauge pressures.

    Parameters
    ----------
    config : dict with keys "port" and (optionally) "baudrate"

    Returns
    -------
    dict with keys KEY_PIRANI and KEY_WIDE_RANGE, values in mbar

    Raises
    ------
    RuntimeError   if pyserial is not installed
    KeyError       if "port" is missing from config
    serial.SerialException / IOError on communication failure
    """
    if not SERIAL_AVAILABLE:
        raise RuntimeError("pyserial not installed — run: pip install pyserial")

    port     = config["port"]
    baudrate = int(config.get("baudrate", 9600))

    with serial.Serial(
        port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2.0,
    ) as ser:
        apgx = _query(ser, PARAM_PIRANI)
        wrg  = _query(ser, PARAM_WIDE_RANGE)

    return {
        KEY_APGX: apgx,
        KEY_WRG:  wrg,
    }


def test(config: dict) -> tuple[bool, str]:
    """
    Attempt a read and return (success, message).
    Called from the GUI Test button — safe to call from a worker thread.
    """
    try:
        values = read(config)
        apgx = values[KEY_APGX]
        wrg  = values[KEY_WRG]
        return True, (
            f"OK — APGX: {apgx:.3g} mbar  |  "
            f"WRG: {wrg:.3g} mbar"
        )
    except KeyError as e:
        return False, f"Missing config field: {e}"
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    cfg = {
        "port":     CONFIG_FIELDS[0]["default"],
        "baudrate": CONFIG_FIELDS[1]["default"],
    }
    if len(sys.argv) > 1:
        cfg["port"] = sys.argv[1]

    print(f"Testing {DEVICE_NAME} on {cfg['port']}…")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
    if ok:
        values = read(cfg)
        print("\nGauge readings:")
        for name, val in values.items():
            print(f"  {name:<30s}: {val:.3e} mbar")
