"""
daq_fpga.py

Device plugin for the NI PXIe-7856R FPGA module.
Reads PID feedback parameters via the nifpga Python interface.

Plugin protocol (required by daq_core.py)
------------------------------------------
  DEVICE_NAME : str   — label used in log messages
  DEFAULTS    : dict  — H5 attribute values written when the device is unavailable
  read()      : dict  — reads live values; must raise on any error so
                        daq_core can catch it and fall back to DEFAULTS

Adding this device to daq_core
-------------------------------
This module is auto-loaded by daq_core via _load_plugin("daq_fpga").
No other changes to daq_core are needed.

Dependencies
------------
    pip install nifpga
NI-RIO driver must also be installed (ships with LabVIEW or NI-RIO standalone).
"""

from __future__ import annotations

import numpy as np

try:
    import nifpga
    NIFPGA_AVAILABLE = True
except ImportError:
    NIFPGA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration — update these if the bitfile or slot changes
# ---------------------------------------------------------------------------

BITFILE = (
    r"C:\Users\yalem\GitHub\Documents\Optlev\LabView Code"
    r"\FPGA code\FPGA Bitfiles"
    r"\Microspherefeedb_FPGATarget2_Notches_all_channels_20260203_DCM.lvbitx"
)

# Device resource name as shown in NI MAX (e.g. "RIO0" or "PXI1Slot3").
# Verify with NI MAX if reads fail — FlexRIO cards sometimes appear as "RIO<n>"
# rather than the chassis slot address.
RESOURCE = "PXI1Slot2"

# Ordered list of FPGA control names exactly as they appear in the LabVIEW VI.
# The position in this list determines the index in the 10-element PID array
# stored in the H5 file under ds.attrs["PID"].
CONTROL_NAMES: list[str] = [
    "Dg X",         # [0] Derivative gain,    X axis
    "Ig X",         # [1] Integral gain,       X axis
    "Dg Y",         # [2] Derivative gain,     Y axis
    "Ig Y",         # [3] Integral gain,       Y axis
    "Pg Z",         # [4] Proportional gain,   Z axis
    "Ig Z",         # [5] Integral gain,       Z axis
    "Dg Z",         # [6] Derivative gain,     Z axis
    "DC offset X",  # [7] DC offset,           X axis
    "DC offset Y",  # [8] DC offset,           Y axis
    "DC offset Z",  # [9] DC offset,           Z axis
]


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

DEVICE_NAME = "NI PXIe-7856R FPGA"

# Values used when the FPGA is unreachable — same shape/dtype as live data
DEFAULTS: dict = {
    "PID": np.zeros(len(CONTROL_NAMES), dtype=np.float32),
}


def read() -> dict:
    """
    Open a read-only FPGA session, sample all PID controls, and return them.

    The session is opened with run=False and reset_if_last_session_on_exit=False
    so that a running LabVIEW VI is not interrupted.

    Raises
    ------
    RuntimeError
        If nifpga is not installed.
    nifpga.ErrorStatus (or any other exception)
        If the device is unreachable or a register name is wrong.
        daq_core catches all exceptions and falls back to DEFAULTS.
    """
    if not NIFPGA_AVAILABLE:
        raise RuntimeError(
            "nifpga package not installed — run: pip install nifpga"
        )

    values: list[float] = []
    with nifpga.Session(
        bitfile=BITFILE,
        resource=RESOURCE,
        run=False,                          # do not start/restart the FPGA
        reset_if_last_session_on_exit=False,# do not reset the FPGA on close
    ) as session:
        for name in CONTROL_NAMES:
            values.append(float(session.registers[name].read()))

    return {"PID": np.array(values, dtype=np.float32)}


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Connecting to {DEVICE_NAME} ({RESOURCE})…")
    try:
        result = read()
        pid = result["PID"]
        print("PID parameters read successfully:")
        for name, val in zip(CONTROL_NAMES, pid):
            print(f"  {name:<16s}: {val:.6g}")
    except Exception as e:
        print(f"Error: {e}")
        print("Defaults would be written to H5:")
        for name, val in zip(CONTROL_NAMES, DEFAULTS["PID"]):
            print(f"  {name:<16s}: {val}")
