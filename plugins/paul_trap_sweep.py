"""
paul_trap_sweep.py

DAQ plugin: automated parameter sweep on the Paul trap electrode waveform.

Intended use
------------
Play any waveform (sine, square, ramp, noise, or a pre-loaded arbitrary /
frequency-comb waveform) on a chosen AFG channel and record N DAQ files at
each sweep step.

Sweep modes
-----------
Amplitude sweep
    Steps through output amplitude (Vpp) while holding frequency fixed.
    Works with ALL waveform types, including ARB frequency combs.

Frequency sweep
    Steps through frequency (Hz) while holding amplitude fixed.
    Available for Sine / Square / Ramp / Pulse only.  ARB and Noise
    disable this mode automatically.

File naming
-----------
    {prefix}_amp_{value:.4g}    e.g.  ptrap_amp_1.5_001.h5
    {prefix}_freq_{value:.4g}   e.g.  ptrap_freq_1000_001.h5

Date: 2026-05-04
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
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
    import importlib.util
    if str(_AFG_DIR) not in sys.path:
        sys.path.insert(0, str(_AFG_DIR))
    spec = importlib.util.spec_from_file_location(
        "_ext_afg2225_controller", str(_AFG_DIR / "afg2225_controller.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.AFG2225Controller


# Waveform types whose frequencies can be swept
_FREQ_SWEEPABLE = {"Sine", "Square", "Ramp", "Noise"}


# ---------------------------------------------------------------------------
# Sweep worker thread
# ---------------------------------------------------------------------------

class _SweepWorker(QThread):
    log               = pyqtSignal(str)
    progress          = pyqtSignal(int, int)    # (current, total)
    request_recording = pyqtSignal(str, int)    # (basename, n_files)
    finished          = pyqtSignal(bool)

    def __init__(self, afg, channel: int, mode: str,
                 values: list[float], fixed_param: float,
                 settle_s: float, files_per_step: int, prefix: str,
                 parent=None):
        super().__init__(parent)
        self._afg            = afg
        self._channel        = channel
        self._mode           = mode          # "amplitude" or "frequency"
        self._values         = values        # list of amp or freq to sweep
        self._fixed_param    = fixed_param   # freq (if amp sweep) or amp (if freq sweep)
        self._settle_s       = settle_s
        self._files_per_step = files_per_step
        self._prefix         = prefix
        self._cancel         = False
        self._files_received = 0
        self._files_expected = 0

    def cancel(self):
        self._cancel = True

    def notify_file_written(self):
        self._files_received += 1

    def run(self):
        try:
            n = len(self._values)
            for i, val in enumerate(self._values):
                if self._cancel:
                    self.log.emit("Sweep cancelled.")
                    break

                self.progress.emit(i + 1, n)

                if self._mode == "amplitude":
                    self.log.emit(
                        f"--- Step {i+1}/{n}: amplitude = {val:.4g} Vpp ---"
                    )
                    try:
                        self._afg.set_amplitude(self._channel, val)
                    except Exception as exc:
                        self.log.emit(f"  ERROR setting amplitude: {exc}")
                        self.finished.emit(False)
                        return
                    basename = f"{self._prefix}_amp_{val:.4g}"
                else:
                    self.log.emit(
                        f"--- Step {i+1}/{n}: frequency = {val:.4g} Hz ---"
                    )
                    try:
                        self._afg.set_frequency(self._channel, val)
                    except Exception as exc:
                        self.log.emit(f"  ERROR setting frequency: {exc}")
                        self.finished.emit(False)
                        return
                    basename = f"{self._prefix}_freq_{val:.4g}"

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
# Plugin widget
# ---------------------------------------------------------------------------

class _SweepWidget(QWidget):

    # Waveforms that support frequency sweeping
    _FREQ_SWEEPABLE = {"Sine", "Square", "Ramp", "Noise"}

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
        self._port_edit.setPlaceholderText("e.g. COM5  (blank = auto-detect)")
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

        wr0 = QHBoxLayout()
        wr0.addWidget(QLabel("Channel:"))
        self._ch_combo = QComboBox()
        self._ch_combo.addItems(["CH 1", "CH 2"])
        self._ch_combo.setMaximumWidth(80)
        wr0.addWidget(self._ch_combo)
        wr0.addWidget(QLabel("Type:"))
        self._wf_type = QComboBox()
        self._wf_type.addItems(["Sine", "Square", "Ramp", "Noise", "ARB (loaded)"])
        self._wf_type.setMaximumWidth(130)
        self._wf_type.currentTextChanged.connect(self._on_wf_type_changed)
        wr0.addWidget(self._wf_type)
        wr0.addStretch()
        wl.addLayout(wr0)

        wr1 = QHBoxLayout()
        wr1.addWidget(QLabel("Frequency:"))
        self._freq_spin = QDoubleSpinBox()
        self._freq_spin.setRange(0.001, 25e6)
        self._freq_spin.setDecimals(3)
        self._freq_spin.setValue(100.0)
        self._freq_spin.setSuffix(" Hz")
        self._freq_spin.setMinimumWidth(130)
        wr1.addWidget(self._freq_spin)
        wr1.addWidget(QLabel("Amplitude:"))
        self._amp_spin = QDoubleSpinBox()
        self._amp_spin.setRange(0.001, 20.0)
        self._amp_spin.setDecimals(3)
        self._amp_spin.setValue(1.0)
        self._amp_spin.setSuffix(" Vpp")
        self._amp_spin.setMinimumWidth(110)
        wr1.addWidget(self._amp_spin)
        wr1.addStretch()
        wl.addLayout(wr1)

        wr2 = QHBoxLayout()
        wr2.addWidget(QLabel("Offset:"))
        self._offset_spin = QDoubleSpinBox()
        self._offset_spin.setRange(-10.0, 10.0)
        self._offset_spin.setDecimals(3)
        self._offset_spin.setValue(0.0)
        self._offset_spin.setSuffix(" V")
        self._offset_spin.setMinimumWidth(100)
        wr2.addWidget(self._offset_spin)
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
        self._on_btn = QPushButton("Output ON")
        self._on_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; }"
        )
        self._on_btn.clicked.connect(self._output_on)
        wr3.addWidget(self._on_btn)
        self._off_btn = QPushButton("Output OFF")
        self._off_btn.setStyleSheet(
            "QPushButton { background-color: #F44336; color: white; }"
        )
        self._off_btn.clicked.connect(self._output_off)
        wr3.addWidget(self._off_btn)
        self._wf_status = QLabel("—")
        self._wf_status.setStyleSheet("color: gray;")
        wr3.addWidget(self._wf_status)
        wr3.addStretch()
        wl.addLayout(wr3)
        root.addWidget(wf_box)

        # --- Sweep parameters ---
        sw_box = QGroupBox("Sweep")
        sl = QVBoxLayout(sw_box)

        # Mode selection
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Sweep:"))
        self._mode_bg = QButtonGroup(self)
        self._amp_mode_rb = QRadioButton("Amplitude (Vpp)")
        self._freq_mode_rb = QRadioButton("Frequency (Hz)")
        self._amp_mode_rb.setChecked(True)
        self._mode_bg.addButton(self._amp_mode_rb)
        self._mode_bg.addButton(self._freq_mode_rb)
        self._amp_mode_rb.toggled.connect(self._on_mode_changed)
        mode_row.addWidget(self._amp_mode_rb)
        mode_row.addWidget(self._freq_mode_rb)
        mode_row.addStretch()
        sl.addLayout(mode_row)

        # Sweep range row
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Start:"))
        self._start_spin = QDoubleSpinBox()
        self._start_spin.setDecimals(4)
        self._start_spin.setMinimumWidth(110)
        range_row.addWidget(self._start_spin)
        range_row.addWidget(QLabel("Stop:"))
        self._stop_spin = QDoubleSpinBox()
        self._stop_spin.setDecimals(4)
        self._stop_spin.setMinimumWidth(110)
        range_row.addWidget(self._stop_spin)
        range_row.addWidget(QLabel("Step:"))
        self._step_spin = QDoubleSpinBox()
        self._step_spin.setDecimals(4)
        self._step_spin.setMinimumWidth(100)
        range_row.addWidget(self._step_spin)
        range_row.addStretch()
        sl.addLayout(range_row)

        # Acquisition row
        acq_row = QHBoxLayout()
        acq_row.addWidget(QLabel("Files/step:"))
        self._files_spin = QSpinBox()
        self._files_spin.setRange(1, 100)
        self._files_spin.setValue(1)
        self._files_spin.setMinimumWidth(60)
        acq_row.addWidget(self._files_spin)
        acq_row.addWidget(QLabel("Settle (s):"))
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.0, 300.0)
        self._settle_spin.setDecimals(1)
        self._settle_spin.setValue(2.0)
        self._settle_spin.setMinimumWidth(80)
        acq_row.addWidget(self._settle_spin)
        acq_row.addWidget(QLabel("Prefix:"))
        self._prefix_edit = QLineEdit("ptrap")
        self._prefix_edit.setMaximumWidth(120)
        self._prefix_edit.setToolTip(
            "Filename prefix — files will be named {prefix}_amp_{val} or {prefix}_freq_{val}"
        )
        acq_row.addWidget(self._prefix_edit)
        acq_row.addStretch()
        sl.addLayout(acq_row)
        root.addWidget(sw_box)

        # Initialize spinbox ranges and mode
        self._on_mode_changed()

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
    # Mode / waveform type change handlers
    # ------------------------------------------------------------------

    def _on_wf_type_changed(self, wf_type: str):
        is_arb_or_noise = wf_type in ("ARB (loaded)", "Noise")
        self._freq_mode_rb.setEnabled(not is_arb_or_noise)
        # Force amplitude mode for ARB/Noise
        if is_arb_or_noise and self._freq_mode_rb.isChecked():
            self._amp_mode_rb.setChecked(True)
        # Disable frequency control for ARB (freq is set via ARB rate)
        self._freq_spin.setEnabled(wf_type != "ARB (loaded)")

    def _on_mode_changed(self):
        is_amp = self._amp_mode_rb.isChecked()
        if is_amp:
            self._start_spin.setRange(0.001, 20.0)
            self._stop_spin.setRange(0.001, 20.0)
            self._step_spin.setRange(0.001, 10.0)
            if self._start_spin.value() == 0 or self._start_spin.value() > 20:
                self._start_spin.setValue(0.1)
                self._stop_spin.setValue(2.0)
                self._step_spin.setValue(0.1)
        else:
            self._start_spin.setRange(0.001, 25e6)
            self._stop_spin.setRange(0.001, 25e6)
            self._step_spin.setRange(0.001, 1e6)
            if self._start_spin.value() < 1.0:
                self._start_spin.setValue(100.0)
                self._stop_spin.setValue(1000.0)
                self._step_spin.setValue(100.0)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_box.append(msg)
        self._log_box.verticalScrollBar().setValue(
            self._log_box.verticalScrollBar().maximum()
        )

    # ------------------------------------------------------------------
    # Connection
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
            ok = afg.connect(port) if port else afg.auto_connect()
        except Exception as exc:
            self._log(f"Connection error: {exc}")
            return

        if not ok:
            self._log("ERROR: Could not connect to AFG-2225.")
            return

        self._afg = afg
        detected_port = afg.connection.port or port or "?"
        if not self._port_edit.text().strip():
            self._port_edit.setText(str(detected_port))
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
        ch     = self._channel()
        freq   = self._freq_spin.value()
        amp    = self._amp_spin.value()
        offset = self._offset_spin.value()
        phase  = self._phase_spin.value()
        wt     = self._wf_type.currentText()
        try:
            if wt == "Sine":
                self._afg.setup_sine(ch, frequency=freq, amplitude=amp,
                                     offset=offset)
                self._afg.set_phase(ch, phase)
            elif wt == "Square":
                self._afg.setup_square(ch, frequency=freq, amplitude=amp,
                                       offset=offset)
                self._afg.set_phase(ch, phase)
            elif wt == "Ramp":
                self._afg.setup_ramp(ch, frequency=freq, amplitude=amp,
                                     offset=offset)
                self._afg.set_phase(ch, phase)
            elif wt == "Noise":
                self._afg.setup_noise(ch, amplitude=amp, offset=offset)
            elif wt == "ARB (loaded)":
                # Apply amplitude/offset to the currently loaded ARB waveform
                self._afg.set_amplitude(ch, amp)
                self._afg.set_offset(ch, offset)
            self._wf_status.setText(
                f"Applied — {wt}, {freq:.4g} Hz, {amp:.3g} Vpp"
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

    def _build_values(self) -> list[float]:
        start = self._start_spin.value()
        stop  = self._stop_spin.value()
        step  = self._step_spin.value()
        vals  = []
        v = start
        while v <= stop + 1e-9:
            vals.append(round(v, 8))
            v += step
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

        mode = "amplitude" if self._amp_mode_rb.isChecked() else "frequency"
        vals = self._build_values()
        if not vals:
            self._log("ERROR: No sweep steps in range.")
            return

        prefix       = self._prefix_edit.text().strip() or "ptrap"
        files_step   = self._files_spin.value()
        fixed_param  = (self._freq_spin.value() if mode == "amplitude"
                        else self._amp_spin.value())

        self._log(
            f"Starting {mode} sweep: {len(vals)} step(s), "
            f"{vals[0]:.4g}→{vals[-1]:.4g}, {files_step} file(s)/step"
        )

        self._worker = _SweepWorker(
            afg=self._afg,
            channel=self._channel(),
            mode=mode,
            values=vals,
            fixed_param=fixed_param,
            settle_s=self._settle_spin.value(),
            files_per_step=files_step,
            prefix=prefix,
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
    NAME = "Paul Trap Sweep"
    DESCRIPTION = (
        "Automated AFG-2225 amplitude or frequency sweep for Paul trap electrodes. "
        "Supports any waveform type including pre-loaded ARB frequency combs."
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
