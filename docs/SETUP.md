# Setup and Installation

This guide covers how to install the software dependencies for usphere-DAQ and get the GUI running.

---

## Prerequisites

### 1. Python

Python 3.10 or later is required.
Download from [python.org](https://www.python.org/downloads/) if not already installed.

Verify your version:
```bash
python --version
```

### 2. NI-DAQmx Driver

The `nidaqmx` Python package is only a thin wrapper — the full NI-DAQmx driver
must be installed separately from National Instruments:

- Download **NI-DAQmx** from [ni.com/downloads](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html)
- After installation, open **NI MAX** (Measurement & Automation Explorer) to verify
  that your PXIe-6363 is recognized and note the device name (e.g. `PXI1Slot2`).

If the driver is not installed, `daq_core.py` automatically falls back to
**simulation mode**, which generates synthetic sine-wave data so the GUI and
file-writing pipeline can be tested without hardware.

---

## Installing Python Dependencies

### Recommended: using the installer script

From the project root, run:

```bash
python install_deps.py
```

This will:
1. Create a virtual environment at `.venv/` (next to the script)
2. Upgrade `pip` inside the venv
3. Install all packages listed in `requirements.txt`
4. Print activation instructions

To install into your **current** Python environment instead:

```bash
python install_deps.py --no-venv
```

### Alternative: manual pip install

```bash
pip install -r requirements.txt
```

---

## Python Packages Installed

| Package | Purpose | Minimum version |
|---------|---------|----------------|
| `nidaqmx` | NI-DAQmx Python bindings (hardware interface) | 0.9.0 |
| `h5py` | HDF5 file writing | 3.8.0 |
| `numpy` | Numerical arrays | 1.24.0 |
| `PyQt5` | GUI framework | 5.15.0 |

---

## Running the GUI

Activate the virtual environment, then:

```bash
# Windows (PowerShell or cmd)
.venv\Scripts\activate
python daq_gui.py

# Windows (bash / Git Bash)
source .venv/Scripts/activate
python daq_gui.py
```

On first launch the GUI will start with default parameters.
On subsequent launches it reads `daq_session_log.jsonl` and restores the
settings from the last session automatically.

---

## Project File Overview

```
usphere-DAQ/
├── daq_gui.py              # PyQt5 GUI — run this to operate the DAQ
├── daq_core.py             # Backend: DAQConfig, DAQRecorder, HDF5 writing
├── install_deps.py         # Dependency installer (creates .venv, runs pip)
├── requirements.txt        # Pinned package list
├── daq_session_log.jsonl   # Rolling session log (created on first run)
│
├── data/                   # Default output directory for HDF5 files
│
├── docs/
│   ├── SETUP.md            # This file
│   ├── DAQ_CORE.md
│   └── DAQ_GUI.md
│
├── development/
│   ├── MEASUREMENT_PROTOCOL.md
│   ├── HARDWARE_ELECTRONICS_AND_CONTROL.md
│   └── PROJECT_ORGANIZATION.md
│
└── papers/                 # Reference papers
```

---

## HDF5 File Format

Each output file contains one dataset per analog input channel (`ai0`–`ai31`),
regardless of which channels were active during that run.

| Dataset | Shape | Content |
|---------|-------|---------|
| `ai0` … `ai31` | `(n_samples,)` if recorded, `(0,)` if not | Voltage in V, float64 |

File-level attributes stored in each `.h5`:

| Attribute | Description |
|-----------|-------------|
| `device` | NI device name (e.g. `PXI1Slot2`) |
| `sample_rate_hz` | Sample rate in Hz |
| `n_samples` | Total samples per channel (= 2^N) |
| `n_bits` | Bit depth N used to compute n_samples |
| `active_channels` | List of channel names actually recorded |
| `voltage_min_v` / `voltage_max_v` | Input range |
| `start_time_utc` | ISO-8601 timestamp |
| `duration_s` | File duration in seconds |

Because all 32 datasets are always present at the same paths, adding or
removing channels between runs only changes file size — not dataset layout.
This makes it safe to process files with the same analysis code regardless
of channel configuration.

---

## Session Log

Each time you click **Start Recording**, the GUI appends one JSON entry to
`daq_session_log.jsonl` in the project root:

```json
{"timestamp": "2026-03-18T14:32:01.123", "config": {"device": "PXI1Slot2", "active_channels": ["ai0", "ai1"], "sample_rate": 10000.0, "n_bits": 20, ...}}
```

The last entry is read on startup to pre-populate all GUI fields.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: nidaqmx` | Package not installed | Run `install_deps.py` |
| `DaqError: device not found` | Wrong device name | Check NI MAX; update "NI device name" in GUI |
| GUI opens but shows `[SIM]` in log | NI-DAQmx driver not installed | Install driver from ni.com |
| `ModuleNotFoundError: PyQt5` | Package not installed | Run `install_deps.py` |
| HDF5 files are very large | Uncompressed channels | Compression is applied automatically (gzip level 1) |

---

*See [MEASUREMENT_PROTOCOL.md](../development/MEASUREMENT_PROTOCOL.md) and
[HARDWARE_ELECTRONICS_AND_CONTROL.md](../development/HARDWARE_ELECTRONICS_AND_CONTROL.md)
for experimental procedures and hardware details.*
