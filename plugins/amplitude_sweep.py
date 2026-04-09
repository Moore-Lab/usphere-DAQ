"""
Parameter Sweep Plugin
======================
Automated Coriolis-force optimization: sweep the linear stage through a
range of oscillation amplitudes or frequencies to find the optimum
signal-to-noise ratio.

Physics:
 - Real acceleration scales linearly with amplitude at fixed frequency
   (a ∝ A).  Normalised metric: RMS / A.
 - Real acceleration scales as frequency² at fixed amplitude
   (a ∝ f²).  Normalised metric: RMS / f².
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
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
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure
from scipy.signal import welch

import daq_h5
from plugins.base import AnalysisPlugin

# ---------------------------------------------------------------------------
# ESP32 controller import helper
# ---------------------------------------------------------------------------
_ESP_DIR = Path(__file__).resolve().parent.parent / "ESP32-stepper-controller"
_CTRL_PATH = _ESP_DIR / "controller.py"


def _get_stepper_controller_class():
    """Import StepperController from the ESP32 submodule by file path."""
    import importlib.util
    if str(_ESP_DIR) not in sys.path:
        sys.path.insert(0, str(_ESP_DIR))
    spec = importlib.util.spec_from_file_location(
        "_ext_stepper_controller", str(_CTRL_PATH),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.StepperController, mod.find_esp32_port


# ---------------------------------------------------------------------------
# Sweep worker thread
# ---------------------------------------------------------------------------

class _SweepWorker(QThread):
    """Runs a parameter sweep in a background thread.

    Accepts a list of (amplitude, frequency) pairs.
    """
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)          # (step, total)
    step_result = pyqtSignal(float, float)   # unused placeholder
    finished = pyqtSignal(bool)              # success flag
    request_recording = pyqtSignal(str)      # basename for one acquisition

    def __init__(self, ctrl, steps: list[tuple[float, float]], settle_s: float,
                 mode: str, parent=None):
        super().__init__(parent)
        self._ctrl = ctrl
        self._steps = steps          # [(amp_mm, freq_hz), ...]
        self._settle_s = settle_s
        self._mode = mode            # "amplitude" or "frequency"
        self._cancel = False
        self._file_path: str | None = None
        self._file_event = False

    def cancel(self):
        self._cancel = True

    def notify_file_written(self, path: str):
        """Called from main thread when DAQ writes a file."""
        self._file_path = path
        self._file_event = True

    def run(self):
        try:
            ctrl = self._ctrl
            n = len(self._steps)

            ctrl.set_waveform("SINE")
            ctrl.enable()
            time.sleep(0.5)

            for i, (amp, freq) in enumerate(self._steps):
                if self._cancel:
                    self.log.emit("Sweep cancelled.")
                    break

                self.progress.emit(i + 1, n)
                if self._mode == "amplitude":
                    self.log.emit(f"--- Step {i+1}/{n}: amplitude = {amp:.3f} mm ---")
                else:
                    self.log.emit(f"--- Step {i+1}/{n}: frequency = {freq:.3f} Hz ---")

                ctrl.set_amplitude(amp)
                ctrl.set_frequency(freq)
                ctrl.start()

                # Wait for motion to settle
                self.log.emit(f"  Settling for {self._settle_s:.1f} s ...")
                t0 = time.time()
                while time.time() - t0 < self._settle_s:
                    if self._cancel:
                        break
                    time.sleep(0.1)

                if self._cancel:
                    ctrl.stop()
                    break

                # Trigger DAQ recording
                if self._mode == "amplitude":
                    basename = f"sweep_amp_{amp:.3f}mm".replace('.', '_')
                else:
                    basename = f"sweep_freq_{freq:.3f}Hz".replace('.', '_')
                self._file_event = False
                self._file_path = None
                self.request_recording.emit(basename)

                # Wait for file (timeout 120 s)
                self.log.emit("  Waiting for DAQ acquisition ...")
                t0 = time.time()
                while not self._file_event:
                    if self._cancel:
                        break
                    if time.time() - t0 > 120:
                        self.log.emit("  ERROR: DAQ acquisition timeout.")
                        break
                    time.sleep(0.1)

                # Stop table motion
                ctrl.stop()
                time.sleep(0.3)

                if self._cancel or self._file_path is None:
                    continue

                self.log.emit(f"  File: {self._file_path}")
                self.step_result.emit(amp, freq)

            ctrl.stop()
            self.finished.emit(not self._cancel)

        except Exception as exc:
            self.log.emit(f"Sweep error: {exc}")
            try:
                self._ctrl.stop()
            except Exception:
                pass
            self.finished.emit(False)


# ---------------------------------------------------------------------------
# Sweep widget
# ---------------------------------------------------------------------------

class SweepWidget(QWidget):
    def __init__(self, plugin: "Plugin", parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._ctrl = None          # StepperController instance
        self._worker: _SweepWorker | None = None
        self._results: list[tuple[float, float, float]] = []  # (amp, freq, rms)
        self._sweep_mode: str = "amplitude"  # "amplitude" or "frequency"
        self._files: dict[str, str] = {}  # key → filepath
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        # --- Connection ---
        conn = QGroupBox("ESP32 Connection")
        cl = QHBoxLayout(conn)
        cl.addWidget(QLabel("Port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("Auto-detect")
        self._port_edit.setMaximumWidth(120)
        cl.addWidget(self._port_edit)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._toggle_connect)
        cl.addWidget(self._connect_btn)
        self._conn_status = QLabel("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        cl.addWidget(self._conn_status)
        cl.addStretch()
        root.addWidget(conn)

        # --- Sweep parameters ---
        params = QGroupBox("Sweep Parameters")
        gl = QVBoxLayout(params)

        # Mode selector
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Sweep mode:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Amplitude", "Frequency"])
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        gl.addLayout(mode_row)

        # Amplitude range row
        r1 = QHBoxLayout()
        self._amp_range_lbl = QLabel("Amplitude range (mm):")
        r1.addWidget(self._amp_range_lbl)
        self._amp_lo = QDoubleSpinBox()
        self._amp_lo.setRange(0.01, 10.0)
        self._amp_lo.setValue(0.1)
        self._amp_lo.setDecimals(3)
        self._amp_lo.setSingleStep(0.1)
        r1.addWidget(self._amp_lo)
        r1.addWidget(QLabel("to"))
        self._amp_hi = QDoubleSpinBox()
        self._amp_hi.setRange(0.01, 10.0)
        self._amp_hi.setValue(2.0)
        self._amp_hi.setDecimals(3)
        self._amp_hi.setSingleStep(0.1)
        r1.addWidget(self._amp_hi)
        self._amp_step_lbl = QLabel("Step:")
        r1.addWidget(self._amp_step_lbl)
        self._amp_step = QDoubleSpinBox()
        self._amp_step.setRange(0.001, 5.0)
        self._amp_step.setValue(0.1)
        self._amp_step.setDecimals(3)
        self._amp_step.setSingleStep(0.05)
        r1.addWidget(self._amp_step)
        r1.addStretch()
        gl.addLayout(r1)

        # Frequency range row
        r1f = QHBoxLayout()
        self._freq_range_lbl = QLabel("Frequency range (Hz):")
        r1f.addWidget(self._freq_range_lbl)
        self._freq_lo = QDoubleSpinBox()
        self._freq_lo.setRange(0.01, 100.0)
        self._freq_lo.setValue(0.5)
        self._freq_lo.setDecimals(3)
        self._freq_lo.setSingleStep(0.1)
        r1f.addWidget(self._freq_lo)
        self._freq_lo_to_lbl = QLabel("to")
        r1f.addWidget(self._freq_lo_to_lbl)
        self._freq_hi = QDoubleSpinBox()
        self._freq_hi.setRange(0.01, 100.0)
        self._freq_hi.setValue(5.0)
        self._freq_hi.setDecimals(3)
        self._freq_hi.setSingleStep(0.5)
        r1f.addWidget(self._freq_hi)
        self._freq_step_lbl = QLabel("Step:")
        r1f.addWidget(self._freq_step_lbl)
        self._freq_step = QDoubleSpinBox()
        self._freq_step.setRange(0.001, 50.0)
        self._freq_step.setValue(0.5)
        self._freq_step.setDecimals(3)
        self._freq_step.setSingleStep(0.1)
        r1f.addWidget(self._freq_step)
        r1f.addStretch()
        gl.addLayout(r1f)

        # Fixed parameter row
        r2 = QHBoxLayout()
        self._fixed_freq_lbl = QLabel("Frequency (Hz):")
        r2.addWidget(self._fixed_freq_lbl)
        self._freq_spin = QDoubleSpinBox()
        self._freq_spin.setRange(0.01, 50.0)
        self._freq_spin.setValue(1.0)
        self._freq_spin.setDecimals(2)
        r2.addWidget(self._freq_spin)
        self._fixed_amp_lbl = QLabel("Amplitude (mm):")
        r2.addWidget(self._fixed_amp_lbl)
        self._amp_spin = QDoubleSpinBox()
        self._amp_spin.setRange(0.01, 10.0)
        self._amp_spin.setValue(0.5)
        self._amp_spin.setDecimals(3)
        r2.addWidget(self._amp_spin)
        r2.addWidget(QLabel("Settle time (s):"))
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.5, 60.0)
        self._settle_spin.setValue(3.0)
        self._settle_spin.setDecimals(1)
        r2.addWidget(self._settle_spin)
        r2.addStretch()
        gl.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("RMS band (Hz):"))
        self._rms_lo = QDoubleSpinBox()
        self._rms_lo.setRange(0.0, 1000.0)
        self._rms_lo.setValue(0.1)
        self._rms_lo.setDecimals(2)
        r3.addWidget(self._rms_lo)
        r3.addWidget(QLabel("–"))
        self._rms_hi = QDoubleSpinBox()
        self._rms_hi.setRange(0.0, 10000.0)
        self._rms_hi.setValue(20.0)
        self._rms_hi.setDecimals(2)
        r3.addWidget(self._rms_hi)
        r3.addWidget(QLabel("Accel ch:"))
        self._accel_ch = QComboBox()
        self._accel_ch.addItems(daq_h5.ALL_CHANNELS)
        self._accel_ch.setCurrentIndex(0)
        r3.addWidget(self._accel_ch)
        r3.addWidget(QLabel("Sensitivity (mV/g):"))
        self._sens_edit = QLineEdit("1000")
        self._sens_edit.setMaximumWidth(70)
        r3.addWidget(self._sens_edit)
        r3.addStretch()
        gl.addLayout(r3)

        root.addWidget(params)

        # Apply initial mode visibility
        self._on_mode_changed(0)

        # --- Controls ---
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
        self._log_box.setMaximumHeight(150)
        self._log_box.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(self._log_box)

        # --- Plot area ---
        self._plot_layout = QVBoxLayout()
        root.addLayout(self._plot_layout)
        root.addStretch()

    def _on_mode_changed(self, idx: int):
        """Show/hide widgets based on sweep mode."""
        is_amp = (idx == 0)
        # Amplitude range row — visible in amplitude mode
        for w in (self._amp_range_lbl, self._amp_lo, self._amp_hi,
                  self._amp_step_lbl, self._amp_step):
            w.setVisible(is_amp)
        # Frequency range row — visible in frequency mode
        for w in (self._freq_range_lbl, self._freq_lo, self._freq_lo_to_lbl,
                  self._freq_hi, self._freq_step_lbl, self._freq_step):
            w.setVisible(not is_amp)
        # Fixed frequency — visible in amplitude mode
        self._fixed_freq_lbl.setVisible(is_amp)
        self._freq_spin.setVisible(is_amp)
        # Fixed amplitude — visible in frequency mode
        self._fixed_amp_lbl.setVisible(not is_amp)
        self._amp_spin.setVisible(not is_amp)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # ESP32 connection
    # ------------------------------------------------------------------

    def _toggle_connect(self):
        if self._ctrl is not None:
            try:
                self._ctrl.stop()
                self._ctrl.disable()
                self._ctrl.disconnect()
            except Exception:
                pass
            self._ctrl = None
            self._connect_btn.setText("Connect")
            self._conn_status.setText("Disconnected")
            self._conn_status.setStyleSheet("color: gray;")
            self._log("ESP32 disconnected.")
            return

        try:
            SC, find_port = _get_stepper_controller_class()
            port = self._port_edit.text().strip()
            if not port:
                port = find_port()
                if port is None:
                    self._log("ERROR: Could not auto-detect ESP32 port.")
                    return
                self._port_edit.setText(port)

            self._ctrl = SC(port)
            self._ctrl.set_line_callback(lambda line: None)  # suppress serial output
            self._ctrl.connect()
            self._connect_btn.setText("Disconnect")
            self._conn_status.setText(f"Connected ({port})")
            self._conn_status.setStyleSheet("color: green; font-weight: bold;")
            self._log(f"Connected to ESP32 on {port}")
        except Exception as exc:
            self._log(f"Connection error: {exc}")
            self._ctrl = None

    # ------------------------------------------------------------------
    # Sweep control
    # ------------------------------------------------------------------

    def _build_amplitudes(self) -> list[float]:
        lo = self._amp_lo.value()
        hi = self._amp_hi.value()
        step = self._amp_step.value()
        vals = []
        a = lo
        while a <= hi + 1e-9:
            vals.append(round(a, 4))
            a += step
        return vals

    def _build_frequencies(self) -> list[float]:
        lo = self._freq_lo.value()
        hi = self._freq_hi.value()
        step = self._freq_step.value()
        vals = []
        f = lo
        while f <= hi + 1e-9:
            vals.append(round(f, 4))
            f += step
        return vals

    def _start_sweep(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return

        if self._plugin.daq is None:
            self._log("ERROR: DAQ controller not available.")
            return

        if self._plugin.daq.is_recording():
            self._log("ERROR: A recording is already in progress.")
            return

        mode = "amplitude" if self._mode_combo.currentIndex() == 0 else "frequency"
        self._sweep_mode = mode

        if mode == "amplitude":
            amps = self._build_amplitudes()
            freq = self._freq_spin.value()
            steps = [(a, freq) for a in amps]
            self._log(f"Starting amplitude sweep: {len(amps)} steps from "
                      f"{amps[0]:.3f} to {amps[-1]:.3f} mm at {freq:.2f} Hz")
        else:
            freqs = self._build_frequencies()
            amp = self._amp_spin.value()
            steps = [(amp, f) for f in freqs]
            self._log(f"Starting frequency sweep: {len(freqs)} steps from "
                      f"{freqs[0]:.3f} to {freqs[-1]:.3f} Hz at {amp:.3f} mm")

        if not steps:
            self._log("ERROR: No sweep steps in range.")
            return

        self._results.clear()
        self._files.clear()
        self._clear_plots()

        self._worker = _SweepWorker(
            ctrl=self._ctrl,
            steps=steps,
            settle_s=self._settle_spin.value(),
            mode=mode,
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.step_result.connect(self._on_step_result)
        self._worker.request_recording.connect(self._on_request_recording)
        self._worker.finished.connect(self._on_sweep_finished)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

    def _cancel_sweep(self):
        if self._worker:
            self._worker.cancel()
            self._log("Cancelling sweep ...")

    def _on_progress(self, step: int, total: int):
        self._progress_lbl.setText(f"Step {step}/{total}")

    def _on_request_recording(self, basename: str):
        """Worker requests a single DAQ acquisition."""
        self._plugin.daq.start_recording(n_files=1, basename=basename)

    def on_file_written(self, filepath: str):
        """Called by the plugin when a file is written."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.notify_file_written(filepath)
            try:
                rms = self._analyse_file(filepath)
                idx = len(self._results)
                if idx < len(self._worker._steps):
                    amp, freq = self._worker._steps[idx]
                else:
                    amp, freq = 0.0, 0.0
                self._results.append((amp, freq, rms))
                self._files[f"{amp}_{freq}"] = filepath

                if self._sweep_mode == "amplitude":
                    normed = rms / amp if amp > 0 else float('inf')
                    self._log(f"  RMS = {rms:.4e} g  |  RMS/A = {normed:.4e} g/mm")
                else:
                    normed = rms / freq**2 if freq > 0 else float('inf')
                    self._log(f"  RMS = {rms:.4e} g  |  RMS/f² = {normed:.4e} g/Hz²")
                self._update_plot()
            except Exception as exc:
                self._log(f"  Analysis error: {exc}")

    def _on_step_result(self, amp: float, rms: float):
        pass  # analysis done in on_file_written

    def _on_sweep_finished(self, success: bool):
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress_lbl.setText("")
        if success:
            self._log("Sweep complete.")
            if self._results:
                if self._sweep_mode == "amplitude":
                    best = min(self._results,
                               key=lambda x: x[2] / x[0] if x[0] > 0 else float('inf'))
                    normed = best[2] / best[0] if best[0] > 0 else float('inf')
                    self._log(f"  Optimal amplitude: {best[0]:.3f} mm "
                              f"(RMS/A = {normed:.4e} g/mm, "
                              f"RMS = {best[2]:.4e} g)")
                else:
                    best = min(self._results,
                               key=lambda x: x[2] / x[1]**2 if x[1] > 0 else float('inf'))
                    normed = best[2] / best[1]**2 if best[1] > 0 else float('inf')
                    self._log(f"  Optimal frequency: {best[1]:.3f} Hz "
                              f"(RMS/f² = {normed:.4e} g/Hz², "
                              f"RMS = {best[2]:.4e} g)")
        self._worker = None

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _analyse_file(self, filepath: str) -> float:
        """Compute broadband RMS acceleration in [rms_lo, rms_hi] Hz."""
        ch = self._accel_ch.currentText()
        sensitivity = float(self._sens_edit.text()) if self._sens_edit.text() else 1000.0
        f_lo = self._rms_lo.value()
        f_hi = self._rms_hi.value()

        data, fs = daq_h5.read_channel(filepath, ch)

        # Compute ASD
        nperseg = min(2**15, len(data))
        freq, Pxx = welch(data - data.mean(), fs=fs, nperseg=nperseg)
        asd_g = np.sqrt(Pxx) / sensitivity

        # RMS in band
        df = freq[1] - freq[0] if len(freq) > 1 else 1.0
        band = (freq >= f_lo) & (freq <= f_hi)
        rms = np.sqrt(np.sum(asd_g[band] ** 2) * df)
        return float(rms)

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _clear_plots(self):
        while self._plot_layout.count():
            item = self._plot_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _update_plot(self):
        self._clear_plots()
        if not self._results:
            return

        f_lo_rms, f_hi_rms = self._rms_lo.value(), self._rms_hi.value()

        if self._sweep_mode == "amplitude":
            x = np.array([r[0] for r in self._results])  # amplitude
            rmses = np.array([r[2] for r in self._results])
            normed = rmses / x
            x_label = 'Amplitude (mm)'
            norm_label = 'RMS / A  (g/mm)'
            ref_power = 1  # RMS ∝ A^1
        else:
            x = np.array([r[1] for r in self._results])  # frequency
            rmses = np.array([r[2] for r in self._results])
            normed = rmses / x**2
            x_label = 'Frequency (Hz)'
            norm_label = 'RMS / f²  (g/Hz²)'
            ref_power = 2  # RMS ∝ f^2

        fig = Figure(figsize=(10, 4), tight_layout=True)
        ax1 = fig.add_subplot(1, 2, 1)
        ax2 = fig.add_subplot(1, 2, 2)

        # Left panel: raw RMS + scaling reference
        ax1.plot(x, rmses, 'o-', color='C0', lw=1.5, ms=6, label='Measured RMS')
        if len(self._results) >= 2:
            # Reference: RMS ∝ x^ref_power (fit coefficient through origin)
            coeff = np.sum(x**ref_power * rmses) / np.sum(x**(2 * ref_power))
            x_fit = np.linspace(0, float(x.max()) * 1.05, 100)
            dep_str = 'A' if ref_power == 1 else 'f²'
            ax1.plot(x_fit, coeff * x_fit**ref_power, '--', color='gray', lw=1.0,
                     label=f'Linear ref (∝ {dep_str})')
        ax1.set_xlabel(x_label)
        ax1.set_ylabel('RMS Accel (g)')
        ax1.set_title(f'Raw RMS ({f_lo_rms:g}–{f_hi_rms:g} Hz)', fontsize=10)
        ax1.legend(fontsize=8)
        ax1.grid(True, ls=':')

        # Right panel: normalised metric
        ax2.plot(x, normed, 's-', color='C1', lw=1.5, ms=6)
        if len(self._results) >= 2:
            best_idx = int(np.argmin(normed))
            opt_label = (f'{x[best_idx]:.3f} mm' if self._sweep_mode == "amplitude"
                         else f'{x[best_idx]:.3f} Hz')
            ax2.plot(x[best_idx], normed[best_idx], '*', color='red',
                     ms=14, zorder=5, label=f'Optimum: {opt_label}')
            ax2.legend(fontsize=9)
        ax2.set_xlabel(x_label)
        ax2.set_ylabel(norm_label)
        ax2.set_title('Normalised noise (lower = better)', fontsize=10)
        ax2.grid(True, ls=':')

        canvas = FigureCanvasQTAgg(fig)
        toolbar = NavigationToolbar2QT(canvas, self)
        self._plot_layout.addWidget(toolbar)
        self._plot_layout.addWidget(canvas)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def teardown(self):
        if self._worker:
            self._worker.cancel()
            self._worker.wait(5000)
        if self._ctrl:
            try:
                self._ctrl.stop()
                self._ctrl.disable()
                self._ctrl.disconnect()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class Plugin(AnalysisPlugin):
    NAME = "Parameter Sweep"
    DESCRIPTION = (
        "Automated Coriolis optimization: sweep amplitude (RMS ∝ A) or "
        "frequency (RMS ∝ f²) while recording accelerometer data to find "
        "the optimum signal-to-noise operating point."
    )

    def __init__(self):
        self._widget: SweepWidget | None = None

    def create_widget(self, parent=None):
        self._widget = SweepWidget(self, parent)
        return self._widget

    def on_file_written(self, filepath: str):
        if self._widget is not None:
            self._widget.on_file_written(filepath)

    def teardown(self):
        if self._widget is not None:
            self._widget.teardown()
