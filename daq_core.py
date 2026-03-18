"""
daq_core.py

Backend for NI PXIe-6363 analog input recording.
Handles DAQ configuration, task setup, HDF5 writing, and multi-file recording.
"""

from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np

try:
    import nidaqmx
    import nidaqmx.constants as nidaqmx_const
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False

# Fixed channel schema — PXIe-6363 has 32 analog input channels.
# All files always contain a dataset for every channel so column positions
# never change; unrecorded channels are stored with shape (0,).
ALL_CHANNELS: list[str] = [f"ai{i}" for i in range(32)]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class DAQConfig:
    device: str = "PXI1Slot2"
    active_channels: list[str] = field(default_factory=lambda: ["ai0", "ai1", "ai2", "ai3"])
    sample_rate: float = 10_000.0       # Hz
    n_bits: int = 20                    # samples per file = 2 ** n_bits
    output_dir: str = "data"
    basename: str = "run"
    n_files: int = 1                    # 0 = run continuously until stopped
    voltage_min: float = -10.0          # V
    voltage_max: float = 10.0           # V

    @property
    def n_samples(self) -> int:
        return 2 ** self.n_bits

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.sample_rate if self.sample_rate else 0.0

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "active_channels": list(self.active_channels),
            "sample_rate": self.sample_rate,
            "n_bits": self.n_bits,
            "output_dir": str(self.output_dir),
            "basename": self.basename,
            "n_files": self.n_files,
            "voltage_min": self.voltage_min,
            "voltage_max": self.voltage_max,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DAQConfig:
        valid_keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in valid_keys})


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class DAQRecorder:
    """
    Records analog input from the NI PXIe-6363.

    All callbacks are invoked from the background thread — wire them through
    thread-safe mechanisms (e.g. Qt signals) before updating UI elements.

    Callbacks
    ---------
    on_status(msg: str)
        Log / status message.
    on_file_written(path: Path)
        Called after each HDF5 file is successfully saved.
    on_finished()
        Called when recording ends (completed or stopped).
    """

    def __init__(
        self,
        config: DAQConfig,
        on_status=None,
        on_file_written=None,
        on_finished=None,
    ):
        self.config = config
        self._on_status = on_status or print
        self._on_file_written = on_file_written
        self._on_finished = on_finished
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Start recording in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the recorder to stop after the current acquisition chunk."""
        self._stop_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._on_status(msg)

    def _filepath(self, index: int) -> Path:
        out = Path(self.config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return out / f"{self.config.basename}_{index}.h5"

    def _write_h5(self, filepath: Path, data: dict[str, np.ndarray]):
        """Write one HDF5 file with a fixed schema for all 32 channels."""
        cfg = self.config
        with h5py.File(filepath, "w") as f:
            # File-level metadata
            f.attrs["device"] = cfg.device
            f.attrs["sample_rate_hz"] = cfg.sample_rate
            f.attrs["n_samples"] = cfg.n_samples
            f.attrs["n_bits"] = cfg.n_bits
            f.attrs["voltage_min_v"] = cfg.voltage_min
            f.attrs["voltage_max_v"] = cfg.voltage_max
            f.attrs["active_channels"] = list(cfg.active_channels)
            f.attrs["start_time_utc"] = datetime.datetime.utcnow().isoformat()
            f.attrs["duration_s"] = cfg.duration_s

            # One dataset per channel — always at the same location.
            # Recorded channels: shape (n_samples,), gzip-compressed.
            # Unrecorded channels: shape (0,) — present but empty.
            for ch in ALL_CHANNELS:
                if ch in data:
                    ds = f.create_dataset(
                        ch,
                        data=data[ch],
                        dtype=np.float64,
                        compression="gzip",
                        compression_opts=1,
                    )
                    ds.attrs["recorded"] = True
                else:
                    ds = f.create_dataset(ch, shape=(0,), dtype=np.float64)
                    ds.attrs["recorded"] = False
                ds.attrs["units"] = "V"
                ds.attrs["channel_index"] = int(ch[2:])

    def _acquire_one_file(self, file_index: int) -> bool:
        """
        Acquire one file's worth of samples and write to HDF5.
        Returns True on success, False if the stop event was set.
        """
        cfg = self.config
        filepath = self._filepath(file_index)
        n_samples = cfg.n_samples
        active = list(cfg.active_channels)

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log(
            f"[{ts}] File {file_index}: {filepath.name}  "
            f"({n_samples:,} samples @ {cfg.sample_rate:g} Hz = {cfg.duration_s:.2f} s)"
        )

        data: dict[str, np.ndarray] = {}

        if not NIDAQMX_AVAILABLE:
            # ----- Simulation mode (no hardware) -----
            self._log("  [SIM] nidaqmx not available — generating synthetic data")
            t = np.linspace(0, cfg.duration_s, n_samples)
            for i, ch in enumerate(active):
                data[ch] = (
                    np.sin(2 * np.pi * (i + 1) * 10 * t)
                    + 0.05 * np.random.randn(n_samples)
                )
            # Simulate the passage of time (capped so the GUI stays snappy)
            deadline = time.monotonic() + min(cfg.duration_s, 2.0)
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    return False
                time.sleep(0.05)

        else:
            # ----- Real hardware -----
            chunk = min(100_000, n_samples)
            buffers: dict[str, list[np.ndarray]] = {ch: [] for ch in active}

            with nidaqmx.Task() as task:
                for ch in active:
                    task.ai_channels.add_ai_voltage_chan(
                        f"{cfg.device}/{ch}",
                        terminal_config=nidaqmx_const.TerminalConfiguration.RSE,
                        min_val=cfg.voltage_min,
                        max_val=cfg.voltage_max,
                    )
                task.timing.cfg_samp_clk_timing(
                    rate=cfg.sample_rate,
                    sample_mode=nidaqmx_const.AcquisitionType.FINITE,
                    samps_per_chan=n_samples,
                )
                task.start()

                samples_read = 0
                while samples_read < n_samples:
                    if self._stop_event.is_set():
                        task.stop()
                        return False
                    to_read = min(chunk, n_samples - samples_read)
                    raw = task.read(
                        number_of_samples_per_channel=to_read,
                        timeout=max(30.0, cfg.duration_s * 2),
                    )
                    if len(active) == 1:
                        buffers[active[0]].append(np.asarray(raw, dtype=np.float64))
                    else:
                        for i, ch in enumerate(active):
                            buffers[ch].append(np.asarray(raw[i], dtype=np.float64))
                    samples_read += to_read

                task.stop()

            for ch in active:
                data[ch] = np.concatenate(buffers[ch])

        self._write_h5(filepath, data)
        self._log(f"  -> Saved: {filepath}")
        if self._on_file_written:
            self._on_file_written(filepath)
        return True

    def _run(self):
        cfg = self.config
        continuous = (cfg.n_files == 0)
        file_index = 0
        try:
            while not self._stop_event.is_set():
                ok = self._acquire_one_file(file_index)
                if not ok:
                    self._log("Recording interrupted by user.")
                    break
                file_index += 1
                if not continuous and file_index >= cfg.n_files:
                    self._log(f"Done — {file_index} file(s) written.")
                    break
        except Exception as e:
            self._log(f"Fatal error: {e}")
        finally:
            if self._on_finished:
                self._on_finished()
