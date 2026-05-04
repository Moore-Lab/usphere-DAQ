"""
arb_sweep.py

DAQ plugin: automated amplitude sweep on the AFG-2225 waveform generator.

Intended use
------------
Play a waveform on a chosen axis (X / Y / Z mirror / laser) and record N DAQ
files at each amplitude step.  The output filename includes the amplitude so
files are easy to sort: ``xsweep_1.5_001.h5``, ``xsweep_1.5_002.h5``, …

Workflow
--------
1. Connect to the AFG-2225 via its COM port.
2. Set up the waveform (type / frequency / phase / channel) and click Apply.
3. Fill in the sweep parameters (start, stop, step, files per step, settle).
4. Click Start Sweep.  The plugin steps through amplitudes, sets the AFG,
   waits for the settle time, then triggers the DAQ to record ``files_per_step``
   files with basename ``{axis}sweep_{amp:.4g}``.  The DAQ appends a counter
   suffix automatically when recording more than one file.

Date: 2026-05-03
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from plugins.base import AnalysisPlugin

# ---------------------------------------------------------------------------
# AFG-2225 import helper
# ---------------------------------------------------------------------------

_AFG_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "usphere-Q"
    / "resources"
    / "GWINSTEKAFG2225_controller"
)


def _get_afg_class():
    """Import AFG2225Controller from the usphere-Q submodule by file path."""
    import importlib.util

    if str(_AFG_DIR) not in sys.path:
        sys.path.insert(0, str(_AFG_DIR))

    spec = importlib.util.spec_from_file_location(
        "_ext_afg2225_controller", str(_AFG_DIR / "afg2225_controller.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AFG2225Controller


# ---------------------------------------------------------------------------
# Sweep worker thread
# ---------------------------------------------------------------------------

class _SweepWorker(QThread):
    """Runs an amplitude sweep on a background thread."""

    log              = pyqtSignal(str)
    progress         = pyqtSignal(int, int)   # (current_step, total_steps)
    request_recording = pyqtSignal(str, int)  # (basename, n_files)
    finished         = pyqtSignal(bool)       # success flag

    def __init__(self, afg, channel: int, amplitudes: list[float],
                 settle_s: float, files_per_step: int, axis: str,
                 parent=None):
        super().__init__(parent)
        self._afg          = afg
        self._channel      = channel
        self._amplitudes   = amplitudes
        self._settle_s     = settle_s
        self._files_per_step = files_per_step
        self._axis         = axis.lower()
        self._cancel       = False
        self._files_received = 0
        self._files_expected = 0

    def cancel(self):
        self._cancel = True

    def notify_file_written(self):
        """Called from the main thread each time a DAQ file is written."""
        self._files_received += 1

    def run(self):
        try:
            n = len(self._amplitudes)
            for i, amp in enumerate(self._amplitudes):
                if self._cancel:
                    self.log.emit("Sweep cancelled.")
                    break

                self.progress.emit(i + 1, n)
                self.log.emit(
                    f"--- Step {i+1}/{n}: amplitude = {amp:.4g} Vpp ---"
                )

                try:
                    self._afg.set_amplitude(self._channel, amp)
                except Exception as exc:
                    self.log.emit(f"  ERROR setting amplitude: {exc}")
                    self.finished.emit(False)
                    return

                # Settle
                self.log.emit(f"  Settling {self._settle_s:.1f} s …")
                t0 = time.time()
                while time.time() - t0 < self._settle_s:
                    if self._cancel:
                        break
                    time.sleep(0.05)

                if self._cancel:
                    break

                # Trigger DAQ
                basename = f"{self._axis}sweep_{amp:.4g}"
                self._files_received = 0
                self._files_expected = self._files_per_step
                self.log.emit(
                    f"  Recording {self._files_per_step} file(s) → {basename}"
                )
                self.request_recording.emit(basename, self._files_per_step)

                # Wait for all files (120 s timeout)
                t0 = time.time()
                while self._files_received < self._files_expected:
                    if self._cancel:
                        break
                    if time.time() - t0 > 120:
                        self.log.emit("  ERROR: DAQ acquisition timed out.")
                        self.finished.emit(False)
                        return
                    time.sleep(0.05)

                if self._cancel:
                    break

                self.log.emit(
                    f"  Done — {self._files_received} file(s) received."
                )

            self.finished.emit(not self._cancel)

        except Exception as exc:
            self.log.emit(f"Sweep error: {exc}")
            self.finished.emit(False)


# ---------------------------------------------------------------------------
# Sweep widget
# ---------------------------------------------------------------------------

class _SweepWidget(QWidget):

    def __init__(self, plugin: "Plugin", parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._afg    = None
        self._worker: _SweepWorker | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        # --- AFG Connection ---
        conn_box = QGroupBox("AFG-2225 Connection")
        cl = QVBoxLayout(conn_box)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("e.g. COM13  (blank = auto-detect)")
        self._port_edit.setMaximumWidth(160)
        row1.addWidget(self._port_edit)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setMinimumWidth(90)
        self._connect_btn.clicked.connect(self._toggle_connect)
        row1.addWidget(self._connect_btn)
        self._conn_status = QLabel("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        row1.addWidget(self._conn_status)
        row1.addStretch()
        cl.addLayout(row1)
        root.addWidget(conn_box)

        # --- Waveform setup ---
        wf_box = QGroupBox("Waveform")
        wl = QVBoxLayout(wf_box)

        wr1 = QHBoxLayout()
        wr1.addWidget(QLabel("Channel:"))
        self._ch_combo = QComboBox()
        self._ch_combo.addItems(["CH 1", "CH 2"])
        self._ch_combo.setMaximumWidth(80)
        wr1.addWidget(self._ch_combo)
        wr1.addWidget(QLabel("Type:"))
        self._wf_type = QComboBox()
        self._wf_type.addItems(["Sine", "Square", "Ramp", "Noise"])
        self._wf_type.setMaximumWidth(90)
        wr1.addWidget(self._wf_type)
        wr1.addStretch()
        wl.addLayout(wr1)

        wr2 = QHBoxLayout()
        wr2.addWidget(QLabel("Frequency:"))
        self._freq_spin = QDoubleSpinBox()
        self._freq_spin.setRange(0.001, 25e6)
        self._freq_spin.setDecimals(3)
        self._freq_spin.setValue(100.0)
        self._freq_spin.setSuffix(" Hz")
        self._freq_spin.setMinimumWidth(130)
        wr2.addWidget(self._freq_spin)
        wr2.addWidget(QLabel("Amplitude:"))
        self._amp_spin = QDoubleSpinBox()
        self._amp_spin.setRange(0.001, 20.0)
        self._amp_spin.setDecimals(3)
        self._amp_spin.setValue(1.0)
        self._amp_spin.setSuffix(" Vpp")
        self._amp_spin.setMinimumWidth(110)
        wr2.addWidget(self._amp_spin)
        wr2.addWidget(QLabel("Phase:"))
        self._phase_spin = QDoubleSpinBox()
        self._phase_spin.setRange(-180.0, 180.0)
        self._phase_spin.setDecimals(2)
        self._phase_spin.setValue(0.0)
        self._phase_spin.setSuffix(" °")
        self._phase_spin.setMinimumWidth(100)
        wr2.addWidget(self._phase_spin)
        wr2.addStretch()
        wl.addLayout(wr2)

        wr3 = QHBoxLayout()
        self._apply_btn = QPushButton("Apply Waveform")
        self._apply_btn.clicked.connect(self._apply_waveform)
        wr3.addWidget(self._apply_btn)
        self._output_on_btn = QPushButton("Output ON")
        self._output_on_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; }"
        )
        self._output_on_btn.clicked.connect(self._output_on)
        wr3.addWidget(self._output_on_btn)
        self._output_off_btn = QPushButton("Output OFF")
        self._output_off_btn.setStyleSheet(
            "QPushButton { background-color: #F44336; color: white; }"
        )
        self._output_off_btn.clicked.connect(self._output_off)
        wr3.addWidget(self._output_off_btn)
        self._wf_status = QLabel("—")
        self._wf_status.setStyleSheet("color: gray;")
        wr3.addWidget(self._wf_status)
        wr3.addStretch()
        wl.addLayout(wr3)
        root.addWidget(wf_box)

        # --- Sweep parameters ---
        sw_box = QGroupBox("Sweep Parameters")
        sl = QVBoxLayout(sw_box)

        sr1 = QHBoxLayout()
        sr1.addWidget(QLabel("Axis:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItems(["X", "Y", "Z"])
        self._axis_combo.setMaximumWidth(70)
        sr1.addWidget(self._axis_combo)
        sr1.addWidget(QLabel("Start (Vpp):"))
        self._start_spin = QDoubleSpinBox()
        self._start_spin.setRange(0.001, 20.0)
        self._start_spin.setDecimals(3)
        self._start_spin.setValue(0.1)
        self._start_spin.setMinimumWidth(100)
        sr1.addWidget(self._start_spin)
        sr1.addWidget(QLabel("Stop (Vpp):"))
        self._stop_spin = QDoubleSpinBox()
        self._stop_spin.setRange(0.001, 20.0)
        self._stop_spin.setDecimals(3)
        self._stop_spin.setValue(2.0)
        self._stop_spin.setMinimumWidth(100)
        sr1.addWidget(self._stop_spin)
        sr1.addWidget(QLabel("Step (Vpp):"))
        self._step_spin = QDoubleSpinBox()
        self._step_spin.setRange(0.001, 10.0)
        self._step_spin.setDecimals(3)
        self._step_spin.setValue(0.1)
        self._step_spin.setMinimumWidth(100)
        sr1.addWidget(self._step_spin)
        sr1.addStretch()
        sl.addLayout(sr1)

        sr2 = QHBoxLayout()
        sr2.addWidget(QLabel("Files per step:"))
        self._files_spin = QSpinBox()
        self._files_spin.setRange(1, 100)
        self._files_spin.setValue(1)
        self._files_spin.setMinimumWidth(70)
        sr2.addWidget(self._files_spin)
        sr2.addWidget(QLabel("Settle time (s):"))
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.0, 300.0)
        self._settle_spin.setDecimals(1)
        self._settle_spin.setValue(2.0)
        self._settle_spin.setMinimumWidth(80)
        sr2.addWidget(self._settle_spin)
        sr2.addStretch()
        sl.addLayout(sr2)
        root.addWidget(sw_box)

        # --- Sweep controls ---
        ctrl_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Sweep")
        self._start_btn.setMinimumWidth(120)
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 4px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        self._start_btn.clicked.connect(self._start_sweep)
        ctrl_row.addWidget(self._start_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_sweep)
        ctrl_row.addWidget(self._cancel_btn)
        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet("color: gray; font-size: 10px;")
        ctrl_row.addWidget(self._progress_lbl)
        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # --- Log ---
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMinimumHeight(120)
        self._log_box.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;"
        )
        root.addWidget(self._log_box)
        root.addStretch()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _channel(self) -> int:
        return self._ch_combo.currentIndex() + 1

    def _toggle_connect(self):
        if self._afg is not None:
            try:
                self._afg.disconnect()
            except Exception:
                pass
            self._afg = None
            self._connect_btn.setText("Connect")
            self._conn_status.setText("Disconnected")
            self._conn_status.setStyleSheet("color: gray;")
            self._log("AFG-2225 disconnected.")
            return

        try:
            AFG = _get_afg_class()
        except Exception as exc:
            self._log(f"ERROR importing AFG2225Controller: {exc}")
            return

        port = self._port_edit.text().strip() or None
        afg = AFG(port)
        self._log("Connecting to AFG-2225 …")
        try:
            if port:
                ok = afg.connect(port)
            else:
                ok = afg.auto_connect()
        except Exception as exc:
            self._log(f"Connection error: {exc}")
            return

        if not ok:
            self._log("ERROR: Could not connect to AFG-2225.")
            return

        self._afg = afg
        detected_port = afg.connection.port or port or "?"
        if not self._port_edit.text().strip():
            self._port_edit.setText(detected_port)
        self._connect_btn.setText("Disconnect")
        self._conn_status.setText(f"Connected ({detected_port})")
        self._conn_status.setStyleSheet("color: green; font-weight: bold;")
        self._log(f"Connected: {afg.idn}")

    # ------------------------------------------------------------------
    # Waveform control
    # ------------------------------------------------------------------

    def _apply_waveform(self):
        if self._afg is None or not self._afg.is_connected:
            self._wf_status.setText("Not connected")
            self._wf_status.setStyleSheet("color: red;")
            return
        ch   = self._channel()
        freq = self._freq_spin.value()
        amp  = self._amp_spin.value()
        ph   = self._phase_spin.value()
        wt   = self._wf_type.currentText()
        try:
            if wt == "Sine":
                self._afg.setup_sine(ch, frequency=freq, amplitude=amp, offset=0.0)
            elif wt == "Square":
                self._afg.setup_square(ch, frequency=freq, amplitude=amp, offset=0.0)
            elif wt == "Ramp":
                self._afg.setup_ramp(ch, frequency=freq, amplitude=amp, offset=0.0)
            elif wt == "Noise":
                self._afg.setup_noise(ch, amplitude=amp, offset=0.0)
            self._afg.set_phase(ch, ph)
            self._wf_status.setText(
                f"Applied — {freq:.3f} Hz, {amp:.3f} Vpp, {ph:.1f}°"
            )
            self._wf_status.setStyleSheet("color: green;")
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color: red;")

    def _output_on(self):
        if self._afg is None or not self._afg.is_connected:
            return
        try:
            self._afg.output_on(self._channel())
            self._wf_status.setText("Output ON")
            self._wf_status.setStyleSheet("color: green;")
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color: red;")

    def _output_off(self):
        if self._afg is None or not self._afg.is_connected:
            return
        try:
            self._afg.output_off(self._channel())
            self._wf_status.setText("Output OFF")
            self._wf_status.setStyleSheet("color: gray;")
        except Exception as exc:
            self._wf_status.setText(f"Error: {exc}")
            self._wf_status.setStyleSheet("color: red;")

    # ------------------------------------------------------------------
    # Sweep
    # ------------------------------------------------------------------

    def _build_amplitudes(self) -> list[float]:
        start = self._start_spin.value()
        stop  = self._stop_spin.value()
        step  = self._step_spin.value()
        vals  = []
        a = start
        while a <= stop + 1e-9:
            vals.append(round(a, 6))
            a += step
        return vals

    def _start_sweep(self):
        if self._afg is None or not self._afg.is_connected:
            self._log("ERROR: Connect to AFG-2225 first.")
            return
        if self._plugin.daq is None:
            self._log("ERROR: DAQ controller not available.")
            return
        if self._plugin.daq.is_recording():
            self._log("ERROR: A recording is already in progress.")
            return

        amps = self._build_amplitudes()
        if not amps:
            self._log("ERROR: No amplitude steps in range.")
            return

        axis = self._axis_combo.currentText()
        self._log(
            f"Starting {axis} amplitude sweep: {len(amps)} step(s), "
            f"{amps[0]:.4g}→{amps[-1]:.4g} Vpp, "
            f"{self._files_spin.value()} file(s)/step"
        )

        self._worker = _SweepWorker(
            afg=self._afg,
            channel=self._channel(),
            amplitudes=amps,
            settle_s=self._settle_spin.value(),
            files_per_step=self._files_spin.value(),
            axis=axis,
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.request_recording.connect(self._on_request_recording)
        self._worker.finished.connect(self._on_sweep_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

    def _cancel_sweep(self):
        if self._worker:
            self._worker.cancel()
            self._log("Cancelling sweep …")

    def _on_progress(self, step: int, total: int):
        self._progress_lbl.setText(f"Step {step}/{total}")

    def _on_request_recording(self, basename: str, n_files: int):
        self._plugin.daq.start_recording(n_files=n_files, basename=basename)

    def _on_sweep_finished(self, success: bool):
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress_lbl.setText("")
        self._log("Sweep complete." if success else "Sweep stopped.")
        self._worker = None

    def on_file_written(self, filepath: str):
        """Forwarded from the plugin when the DAQ writes a file."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.notify_file_written()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self):
        if self._worker:
            self._worker.cancel()
            self._worker.wait(5000)
        if self._afg:
            try:
                self._afg.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin(AnalysisPlugin):
    NAME = "Arb Sweep (Mirror / Laser)"
    DESCRIPTION = (
        "Automated AFG-2225 amplitude sweep for X/Y/Z mirror or laser power. "
        "Steps through a range of amplitudes and records N DAQ files per step."
    )

    def __init__(self):
        self._widget: _SweepWidget | None = None

    def create_widget(self, parent=None):
        self._widget = _SweepWidget(self, parent)
        return self._widget

    def on_file_written(self, filepath: str):
        if self._widget is not None:
            self._widget.on_file_written(filepath)

    def teardown(self):
        if self._widget is not None:
            self._widget.teardown()
