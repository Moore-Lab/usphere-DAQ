"""
daq_fpga.py

Device plugin for the NI PXIe-7856R FPGA module.
Reads PID feedback parameters via the nifpga Python interface.

Plugin protocol (required by daq_core)
---------------------------------------
  MODULE_NAME   : str        — dataset name written inside beads/data/ in the H5 file
  DEVICE_NAME   : str        — human-readable label for log messages and the GUI
  CONFIG_FIELDS : list[dict] — describes the GUI fields; keys: key, label, type,
                               default, and (for file fields) filter
  DEFAULTS      : dict       — attribute values written when the device is unavailable
  read(config)  : dict       — read live values using config; raises on any error
  test(config)  : (bool,str) — try read(); return (success, message) for the GUI

Bitfile and resource are supplied at runtime via config (from the Modules tab),
not hardcoded here.  Default values shown in CONFIG_FIELDS appear in the GUI on
first launch; they are then saved to the session log and restored automatically.
"""

from __future__ import annotations

import numpy as np

try:
    import nifpga
    NIFPGA_AVAILABLE = True
except ImportError:
    NIFPGA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------

MODULE_NAME = "FPGA"
DEVICE_NAME = "NI PXIe-7856R FPGA"


# ---------------------------------------------------------------------------
# GUI configuration fields
# Rendered automatically by ModulesWidget in daq_gui.py.
# Each dict: key (config key), label (display), type ("text" or "file"),
#            default (pre-filled value), filter (file dialog, file type only).
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {
        "key":     "bitfile",
        "label":   "Bitfile (.lvbitx)",
        "type":    "file",
        "filter":  "FPGA Bitfiles (*.lvbitx);;All files (*)",
        "default": (
            r"C:\Users\yalem\GitHub\Documents\Optlev\LabView Code"
            r"\FPGA code\FPGA Bitfiles"
            r"\Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx"
        ),
    },
    {
        "key":     "resource",
        "label":   "Resource name",
        "type":    "text",
        "default": "PXI1Slot2",
    },
]


# ---------------------------------------------------------------------------
# FPGA control names
# Order determines the logical grouping in the H5 attrs; each name is used
# as-is as both the nifpga register key and the H5 attribute name.
# ---------------------------------------------------------------------------

CONTROL_NAMES: list[str] = [
    "dg X",         # Derivative gain,     X axis
    " ig X",        # Integral gain,       X axis
    "dg Y",         # Derivative gain,     Y axis
    " ig Y",        # Integral gain,       Y axis
    "pg Z",         # Proportional gain,   Z axis
    " ig Z",        # Integral gain,       Z axis
    "dg Z",         # Derivative gain,     Z axis
    "DC offset X",  # DC offset,           X axis
    "DC offset Y",  # DC offset,           Y axis
    "DC offset Z",  # DC offset,           Z axis
]

# Default values used when the device is unavailable — written as zeros to H5
DEFAULTS: dict = {name: 0.0 for name in CONTROL_NAMES}


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Open a read-only FPGA session and sample all PID controls.

    Parameters
    ----------
    config : dict with keys "bitfile" and "resource"

    Returns
    -------
    dict mapping each control name to its float value

    Raises
    ------
    RuntimeError   if nifpga is not installed
    KeyError       if "bitfile" or "resource" missing from config
    Any nifpga exception if the device is unreachable or a name is wrong
    """
    if not NIFPGA_AVAILABLE:
        raise RuntimeError("nifpga not installed — run: pip install nifpga")

    bitfile  = config["bitfile"]
    resource = config["resource"]

    with nifpga.Session(
        bitfile=bitfile,
        resource=resource,
        run=False,                          # do not start/restart the FPGA
        reset_if_last_session_on_exit=False,# do not reset on close
    ) as session:
        return {name: float(session.registers[name].read()) for name in CONTROL_NAMES}


def test(config: dict) -> tuple[bool, str]:
    """
    Attempt a read and return (success, message).
    Called from the GUI Test button — safe to call from a worker thread.
    """
    try:
        values = read(config)
        sample = ", ".join(f"{k}={v:.4g}" for k, v in list(values.items())[:3])
        return True, f"OK — read {len(values)} parameters  ({sample}, …)"
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
        "bitfile":  CONFIG_FIELDS[0]["default"],
        "resource": CONFIG_FIELDS[1]["default"],
    }
    print(f"Testing {DEVICE_NAME}  ({cfg['resource']})…")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
    if ok:
        values = read(cfg)
        print("\nPID parameters:")
        for name, val in values.items():
            print(f"  {name:<16s}: {val:.6g}")
