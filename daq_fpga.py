"""
daq_fpga.py

Device plugin for the NI PXIe-7856R FPGA module.
Reads all control/indicator registers via the nifpga Python interface.

Plugin protocol (required by daq_core)
---------------------------------------
  MODULE_NAME   : str        — dataset name written inside beads/data/ in the H5 file
  DEVICE_NAME   : str        — human-readable label for log messages and the GUI
  CONFIG_FIELDS : list[dict] — describes the GUI fields; keys: key, label, type,
                               default, and (for file fields) filter
  DEFAULTS      : dict       — attribute values written when the device is unavailable
  read(config)  : dict       — read live values using config; raises on any error
  test(config)  : (bool,str) — try read(); return (success, message) for the GUI

Each register is attempted individually so that a single unreadable register
(e.g. a LabVIEW cluster type) never blocks the rest. Unreadable registers
are stored as 0.0. Booleans are stored as 0.0 / 1.0.

Bitfile and resource are supplied at runtime via config (from the Modules tab),
not hardcoded here.  Default values shown in CONFIG_FIELDS appear in the GUI on
first launch; they are then saved to the session log and restored automatically.
"""

from __future__ import annotations

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
# FPGA register names
# Names must match exactly (including case) as they appear in the bitfile.
# Each is read as float; booleans become 0.0/1.0, integers are cast to float.
# Registers that cannot be read (e.g. cluster types) are stored as 0.0.
# ---------------------------------------------------------------------------

CONTROL_NAMES: list[str] = [

    # --- Status / timing ---
    "Stop",
    "FPGA Error Out",
    "Count(uSec)",

    # --- Z axis ---
    "Z Setpoint",
    "AI Z plot",
    "Upper lim Z",
    "Lower lim Z",
    " ig Z",
    "fb Z plot",
    "dg Z",
    "dg Z before",
    " ig Z before",
    "pg Z",
    "pg Z before",
    "pg Z mod",
    "pz?",
    "DC offset Z",
    "fb Z before chamber plot",
    "tot_fb Z plot",
    "Z before Setpoint",
    "AI Z before chamber plot",
    "Use Z PID before",
    "HP Coeff Z",
    "HP Coeff Z before",
    "dg band Z",
    "dg band Z before",
    "HP Coeff band Z",
    "LP Coeff band Z",
    "LP Coeff band Z before",
    "HP coeff band Z before",
    "LP Coeff Z",
    "LP Coeff Z before",
    "final filter coeff Z",
    "final filter coeff Z before",
    "Lower lim Z before",
    "Upper lim Z before",
    "activate COMz",
    "dgz mod",
    "Reset z accum",
    "accum reset z1",
    "accum out z1",
    "accurrm reset z2",
    "accum out z2",
    "Notch coeff z 1",
    "Notch coeff z 2",
    "Notch coeff z 3",
    "Notch coeff z 4",

    # --- Y axis ---
    "Y Setpoint",
    "AI Y plot",
    "pg Y",
    "Upper lim Y",
    "Lower lim Y",
    " ig Y",
    "fb Y plot",
    "dg Y",
    "dg Y before",
    " ig Y before",
    "pg Y before",
    "DC offset Y",
    "fb Y before chamber plot",
    "tot_fb Y plot",
    "Y before Setpoint",
    "AI Y before chamber plot",
    "Use Y PID before",
    "HP Coeff Y",
    "HP Coeff Y before",
    "dg band Y",
    "dg band Y before",
    "HP Coeff band Y",
    "LP Coeff band Y",
    "LP Coeff band Y before",
    "HP coeff band Y before",
    "LP Coeff Y",
    "LP Coeff Y before",
    "final filter coeff Y",
    "final filter coeff Y before",
    "Lower lim Y before",
    "Upper lim Y before",
    "dgy mod",
    "activate COMy",
    "Reset y accum",
    "Notch coeff y 1",
    "Notch coeff y 2",
    "Notch coeff y 3",
    "Notch coeff y 4",

    # --- X axis ---
    "X Setpoint",
    "AI X plot",
    "pg X",
    "Upper lim X",
    "Lower lim X",
    "ig X",
    "fb X plot",
    "dg X",
    "dg X before",
    " ig X before",
    "pg X before",
    "DC offset X",
    "fb X before chamber plot",
    "tot_fb X plot",
    "X before Setpoint",
    "AI X before chamber plot",
    "Use X PID before",
    "HP Coeff X",
    "HP Coeff X before",
    "dg band X",
    "dg band X before",
    "HP Coeff band X",
    "LP Coeff band X",
    "LP Coeff band X before",
    "HP coeff band X before",
    "LP Coeff X",
    "LP Coeff X before",
    "final filter coeff X",
    "final filter coeff X before",
    "Lower lim X before",
    "Upper lim X before",
    "dgx mod",
    "activate COMx",
    "Reset x accum",
    "Notch coeff x 1",
    "Notch coeff x 2",
    "Notch coeff x 3",
    "Notch coeff x 4",

    # --- Arbitrary waveform ---
    "Arb gain (ch0)",
    "Arb gain (ch1)",
    "Arb gain (ch2)",
    "write_address",
    "data_buffer_1",
    "data_buffer2",
    "data_buffer3",
    "Arb steps per cycle",
    "ready_to_write",
    "written_address",

    # --- EOM ---
    "EOM_amplitude",
    "EOM_threshold",
    "EOM reset",
    "EOM_seed",
    "EOM_offset",
    "eom sine frequency (periods/tick)",
    "Amplitude_sine_EOM",

    # --- COM output ---
    "Trigger for COM out",
    "offset",
    "amplitude",
    "frequency (periods/tick)",
    "duty cycle (periods)",

    # --- Global ---
    "Big Number",
    "X_emergency_threshould",
    "Y_emergency_threshould",
    "No_integral_gain",
    "master x",
    "master y",

    # --- AO channels (4–7) ---
    "frequency AO4",
    "reset AO4",
    "phase offset AO4",
    "Amplitude AO4",
    "frequency AO5",
    "reset AO5",
    "phase offset AO5",
    "Amplitude AO5",
    "frequency AO6",
    "reset AO6",
    "phase offset AO6",
    "Amplitude AO6",
    "frequency AO7",
    "reset AO7",
    "phase offset AO7",
    "Amplitude AO7",
]

# Default values used when the device is unavailable — written as zeros to H5
DEFAULTS: dict = {name: 0.0 for name in CONTROL_NAMES}


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """
    Open a read-only FPGA session and sample all registers.

    Each register is attempted individually so that a single unreadable
    register (e.g. a LabVIEW cluster type) never prevents the rest from
    being saved.

    Parameters
    ----------
    config : dict with keys "bitfile" and "resource"

    Returns
    -------
    dict mapping each control name to its float value (0.0 if unreadable)

    Raises
    ------
    RuntimeError   if nifpga is not installed
    KeyError       if "bitfile" or "resource" is missing from config
    Any nifpga exception if the session itself cannot be opened
    """
    if not NIFPGA_AVAILABLE:
        raise RuntimeError("nifpga not installed — run: pip install nifpga")

    bitfile  = config["bitfile"]
    resource = config["resource"]

    with nifpga.Session(
        bitfile=bitfile,
        resource=resource,
        run=False,
        reset_if_last_session_on_exit=False,
    ) as session:
        result = {}
        for name in CONTROL_NAMES:
            try:
                result[name] = float(session.registers[name].read())
            except Exception:
                result[name] = 0.0
        return result


def test(config: dict) -> tuple[bool, str]:
    """
    Attempt a read and return (success, message).
    Called from the GUI Test button — safe to call from a worker thread.
    """
    try:
        values = read(config)
        n_nonzero = sum(1 for v in values.values() if v != 0.0)
        sample = ", ".join(
            f"{k}={v:.4g}"
            for k, v in list(values.items())[:3]
        )
        return True, f"OK — {len(values)} registers ({n_nonzero} non-zero)  [{sample}, …]"
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
    if len(sys.argv) > 1:
        cfg["resource"] = sys.argv[1]

    print(f"Testing {DEVICE_NAME}  ({cfg['resource']})…")
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
    if ok:
        values = read(cfg)
        print(f"\nAll {len(values)} registers:")
        for name, val in values.items():
            print(f"  {name:<40s}: {val:.6g}")
