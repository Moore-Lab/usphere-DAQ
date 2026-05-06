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

import re
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
        self._overlay_rows: dict[QWidget, QLineEdit] = {}
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(self._make_file_row())
        layout.addWidget(self._make_overlay_panel())
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

        self._prev_btn = QPushButton("◄")
        self._prev_btn.setFixedWidth(34)
        self._prev_btn.setToolTip("Previous H5 file in directory")
        self._prev_btn.clicked.connect(lambda: self._nav_file(-1))
        row.addWidget(self._prev_btn)

        self._next_btn = QPushButton("►")
        self._next_btn.setFixedWidth(34)
        self._next_btn.setToolTip("Next H5 file in directory")
        self._next_btn.clicked.connect(lambda: self._nav_file(+1))
        row.addWidget(self._next_btn)

        self._live_btn = QPushButton("Live Plot: OFF")
        self._live_btn.setCheckable(True)
        self._live_btn.setMinimumWidth(120)
        self._live_btn.toggled.connect(self._on_live_toggled)
        self._apply_live_style(False)
        row.addWidget(self._live_btn)

        return box

    # --- Overlay files ---

    def _make_overlay_panel(self) -> QGroupBox:
        self._overlay_box = QGroupBox("Overlay Files")
        self._overlay_box.setCheckable(True)
        self._overlay_box.setChecked(False)
        vbox = QVBoxLayout(self._overlay_box)
        vbox.setSpacing(4)
        vbox.setContentsMargins(6, 4, 6, 4)

        add_btn = QPushButton("+ Add file…")
        add_btn.setMaximumWidth(110)
        add_btn.clicked.connect(self._add_overlay_file)
        vbox.addWidget(add_btn)

        self._overlay_list_widget = QWidget()
        self._overlay_list_layout = QVBoxLayout(self._overlay_list_widget)
        self._overlay_list_layout.setSpacing(3)
        self._overlay_list_layout.setContentsMargins(0, 0, 0, 0)

        self._overlay_scroll = QScrollArea()
        self._overlay_scroll.setWidgetResizable(True)
        self._overlay_scroll.setFrameShape(QScrollArea.NoFrame)
        self._overlay_scroll.setMaximumHeight(110)
        self._overlay_scroll.setWidget(self._overlay_list_widget)
        vbox.addWidget(self._overlay_scroll)

        return self._overlay_box

    def _add_overlay_file(self, filepath: str = ""):
        if not filepath:
            start = str(Path(self._file_edit.text()).parent) if self._file_edit.text() else ""
            filepath, _ = QFileDialog.getOpenFileName(
                self, "Open HDF5 file for overlay", start,
                "HDF5 files (*.h5 *.hdf5);;All files (*)",
            )
        if not filepath:
            return

        row_w = QWidget()
        row_h = QHBoxLayout(row_w)
        row_h.setContentsMargins(0, 0, 0, 0)
        row_h.setSpacing(4)

        edit = QLineEdit(filepath)
        edit.setReadOnly(True)
        row_h.addWidget(edit, stretch=1)

        prev_btn = QPushButton("◄")
        prev_btn.setFixedWidth(34)
        prev_btn.setToolTip("Previous H5 file in directory")
        prev_btn.clicked.connect(lambda: self._nav_overlay(row_w, -1))
        row_h.addWidget(prev_btn)

        next_btn = QPushButton("►")
        next_btn.setFixedWidth(34)
        next_btn.setToolTip("Next H5 file in directory")
        next_btn.clicked.connect(lambda: self._nav_overlay(row_w, +1))
        row_h.addWidget(next_btn)

        rm_btn = QPushButton("✕")
        rm_btn.setFixedWidth(34)
        rm_btn.setToolTip("Remove this overlay")
        rm_btn.clicked.connect(lambda: self._remove_overlay_row(row_w))
        row_h.addWidget(rm_btn)

        self._overlay_rows[row_w] = edit
        self._overlay_list_layout.addWidget(row_w)

    def _remove_overlay_row(self, row_w: QWidget):
        self._overlay_rows.pop(row_w, None)
        self._overlay_list_layout.removeWidget(row_w)
        row_w.deleteLater()

    def _nav_overlay(self, row_w: QWidget, direction: int):
        edit = self._overlay_rows.get(row_w)
        if edit is None:
            return
        current_path = edit.text()
        if not current_path:
            return
        siblings = self._sibling_files_for(current_path)
        if not siblings:
            return
        current = Path(current_path).resolve()
        try:
            idx = siblings.index(current)
        except ValueError:
            idx = 0
        new_idx = idx + direction
        if 0 <= new_idx < len(siblings):
            edit.setText(str(siblings[new_idx]))

    def _get_all_files(self) -> list[str]:
        """Return primary file + all overlay files (if overlay box is checked)."""
        files = []
        if self._filepath:
            files.append(self._filepath)
        if self._overlay_box.isChecked():
            for edit in self._overlay_rows.values():
                p = edit.text().strip()
                if p:
                    files.append(p)
        return files

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
        """
        Return bandpass-filtered data.
        Raises ValueError with a descriptive message if parameters are invalid.
        """
        try:
            f_low  = float(self._bp_low.text())
            f_high = float(self._bp_high.text())
        except ValueError:
            raise ValueError("Bandpass filter: f low and f high must be numbers.")

        order = self._bp_order.value()
        nyq   = sr / 2.0

        if f_low <= 0:
            raise ValueError(f"Bandpass filter: f low must be > 0 (got {f_low}).")
        if f_high >= nyq:
            raise ValueError(
                f"Bandpass filter: f high ({f_high} Hz) must be below Nyquist ({nyq} Hz)."
            )
        if f_low >= f_high:
            raise ValueError(
                f"Bandpass filter: f low ({f_low}) must be less than f high ({f_high})."
            )

        sos = butter(order, [f_low / nyq, f_high / nyq], btype="band", output="sos")
        return sosfilt(sos, data)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _sibling_files_for(self, filepath: str | Path) -> list[Path]:
        """Natural-sorted .h5 siblings of filepath (including filepath itself)."""
        parent = Path(filepath).resolve().parent
        files = sorted(
            parent.glob("*.h5"),
            key=lambda f: [int(c) if c.isdigit() else c.lower()
                           for c in re.split(r"(\d+)", f.name)],
        )
        return [f.resolve() for f in files]

    def _sibling_files(self) -> list[Path]:
        if not self._filepath:
            return []
        return self._sibling_files_for(self._filepath)

    def _nav_file(self, direction: int) -> None:
        """Step to the previous (direction=-1) or next (+1) H5 file and plot it."""
        siblings = self._sibling_files()
        if not siblings:
            return
        current = Path(self._filepath).resolve() if self._filepath else None
        try:
            idx = siblings.index(current)
        except ValueError:
            idx = 0
        new_idx = idx + direction
        if 0 <= new_idx < len(siblings):
            self.load_file(str(siblings[new_idx]))
            if not self._live_btn.isChecked():
                self._plot()

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

        all_files = self._get_all_files()
        multi_file = len(all_files) > 1

        self._time_ax.clear()
        self._psd_ax.clear()

        nperseg_bits = self._nperseg_spin.value()
        logx       = self._logx_cb.isChecked()
        logy       = self._logy_cb.isChecked()
        use_asd    = self._spectrum_combo.currentIndex() == 0
        use_filter = self._bp_enable.isChecked()
        sr_label = None
        last_n   = 0

        for filepath in all_files:
            stem = Path(filepath).stem
            for ch in selected:
                try:
                    data, sr = load_channel(filepath, ch)
                except Exception:
                    continue

                if len(data) == 0:
                    continue

                sr_label = sr
                last_n   = len(data)
                label    = f"{stem}:{ch}" if multi_file else ch

                if use_filter:
                    try:
                        data = self._apply_bandpass(data, sr)
                    except ValueError as exc:
                        self._time_ax.set_title(
                            f"Filter error: {exc}", fontsize=9, color="red"
                        )
                        self._time_canvas.draw()
                        return

                t = np.arange(len(data)) / sr
                self._time_ax.plot(t, data, lw=0.6, label=label, rasterized=True)

                nperseg = min(2 ** nperseg_bits, len(data))
                f, Pxx  = welch(data, fs=sr, nperseg=nperseg)
                S       = np.sqrt(Pxx) if use_asd else Pxx

                f_plot = f[1:] if logx else f
                S_plot = S[1:] if logx else S

                if logx and logy:
                    self._psd_ax.loglog(f_plot, S_plot, lw=0.8, label=label)
                elif logx:
                    self._psd_ax.semilogx(f_plot, S_plot, lw=0.8, label=label)
                elif logy:
                    self._psd_ax.semilogy(f_plot, S_plot, lw=0.8, label=label)
                else:
                    self._psd_ax.plot(f_plot, S_plot, lw=0.8, label=label)

        filter_note = ""
        if use_filter:
            try:
                fl = float(self._bp_low.text())
                fh = float(self._bp_high.text())
                filter_note = f"  [BP {fl:g}–{fh:g} Hz, order {self._bp_order.value()}]"
            except Exception:
                pass

        show_legend = multi_file or len(selected) > 1

        fname = Path(self._filepath).name
        title = fname if not multi_file else f"{fname}  + {len(all_files) - 1} overlay(s)"
        if sr_label:
            title += f"   {sr_label:g} Hz   {fmt_duration(last_n / sr_label)}"
        if filter_note:
            title += filter_note
        self._time_ax.set_xlabel("Time (s)")
        self._time_ax.set_ylabel("Voltage (V)")
        self._time_ax.set_title(title, fontsize=9)
        self._time_ax.grid(True, alpha=0.3)
        if show_legend:
            self._time_ax.legend(fontsize=8, loc="best")

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
        if show_legend:
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
