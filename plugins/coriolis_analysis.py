"""
Plugin: Coriolis Force Noise Analysis

Online wrapper for Microsphere-Utility-Scripts calibration tools (getX).
Provides embedded susceptibility and force-noise plots with live updating.

Configuration mirrors the top-of-file constants in the offline script.
Calibration (susceptibility fit) is run once; science data can be
processed in batch or accumulated live as the DAQ records new files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
from scipy.signal import welch

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure

from plugins.base import AnalysisPlugin

# Ensure Microsphere-Utility-Scripts is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "Microsphere-Utility-Scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Worker threads
# ---------------------------------------------------------------------------

class _CalWorker(QThread):
    """Run susceptibility calibration in a background thread."""

    finished = pyqtSignal(object)   # dict on success, str on error
    log = pyqtSignal(str)

    def __init__(self, cal_dir, sphere_col, electrode_col,
                 daq_to_electrode, voltage_to_field, n_charges, nseg):
        super().__init__()
        self.cal_dir = cal_dir
        self.sphere_col = sphere_col
        self.electrode_col = electrode_col
        self.daq_to_electrode = daq_to_electrode
        self.voltage_to_field = voltage_to_field
        self.n_charges = n_charges
        self.nseg = nseg

    def run(self):
        try:
            self.log.emit("Importing getX …")
            from getX import get_susceptibility

            self.log.emit(f"Fitting susceptibility from {self.cal_dir} …")
            result = get_susceptibility(
                self.cal_dir, None,
                sphere_col=self.sphere_col,
                electrode_col=self.electrode_col,
                daq_to_electrode=self.daq_to_electrode,
                voltage_to_field=self.voltage_to_field,
                n_charges=self.n_charges,
                nseg=self.nseg,
                tones=None,
                plot=False,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.finished.emit(f"Calibration error: {exc}")


class _BatchWorker(QThread):
    """Process all science files in a directory."""

    finished = pyqtSignal(object)   # (f_array, psd_avg, n_files) or str
    log = pyqtSignal(str)

    def __init__(self, sci_dir, sphere_col, nseg):
        super().__init__()
        self.sci_dir = sci_dir
        self.sphere_col = sphere_col
        self.nseg = nseg

    def run(self):
        try:
            from daq_h5 import read_channel

            files = sorted(f for f in os.listdir(self.sci_dir) if f.endswith('.h5'))
            if not files:
                self.finished.emit("No .h5 files found in science directory.")
                return

            ch_name = f"ai{self.sphere_col}"
            psd_list: list[np.ndarray] = []
            f_arr = None

            for i, fname in enumerate(files):
                self.log.emit(f"  [{i + 1}/{len(files)}] {fname}")
                fpath = os.path.join(self.sci_dir, fname)
                data, fs = read_channel(fpath, ch_name)
                data = data - np.mean(data)
                f_tmp, Pxx = welch(data, fs=fs, nperseg=self.nseg)
                if f_arr is None:
                    f_arr = f_tmp
                psd_list.append(Pxx)

            psd_avg = np.mean(np.vstack(psd_list), axis=0)
            self.finished.emit((f_arr, psd_avg, len(files)))
        except Exception as exc:
            self.finished.emit(f"Batch error: {exc}")


# ---------------------------------------------------------------------------
# Plugin widget
# ---------------------------------------------------------------------------

class CoriolisWidget(QWidget):
    """Full tab widget for the Coriolis force-noise analysis."""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Calibration state
        self._H = None
        self._f0 = None
        self._gamma = None
        self._tone_freqs = None
        self._transfer_data = None

        # Accumulated science PSD
        self._f_sci = None
        self._psd_sum = None
        self._n_files = 0

        self._live = False
        self._worker = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ---- Left: configuration ----
        left = QWidget()
        left.setMaximumWidth(340)
        lv = QVBoxLayout(left)
        lv.setSpacing(6)
        lv.setContentsMargins(4, 4, 4, 4)

        # Calibration group
        cal = QGroupBox("Calibration")
        cg = QGridLayout(cal)
        cg.setColumnStretch(1, 1)
        r = 0

        cg.addWidget(QLabel("Directory:"), r, 0)
        self._cal_dir_edit = QLineEdit()
        cg.addWidget(self._cal_dir_edit, r, 1)
        b = QPushButton("…")
        b.setMaximumWidth(28)
        b.clicked.connect(lambda: self._browse_dir(self._cal_dir_edit))
        cg.addWidget(b, r, 2)
        r += 1

        self._cfg = {}
        for key, label, default in [
            ("sphere_col",       "Sphere col",      "1"),
            ("electrode_col",    "Electrode col",   "11"),
            ("daq_to_electrode", "DAQ→electrode",   "100.0"),
            ("voltage_to_field", "V→field (V/m/V)", "19.47"),
            ("n_charges",        "Charges",         "25"),
        ]:
            cg.addWidget(QLabel(f"{label}:"), r, 0)
            e = QLineEdit(default)
            e.setMaximumWidth(80)
            self._cfg[key] = e
            cg.addWidget(e, r, 1)
            r += 1

        cg.addWidget(QLabel("Nseg (2^N):"), r, 0)
        self._nseg_cal = QSpinBox()
        self._nseg_cal.setRange(10, 22)
        self._nseg_cal.setValue(17)
        cg.addWidget(self._nseg_cal, r, 1)
        r += 1

        self._cal_btn = QPushButton("Run Calibration")
        self._cal_btn.setStyleSheet("font-weight: bold;")
        self._cal_btn.clicked.connect(self._run_calibration)
        cg.addWidget(self._cal_btn, r, 0, 1, 3)

        lv.addWidget(cal)

        # Science group
        sci = QGroupBox("Science / Analysis")
        sg = QGridLayout(sci)
        sg.setColumnStretch(1, 1)
        r = 0

        sg.addWidget(QLabel("Sci directory:"), r, 0)
        self._sci_dir_edit = QLineEdit()
        sg.addWidget(self._sci_dir_edit, r, 1)
        b2 = QPushButton("…")
        b2.setMaximumWidth(28)
        b2.clicked.connect(lambda: self._browse_dir(self._sci_dir_edit))
        sg.addWidget(b2, r, 2)
        r += 1

        sg.addWidget(QLabel("Nseg (2^N):"), r, 0)
        self._nseg_sci = QSpinBox()
        self._nseg_sci.setRange(10, 22)
        self._nseg_sci.setValue(17)
        sg.addWidget(self._nseg_sci, r, 1)
        r += 1

        sg.addWidget(QLabel("f min (Hz):"), r, 0)
        self._fmin_edit = QLineEdit("10.0")
        self._fmin_edit.setMaximumWidth(80)
        sg.addWidget(self._fmin_edit, r, 1)
        r += 1

        sg.addWidget(QLabel("f max (Hz):"), r, 0)
        self._fmax_edit = QLineEdit("200.0")
        self._fmax_edit.setMaximumWidth(80)
        sg.addWidget(self._fmax_edit, r, 1)
        r += 1

        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("Run Analysis")
        self._run_btn.setStyleSheet("font-weight: bold;")
        self._run_btn.clicked.connect(self._run_batch)
        btn_row.addWidget(self._run_btn)
        self._export_btn = QPushButton("Export CSV")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(self._export_btn)
        sg.addLayout(btn_row, r, 0, 1, 3)

        lv.addWidget(sci)

        # Plot-option flags
        opts = QGroupBox("Plot flags")
        ov = QVBoxLayout(opts)
        self._cb_susceptibility = QCheckBox("Update susceptibility plot")
        self._cb_susceptibility.setChecked(True)
        ov.addWidget(self._cb_susceptibility)
        self._cb_force = QCheckBox("Update force noise plot")
        self._cb_force.setChecked(True)
        ov.addWidget(self._cb_force)

        live_row = QHBoxLayout()
        self._live_btn = QPushButton("Live: OFF")
        self._live_btn.setCheckable(True)
        self._live_btn.setMinimumWidth(100)
        self._live_btn.toggled.connect(self._toggle_live)
        self._apply_live_style(False)
        live_row.addWidget(self._live_btn)
        self._live_lbl = QLabel("")
        self._live_lbl.setStyleSheet("color: gray; font-size: 10px;")
        live_row.addWidget(self._live_lbl)
        live_row.addStretch()
        ov.addLayout(live_row)
        lv.addWidget(opts)

        # Status log
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(110)
        self._log_box.setStyleSheet(
            "font-size: 10px; font-family: Consolas, 'Courier New', monospace;"
        )
        lv.addWidget(self._log_box)
        lv.addStretch()

        splitter.addWidget(left)

        # ---- Right: embedded plots ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setSpacing(2)
        rv.setContentsMargins(0, 0, 0, 0)

        # Susceptibility canvas
        self._cal_fig = Figure(tight_layout=True)
        self._cal_ax = self._cal_fig.add_subplot(111)
        self._cal_canvas = FigureCanvasQTAgg(self._cal_fig)
        rv.addWidget(NavigationToolbar2QT(self._cal_canvas, self))
        rv.addWidget(self._cal_canvas, stretch=1)

        # Force noise canvas
        self._fn_fig = Figure(tight_layout=True)
        self._fn_ax = self._fn_fig.add_subplot(111)
        self._fn_canvas = FigureCanvasQTAgg(self._fn_fig)
        rv.addWidget(NavigationToolbar2QT(self._fn_canvas, self))
        rv.addWidget(self._fn_canvas, stretch=1)

        splitter.addWidget(right)
        splitter.setSizes([300, 700])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _browse_dir(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select directory", edit.text())
        if d:
            edit.setText(d)

    def _apply_live_style(self, on: bool):
        if on:
            self._live_btn.setText("Live: ON")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #1e7e34; color: white; "
                "font-weight: bold; border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #28a745; }"
            )
        else:
            self._live_btn.setText("Live: OFF")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #555; color: white; "
                "font-weight: bold; border-radius: 4px; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #777; }"
            )

    def _toggle_live(self, checked: bool):
        self._live = checked
        self._apply_live_style(checked)
        if checked:
            # Reset the running average so live starts fresh
            self._psd_sum = None
            self._n_files = 0
            self._live_lbl.setText("Waiting for data…")
            self._log("Live mode ON — accumulating PSD from incoming files.")
        else:
            self._live_lbl.setText("")
            self._log("Live mode OFF.")

    def _cfgf(self, key: str, fallback: float = 0.0) -> float:
        try:
            return float(self._cfg[key].text())
        except (ValueError, KeyError):
            return fallback

    def _cfgi(self, key: str, fallback: int = 0) -> int:
        try:
            return int(self._cfg[key].text())
        except (ValueError, KeyError):
            return fallback

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _run_calibration(self):
        cal_dir = self._cal_dir_edit.text().strip()
        if not cal_dir or not os.path.isdir(cal_dir):
            self._log("Error: select a valid calibration directory.")
            return

        self._cal_btn.setEnabled(False)
        self._log(f"Calibration starting — {cal_dir}")

        w = _CalWorker(
            cal_dir,
            sphere_col=self._cfgi("sphere_col", 1),
            electrode_col=self._cfgi("electrode_col", 11),
            daq_to_electrode=self._cfgf("daq_to_electrode", 100.0),
            voltage_to_field=self._cfgf("voltage_to_field", 19.47),
            n_charges=self._cfgi("n_charges", 25),
            nseg=2 ** self._nseg_cal.value(),
        )
        w.log.connect(self._log)
        w.finished.connect(self._on_cal_done)
        w.finished.connect(w.deleteLater)
        self._worker = w
        w.start()

    def _on_cal_done(self, result):
        self._cal_btn.setEnabled(True)
        self._worker = None

        if isinstance(result, str):
            self._log(f"FAILED: {result}")
            return

        self._H = result["H"]
        self._f0 = result["f0"]
        self._gamma = result["gamma"]
        self._tone_freqs = result["tone_freqs"]
        self._transfer_data = result["transfer_data"]

        q = 2 * np.pi * self._f0 / self._gamma
        self._log(
            f"Calibration OK — f₀ = {self._f0:.4f} Hz, "
            f"γ = {self._gamma:.6f} rad/s, Q ≈ {q:.1f}"
        )

        if self._cb_susceptibility.isChecked():
            self._plot_susceptibility()

    def _plot_susceptibility(self):
        ax = self._cal_ax
        ax.clear()

        tf = self._tone_freqs
        span = tf[-1] - tf[0]
        fmin = max(0.1, tf[0] - span)
        fmax = tf[-1] + span
        f_fine = np.linspace(fmin, fmax, 2000)

        ax.semilogy(tf, self._transfer_data, "o", label="Measured tones")
        ax.semilogy(f_fine, self._H(f_fine), "-", label="Lorentzian fit")
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("|H(f)|  [m/N]")
        ax.set_title(
            f"Susceptibility — f₀={self._f0:.3f} Hz, "
            f"γ={self._gamma:.4f} rad/s",
            fontsize=10,
        )
        ax.set_xlim(fmin, fmax)
        ax.legend(fontsize=8)
        ax.grid(True, which="both", ls=":")
        self._cal_canvas.draw()

    # ------------------------------------------------------------------
    # Batch analysis
    # ------------------------------------------------------------------

    def _run_batch(self):
        if self._H is None:
            self._log("Error: run calibration first.")
            return
        sci_dir = self._sci_dir_edit.text().strip()
        if not sci_dir or not os.path.isdir(sci_dir):
            self._log("Error: select a valid science directory.")
            return

        self._run_btn.setEnabled(False)
        self._log(f"Batch processing {sci_dir} …")

        w = _BatchWorker(
            sci_dir,
            sphere_col=self._cfgi("sphere_col", 1),
            nseg=2 ** self._nseg_sci.value(),
        )
        w.log.connect(self._log)
        w.finished.connect(self._on_batch_done)
        w.finished.connect(w.deleteLater)
        self._worker = w
        w.start()

    def _on_batch_done(self, result):
        self._run_btn.setEnabled(True)
        self._worker = None

        if isinstance(result, str):
            self._log(f"FAILED: {result}")
            return

        f_arr, psd_avg, nf = result
        self._f_sci = f_arr
        self._psd_sum = psd_avg * nf
        self._n_files = nf
        self._log(f"Batch OK — {nf} file(s), Δf = {f_arr[1] - f_arr[0]:.4f} Hz")
        self._export_btn.setEnabled(True)

        if self._cb_force.isChecked():
            self._plot_force_noise()

    # ------------------------------------------------------------------
    # Force noise plot
    # ------------------------------------------------------------------

    def _plot_force_noise(self):
        if self._H is None or self._f_sci is None or self._n_files == 0:
            return

        psd_avg = self._psd_sum / self._n_files
        H_vals = self._H(self._f_sci).copy()
        H_vals[H_vals == 0] = np.nan
        force_asd = np.sqrt(psd_avg / H_vals ** 2)

        ax = self._fn_ax
        ax.clear()
        ax.semilogy(
            self._f_sci, force_asd,
            drawstyle="steps-mid", lw=0.8, color="C0",
            label="Force noise ASD",
        )
        ax.axvline(
            self._f0, color="r", ls="--", lw=1.2, alpha=0.8,
            label=f"f₀ = {self._f0:.2f} Hz",
        )
        ax.set_xlabel("Frequency (Hz)")
        ax.set_ylabel("Force ASD  [N/√Hz]")
        ax.set_title(f"Force noise — {self._n_files} file(s)", fontsize=10)

        try:
            fmin = float(self._fmin_edit.text())
            fmax = float(self._fmax_edit.text())
            ax.set_xlim(fmin, fmax)
        except ValueError:
            pass

        ax.legend(fontsize=8)
        ax.grid(True, which="both", ls=":")
        self._fn_canvas.draw()

    # ------------------------------------------------------------------
    # Live mode
    # ------------------------------------------------------------------

    def on_file_written(self, filepath: str):
        """Called by the plugin manager each time the DAQ writes a file."""
        if not self._live or self._H is None:
            return

        try:
            from daq_h5 import read_channel

            ch = f"ai{self._cfgi('sphere_col', 1)}"
            data, fs = read_channel(filepath, ch)
            data = data - np.mean(data)
            nseg = 2 ** self._nseg_sci.value()
            f, Pxx = welch(data, fs=fs, nperseg=nseg)

            if (self._psd_sum is None
                    or self._f_sci is None
                    or len(f) != len(self._f_sci)):
                self._f_sci = f
                self._psd_sum = Pxx.copy()
                self._n_files = 1
            else:
                self._psd_sum += Pxx
                self._n_files += 1

            self._live_lbl.setText(f"{self._n_files} file(s) accumulated")
            self._export_btn.setEnabled(True)

            if self._cb_force.isChecked():
                self._plot_force_noise()

            self._log(
                f"Live: {Path(filepath).name} — {self._n_files} total"
            )
        except Exception as exc:
            self._log(f"Live error: {exc}")

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def _export_csv(self):
        if self._f_sci is None or self._n_files == 0 or self._H is None:
            self._log("Nothing to export.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save force noise CSV", "force_noise_coriolis.csv",
            "CSV files (*.csv)",
        )
        if not path:
            return

        psd_avg = self._psd_sum / self._n_files
        H_vals = self._H(self._f_sci).copy()
        H_vals[H_vals == 0] = np.nan
        force_asd = np.sqrt(psd_avg / H_vals ** 2)

        csv_data = np.column_stack((self._f_sci, force_asd))
        np.savetxt(
            path, csv_data, delimiter=",",
            header="frequency_Hz,force_ASD_N_per_rtHz", comments="",
        )
        self._log(f"Exported → {path}")


# ---------------------------------------------------------------------------
# Plugin class — discovered by plugins/__init__.py
# ---------------------------------------------------------------------------

class Plugin(AnalysisPlugin):
    NAME = "Coriolis Force Noise"
    DESCRIPTION = (
        "Compute force noise ASD from susceptibility calibration "
        "and Coriolis measurement data.  Supports live accumulation."
    )

    def __init__(self):
        self._widget: CoriolisWidget | None = None

    def create_widget(self, parent=None):
        self._widget = CoriolisWidget(parent)
        return self._widget

    def on_file_written(self, filepath: str):
        if self._widget is not None:
            self._widget.on_file_written(filepath)

    def teardown(self):
        self._widget = None
