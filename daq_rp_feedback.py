"""
daq_rp_feedback.py

Device plugin for the Red Pitaya usphere feedback controller (two boards).
Replaces the NI PXIe-7856R FPGA scalar path (daq_fpga.py) with a thin poll of
the current control-loop scalars, written into each run's HDF5 file alongside
the NI-6363 channels.

Plugin protocol (required by daq_core — see daq_fpga.py / daq_edwards_tic.py)
----------------------------------------------------------------------------
  MODULE_NAME   : str        — dataset name written inside beads/data/ in the H5 file
  DEVICE_NAME   : str        — human-readable label for log messages and the GUI
  CONFIG_FIELDS : list[dict] — describes the GUI fields
  DEFAULTS      : dict       — attribute values written when the device is unavailable
  read(config)  : dict       — read live scalars using config; returns the dict
  test(config)  : (bool,str) — try read(); return (success, message) for the GUI

Two boards (INTERFACES): Board A owns the x,y CoM lock-in lanes; Board B owns z
(CoM) plus the spin freq-counter/lock lane.  Each board runs the framework's
`rp_daemon`; this plugin connects with a read-only `rp_optomech.BoardSession`
(``start=False`` — no SSH), reads the scalars by register name, and closes.

Every register is attempted individually: an unreadable register (or an
unreachable board, i.e. SIM mode with no hardware) stores 0.0, exactly like
daq_fpga.  ``read`` therefore always returns the full scalar dict and never
raises for a missing device.

Scalar dict (INTERFACES §5)
---------------------------
  spin_freq_hz  x_mag  y_mag  z_mag
  x_locked  y_locked  z_locked  spin_locked
  drop_count_a  drop_count_b

Notes
-----
* ``spin_freq_hz`` carries the raw decimated freq-counter value
  (``freq_count_dec_spin``, ∝ f_rot); calibration to physical Hz is the driver's
  job (INTERFACES §6), not this thin logger's.
* CoM axes have no hardware "lock-acquisition" bit (only the spin lane does), so
  ``x/y/z_locked`` report the per-axis ``pid_enable`` control bit — i.e. whether
  that feedback loop is engaged.  ``spin_locked`` reads the spin lane's
  ``lock_status`` locked bit.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------

MODULE_NAME = "RP_FEEDBACK"
DEVICE_NAME = "Red Pitaya usphere feedback (A+B)"


# ---------------------------------------------------------------------------
# GUI configuration fields
# ---------------------------------------------------------------------------

CONFIG_FIELDS: list[dict] = [
    {"key": "host_a", "label": "Board A host/IP", "type": "text", "default": "192.168.1.100"},
    {"key": "port_a", "label": "Board A daemon port", "type": "text", "default": "9001"},
    {"key": "host_b", "label": "Board B host/IP", "type": "text", "default": "192.168.1.101"},
    {"key": "port_b", "label": "Board B daemon port", "type": "text", "default": "9001"},
    {"key": "simulate", "label": "Simulate (no hardware)", "type": "text", "default": "false"},
]


# ---------------------------------------------------------------------------
# Output keys and defaults
# ---------------------------------------------------------------------------

_KEYS = (
    "spin_freq_hz",
    "x_mag", "y_mag", "z_mag",
    "x_locked", "y_locked", "z_locked", "spin_locked",
    "drop_count_a", "drop_count_b",
)

DEFAULTS: dict = {k: 0.0 for k in _KEYS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_sim(config: dict) -> bool:
    return str(config.get("simulate", "")).strip().lower() in ("1", "true", "yes", "on")


def _load_deps():
    """Import the framework host package + the two generated register modules.

    Kept lazy so the plugin imports cleanly (and runs in SIM mode) even where
    rp_optomech / the register modules are not on the path.
    """
    from rp_optomech.board import BoardSession
    import registers_board_a as regs_a
    import registers_board_b as regs_b
    return BoardSession, regs_a, regs_b


def _rd(board, name) -> float:
    """Read a register by name; 0.0 on any failure (plugin convention)."""
    try:
        return float(board.read(name))
    except Exception:
        return 0.0


def _rd_field(board, name, field) -> float:
    """Read one bitfield by name; 0.0 on any failure."""
    try:
        return float(board.read_field(name, field))
    except Exception:
        return 0.0


def _open(BoardSession, host, port, regs):
    return BoardSession(host, regs, port=int(port), start=False)


def _safe_close(board) -> None:
    try:
        board.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Plugin interface
# ---------------------------------------------------------------------------

def read(config: dict) -> dict:
    """Poll both boards for the current control-loop scalars.

    Returns the full scalar dict.  Any unreachable board or unreadable register
    leaves the corresponding value at 0.0 (SIM mode = every value 0.0).
    """
    result = dict(DEFAULTS)
    if _is_sim(config):
        return result

    try:
        BoardSession, regs_a, regs_b = _load_deps()
    except Exception:
        return result  # dependencies unavailable -> treat as SIM

    # --- Board A: x, y CoM lock-in lanes + its drop counter ---
    ba = None
    try:
        ba = _open(BoardSession, config.get("host_a", ""),
                   config.get("port_a", 9001), regs_a)
        result["x_mag"] = _rd(ba, "meas_mag_x")
        result["y_mag"] = _rd(ba, "meas_mag_y")
        result["x_locked"] = _rd_field(ba, "control", "pid_enable_x")
        result["y_locked"] = _rd_field(ba, "control", "pid_enable_y")
        result["drop_count_a"] = _rd(ba, "buffer_drop_count")
    except Exception:
        pass  # board unreachable -> leave Board-A scalars at 0.0
    finally:
        if ba is not None:
            _safe_close(ba)

    # --- Board B: z CoM lane + spin freq-counter/lock lane + its drop counter ---
    bb = None
    try:
        bb = _open(BoardSession, config.get("host_b", ""),
                   config.get("port_b", 9001), regs_b)
        result["z_mag"] = _rd(bb, "meas_mag_z")
        result["spin_freq_hz"] = _rd(bb, "freq_count_dec_spin")
        result["z_locked"] = _rd_field(bb, "control", "pid_enable_z")
        result["spin_locked"] = _rd_field(bb, "lock_status_spin", "locked")
        result["drop_count_b"] = _rd(bb, "buffer_drop_count")
    except Exception:
        pass  # board unreachable -> leave Board-B scalars at 0.0
    finally:
        if bb is not None:
            _safe_close(bb)

    return result


def test(config: dict) -> tuple[bool, str]:
    """Attempt a read and return (success, message) for the GUI Test button."""
    try:
        values = read(config)
    except Exception as exc:  # read is defensive, but never let Test throw
        return False, f"{type(exc).__name__}: {exc}"

    if _is_sim(config):
        return True, "SIM — no hardware; all scalars 0.0"

    n_nonzero = sum(1 for v in values.values() if v != 0.0)
    if n_nonzero == 0:
        return False, (
            "No live values (both boards unreachable or all registers 0). "
            "Check host/port and that rp_daemon is running."
        )
    return True, (
        f"OK — spin={values['spin_freq_hz']:.4g}, "
        f"x_mag={values['x_mag']:.4g}, y_mag={values['y_mag']:.4g}, "
        f"z_mag={values['z_mag']:.4g}, "
        f"drops A/B={values['drop_count_a']:.0f}/{values['drop_count_b']:.0f}"
    )


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = {f["key"]: f["default"] for f in CONFIG_FIELDS}
    ok, msg = test(cfg)
    print(f"{'OK' if ok else 'FAILED'}: {msg}")
    for name, val in read(cfg).items():
        print(f"  {name:<16s}: {val}")
