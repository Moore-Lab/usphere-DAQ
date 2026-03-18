"""
daq_gui.py

PyQt5 GUI for usphere DAQ control.
Reads session parameters from a rolling JSON-lines log file on startup so
that the last-used settings are automatically restored.

Run with:
    python daq_gui.py

Dependencies:
    pip install PyQt5 nidaqmx h5py numpy
"""

from __future__ import annotations

import datetime
import json
import sys
import threading
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QCheckBox,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from daq_core import DAQConfig, DAQRecorder, get_plugins
from daq_h5 import ALL_CHANNELS
from daq_plot import PlotWidget

# Rolling session log — sits alongside this script
LOG_FILE = Path(__file__).parent / "daq_session_log.jsonl"


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def load_last_config() -> DAQConfig | None:
    """Return the DAQConfig from the most recent log entry, or None."""
    if not LOG_FILE.exists():
        return None
    try:
        lines = [l for l in LOG_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            return None
        return DAQConfig.from_dict(json.loads(lines[-1])["config"])
    except Exception:
        return None


def append_log(config: DAQConfig):
    """Append one JSON-lines entry to the session log."""
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "config": config.to_dict(),
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Modules tab widget
# Dynamically builds one settings group per registered plugin.
# Each plugin's CONFIG_FIELDS list drives the form fields automatically.
# ---------------------------------------------------------------------------

class ModulesWidget(QWidget):
    """
    Renders one collapsible section per registered plugin, driven by each
    plugin's CONFIG_FIELDS list.  Provides per-module Test buttons and
    persists configs via get_all_configs() / set_all_configs().
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plugins = get_plugins()
        self._fields: dict[str, dict[str, QLineEdit]] = {}  # module_name -> {key: widget}
        self._status_labels: dict[str, QLabel] = {}
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(8, 8, 8, 8)

        if not self._plugins:
            outer.addWidget(QLabel("No device modules registered."))
            outer.addStretch()
            return

        for mod in self._plugins:
            outer.addWidget(self._make_module_group(mod))

        outer.addStretch()

    def _make_module_group(self, mod) -> QGroupBox:
        box = QGroupBox(mod.DEVICE_NAME)
        g = QGridLayout(box)
        g.setColumnStretch(1, 1)

        row = 0
        self._fields[mod.MODULE_NAME] = {}

        for field_def in mod.CONFIG_FIELDS:
            key    = field_def["key"]
            label  = field_def["label"]
            ftype  = field_def.get("type", "text")
            default = field_def.get("default", "")

            g.addWidget(QLabel(f"{label}:"), row, 0)

            edit = QLineEdit(str(default))
            self._fields[mod.MODULE_NAME][key] = edit
            g.addWidget(edit, row, 1)

            if ftype == "file":
                ffilter = field_def.get("filter", "All files (*)")
                browse = QPushButton("Browse…")
                browse.setMaximumWidth(70)
                browse.clicked.connect(
                    lambda _checked, e=edit, ff=ffilter: self._browse_file(e, ff)
                )
                g.addWidget(browse, row, 2)

            row += 1

        # Test button + status label
        test_btn = QPushButton("Test connection")
        test_btn.setMaximumWidth(130)
        test_btn.clicked.connect(lambda _checked, m=mod: self._run_test(m))
        g.addWidget(test_btn, row, 0)

        status_lbl = QLabel("—")
        status_lbl.setWordWrap(True)
        status_lbl.setStyleSheet("color: gray;")
        self._status_labels[mod.MODULE_NAME] = status_lbl
        g.addWidget(status_lbl, row, 1, 1, 2)

        return box

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _browse_file(self, edit: QLineEdit, file_filter: str):
        start = str(Path(edit.text()).parent) if edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Select file", start, file_filter)
        if path:
            edit.setText(path)

    def _run_test(self, mod):
        """Run mod.test() in a worker thread; update the status label when done."""
        config = self.get_module_config(mod.MODULE_NAME)
        lbl = self._status_labels[mod.MODULE_NAME]
        lbl.setText("Testing…")
        lbl.setStyleSheet("color: gray;")

        def _worker():
            ok, msg = mod.test(config)
            # Qt widgets must be updated from the main thread — use a one-shot
            # connection via a local QObject signal trick isn't available here,
            # so we use a simple lambda posted via QApplication.instance().
            from PyQt5.QtCore import QMetaObject, Qt as _Qt
            def _update():
                lbl.setText(msg)
                lbl.setStyleSheet("color: green;" if ok else "color: red;")
            # postEvent approach: schedule on the main thread
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, _update)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Config access (called by MainWindow)
    # ------------------------------------------------------------------

    def get_module_config(self, module_name: str) -> dict:
        """Return the current field values for one module."""
        return {
            key: edit.text()
            for key, edit in self._fields.get(module_name, {}).items()
        }

    def get_all_configs(self) -> dict:
        """Return {MODULE_NAME: {key: value}} for all modules."""
        return {name: self.get_module_config(name) for name in self._fields}

    def set_all_configs(self, configs: dict):
        """Restore saved configs — missing keys are left at their defaults."""
        for module_name, values in configs.items():
            for key, val in values.items():
                widget = self._fields.get(module_name, {}).get(key)
                if widget is not None:
                    widget.setText(str(val))


# ---------------------------------------------------------------------------
# Thread-safe signal bridge
# Callbacks from DAQRecorder run in a worker thread; we relay them to the
# Qt event loop via signals so GUI widgets can be updated safely.
# ---------------------------------------------------------------------------

class _RecorderSignals(QObject):
    status_message = pyqtSignal(str)
    file_written   = pyqtSignal(str)   # str(Path)
    finished       = pyqtSignal()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("usphere DAQ Control")
        self.resize(960, 720)

        self._recorder: DAQRecorder | None = None
        self._signals = _RecorderSignals()
        self._signals.status_message.connect(self._append_status)
        self._signals.file_written.connect(self._on_file_written)
        self._signals.finished.connect(self._on_recording_finished)
        self._files_written = 0

        self._build_ui()
        self._load_initial_config()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setSpacing(4)
        vbox.setContentsMargins(6, 6, 6, 6)

        tabs = QTabWidget()
        vbox.addWidget(tabs)

        # --- Tab 1: Acquire ---
        acquire_w = QWidget()
        acquire_layout = QVBoxLayout(acquire_w)
        acquire_layout.setSpacing(8)
        acquire_layout.setContentsMargins(6, 6, 6, 6)

        top = QHBoxLayout()
        top.addWidget(self._make_channels_panel(), stretch=2)
        top.addWidget(self._make_settings_panel(), stretch=3)
        acquire_layout.addLayout(top, stretch=3)
        acquire_layout.addWidget(self._make_status_panel(), stretch=2)

        tabs.addTab(acquire_w, "Acquire")

        # --- Tab 2: Modules ---
        self._modules_tab = ModulesWidget()
        tabs.addTab(self._modules_tab, "Modules")

        # --- Tab 3: Plot ---
        self._plot_tab = PlotWidget()
        tabs.addTab(self._plot_tab, "Plot")

    # --- Channel panel ---

    def _make_channels_panel(self) -> QGroupBox:
        box = QGroupBox("Analog Input Channels")
        layout = QVBoxLayout(box)

        # Select-all / select-none row
        row = QHBoxLayout()
        for label, state in (("All", True), ("None", False)):
            btn = QPushButton(label)
            btn.setMaximumWidth(55)
            btn.clicked.connect(lambda _checked, s=state: self._set_all_channels(s))
            row.addWidget(btn)
        row.addStretch()
        layout.addLayout(row)

        # Scrollable grid — 8 rows × 4 columns = 32 channels
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setSpacing(3)
        grid.setContentsMargins(2, 2, 2, 2)

        self._ch_boxes: dict[str, QCheckBox] = {}
        N_COLS = 4
        for idx, ch in enumerate(ALL_CHANNELS):
            r, c = divmod(idx, N_COLS)
            cb = QCheckBox(ch)
            self._ch_boxes[ch] = cb
            grid.addWidget(cb, r, c)

        scroll.setWidget(grid_w)
        layout.addWidget(scroll)
        return box

    # --- Settings panel ---

    def _make_settings_panel(self) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setSpacing(8)
        outer.setContentsMargins(0, 0, 0, 0)

        # ---- Acquisition ----
        acq = QGroupBox("Acquisition")
        g = QGridLayout(acq)
        g.setColumnStretch(1, 1)
        r = 0

        g.addWidget(QLabel("Sample rate (Hz):"), r, 0)
        self._sr_edit = QLineEdit()
        self._sr_edit.textChanged.connect(self._update_derived)
        g.addWidget(self._sr_edit, r, 1)
        r += 1

        g.addWidget(QLabel("Samples = 2^N  (N bits):"), r, 0)
        self._bits_spin = QSpinBox()
        self._bits_spin.setRange(1, 25)
        self._bits_spin.valueChanged.connect(self._update_derived)
        g.addWidget(self._bits_spin, r, 1)
        r += 1

        g.addWidget(QLabel("Samples per file:"), r, 0)
        self._samples_lbl = QLabel()
        self._samples_lbl.setStyleSheet("font-weight: bold;")
        g.addWidget(self._samples_lbl, r, 1)
        r += 1

        g.addWidget(QLabel("Duration per file:"), r, 0)
        self._duration_lbl = QLabel()
        self._duration_lbl.setStyleSheet("font-weight: bold;")
        g.addWidget(self._duration_lbl, r, 1)
        r += 1

        g.addWidget(QLabel("Voltage min (V):"), r, 0)
        self._vmin_edit = QLineEdit()
        g.addWidget(self._vmin_edit, r, 1)
        r += 1

        g.addWidget(QLabel("Voltage max (V):"), r, 0)
        self._vmax_edit = QLineEdit()
        g.addWidget(self._vmax_edit, r, 1)

        outer.addWidget(acq)

        # ---- File output ----
        fb = QGroupBox("File Output")
        g2 = QGridLayout(fb)
        g2.setColumnStretch(1, 1)
        r = 0

        g2.addWidget(QLabel("NI device name:"), r, 0)
        self._device_edit = QLineEdit()
        g2.addWidget(self._device_edit, r, 1, 1, 2)
        r += 1

        g2.addWidget(QLabel("Output directory:"), r, 0)
        self._dir_edit = QLineEdit()
        g2.addWidget(self._dir_edit, r, 1)
        browse = QPushButton("Browse…")
        browse.setMaximumWidth(70)
        browse.clicked.connect(self._browse_dir)
        g2.addWidget(browse, r, 2)
        r += 1

        g2.addWidget(QLabel("Basename:"), r, 0)
        self._basename_edit = QLineEdit()
        g2.addWidget(self._basename_edit, r, 1, 1, 2)
        r += 1

        g2.addWidget(QLabel("Number of files:"), r, 0)
        nf_row = QHBoxLayout()
        self._nfiles_spin = QSpinBox()
        self._nfiles_spin.setRange(0, 999_999)
        nf_row.addWidget(self._nfiles_spin)
        nf_row.addWidget(QLabel("  (0 = run until stopped)"))
        nf_row.addStretch()
        g2.addLayout(nf_row, r, 1, 1, 2)

        outer.addWidget(fb)
        outer.addStretch()
        return container

    # --- Status / control panel ---

    def _make_status_panel(self) -> QGroupBox:
        box = QGroupBox("Status")
        layout = QVBoxLayout(box)

        self._status_box = QTextEdit()
        self._status_box.setReadOnly(True)
        self._status_box.setFont(QFont("Courier New", 9))
        layout.addWidget(self._status_box)

        ctrl_row = QHBoxLayout()
        self._files_lbl = QLabel("Files written this session: 0")
        ctrl_row.addWidget(self._files_lbl)
        ctrl_row.addStretch()

        self._start_btn = QPushButton("Start Recording")
        self._start_btn.setMinimumHeight(42)
        self._start_btn.setMinimumWidth(210)
        self._apply_btn_style(running=False)
        self._start_btn.clicked.connect(self._on_start_stop)
        ctrl_row.addWidget(self._start_btn)

        layout.addLayout(ctrl_row)
        return box

    # ------------------------------------------------------------------
    # Config load / save
    # ------------------------------------------------------------------

    def _load_initial_config(self):
        cfg = load_last_config() or DAQConfig()
        self._apply_config(cfg)
        self._modules_tab.set_all_configs(cfg.module_configs)
        self._update_derived()

    def _apply_config(self, cfg: DAQConfig):
        self._device_edit.setText(cfg.device)
        self._sr_edit.setText(str(cfg.sample_rate))
        self._bits_spin.setValue(cfg.n_bits)
        self._vmin_edit.setText(str(cfg.voltage_min))
        self._vmax_edit.setText(str(cfg.voltage_max))
        self._dir_edit.setText(str(cfg.output_dir))
        self._basename_edit.setText(cfg.basename)
        self._nfiles_spin.setValue(cfg.n_files)
        active = set(cfg.active_channels)
        for ch, cb in self._ch_boxes.items():
            cb.setChecked(ch in active)

    def _read_config(self) -> DAQConfig:
        def _float(edit: QLineEdit, fallback: float) -> float:
            try:
                return float(edit.text())
            except ValueError:
                return fallback

        return DAQConfig(
            device=self._device_edit.text().strip(),
            active_channels=[ch for ch, cb in self._ch_boxes.items() if cb.isChecked()],
            sample_rate=_float(self._sr_edit, 10_000.0),
            n_bits=self._bits_spin.value(),
            output_dir=self._dir_edit.text().strip(),
            basename=self._basename_edit.text().strip(),
            n_files=self._nfiles_spin.value(),
            voltage_min=_float(self._vmin_edit, -10.0),
            voltage_max=_float(self._vmax_edit, 10.0),
            module_configs=self._modules_tab.get_all_configs(),
        )

    # ------------------------------------------------------------------
    # Derived-value display
    # ------------------------------------------------------------------

    def _update_derived(self):
        try:
            sr = float(self._sr_edit.text())
        except ValueError:
            sr = 0.0
        bits = self._bits_spin.value()
        n = 2 ** bits
        self._samples_lbl.setText(f"2^{bits} = {n:,}")
        if sr > 0:
            secs = n / sr
            if secs < 1:
                dur = f"{secs * 1000:.2f} ms"
            elif secs < 60:
                dur = f"{secs:.3f} s"
            elif secs < 3600:
                dur = f"{secs / 60:.2f} min"
            else:
                dur = f"{secs / 3600:.2f} hr"
        else:
            dur = "—"
        self._duration_lbl.setText(dur)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_all_channels(self, state: bool):
        for cb in self._ch_boxes.values():
            cb.setChecked(state)

    def _browse_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Select output directory", self._dir_edit.text()
        )
        if chosen:
            self._dir_edit.setText(chosen)

    def _append_status(self, msg: str):
        self._status_box.append(msg)
        sb = self._status_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _apply_btn_style(self, running: bool):
        if running:
            self._start_btn.setText("Stop Recording")
            self._start_btn.setStyleSheet(
                "QPushButton { background-color: #c0392b; color: white; "
                "font-size: 13px; font-weight: bold; border-radius: 5px; }"
                "QPushButton:hover { background-color: #e74c3c; }"
            )
        else:
            self._start_btn.setText("Start Recording")
            self._start_btn.setStyleSheet(
                "QPushButton { background-color: #1e7e34; color: white; "
                "font-size: 13px; font-weight: bold; border-radius: 5px; }"
                "QPushButton:hover { background-color: #28a745; }"
            )

    def _set_inputs_enabled(self, enabled: bool):
        for w in (
            self._device_edit, self._sr_edit, self._bits_spin,
            self._vmin_edit, self._vmax_edit,
            self._dir_edit, self._basename_edit, self._nfiles_spin,
        ):
            w.setEnabled(enabled)
        for cb in self._ch_boxes.values():
            cb.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Recorder callbacks (called from worker thread via signals)
    # ------------------------------------------------------------------

    def _on_file_written(self, path: str):
        self._files_written += 1
        self._files_lbl.setText(f"Files written this session: {self._files_written}")
        self._plot_tab.load_file(path)  # keep plot tab pointed at the latest file

    def _on_recording_finished(self):
        self._apply_btn_style(running=False)
        self._set_inputs_enabled(True)
        self._recorder = None

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def _on_start_stop(self):
        if self._recorder and self._recorder.is_running():
            self._recorder.stop()
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self._append_status(f"[{ts}] Stop requested — finishing current file…")
        else:
            self._start_recording()

    def _start_recording(self):
        cfg = self._read_config()

        if not cfg.active_channels:
            self._append_status("Error: select at least one channel before recording.")
            return

        self._files_written = 0
        self._files_lbl.setText("Files written this session: 0")
        append_log(cfg)

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        mode = "continuous" if cfg.n_files == 0 else f"{cfg.n_files} file(s)"
        self._append_status(
            f"\n[{ts}] Starting  |  {len(cfg.active_channels)} ch  "
            f"|  {cfg.sample_rate:g} Hz  |  2^{cfg.n_bits} = {cfg.n_samples:,} samples  "
            f"|  {cfg.duration_s:.2f} s/file  |  mode: {mode}"
        )

        sig = self._signals
        self._recorder = DAQRecorder(
            config=cfg,
            on_status=lambda msg: sig.status_message.emit(msg),
            on_file_written=lambda p: sig.file_written.emit(str(p)),
            on_finished=sig.finished.emit,
        )
        self._recorder.start()
        self._apply_btn_style(running=True)
        self._set_inputs_enabled(False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
