"""
daq_plot.py

PlotWidget: PyQt5 widget for visualizing usphere-DAQ HDF5 files.
Embedded as the "Plot" tab in daq_gui.py.

Features:
  - Two persistent side-by-side plots: time domain and ASD/PSD (Welch)
  - ASD (V/√Hz) or PSD (V²/Hz) selectable, ASD default
  - Multi-channel overlay: select any combination of channels
  - Bandpass filter (Butterworth) with configurable f_low, f_high, order
  - Live mode: automatically re-plots whenever a new file is written

Standalone usage:
    python daq_plot.py path/to/file.h5
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.signal import welch, butter, sosfilt

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure

from daq_h5 import ALL_CHANNELS, recorded_channels, read_channel

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# HDF5 helpers — thin wrappers around daq_h5 for backward compatibility
# ---------------------------------------------------------------------------

def get_channel_info(filepath: str | Path) -> dict[str, bool]:
    return recorded_channels(filepath)


def load_channel(filepath: str | Path, channel: str) -> tuple[np.ndarray, float]:
    return read_channel(filepath, channel)


def fmt_duration(secs: float) -> str:
    if secs < 1:
        return f"{secs * 1000:.1f} ms"
    if secs < 60:
        return f"{secs:.2f} s"
    if secs < 3600:
        return f"{secs / 60:.2f} min"
    return f"{secs / 3600:.2f} hr"


# ---------------------------------------------------------------------------
# PlotWidget
# ---------------------------------------------------------------------------

class PlotWidget(QWidget):
    """
    Dual-panel (time + ASD/PSD) plot viewer with multi-channel overlay.

    External API
    ------------
    load_file(filepath: str)
        Point the widget at a new HDF5 file.  If Live mode is on, also
        re-plots immediately.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filepath: str | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._make_file_row())
        layout.addWidget(self._make_channel_panel())
        layout.addWidget(self._make_plot_area(), stretch=1)

    # --- File row ---

    def _make_file_row(self) -> QGroupBox:
        box = QGroupBox("File")
        row = QHBoxLayout(box)

        self._file_edit = QLineEdit()
        self._file_edit.setPlaceholderText("Select an HDF5 file…")
        self._file_edit.setReadOnly(True)
        row.addWidget(self._file_edit, stretch=1)

        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_file)
        row.addWidget(browse_btn)

        self._live_btn = QPushButton("Live Plot: OFF")
        self._live_btn.setCheckable(True)
        self._live_btn.setMinimumWidth(120)
        self._live_btn.toggled.connect(self._on_live_toggled)
        self._apply_live_style(False)
        row.addWidget(self._live_btn)

        return box

    # --- Channel checkboxes + plot settings ---

    def _make_channel_panel(self) -> QGroupBox:
        box = QGroupBox("Channels & Settings")
        outer = QVBoxLayout(box)
        outer.setSpacing(6)

        # All / None buttons + scrollable checkbox grid
        ch_row = QHBoxLayout()

        btn_col = QVBoxLayout()
        for label, state in (("All", True), ("None", False)):
            btn = QPushButton(label)
            btn.setMaximumWidth(50)
            btn.clicked.connect(lambda _checked, s=state: self._set_all_channels(s))
            btn_col.addWidget(btn)
        btn_col.addStretch()
        ch_row.addLayout(btn_col)

        # Scrollable checkbox grid — 8 columns × 4 rows = 32 channels
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(90)
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setSpacing(2)
        grid.setContentsMargins(0, 0, 0, 0)

        self._ch_boxes: dict[str, QCheckBox] = {}
        N_COLS = 8
        for idx in range(32):
            ch = f"ai{idx}"
            r, c = divmod(idx, N_COLS)
            cb = QCheckBox(ch)
            cb.setStyleSheet("font-size: 10px;")
            self._ch_boxes[ch] = cb
            grid.addWidget(cb, r, c)

        scroll.setWidget(grid_w)
        ch_row.addWidget(scroll, stretch=1)
        outer.addLayout(ch_row)

        # --- Settings row ---
        settings_row = QHBoxLayout()
        settings_row.setSpacing(10)

        # Welch segment
        settings_row.addWidget(QLabel("Welch segment (2^N):"))
        self._nperseg_spin = QSpinBox()
        self._nperseg_spin.setRange(6, 22)
        self._nperseg_spin.setValue(14)
        self._nperseg_hint = QLabel()
        self._nperseg_hint.setStyleSheet("color: gray; font-size: 10px;")
        self._nperseg_spin.valueChanged.connect(self._update_nperseg_hint)
        self._update_nperseg_hint()
        settings_row.addWidget(self._nperseg_spin)
        settings_row.addWidget(self._nperseg_hint)

        settings_row.addWidget(_vsep())

        # ASD / PSD selector
        settings_row.addWidget(QLabel("Spectrum:"))
        self._spectrum_combo = QComboBox()
        self._spectrum_combo.addItems(["ASD  (V/√Hz)", "PSD  (V²/Hz)"])
        self._spectrum_combo.setCurrentIndex(0)   # ASD default
        settings_row.addWidget(self._spectrum_combo)

        settings_row.addWidget(_vsep())

        # Log axes — log X default for frequency plot
        self._logx_cb = QCheckBox("Log X")
        self._logx_cb.setChecked(True)
        self._logy_cb = QCheckBox("Log Y")
        self._logy_cb.setChecked(True)
        settings_row.addWidget(self._logx_cb)
        settings_row.addWidget(self._logy_cb)

        settings_row.addStretch()

        plot_btn = QPushButton("Plot")
        plot_btn.setMinimumWidth(80)
        plot_btn.setMinimumHeight(28)
        plot_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 2px 14px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }"
        )
        plot_btn.clicked.connect(self._plot)
        settings_row.addWidget(plot_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setMinimumWidth(65)
        clear_btn.setMinimumHeight(28)
        clear_btn.clicked.connect(self._clear)
        settings_row.addWidget(clear_btn)

        outer.addLayout(settings_row)

        # --- Bandpass filter row ---
        outer.addWidget(self._make_filter_row())

        return box

    def _make_filter_row(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._bp_enable = QCheckBox("Bandpass filter")
        self._bp_enable.setChecked(False)
        row.addWidget(self._bp_enable)

        row.addWidget(QLabel("f low (Hz):"))
        self._bp_low = QLineEdit("10")
        self._bp_low.setMaximumWidth(70)
        row.addWidget(self._bp_low)

        row.addWidget(QLabel("f high (Hz):"))
        self._bp_high = QLineEdit("1000")
        self._bp_high.setMaximumWidth(70)
        row.addWidget(self._bp_high)

        row.addWidget(QLabel("Order:"))
        self._bp_order = QSpinBox()
        self._bp_order.setRange(1, 10)
        self._bp_order.setValue(4)
        self._bp_order.setMaximumWidth(55)
        row.addWidget(self._bp_order)

        row.addStretch()
        return w

    # --- Dual matplotlib panels ---

    def _make_plot_area(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)

        # Time domain
        self._time_fig = Figure(tight_layout=True)
        self._time_ax  = self._time_fig.add_subplot(111)
        self._time_canvas  = FigureCanvasQTAgg(self._time_fig)
        self._time_toolbar = NavigationToolbar2QT(self._time_canvas, self)
        time_w = _plot_panel("Time Domain", self._time_toolbar, self._time_canvas)
        splitter.addWidget(time_w)

        # Frequency domain (ASD/PSD)
        self._psd_fig = Figure(tight_layout=True)
        self._psd_ax  = self._psd_fig.add_subplot(111)
        self._psd_canvas  = FigureCanvasQTAgg(self._psd_fig)
        self._psd_toolbar = NavigationToolbar2QT(self._psd_canvas, self)
        self._psd_panel_w = _plot_panel("Frequency", self._psd_toolbar, self._psd_canvas)
        splitter.addWidget(self._psd_panel_w)

        splitter.setSizes([500, 500])
        return splitter

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_nperseg_hint(self):
        self._nperseg_hint.setText(f"({2 ** self._nperseg_spin.value():,} pts)")

    def _set_all_channels(self, state: bool):
        for cb in self._ch_boxes.values():
            cb.setChecked(state)

    def _apply_live_style(self, active: bool):
        if active:
            self._live_btn.setText("Live Plot: ON")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #1e7e34; color: white; "
                "font-weight: bold; border-radius: 4px; }"
                "QPushButton:hover { background-color: #28a745; }"
            )
        else:
            self._live_btn.setText("Live Plot: OFF")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #555; color: white; "
                "font-weight: bold; border-radius: 4px; }"
                "QPushButton:hover { background-color: #777; }"
            )

    def _on_live_toggled(self, checked: bool):
        self._apply_live_style(checked)
        if checked and self._filepath:
            self._plot()

    def _apply_bandpass(self, data: np.ndarray, sr: float) -> np.ndarray:
        """Return bandpass-filtered data, or original data if filter params are invalid."""
        try:
            f_low  = float(self._bp_low.text())
            f_high = float(self._bp_high.text())
            order  = self._bp_order.value()
            nyq = sr / 2.0
            if not (0 < f_low < f_high < nyq):
                return data
            sos = butter(order, [f_low / nyq, f_high / nyq], btype="band", output="sos")
            return sosfilt(sos, data)
        except Exception:
            return data

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _browse_file(self):
        start = str(Path(self._file_edit.text()).parent) if self._file_edit.text() else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open HDF5 file", start,
            "HDF5 files (*.h5 *.hdf5);;All files (*)",
        )
        if path:
            self.load_file(path)

    def load_file(self, filepath: str):
        """
        Point the widget at a new file.
        Called externally from the Acquire tab after each file is written.
        If Live mode is on, re-plots automatically.
        """
        self._filepath = filepath
        self._file_edit.setText(filepath)

        try:
            info = get_channel_info(filepath)
            for ch, cb in self._ch_boxes.items():
                recorded = info.get(ch, False)
                cb.setStyleSheet(
                    "font-size: 10px; font-weight: bold;" if recorded
                    else "font-size: 10px; color: #aaa;"
                )
        except Exception:
            pass

        if self._live_btn.isChecked():
            self._plot()

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def _plot(self):
        if not self._filepath:
            return

        selected = [ch for ch, cb in self._ch_boxes.items() if cb.isChecked()]
        if not selected:
            return

        self._time_ax.clear()
        self._psd_ax.clear()

        nperseg_bits = self._nperseg_spin.value()
        logx  = self._logx_cb.isChecked()
        logy  = self._logy_cb.isChecked()
        use_asd     = self._spectrum_combo.currentIndex() == 0
        use_filter  = self._bp_enable.isChecked()
        fname = Path(self._filepath).name
        sr_label = None
        last_n = 0

        for ch in selected:
            try:
                data, sr = load_channel(self._filepath, ch)
            except Exception:
                continue

            if len(data) == 0:
                continue

            sr_label = sr
            last_n   = len(data)

            # Apply bandpass filter if enabled
            if use_filter:
                data = self._apply_bandpass(data, sr)

            # Time domain
            t = np.arange(len(data)) / sr
            self._time_ax.plot(t, data, lw=0.6, label=ch, rasterized=True)

            # Frequency domain
            nperseg = min(2 ** nperseg_bits, len(data))
            f, Pxx = welch(data, fs=sr, nperseg=nperseg)

            # ASD = sqrt(PSD)
            S = np.sqrt(Pxx) if use_asd else Pxx

            f_plot = f[1:] if logx else f
            S_plot = S[1:] if logx else S

            if logx and logy:
                self._psd_ax.loglog(f_plot, S_plot, lw=0.8, label=ch)
            elif logx:
                self._psd_ax.semilogx(f_plot, S_plot, lw=0.8, label=ch)
            elif logy:
                self._psd_ax.semilogy(f_plot, S_plot, lw=0.8, label=ch)
            else:
                self._psd_ax.plot(f_plot, S_plot, lw=0.8, label=ch)

        # Decorate time plot
        filter_note = ""
        if use_filter:
            try:
                fl = float(self._bp_low.text())
                fh = float(self._bp_high.text())
                filter_note = f"  [BP {fl:g}–{fh:g} Hz, order {self._bp_order.value()}]"
            except Exception:
                pass

        self._time_ax.set_xlabel("Time (s)")
        self._time_ax.set_ylabel("Voltage (V)")
        title = fname
        if sr_label:
            title += f"   {sr_label:g} Hz   {fmt_duration(last_n / sr_label)}"
        if filter_note:
            title += filter_note
        self._time_ax.set_title(title, fontsize=9)
        self._time_ax.grid(True, alpha=0.3)
        if len(selected) > 1:
            self._time_ax.legend(fontsize=8, loc="best")

        # Decorate frequency plot
        nperseg_actual = min(2 ** nperseg_bits, last_n) if sr_label else 2 ** nperseg_bits
        df = (sr_label / nperseg_actual) if sr_label else 0
        spectrum_label = "ASD" if use_asd else "PSD"
        y_unit = "V/√Hz" if use_asd else "V²/Hz"
        self._psd_ax.set_xlabel("Frequency (Hz)")
        self._psd_ax.set_ylabel(f"{spectrum_label}  ({y_unit})")
        self._psd_ax.set_title(
            f"{spectrum_label} (Welch, 2^{nperseg_bits} = {nperseg_actual:,} pts,"
            f"  Δf = {df:.3g} Hz){filter_note}",
            fontsize=9,
        )
        self._psd_ax.grid(True, alpha=0.3, which="both")
        if len(selected) > 1:
            self._psd_ax.legend(fontsize=8, loc="best")

        self._time_canvas.draw()
        self._psd_canvas.draw()

    def _clear(self):
        for ax, canvas in (
            (self._time_ax, self._time_canvas),
            (self._psd_ax,  self._psd_canvas),
        ):
            ax.clear()
            canvas.draw()


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _plot_panel(title: str, toolbar, canvas) -> QWidget:
    """Wrap a toolbar + canvas in a titled container widget."""
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setSpacing(0)
    layout.setContentsMargins(0, 0, 0, 0)

    lbl = QLabel(f" {title}")
    lbl.setStyleSheet(
        "font-weight: bold; font-size: 11px; "
        "background: #e8e8e8; border-bottom: 1px solid #ccc; padding: 3px;"
    )
    layout.addWidget(lbl)
    layout.addWidget(toolbar)
    layout.addWidget(canvas, stretch=1)
    return w


def _vsep() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.VLine)
    sep.setFrameShadow(QFrame.Sunken)
    return sep


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main():
    import sys
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = QMainWindow()
    win.setWindowTitle("usphere-DAQ Plot Viewer")
    win.resize(1200, 700)
    widget = PlotWidget()
    win.setCentralWidget(widget)
    if len(sys.argv) > 1:
        widget.load_file(sys.argv[1])
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
