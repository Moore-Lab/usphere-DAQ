# daq_core.py — DAQ Backend

`daq_core.py` contains all hardware interaction and file-writing logic.
The GUI (`daq_gui.py`) imports from this module but it can also be used
directly in scripts or notebooks.

---

## Key Objects

### `ALL_CHANNELS`

```python
ALL_CHANNELS: list[str]  # ["ai0", "ai1", ..., "ai31"]
```

Fixed list of all 32 analog input channels on the PXIe-6363.
Used to define the HDF5 schema — every output file contains a dataset
for each of these channels regardless of which were active.

---

### `DAQConfig`

Dataclass holding all acquisition parameters.

```python
from daq_core import DAQConfig

cfg = DAQConfig(
    device         = "PXI1Slot2",   # NI device name (check NI MAX)
    active_channels= ["ai0", "ai1"],# which channels to actually sample
    sample_rate    = 10_000.0,      # Hz
    n_bits         = 20,            # samples per file = 2 ** n_bits
    output_dir     = "data",        # directory for .h5 output
    basename       = "run",         # files written as run_0.h5, run_1.h5, …
    n_files        = 5,             # 0 = continuous until stopped
    voltage_min    = -10.0,         # V  (input range lower bound)
    voltage_max    =  10.0,         # V  (input range upper bound)
)
```

#### Computed properties

| Property | Formula | Example |
|----------|---------|---------|
| `cfg.n_samples` | `2 ** n_bits` | `1_048_576` for N=20 |
| `cfg.duration_s` | `n_samples / sample_rate` | `104.86 s` at 10 kHz, N=20 |

#### Serialisation

```python
d   = cfg.to_dict()          # → plain dict (JSON-serialisable)
cfg = DAQConfig.from_dict(d) # round-trip
```

---

### `DAQRecorder`

Runs acquisition in a background thread.
Three optional callbacks let callers (e.g. the GUI) react to events
**without blocking** — all callbacks are invoked from the worker thread,
so use thread-safe mechanisms (Qt signals, `queue.Queue`, etc.) before
touching UI widgets.

```python
from daq_core import DAQConfig, DAQRecorder

cfg = DAQConfig(active_channels=["ai0", "ai1"], n_bits=18)

recorder = DAQRecorder(
    config          = cfg,
    on_status       = lambda msg: print(msg),          # log messages
    on_file_written = lambda path: print(f"saved {path}"),
    on_finished     = lambda: print("done"),
)

recorder.start()   # non-blocking — returns immediately
# … do other work …
recorder.stop()    # request stop after the current file finishes
```

#### Methods

| Method | Description |
|--------|-------------|
| `start()` | Begin recording in a background thread |
| `stop()` | Request a clean stop; current file completes before halting |
| `is_running()` | Returns `True` while the background thread is alive |

---

## HDF5 File Layout

Files are written to `{output_dir}/{basename}_{index}.h5`.

```
run_0.h5
├── attrs
│   ├── device          "PXI1Slot2"
│   ├── sample_rate_hz  10000.0
│   ├── n_samples       1048576
│   ├── n_bits          20
│   ├── active_channels ["ai0", "ai1"]
│   ├── voltage_min_v   -10.0
│   ├── voltage_max_v    10.0
│   ├── start_time_utc  "2026-03-18T14:32:01"
│   └── duration_s      104.8576
├── ai0    (1048576,) float64  — recorded
├── ai1    (1048576,) float64  — recorded
├── ai2    (0,)       float64  — not recorded (empty)
│   …
└── ai31   (0,)       float64  — not recorded (empty)
```

All 32 datasets are always present.
Recorded channels are stored gzip-compressed (level 1).
Unrecorded channels have shape `(0,)` and take negligible space.

---

## Simulation Mode

If the `nidaqmx` package is not installed (or the NI driver is absent),
`DAQRecorder` automatically falls back to **simulation mode**:

- Active channels are filled with synthetic sine waves + noise
- Acquisition proceeds at up to 2× real-time speed (capped to avoid
  freezing the GUI during long-duration files)
- HDF5 files are written identically to real acquisitions
- A `[SIM]` tag appears in status messages

---

## Reading Output Files

```python
import h5py, numpy as np

with h5py.File("data/run_0.h5", "r") as f:
    sr = f.attrs["sample_rate_hz"]
    x  = f["ai0"][:]          # shape (n_samples,) or (0,) if not recorded
    t  = np.arange(len(x)) / sr
```

---

*Last updated: 2026-03-18*
