"""
daq_h5.py

Single source of truth for the HDF5 file schema used by usphere-DAQ.

Every script that reads or writes .h5 files imports from here.
Changing the schema means editing this file only.

File structure
--------------
    beads/
      data/
        pos_data   shape (N_STREAMS × n_samples)  int16 (ADC counts)
          attrs:
            schema_version   int
            Fsamp            float64   sample rate (Hz)
            Time             float64   Unix timestamp at file start
            voltage_min      float64   lower voltage rail (V)
            voltage_max      float64   upper voltage rail (V)

        FPGA         shape (0,)  — FPGA module data
          attrs: one key per FPGA control (e.g. "Dg X", "Ig X", ...)

        <ModuleName> shape (0,)  — any future module
          attrs: whatever that module's plugin defines

Schema versioning
-----------------
- Increment SCHEMA_VERSION for any breaking change (renamed path,
  removed or renamed attribute, reordered channels).
- Purely additive changes (new module, new attribute on a module) do NOT
  require a version bump because read_module() returns {} for absent modules
  and read_attrs() always succeeds regardless of which modules are present.

Adding a module
---------------
No changes needed here. daq_core calls write() with a module_data dict;
write() creates a dataset named after each module automatically.

Renaming the dataset path or changing N_STREAMS
-----------------------------------------------
Change DATASET_PATH / N_STREAMS below and increment SCHEMA_VERSION.
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 3

ADC_BITS: int = 16


# ---------------------------------------------------------------------------
# Dataset location
# ---------------------------------------------------------------------------

DATASET_PATH: str = "beads/data/pos_data"


# ---------------------------------------------------------------------------
# Channel / stream definitions
# ---------------------------------------------------------------------------

N_STREAMS: int = 32
ALL_CHANNELS: list[str] = [f"ai{i}" for i in range(N_STREAMS)]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def volts_to_counts(volts: np.ndarray, voltage_min: float, voltage_max: float) -> np.ndarray:
    """Convert voltage array to 16-bit ADC counts (int16)."""
    lsb = (voltage_max - voltage_min) / (2 ** ADC_BITS)
    counts = np.round(volts / lsb).clip(-32768, 32767).astype(np.int16)
    return counts


def counts_to_volts(counts: np.ndarray, voltage_min: float, voltage_max: float) -> np.ndarray:
    """Convert 16-bit ADC counts back to voltage."""
    lsb = (voltage_max - voltage_min) / (2 ** ADC_BITS)
    return counts.astype(np.float64) * lsb


def write(
    filepath: str | Path,
    channel_data: dict[str, np.ndarray],
    n_samples: int,
    fsamp: float,
    voltage_min: float = -10.0,
    voltage_max: float = 10.0,
    module_data: dict[str, dict] | None = None,
) -> None:
    """
    Write one HDF5 file.

    Parameters
    ----------
    filepath      : destination path
    channel_data  : {channel_name: 1-D array of volts} — only recorded channels needed
    n_samples     : samples per channel (= 2 ** n_bits)
    fsamp         : sample rate in Hz
    voltage_min   : lower voltage rail (V), default -10.0
    voltage_max   : upper voltage rail (V), default  10.0
    module_data   : {module_name: {attr_name: value}}
                    Each module is written as a separate dataset under beads/data/.
                    Missing or None → no module datasets written.
    """
    import time as _time

    pos_data = np.zeros((N_STREAMS, n_samples), dtype=np.int16)
    for i, ch in enumerate(ALL_CHANNELS):
        if ch in channel_data:
            pos_data[i] = volts_to_counts(channel_data[ch], voltage_min, voltage_max)

    with h5py.File(filepath, "w") as f:
        grp = f.require_group("beads/data")

        # Main ADC data — stored as int16 ADC counts
        ds = grp.create_dataset(
            "pos_data",
            data=pos_data,
            compression="gzip",
            compression_opts=1,
        )
        ds.attrs["schema_version"] = SCHEMA_VERSION
        ds.attrs["Fsamp"]          = float(fsamp)
        ds.attrs["Time"]           = _time.time()
        ds.attrs["voltage_min"]    = float(voltage_min)
        ds.attrs["voltage_max"]    = float(voltage_max)

        # One dataset per module — empty array, all data in attrs
        if module_data:
            for module_name, attrs in module_data.items():
                mod_ds = grp.create_dataset(module_name, shape=(0,), dtype=np.float64)
                for key, val in attrs.items():
                    mod_ds.attrs[key] = val


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def recorded_channels(filepath: str | Path) -> dict[str, bool]:
    """
    Return {channel_name: is_recorded} for all N_STREAMS rows.
    A row is 'recorded' if it contains at least one non-zero value.
    Robust to files with fewer rows than N_STREAMS.
    """
    with h5py.File(filepath, "r") as f:
        ds = f[DATASET_PATH]
        n_rows = ds.shape[0]
        return {
            ch: (i < n_rows and bool(np.any(ds[i, :] != 0)))
            for i, ch in enumerate(ALL_CHANNELS)
        }


def read_channel(
    filepath: str | Path,
    channel: str,
) -> tuple[np.ndarray, float]:
    """
    Return (data_array_in_volts, sample_rate_hz) for one channel (row).

    Handles both schema v3 (int16 ADC counts) and v2 (float64 volts).

    Raises
    ------
    ValueError  if channel not in ALL_CHANNELS
    """
    if channel not in ALL_CHANNELS:
        raise ValueError(f"Unknown channel '{channel}'. Valid: {ALL_CHANNELS}")
    idx = ALL_CHANNELS.index(channel)
    with h5py.File(filepath, "r") as f:
        ds = f[DATASET_PATH]
        raw = ds[idx, :]
        fsamp = float(ds.attrs["Fsamp"])
        # Schema v3+: int16 ADC counts → convert to volts
        if raw.dtype == np.int16:
            vmin = float(ds.attrs.get("voltage_min", -10.0))
            vmax = float(ds.attrs.get("voltage_max",  10.0))
            data = counts_to_volts(raw, vmin, vmax)
        else:
            # Schema v2: already float64 volts
            data = raw.astype(np.float64)
    return data, fsamp


def read_attrs(filepath: str | Path) -> dict:
    """Return pos_data fixed attributes (schema_version, Fsamp, Time, voltage range)."""
    with h5py.File(filepath, "r") as f:
        ds = f[DATASET_PATH]
        return {
            "schema_version": int(ds.attrs.get("schema_version", 0)),
            "Fsamp": float(ds.attrs.get("Fsamp", 0.0)),
            "Time":  float(ds.attrs.get("Time",  0.0)),
            "voltage_min": float(ds.attrs.get("voltage_min", -10.0)),
            "voltage_max": float(ds.attrs.get("voltage_max",  10.0)),
        }


def read_module(filepath: str | Path, module_name: str) -> dict:
    """
    Return the attribute dict for a named module dataset.
    Returns {} if the module is not present in the file (e.g. older schema).
    """
    path = f"beads/data/{module_name}"
    with h5py.File(filepath, "r") as f:
        if path not in f:
            return {}
        return {k: (np.array(v).tolist() if isinstance(v, np.ndarray) else v)
                for k, v in f[path].attrs.items()}


def list_modules(filepath: str | Path) -> list[str]:
    """Return names of all module datasets present in the file."""
    with h5py.File(filepath, "r") as f:
        grp = f.get("beads/data", {})
        return [k for k in grp.keys() if k != "pos_data"]


def check_schema(filepath: str | Path) -> tuple[bool, str]:
    """Check whether the file's schema_version matches the current code."""
    try:
        with h5py.File(filepath, "r") as f:
            file_ver = int(f[DATASET_PATH].attrs.get("schema_version", 0))
    except Exception as exc:
        return False, f"Could not open file: {exc}"
    if file_ver == SCHEMA_VERSION:
        return True, f"Schema version {SCHEMA_VERSION} — OK"
    return False, (
        f"Schema mismatch: file has version {file_ver}, "
        f"code expects {SCHEMA_VERSION}"
    )
