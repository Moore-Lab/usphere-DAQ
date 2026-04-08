"""
Plugin: Coriolis Alignment Diagnostics

Online wrapper for Microsphere-Utility-Scripts/coriolis_alignment.py.
Provides embedded accelerometer & encoder diagnostic plots with live updating.

Plot flags mirror the offline script:
  - Raw voltage time series
  - Calibrated (g / mm) time series
  - ASD per axis (accel + encoder-derived)
  - Transverse floor (y vs 1%·x, 1%·z)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
from scipy.signal import welch, butter, filtfilt

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
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
# Worker thread — loads a directory of h5 files
# ---------------------------------------------------------------------------

class _LoadWorker(QThread):
    finished = pyqtSignal(object)  # list[dict] on success, str on error
    log = pyqtSignal(str)

    def __init__(self, data_dir, accel_col, encoder_col):
        super().__init__()
        self.data_dir = data_dir
        self.accel_col = accel_col
        self.encoder_col = encoder_col

    def run(self):
        try:
            from coriolis_alignment import load_dataset, ACCEL_COL, ENCODER_COL
            # Temporarily override column constants
            import coriolis_alignment as ca
            orig_a, orig_e = ca.ACCEL_COL, ca.ENCODER_COL
            ca.ACCEL_COL = self.accel_col
            ca.ENCODER_COL = self.encoder_col
            try:
                records = load_dataset(self.data_dir)
            finally:
                ca.ACCEL_COL = orig_a
                ca.ENCODER_COL = orig_e

            self.log.emit(f"Loaded {len(records)} file(s) from {self.data_dir}")
            self.finished.emit(records)
        except Exception as exc:
            self.finished.emit(f"Load error: {exc}")


# ---------------------------------------------------------------------------
# Plugin widget
# ---------------------------------------------------------------------------

class AlignmentWidget(QWidget):
    """Full tab widget for coriolis_alignment diagnostics."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._records: list[dict] = []
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

        # Data directory
        dg = QGroupBox("Data")
        dgl = QGridLayout(dg)
        dgl.setColumnStretch(1, 1)
        r = 0

        dgl.addWidget(QLabel("Directory:"), r, 0)
        self._dir_edit = QLineEdit()
        dgl.addWidget(self._dir_edit, r, 1)
        b = QPushButton("…")
        b.setMaximumWidth(28)
        b.clicked.connect(lambda: self._browse_dir(self._dir_edit))
        dgl.addWidget(b, r, 2)
        r += 1

        load_btn = QPushButton("Load Dataset")
        load_btn.setStyleSheet("font-weight: bold;")
        load_btn.clicked.connect(self._load_dataset)
        dgl.addWidget(load_btn, r, 0, 1, 3)
        self._load_btn = load_btn
        lv.addWidget(dg)

        # Hardware constants
        hw = QGroupBox("Hardware")
        hg = QGridLayout(hw)
        hg.setColumnStretch(1, 1)
        r = 0

        self._hw = {}
        for key, label, default in [
            ("sensitivity",  "Sensitivity (V/g)", "1000.0"),
            ("encoder_v_fs", "Encoder V FS",      "3.3"),
            ("encoder_mm_lo", "Enc mm low",        "-0.6375"),
            ("encoder_mm_hi", "Enc mm high",       "0.6375"),
            ("accel_col",     "Accel column",      "16"),
            ("encoder_col",   "Encoder column",    "17"),
        ]:
            hg.addWidget(QLabel(f"{label}:"), r, 0)
            e = QLineEdit(default)
            e.setMaximumWidth(80)
            self._hw[key] = e
            hg.addWidget(e, r, 1)
            r += 1

        hg.addWidget(QLabel("Nseg (2^N):"), r, 0)
        self._nseg_spin = QSpinBox()
        self._nseg_spin.setRange(8, 22)
        self._nseg_spin.setValue(15)
        hg.addWidget(self._nseg_spin, r, 1)
        lv.addWidget(hw)

        # Plot flags
        flags = QGroupBox("Plot flags")
        fv = QVBoxLayout(flags)
        self._cb_raw_ts = QCheckBox("Raw voltage time series")
        self._cb_raw_ts.setChecked(False)
        fv.addWidget(self._cb_raw_ts)
        self._cb_cal_ts = QCheckBox("Calibrated time series")
        self._cb_cal_ts.setChecked(False)
        fv.addWidget(self._cb_cal_ts)
        self._cb_psd = QCheckBox("ASD per axis")
        self._cb_psd.setChecked(True)
        fv.addWidget(self._cb_psd)
        self._cb_transverse = QCheckBox("Transverse floor")
        self._cb_transverse.setChecked(False)
        fv.addWidget(self._cb_transverse)

        # Axis selector
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axis:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItems(["x", "y", "z"])
        self._axis_combo.setCurrentIndex(0)
        ax_row.addWidget(self._axis_combo)
        ax_row.addStretch()
        fv.addLayout(ax_row)

        plot_row = QHBoxLayout()
        self._plot_btn = QPushButton("Plot")
        self._plot_btn.setMinimumWidth(80)
        self._plot_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 2px 14px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        self._plot_btn.clicked.connect(self._plot)
        plot_row.addWidget(self._plot_btn)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._clear_plots)
        plot_row.addWidget(self._clear_btn)
        plot_row.addStretch()
        fv.addLayout(plot_row)

        # Live mode
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
        fv.addLayout(live_row)

        lv.addWidget(flags)

        # Log
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(100)
        self._log_box.setStyleSheet(
            "font-size: 10px; font-family: Consolas, 'Courier New', monospace;"
        )
        lv.addWidget(self._log_box)
        lv.addStretch()

        splitter.addWidget(left)

        # ---- Right: scrollable plot area ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setSpacing(2)
        rv.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._plot_container = QWidget()
        self._plot_layout = QVBoxLayout(self._plot_container)
        self._plot_layout.setSpacing(4)
        self._plot_layout.setContentsMargins(4, 4, 4, 4)
        scroll.setWidget(self._plot_container)
        rv.addWidget(scroll)

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

    def _hwf(self, key: str, fallback: float = 0.0) -> float:
        try:
            return float(self._hw[key].text())
        except (ValueError, KeyError):
            return fallback

    def _hwi(self, key: str, fallback: int = 0) -> int:
        try:
            return int(self._hw[key].text())
        except (ValueError, KeyError):
            return fallback

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
            self._records.clear()
            self._live_lbl.setText("Waiting for data…")
            self._log("Live mode ON — plotting each incoming file.")
        else:
            self._live_lbl.setText("")
            self._log("Live mode OFF.")

    # ------------------------------------------------------------------
    # Calibration helpers (use widget settings, not module globals)
    # ------------------------------------------------------------------

    def _to_accel_g(self, V):
        return V / self._hwf("sensitivity", 1000.0)

    def _to_position_mm(self, V):
        v_fs = self._hwf("encoder_v_fs", 3.3)
        mm_lo = self._hwf("encoder_mm_lo", -0.6375)
        mm_hi = self._hwf("encoder_mm_hi", 0.6375)
        return (V / v_fs) * (mm_hi - mm_lo) + mm_lo

    def _encoder_to_accel_g_td(self, encoder_V, fs, lowpass_hz=20.0):
        pos_mm = self._to_position_mm(encoder_V)
        b, a = butter(4, lowpass_hz / (0.5 * fs), btype='low')
        pos_filt = filtfilt(b, a, pos_mm)
        dt = 1.0 / fs
        vel = np.gradient(pos_filt, dt)
        acc = np.gradient(vel, dt)
        return acc * 1e-3 / 9.80665

    def _mm_per_V(self):
        v_fs = self._hwf("encoder_v_fs", 3.3)
        mm_lo = self._hwf("encoder_mm_lo", -0.6375)
        mm_hi = self._hwf("encoder_mm_hi", 0.6375)
        return (mm_hi - mm_lo) / v_fs

    def _encoder_asd_to_accel_g(self, f, asd_mm):
        out = (2 * np.pi * f) ** 2 * asd_mm * 1e-3 / 9.80665
        out[f == 0] = 0.0
        return out

    # ------------------------------------------------------------------
    # Load dataset
    # ------------------------------------------------------------------

    def _load_dataset(self):
        d = self._dir_edit.text().strip()
        if not d or not os.path.isdir(d):
            self._log("Error: select a valid data directory.")
            return
        self._load_btn.setEnabled(False)
        self._log(f"Loading {d} …")

        w = _LoadWorker(d, self._hwi("accel_col", 16), self._hwi("encoder_col", 17))
        w.log.connect(self._log)
        w.finished.connect(self._on_loaded)
        w.finished.connect(w.deleteLater)
        self._worker = w
        w.start()

    def _on_loaded(self, result):
        self._load_btn.setEnabled(True)
        self._worker = None
        if isinstance(result, str):
            self._log(f"FAILED: {result}")
            return
        self._records = result
        self._log(f"  {len(result)} record(s) ready.")

    # ------------------------------------------------------------------
    # Plot helpers
    # ------------------------------------------------------------------

    def _add_figure(self, fig):
        """Add a matplotlib Figure to the scrollable plot area."""
        canvas = FigureCanvasQTAgg(fig)
        toolbar = NavigationToolbar2QT(canvas, self)
        self._plot_layout.addWidget(toolbar)
        self._plot_layout.addWidget(canvas)

    def _clear_plots(self):
        while self._plot_layout.count():
            item = self._plot_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _file_label(self, rec):
        if rec['kind'] == 'sine':
            return f"{rec['amp_mm']:.1f} mm @ {rec['freq_hz']:.0f} Hz"
        return 'noise'

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _plot(self):
        if not self._records:
            self._log("No records loaded. Load a dataset first.")
            return

        self._clear_plots()
        axis = self._axis_combo.currentText()
        label = Path(self._dir_edit.text()).name

        if self._cb_raw_ts.isChecked():
            self._plot_raw_ts(axis, label)
        if self._cb_cal_ts.isChecked():
            self._plot_cal_ts(axis, label)
        if self._cb_psd.isChecked():
            self._plot_psd(axis, label)
        if self._cb_transverse.isChecked():
            self._plot_transverse(label)

    def _plot_raw_ts(self, axis: str, label: str):
        subset = [r for r in self._records if r['axis'] == axis]
        if not subset:
            return
        thin = 5
        fig = Figure(figsize=(10, 2.6 * len(subset)), tight_layout=True)
        for i, rec in enumerate(subset):
            ax_a = fig.add_subplot(len(subset), 2, 2 * i + 1)
            ax_e = fig.add_subplot(len(subset), 2, 2 * i + 2)
            t = rec['t'][::thin]
            ax_a.plot(t, rec['accel_V'][::thin], lw=0.5, color='C0')
            ax_a.set_ylabel('Accel (V)')
            ax_a.set_title(self._file_label(rec), fontsize=9)
            ax_a.grid(True, ls=':')
            ax_e.plot(t, rec['encoder_V'][::thin], lw=0.5, color='C1')
            ax_e.set_ylabel('Encoder (V)')
            ax_e.set_title(self._file_label(rec), fontsize=9)
            ax_e.grid(True, ls=':')
            if i == len(subset) - 1:
                ax_a.set_xlabel('Time (s)')
                ax_e.set_xlabel('Time (s)')
        fig.suptitle(f'{label} — {axis.upper()} axis — raw voltages', fontsize=11)
        self._add_figure(fig)

    def _plot_cal_ts(self, axis: str, label: str):
        subset = [r for r in self._records if r['axis'] == axis]
        if not subset:
            return
        thin = 5
        fig = Figure(figsize=(10, 2.6 * len(subset)), tight_layout=True)
        for i, rec in enumerate(subset):
            ax_a = fig.add_subplot(len(subset), 2, 2 * i + 1)
            ax_e = fig.add_subplot(len(subset), 2, 2 * i + 2)
            t = rec['t'][::thin]
            a_g = self._to_accel_g(rec['accel_V'])[::thin]
            enc_g = self._encoder_to_accel_g_td(rec['encoder_V'], rec['fs'])[::thin]
            pos_mm = self._to_position_mm(rec['encoder_V'])[::thin]
            ax_a.plot(t, a_g, lw=0.5, color='C0', label='accel')
            ax_a.plot(t, enc_g, lw=0.5, color='C3', ls='--', label='enc-derived')
            ax_a.set_ylabel('accel (g)')
            ax_a.set_title(self._file_label(rec), fontsize=9)
            ax_a.legend(fontsize=7, loc='upper right')
            ax_a.grid(True, ls=':')
            ax_e.plot(t, pos_mm, lw=0.5, color='C1')
            ax_e.set_ylabel('Position (mm)')
            ax_e.set_title(self._file_label(rec), fontsize=9)
            ax_e.grid(True, ls=':')
            if i == len(subset) - 1:
                ax_a.set_xlabel('Time (s)')
                ax_e.set_xlabel('Time (s)')
        fig.suptitle(f'{label} — {axis.upper()} axis — calibrated', fontsize=11)
        self._add_figure(fig)

    def _plot_psd(self, axis: str, label: str):
        subset = [r for r in self._records if r['axis'] == axis]
        if not subset:
            return

        sensitivity = self._hwf("sensitivity", 1000.0)
        mm_per_V = self._mm_per_V()
        nseg_bits = self._nseg_spin.value()
        noise_floor_g = self._hwf("sensitivity", 1000.0)  # just for ref line calc
        noise_spec_ug = 0.03
        noise_floor_g = noise_spec_ug * 1e-6

        fig = Figure(figsize=(10, 7), tight_layout=True)
        ax_top = fig.add_subplot(2, 1, 1)
        ax_bot = fig.add_subplot(2, 1, 2)

        colors = ['C0', 'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9']

        for i, rec in enumerate(subset):
            color = colors[i % len(colors)]
            fs = rec['fs']
            nperseg = min(2 ** nseg_bits, len(rec['accel_V']))

            f, Pxx_a = welch(rec['accel_V'] - rec['accel_V'].mean(),
                             fs=fs, nperseg=nperseg)
            _, Pxx_e = welch(rec['encoder_V'] - rec['encoder_V'].mean(),
                             fs=fs, nperseg=nperseg)

            asd_a_g = np.sqrt(Pxx_a) / sensitivity
            asd_e_mm = np.sqrt(Pxx_e) * mm_per_V
            asd_enc_g = self._encoder_asd_to_accel_g(f, asd_e_mm)

            lbl = self._file_label(rec)
            ax_top.plot(f, asd_a_g, lw=0.9, color=color, label=f'{lbl} [accel]')
            ax_top.plot(f, asd_enc_g, lw=0.9, color=color, ls='--',
                        label=f'{lbl} [enc]')
            ax_bot.plot(f, asd_e_mm, lw=0.8, color=color, label=lbl)

        ax_top.axhline(noise_floor_g, color='k', ls=':', lw=1.0, alpha=0.7,
                       label=f'Spec @ 2 Hz: {noise_spec_ug} µg/√Hz')
        ax_top.set_xlim(0.1, 20)
        ax_top.set_ylabel('Accel ASD  [g/√Hz]')
        ax_top.set_title(
            f'{label} {axis.upper()} — solid: accelerometer, dashed: enc-derived',
            fontsize=10,
        )
        ax_top.legend(fontsize=7, ncol=2)
        ax_top.grid(True, which='both', ls=':')

        ax_bot.set_xlim(0.1, 20)
        ax_bot.set_ylabel('Position ASD  [mm/√Hz]')
        ax_bot.set_title(f'{label} {axis.upper()} — encoder position', fontsize=10)
        ax_bot.set_xlabel('Frequency (Hz)')
        ax_bot.legend(fontsize=8, ncol=2)
        ax_bot.grid(True, which='both', ls=':')

        self._add_figure(fig)

    def _plot_transverse(self, label: str):
        sine_recs = [r for r in self._records if r['kind'] == 'sine']
        amps = sorted(set(r['amp_mm'] for r in sine_recs))
        if not amps:
            self._log("No sine records for transverse floor plot.")
            return

        sensitivity = self._hwf("sensitivity", 1000.0)
        nseg_bits = self._nseg_spin.value()
        trans_sens = 0.01
        noise_spec_ug = 0.03

        fig = Figure(figsize=(10, 4 * len(amps)), tight_layout=True)

        for idx, amp in enumerate(amps):
            ax = fig.add_subplot(len(amps), 1, idx + 1)

            by_axis: dict[str, dict] = {}
            for r in sine_recs:
                if r['amp_mm'] == amp:
                    by_axis[r['axis']] = r

            y_rec = by_axis.get('y')
            if y_rec is None:
                ax.set_title(f'{amp:.1f} mm — no y data')
                continue

            def _asd_g(rec):
                fs = rec['fs']
                nperseg = min(2 ** nseg_bits, len(rec['accel_V']))
                sig = rec['accel_V'] - rec['accel_V'].mean()
                f, Pxx = welch(sig, fs=fs, nperseg=nperseg)
                return f, np.sqrt(Pxx) / sensitivity

            f_y, asd_y = _asd_g(y_rec)
            ax.plot(f_y, asd_y, lw=1.0, color='C0', label='y (measured)')

            asd_x_i, asd_z_i = None, None
            x_rec = by_axis.get('x')
            if x_rec is not None:
                f_x, asd_x = _asd_g(x_rec)
                asd_x_i = np.interp(f_y, f_x, asd_x)
                ax.plot(f_y, trans_sens * asd_x_i, lw=0.9, ls='--', color='C1',
                        label='1% × x')

            z_rec = by_axis.get('z')
            if z_rec is not None:
                f_z, asd_z = _asd_g(z_rec)
                asd_z_i = np.interp(f_y, f_z, asd_z)
                ax.plot(f_y, trans_sens * asd_z_i, lw=0.9, ls='-.', color='C2',
                        label='1% × z')

            if asd_x_i is not None and asd_z_i is not None:
                quad = trans_sens * np.sqrt(asd_x_i ** 2 + asd_z_i ** 2)
                ax.plot(f_y, quad, lw=1.2, ls=':', color='C3',
                        label=r'1%×√(x²+z²)')

            noise_floor_g = noise_spec_ug * 1e-6
            ax.axhline(noise_floor_g, color='k', ls='--', lw=0.8, alpha=0.6,
                       label=f'Spec: {noise_spec_ug} µg/√Hz')

            freq_tag = f"{y_rec.get('freq_hz', '?'):.0f} Hz" if y_rec.get('freq_hz') else ''
            ax.set_title(
                f'{label} — {amp:.1f} mm @ {freq_tag}  |  y vs transverse floor',
                fontsize=10,
            )
            ax.set_xlabel('Frequency (Hz)')
            ax.set_ylabel('ASD  [g/√Hz]')
            ax.set_xlim(0.1, 20)
            ax.legend(fontsize=8)
            ax.grid(True, which='both', ls=':')

        self._add_figure(fig)

    # ------------------------------------------------------------------
    # Live mode
    # ------------------------------------------------------------------

    def on_file_written(self, filepath: str):
        """Called by the plugin manager each time the DAQ writes a file."""
        if not self._live:
            return

        try:
            from coriolis_alignment import parse_filename
            from daq_h5 import read_channel

            fname = Path(filepath).name
            try:
                meta = parse_filename(fname)
            except Exception:
                # File doesn't follow the expected naming convention — skip
                return

            accel_col = self._hwi("accel_col", 16)
            encoder_col = self._hwi("encoder_col", 17)
            accel_data, fs = read_channel(filepath, f"ai{accel_col}")
            encoder_data, _ = read_channel(filepath, f"ai{encoder_col}")
            t = np.arange(len(accel_data)) / fs

            rec = {
                **meta,
                'fname': fname,
                't': t,
                'accel_V': accel_data,
                'encoder_V': encoder_data,
                'fs': fs,
            }
            self._records.append(rec)
            self._live_lbl.setText(f"{len(self._records)} file(s)")
            self._log(f"Live: {fname}")

            # Re-plot with selected flags
            axis = self._axis_combo.currentText()
            label = "live"

            self._clear_plots()
            if self._cb_raw_ts.isChecked():
                self._plot_raw_ts(axis, label)
            if self._cb_cal_ts.isChecked():
                self._plot_cal_ts(axis, label)
            if self._cb_psd.isChecked():
                self._plot_psd(axis, label)
            if self._cb_transverse.isChecked():
                self._plot_transverse(label)

        except Exception as exc:
            self._log(f"Live error: {exc}")


# ---------------------------------------------------------------------------
# Plugin class — discovered by plugins/__init__.py
# ---------------------------------------------------------------------------

class Plugin(AnalysisPlugin):
    NAME = "Coriolis Alignment"
    DESCRIPTION = (
        "Accelerometer & encoder diagnostic plots: raw/calibrated time series, "
        "ASD per axis, and transverse floor analysis."
    )

    def __init__(self):
        self._widget: AlignmentWidget | None = None

    def create_widget(self, parent=None):
        self._widget = AlignmentWidget(parent)
        return self._widget

    def on_file_written(self, filepath: str):
        if self._widget is not None:
            self._widget.on_file_written(filepath)

    def teardown(self):
        self._widget = None
