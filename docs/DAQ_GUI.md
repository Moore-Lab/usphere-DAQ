# daq_gui.py — DAQ GUI

`daq_gui.py` is the PyQt5 graphical interface for controlling the DAQ.
It imports `DAQConfig` and `DAQRecorder` from `daq_core.py` and adds
session logging, parameter persistence, and a live status display.

Run with:
```bash
python daq_gui.py
```

---

## Window Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Analog Input Channels  │  Acquisition          File Output  │
│  ┌─────────────────┐    │  Sample rate (Hz)     NI device   │
│  │ [x] ai0  [ ] ai8│    │  Samples = 2^N        Directory   │
│  │ [x] ai1  [ ] ai9│    │  → Samples per file   Basename    │
│  │ ...              │    │  → Duration per file  # of files  │
│  └─────────────────┘    │  Voltage min/max                   │
├─────────────────────────────────────────────────────────────┤
│  Status log (scrolling)                                      │
│                              Files: 0  [ Start Recording ]  │
└─────────────────────────────────────────────────────────────┘
```

---

## Controls Reference

### Channel Selection

- **32 checkboxes** (ai0–ai31) in a scrollable 4-column grid
- **All** / **None** buttons select or deselect everything
- Only checked channels are recorded; unchecked channels appear as
  empty `(0,)` datasets in the HDF5 file (schema is always fixed)

### Acquisition Settings

| Control | Description |
|---------|-------------|
| **Sample rate (Hz)** | Samples per second per channel. PXIe-6363 supports up to 2 MS/s aggregate. |
| **Samples = 2^N** | Spinner for N (1–25). Number of samples per file = 2^N. |
| **Samples per file** | Derived, updated live: displays `2^N = value` |
| **Duration per file** | Derived, updated live: displays ms / s / min / hr |
| **Voltage min/max** | Input range in volts (default ±10 V) |

### File Output Settings

| Control | Description |
|---------|-------------|
| **NI device name** | Device identifier from NI MAX (e.g. `PXI1Slot2`) |
| **Output directory** | Folder where `.h5` files are written. Use **Browse…** or type a path. |
| **Basename** | File name prefix. Files are written as `{basename}_0.h5`, `{basename}_1.h5`, … |
| **Number of files** | How many files to write before stopping. **0 = run continuously** until the Stop button is pressed. |

### Start / Stop Button

- Click **Start Recording** to begin.
  All input controls lock while recording is in progress.
- Click **Stop Recording** to request a clean stop.
  The current file finishes writing before the recorder halts.
- The button is the **same** for both fixed-count and continuous modes.

---

## Session Log

Every time **Start Recording** is clicked, the current configuration is
appended as one JSON line to `daq_session_log.jsonl` in the project root:

```json
{"timestamp": "2026-03-18T14:32:01", "config": {"device": "PXI1Slot2", "active_channels": ["ai0","ai1"], "sample_rate": 10000.0, "n_bits": 20, ...}}
```

On the **next launch**, the GUI reads the last entry from this file and
pre-populates all controls — so you never need to re-enter parameters
that haven't changed between sessions.

The log is append-only and grows over time; it serves as a lightweight
record of all acquisition sessions.

---

## Thread Safety

`DAQRecorder` runs in a background thread.
The GUI bridges callbacks to the Qt event loop using `pyqtSignal`:

| Signal | Trigger | Qt slot |
|--------|---------|---------|
| `status_message(str)` | Any log message from the recorder | Appends to the status box |
| `file_written(str)` | Each file saved | Increments the file counter |
| `finished()` | Recording ends | Re-enables controls, resets button |

Do not call Qt widget methods directly from the recorder callbacks —
always route through these signals.

---

## Adding New Controls

1. Add the widget in the appropriate `_make_*_panel()` method.
2. Read its value in `_read_config()` and map it to a `DAQConfig` field.
3. Populate it in `_apply_config()` so the session log restores it.
4. If it affects derived displays, call `_update_derived()` in its
   change signal.
5. Add it to `_set_inputs_enabled()` so it locks/unlocks with the
   rest of the controls during recording.

---

*Last updated: 2026-03-18*
