# usphere-DAQ — Overview

Software for data acquisition and experiment control in the optically
levitated microsphere experiment.
Interfaces with a **National Instruments PXIe-6363** DAQ card via
NI-DAQmx and saves data to HDF5.

---

## Quick Start

```bash
# 1. Install dependencies (creates a .venv automatically)
python install_deps.py

# 2. Activate the environment
source .venv/Scripts/activate      # Windows bash / Git Bash
# or
.venv\Scripts\activate             # Windows cmd / PowerShell

# 3. Launch the GUI
python daq_gui.py
```

> **No hardware?** The software runs in simulation mode if the NI-DAQmx
> driver is not installed. HDF5 files are written with synthetic data so
> the full pipeline can be tested on any machine.

---

## Repository Layout

```
usphere-DAQ/
│
├── daq_gui.py              ← Run this to operate the DAQ
├── daq_core.py             ← Backend (DAQConfig, DAQRecorder, HDF5 writing)
│
├── install_deps.py         ← Creates .venv and installs requirements.txt
├── requirements.txt        ← Python package list
│
├── daq_session_log.jsonl   ← Rolling session log (auto-created on first run)
│
├── data/                   ← Default output directory for .h5 files
│
├── README.md               ← This file
│
├── docs/
│   ├── SETUP.md            ← Installation and environment details
│   ├── DAQ_CORE.md         ← daq_core.py API reference
│   └── DAQ_GUI.md          ← daq_gui.py GUI reference
│
├── development/
│   ├── MEASUREMENT_PROTOCOL.md
│   ├── HARDWARE_ELECTRONICS_AND_CONTROL.md
│   └── PROJECT_ORGANIZATION.md
│
└── papers/                 ← Reference papers
```

---

## Typical Workflow

### 1. Set acquisition parameters in the GUI

| Parameter | Where | Notes |
|-----------|-------|-------|
| Channels to record | Channel checkboxes | Unchecked channels are stored as empty datasets |
| Sample rate | Acquisition panel | Hz; PXIe-6363 max is 2 MS/s aggregate |
| Samples per file | Bit-depth spinner (N) | Stored as 2^N samples |
| Output directory | File Output panel | Created automatically if it doesn't exist |
| Basename | File Output panel | Files written as `{basename}_0.h5`, `_1.h5`, … |
| Number of files | File Output panel | 0 = record continuously until Stop is pressed |

### 2. Start recording

Click **Start Recording**.
All controls lock. The status log shows progress as files are written.

### 3. Stop

Click **Stop Recording** at any time.
The current file finishes before the recorder halts.

### 4. Data are saved

Each `.h5` file always contains 32 channel datasets (`ai0`–`ai31`).
Channels that were not recorded have shape `(0,)`.
This fixed schema means analysis code never needs to know which channels
were active — it can always address `f["ai3"]` and check
`f["ai3"].attrs["recorded"]`.

---

## Reading Data

```python
import h5py
import numpy as np

with h5py.File("data/run_0.h5", "r") as f:
    sr   = f.attrs["sample_rate_hz"]        # samples per second
    ch0  = f["ai0"][:]                       # array shape (n_samples,) or (0,)
    time = np.arange(len(ch0)) / sr
```

---

## Session Persistence

Settings are saved to `daq_session_log.jsonl` on every **Start Recording**.
The last entry is restored automatically the next time `daq_gui.py` opens,
so channels, sample rate, output path, etc. persist between runs without
any manual re-entry.

---

## Further Reading

- [docs/SETUP.md](docs/SETUP.md) — installation details, package versions, troubleshooting
- [docs/DAQ_CORE.md](docs/DAQ_CORE.md) — `daq_core.py` API reference
- [docs/DAQ_GUI.md](docs/DAQ_GUI.md) — GUI controls reference and extension guide
- [development/MEASUREMENT_PROTOCOL.md](development/MEASUREMENT_PROTOCOL.md) — experimental procedures
- [development/HARDWARE_ELECTRONICS_AND_CONTROL.md](development/HARDWARE_ELECTRONICS_AND_CONTROL.md) — hardware reference

---

*Last updated: 2026-03-18*
