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

import numpy as np
import daq_h5

try:
    import nidaqmx
    import nidaqmx.constants as nidaqmx_const
    NIDAQMX_AVAILABLE = True
except ImportError:
    NIDAQMX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Device plugin registry
# ---------------------------------------------------------------------------
# Each plugin module must live alongside daq_core.py and expose:
#
#   DEVICE_NAME : str   — label printed in status/log messages
#   DEFAULTS    : dict  — H5 attribute values used when the device is absent
#   read()      : dict  — reads live values; MUST raise on any error
#
# daq_core calls every registered plugin at the start of each file acquisition.
# If read() raises for any reason the plugin's DEFAULTS are substituted, so a
# missing or crashing device never prevents a file from being written.
#
# To add a new device: create a new module following the protocol above,
# then append its name to _PLUGIN_MODULES below.
# ---------------------------------------------------------------------------

_PLUGIN_MODULES: list[str] = [
    "daq_fpga",
    "daq_edwards_tic",
    # add future device modules here
]

# Populated at import time: list of plugin module objects
_PLUGINS: list = []


def _load_plugins() -> None:
    import importlib
    for module_name in _PLUGIN_MODULES:
        try:
            mod = importlib.import_module(module_name)
            _PLUGINS.append(mod)
        except Exception:
            pass  # module absent or broken at import — skip silently


_load_plugins()


def get_plugins() -> list:
    """Return registered plugin module objects (used by the GUI Modules tab)."""
    return list(_PLUGINS)


def _collect_module_data(log_fn, module_configs: dict) -> dict[str, dict]:
    """
    Query every registered plugin and return {MODULE_NAME: {attr: value}}.

    config for each plugin is looked up by MODULE_NAME from module_configs.
    Plugins that raise have their DEFAULTS substituted so a missing device
    never prevents a file from being written.
    """
    result: dict[str, dict] = {}
    for mod in _PLUGINS:
        config = module_configs.get(mod.MODULE_NAME, {})
        try:
            result[mod.MODULE_NAME] = mod.read(config)
        except Exception as exc:
            log_fn(f"  [WARN] {mod.DEVICE_NAME}: {exc} — using defaults")
            result[mod.MODULE_NAME] = dict(mod.DEFAULTS)
    return result


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
    module_configs: dict = field(default_factory=dict)  # {MODULE_NAME: {key: value}}

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
            "module_configs": dict(self.module_configs),
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

    def _write_h5(
        self,
        filepath: Path,
        data: dict[str, np.ndarray],
        module_data: dict[str, dict],
    ):
        """Delegate all file writing to daq_h5 — schema lives there."""
        cfg = self.config
        daq_h5.write(
            filepath=filepath,
            channel_data=data,
            n_samples=cfg.n_samples,
            fsamp=cfg.sample_rate,
            module_data=module_data,
        )

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

        module_data = _collect_module_data(self._log, cfg.module_configs)
        self._write_h5(filepath, data, module_data)
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
