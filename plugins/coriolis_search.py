"""
Coriolis Search Plugin
======================
Phase-folded template averaging to extract Coriolis forces from
continuous oscillation data.

Two modes
---------
**Continuous** – The user runs the motor at one set of parameters and
enables *Live*.  Each incoming HDF5 file is segmented into complete
oscillation cycles and phase-folded onto a common grid.  Over many
cycles noise averages down and a Coriolis signal emerges.

**Scan** – The plugin controls the ESP32 motor and DAQ to
systematically sweep across waveform types, amplitudes, and
frequencies.  At each parameter combination it records a configurable
number of files (exposure), phase-folds the cycles, and accumulates a
template.  A summary heatmap of SNR vs. parameters is updated live.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import butter, filtfilt
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qt5agg import (
    FigureCanvasQTAgg,
    NavigationToolbar2QT,
)
from matplotlib.figure import Figure

import daq_h5
from plugins.base import AnalysisPlugin

# ---------------------------------------------------------------------------
# ESP32 controller import helper (shared with amplitude_sweep)
# ---------------------------------------------------------------------------
_ESP_DIR = Path(__file__).resolve().parent.parent / "ESP32-stepper-controller"
_CTRL_PATH = _ESP_DIR / "controller.py"


def _get_stepper_controller_class():
    import importlib.util
    if str(_ESP_DIR) not in sys.path:
        sys.path.insert(0, str(_ESP_DIR))
    spec = importlib.util.spec_from_file_location(
        "_ext_stepper_controller_cs", str(_CTRL_PATH),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.StepperController, mod.find_esp32_port


_SETTINGS_FILE = _ESP_DIR / "settings.json"


def _load_esp_position() -> float:
    """Read last_position_mm from the ESP32 settings.json."""
    try:
        with open(_SETTINGS_FILE) as f:
            return json.load(f).get("last_position_mm", 0.0)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return 0.0


def _save_esp_position(pos_mm: float) -> None:
    """Update last_position_mm in the ESP32 settings.json."""
    try:
        data = {}
        if _SETTINGS_FILE.exists():
            with open(_SETTINGS_FILE) as f:
                data = json.load(f)
        data["last_position_mm"] = pos_mm
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except (IOError, json.JSONDecodeError):
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
N_PHASE_BINS = 512

# Physical constants for Coriolis acceleration projection
OMEGA_EARTH = 7.292e-5     # rad/s  — Earth's angular rotation rate
LATITUDE_DEG = 41.3        # degrees — Yale University, New Haven CT
KAPPA = 2 * OMEGA_EARTH * math.sin(math.radians(LATITUDE_DEG))  # ≈ 9.61e-5 rad/s
G_ACCEL = 9.80665          # m/s²  — standard gravitational acceleration

# Cardinal-direction unit vectors (East-North-Up)
_DIR_VECTORS = {
    "+East":  ( 1,  0),
    "–East":  (-1,  0),
    "+North": ( 0,  1),
    "–North": ( 0, -1),
}

_WAVE_FIRMWARE = {"Sine": "SINE", "Triangle": "TRAP", "Rounded Triangle": "SCURVE"}


def _tri_speed_from_freq(wave: str, amp: float, freq: float, duty: float) -> float:
    """Constant-velocity segment speed (mm/s) for a triangle-family waveform."""
    if wave == "Rounded Triangle":
        return 12.0 * amp * freq / (2.0 + duty)
    return 8.0 * amp * freq / (1.0 + duty)


def _tri_freq_from_speed(wave: str, amp: float, speed: float, duty: float) -> float:
    """Oscillation frequency (Hz) for a triangle-family waveform given speed."""
    if amp <= 0:
        return 0.0
    if wave == "Rounded Triangle":
        return speed * (2.0 + duty) / (12.0 * amp)
    return speed * (1.0 + duty) / (8.0 * amp)


def _apply_waveform_params(ctrl, wave_name: str, amp: float, freq: float, duty: float):
    """Send waveform parameters to ESP32 with proper velocity/acceleration.

    Mirrors the logic from the ESP32 GUI's _on_apply_waveform so that
    max velocity and acceleration are set correctly for each waveform.
    """
    ctrl.set_amplitude(amp)

    if wave_name == "Sine":
        ctrl.set_frequency(freq)
        ctrl.set_waveform("SINE")
        v_max = 2.0 * math.pi * freq * amp
        a_max = (2.0 * math.pi * freq) ** 2 * amp
        ctrl.set_velocity(v_max)
        ctrl.set_acceleration(a_max)

    elif wave_name == "Triangle":
        # T0 = 8A / (s*(1+F)) and freq = 1/T0  =>  s = 8*A*f / (1+F)
        vel = 8.0 * amp * freq / (1.0 + duty)
        T0 = 1.0 / freq if freq > 0 else 1.0
        dc = (1.0 - duty) * T0 / 4.0
        a_cap = vel / dc if dc > 0 else 0.0
        ctrl.set_velocity(vel)
        ctrl.set_acceleration(a_cap)
        ctrl.set_duty_cycle(duty)
        ctrl.set_waveform("TRAP")

    elif wave_name == "Rounded Triangle":
        # T0 = 12A / (s*(2+F)) and freq = 1/T0  =>  s = 12*A*f / (2+F)
        vel = 12.0 * amp * freq / (2.0 + duty)
        T0 = 1.0 / freq if freq > 0 else 1.0
        Tj = (1.0 - duty) * T0 / 4.0
        jerk = 2.0 * vel / (Tj * Tj) if Tj > 0 else 0.0
        a_peak = jerk * Tj if Tj > 0 else 0.0
        ctrl.set_velocity(vel)
        ctrl.set_acceleration(a_peak)
        ctrl.set_jerk(jerk)
        ctrl.set_duty_cycle(duty)
        ctrl.set_waveform("SCURVE")


# ---------------------------------------------------------------------------
# Helper: segment encoder into complete cycles
# ---------------------------------------------------------------------------

def _find_zero_crossings(x: NDArray, direction: str = "rising") -> NDArray:
    if direction == "rising":
        return np.where((x[:-1] <= 0) & (x[1:] > 0))[0]
    elif direction == "falling":
        return np.where((x[:-1] >= 0) & (x[1:] < 0))[0]
    else:
        return np.where(np.diff(np.sign(x)) != 0)[0]


def segment_cycles(
    encoder: NDArray,
    fs: float,
    min_period_s: float = 0.05,
    max_period_s: float = 20.0,
) -> list[tuple[int, int]]:
    enc_c = encoder - np.mean(encoder)
    crossings = _find_zero_crossings(enc_c, "rising")
    if len(crossings) < 2:
        return []
    min_samples = int(min_period_s * fs)
    max_samples = int(max_period_s * fs)
    cycles: list[tuple[int, int]] = []
    for i in range(len(crossings) - 1):
        s, e = int(crossings[i]), int(crossings[i + 1])
        n = e - s
        if min_samples <= n <= max_samples:
            cycles.append((s, e))
    return cycles


def phase_fold(
    data: NDArray, start: int, end: int, n_bins: int = N_PHASE_BINS,
) -> NDArray:
    segment = data[start:end].astype(np.float64)
    n = len(segment)
    phase_orig = np.linspace(0.0, 1.0, n, endpoint=False)
    phase_grid = np.linspace(0.0, 1.0, n_bins, endpoint=False)
    return np.interp(phase_grid, phase_orig, segment)


# ---------------------------------------------------------------------------
# Template accumulator
# ---------------------------------------------------------------------------

class TemplateAccumulator:
    """Welford-style online mean & variance for phase-folded templates."""

    def __init__(self, n_bins: int = N_PHASE_BINS):
        self.n_bins = n_bins
        self.count = 0
        self._mean = np.zeros(n_bins, dtype=np.float64)
        self._m2 = np.zeros(n_bins, dtype=np.float64)

    def add_cycle(self, folded: NDArray) -> None:
        self.count += 1
        delta = folded - self._mean
        self._mean += delta / self.count
        delta2 = folded - self._mean
        self._m2 += delta * delta2

    @property
    def mean(self) -> NDArray:
        return self._mean.copy()

    @property
    def std(self) -> NDArray:
        if self.count < 2:
            return np.zeros(self.n_bins)
        return np.sqrt(self._m2 / (self.count - 1))

    @property
    def sem(self) -> NDArray:
        if self.count < 2:
            return np.zeros(self.n_bins)
        return self.std / np.sqrt(self.count)

    def reset(self) -> None:
        self.count = 0
        self._mean[:] = 0.0
        self._m2[:] = 0.0


# ---------------------------------------------------------------------------
# Shared helpers: Coriolis region & SNR
# ---------------------------------------------------------------------------

def get_coriolis_phase_ranges(
    half_width_deg: float = 60.0,
) -> list[tuple[float, float]]:
    """Phase ranges (degrees) where table velocity is large.

    Phase is defined by rising zero crossings of the encoder position:
        0° / 360° = position zero crossing (velocity = +v_max)
        90°       = position peak (+A, velocity = 0, turning point)
        180°      = position zero crossing (velocity = -v_max)
        270°      = position trough (-A, velocity = 0, turning point)

    The Coriolis acceleration is proportional to velocity, so the
    Coriolis-active regions are centred on 0° and 180° (velocity peaks)
    with a user-specified half-width.
    """
    hw = max(1.0, min(half_width_deg, 89.0))  # clamp to (1, 89)
    return [
        (0.0, hw),
        (180.0 - hw, 180.0 + hw),
        (360.0 - hw, 360.0),
    ]


def _default_half_width(waveform: str, duty: float = 0.9) -> float:
    """Suggested half-width based on the waveform's constant-velocity fraction."""
    if waveform == "Sine":
        return 60.0   # |cos θ| > 0.5
    corner_half = (1.0 - duty) / 2.0 * 180.0
    return 90.0 - corner_half  # excludes turning-point corners


def mark_coriolis_region(ax, phase_deg: NDArray, half_width_deg: float = 60.0):
    regions = get_coriolis_phase_ranges(half_width_deg)
    for lo, hi in regions:
        ax.axvspan(lo, hi, alpha=0.10, color="green", zorder=0)
    if regions and len(regions) > 1:
        ax.annotate(
            "Search region", xy=(180.0, 0),
            xycoords=("data", "axes fraction"),
            xytext=(0, -12), textcoords="offset points",
            fontsize=7, color="green", ha="center", va="top",
        )


def compute_snr(
    accel_mean: NDArray, accel_sem: NDArray,
    phase: NDArray, half_width_deg: float = 60.0,
) -> tuple[float, float, float]:
    """Return (|mean_residual|, mean_sem, snr) in the search region."""
    phase_deg = phase * 360.0
    regions = get_coriolis_phase_ranges(half_width_deg)
    mask = np.zeros(len(phase_deg), dtype=bool)
    for lo, hi in regions:
        mask |= (phase_deg >= lo) & (phase_deg <= hi)
    if not np.any(mask):
        return 0.0, 0.0, 0.0
    residual = accel_mean - np.mean(accel_mean)
    mean_abs = float(np.mean(np.abs(residual[mask])))
    sem_avg = float(np.mean(accel_sem[mask]))
    snr = mean_abs / sem_avg if sem_avg > 0 else 0.0
    return mean_abs, sem_avg, snr


def matched_filter_snr(
    accel_mean: NDArray, accel_sem: NDArray,
    template: NDArray,
) -> tuple[float, float, float]:
    """Optimal matched-filter amplitude estimate and SNR.

    The template *t* is the predicted Coriolis shape (arbitrary units).
    The data *d* is the mean-subtracted accelerometer template.

        Â  = Σ(t·d / σ²) / Σ(t² / σ²)
        σ_Â² = 1 / Σ(t² / σ²)
        SNR = |Â| / σ_Â

    This is the Wiener/optimal linear estimator: it weights each phase
    bin by the local SEM, so noisy bins contribute less.  Harmonics
    orthogonal to *t* are rejected regardless of amplitude.

    Returns (A_hat, sigma_A, snr)  — all in the same units as *accel_mean*.
    """
    d = accel_mean - np.mean(accel_mean)
    t = template.copy()
    sigma = np.maximum(accel_sem, 1e-30)  # avoid /0
    w = t / (sigma * sigma)               # weight vector
    denom = float(np.sum(t * w))           # Σ t²/σ²
    if denom <= 0:
        return 0.0, 0.0, 0.0
    A_hat = float(np.sum(d * w)) / denom
    sigma_A = 1.0 / math.sqrt(denom)
    snr = abs(A_hat) / sigma_A if sigma_A > 0 else 0.0
    return A_hat, sigma_A, snr


def coriolis_sign(drive_dir: str, accel_dir: str) -> float:
    """Return the sign (+1 or -1) for  a_meas = sign * κ * |ẋ|.

    The horizontal Coriolis acceleration for velocity v_drive is:
        a_Cor = -2Ω×v  (projected horizontally, Northern hemisphere).
    For a drive along (dx, dy) the Coriolis deflection is along (-dy, dx)
    scaled by 2Ω sinλ.  The measured component is the projection of that
    onto the accelerometer axis.
    """
    dx, dy = _DIR_VECTORS.get(drive_dir, (1, 0))
    ax, ay = _DIR_VECTORS.get(accel_dir, (0, 1))
    # Coriolis deflection direction when drive velocity > 0: (-dy, dx)
    proj = (-dy) * ax + dx * ay
    if proj == 0:
        return -1.0  # fallback
    return float(proj)  # +1 or -1


def coriolis_template(
    enc_mean_mm: NDArray, phase: NDArray, cycle_period_s: float,
    sign: float = -1.0,
) -> tuple[NDArray, NDArray]:
    """Predicted Coriolis acceleration from the phase-folded encoder.

    The table oscillates along the drive axis; the accelerometer
    measures the perpendicular axis.  *sign* encodes the coordinate
    convention (see `coriolis_sign`).

    Parameters
    ----------
    enc_mean_mm    : Phase-folded encoder position [mm].
    phase          : Phase grid (0 to 1, N_PHASE_BINS points).
    cycle_period_s : Mean oscillation period [s].
    sign           : +1 or -1, from `coriolis_sign()`.

    Returns
    -------
    a_cor_g : Predicted Coriolis acceleration along accel axis [g].
    v_mms   : Table velocity [mm/s].
    """
    dphi = phase[1] - phase[0] if len(phase) > 1 else 1.0
    dt = dphi * cycle_period_s                        # seconds per phase bin
    v_mms = np.gradient(enc_mean_mm, dt)              # mm/s
    v_ms  = v_mms * 1e-3                              # m/s
    a_cor_ms2 = sign * KAPPA * v_ms                    # m/s²
    a_cor_g   = a_cor_ms2 / G_ACCEL                   # g
    return a_cor_g, v_mms


# ===================================================================
#  CONTINUOUS MODE WIDGET (original behaviour)
# ===================================================================

class ContinuousWidget(QWidget):
    """Run the motor at one setting, accumulate cycles indefinitely."""

    def __init__(self, plugin: "Plugin", parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._ctrl = None
        self._accel_acc = TemplateAccumulator()
        self._encoder_acc = TemplateAccumulator()
        self._total_cycles = 0
        self._mean_period_s = 0.0
        self._period_count = 0
        self._live = False
        self._init_ui()

    def _init_ui(self):
        top = QVBoxLayout(self)

        # --- ESP32 Connection ---
        conn_grp = QGroupBox("ESP32 Connection")
        conn_lay = QVBoxLayout(conn_grp)
        cr1 = QHBoxLayout()
        cr1.addWidget(QLabel("Port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("Auto-detect")
        self._port_edit.setMaximumWidth(120)
        cr1.addWidget(self._port_edit)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._toggle_connect)
        cr1.addWidget(self._connect_btn)
        self._conn_status = QLabel("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        cr1.addWidget(self._conn_status)
        cr1.addStretch()
        conn_lay.addLayout(cr1)
        cr2 = QHBoxLayout()
        self._pos_label = QLabel("Position: — mm")
        self._pos_label.setStyleSheet("font-weight: bold;")
        cr2.addWidget(self._pos_label)
        self._zero_btn = QPushButton("Zero")
        self._zero_btn.setToolTip("Set current position as zero reference")
        self._zero_btn.clicked.connect(self._on_zero)
        cr2.addWidget(self._zero_btn)
        self._go_home_btn = QPushButton("Go Home")
        self._go_home_btn.setToolTip("Move motor to position 0")
        self._go_home_btn.clicked.connect(self._on_go_home)
        cr2.addWidget(self._go_home_btn)
        cr2.addWidget(QLabel("Move to (mm):"))
        self._move_to_spin = QDoubleSpinBox()
        self._move_to_spin.setRange(-100.0, 100.0); self._move_to_spin.setValue(0.0)
        self._move_to_spin.setDecimals(4); self._move_to_spin.setSingleStep(0.1)
        self._move_to_spin.setMaximumWidth(90)
        cr2.addWidget(self._move_to_spin)
        self._move_to_btn = QPushButton("Move")
        self._move_to_btn.clicked.connect(self._on_move_to)
        cr2.addWidget(self._move_to_btn)
        cr2.addStretch()
        conn_lay.addLayout(cr2)
        top.addWidget(conn_grp)

        # --- Motor Control ---
        motor_grp = QGroupBox("Motor Control")
        motor_lay = QVBoxLayout(motor_grp)
        m1 = QHBoxLayout()
        m1.addWidget(QLabel("Waveform:"))
        self._wave_combo = QComboBox()
        self._wave_combo.addItems(["Sine", "Triangle", "Rounded Triangle"])
        self._wave_combo.currentTextChanged.connect(self._on_wave_or_param_changed)
        m1.addWidget(self._wave_combo)
        m1.addWidget(QLabel("Amplitude (mm):"))
        self._amp_spin = QDoubleSpinBox()
        self._amp_spin.setRange(0.001, 10.0); self._amp_spin.setValue(0.5)
        self._amp_spin.setDecimals(3); self._amp_spin.setSingleStep(0.1)
        self._amp_spin.setMaximumWidth(80)
        self._amp_spin.valueChanged.connect(self._on_wave_or_param_changed)
        m1.addWidget(self._amp_spin)

        # Drive-mode radio buttons (Frequency vs Speed)
        self._freq_radio = QRadioButton("Frequency (Hz):")
        self._freq_radio.setChecked(True)
        self._speed_radio = QRadioButton("Speed (mm/s):")
        self._drive_mode_group = QButtonGroup(self)
        self._drive_mode_group.addButton(self._freq_radio)
        self._drive_mode_group.addButton(self._speed_radio)
        self._drive_mode_group.buttonToggled.connect(self._on_drive_mode_changed)
        m1.addWidget(self._freq_radio)
        self._freq_spin = QDoubleSpinBox()
        self._freq_spin.setRange(0.01, 100.0); self._freq_spin.setValue(1.0)
        self._freq_spin.setDecimals(3); self._freq_spin.setSingleStep(0.1)
        self._freq_spin.setMaximumWidth(80)
        self._freq_spin.valueChanged.connect(self._on_wave_or_param_changed)
        m1.addWidget(self._freq_spin)
        m1.addWidget(self._speed_radio)
        self._speed_spin = QDoubleSpinBox()
        self._speed_spin.setRange(0.001, 500.0); self._speed_spin.setValue(1.0)
        self._speed_spin.setDecimals(3); self._speed_spin.setSingleStep(0.5)
        self._speed_spin.setMaximumWidth(80)
        self._speed_spin.setEnabled(False)
        self._speed_spin.valueChanged.connect(self._on_wave_or_param_changed)
        m1.addWidget(self._speed_spin)

        m1.addWidget(QLabel("Duty cycle:"))
        self._duty_spin = QDoubleSpinBox()
        self._duty_spin.setRange(0.1, 0.99); self._duty_spin.setValue(0.90)
        self._duty_spin.setDecimals(2); self._duty_spin.setSingleStep(0.01)
        self._duty_spin.setMaximumWidth(70)
        self._duty_spin.valueChanged.connect(self._on_wave_or_param_changed)
        m1.addWidget(self._duty_spin)
        motor_lay.addLayout(m1)

        # Computed readout line
        self._computed_lbl = QLabel("")
        self._computed_lbl.setStyleSheet(
            "color: #2563eb; font-size: 10px; padding-left: 4px;")
        motor_lay.addWidget(self._computed_lbl)

        m2 = QHBoxLayout()
        self._apply_start_btn = QPushButton("Apply && Start Motor")
        self._apply_start_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }")
        self._apply_start_btn.clicked.connect(self._apply_and_start_motor)
        m2.addWidget(self._apply_start_btn)
        self._stop_motor_btn = QPushButton("Stop Motor")
        self._stop_motor_btn.setStyleSheet(
            "QPushButton { background-color: #dc2626; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #ef4444; }")
        self._stop_motor_btn.clicked.connect(self._stop_motor)
        m2.addWidget(self._stop_motor_btn)
        self._motor_status = QLabel("")
        self._motor_status.setStyleSheet("color: gray; font-size: 10px;")
        m2.addWidget(self._motor_status)
        m2.addStretch()
        motor_lay.addLayout(m2)
        top.addWidget(motor_grp)

        # --- Hardware & Calibration ---
        hw_grp = QGroupBox("Hardware && Calibration")
        hw_lay = QVBoxLayout(hw_grp)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Accel channel:"))
        self._accel_ch = QComboBox()
        self._accel_ch.addItems(daq_h5.ALL_CHANNELS)
        self._accel_ch.setCurrentIndex(min(16, len(daq_h5.ALL_CHANNELS) - 1))
        r1.addWidget(self._accel_ch)
        r1.addWidget(QLabel("Encoder channel:"))
        self._encoder_ch = QComboBox()
        self._encoder_ch.addItems(daq_h5.ALL_CHANNELS)
        self._encoder_ch.setCurrentIndex(min(17, len(daq_h5.ALL_CHANNELS) - 1))
        r1.addWidget(self._encoder_ch)
        hw_lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Accel sensitivity (mV/g):"))
        self._sens_edit = QLineEdit("1000.0")
        self._sens_edit.setMaximumWidth(80)
        r2.addWidget(self._sens_edit)
        r2.addWidget(QLabel("Encoder V FS:"))
        self._enc_vfs = QLineEdit("3.3")
        self._enc_vfs.setMaximumWidth(60)
        r2.addWidget(self._enc_vfs)
        r2.addWidget(QLabel("Enc mm low:"))
        self._enc_mm_lo = QLineEdit("-0.6375")
        self._enc_mm_lo.setMaximumWidth(70)
        r2.addWidget(self._enc_mm_lo)
        r2.addWidget(QLabel("Enc mm hi:"))
        self._enc_mm_hi = QLineEdit("0.6375")
        self._enc_mm_hi.setMaximumWidth(70)
        r2.addWidget(self._enc_mm_hi)
        hw_lay.addLayout(r2)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("Encoder LP (Hz):"))
        self._enc_lp = QDoubleSpinBox()
        self._enc_lp.setRange(0.1, 1000.0); self._enc_lp.setValue(50.0)
        self._enc_lp.setDecimals(1); self._enc_lp.setMaximumWidth(70)
        r3.addWidget(self._enc_lp)
        r3.addWidget(QLabel("Min period (s):"))
        self._min_period = QDoubleSpinBox()
        self._min_period.setRange(0.01, 100.0); self._min_period.setValue(0.05)
        self._min_period.setDecimals(2); self._min_period.setMaximumWidth(70)
        r3.addWidget(self._min_period)
        r3.addWidget(QLabel("Max period (s):"))
        self._max_period = QDoubleSpinBox()
        self._max_period.setRange(0.1, 100.0); self._max_period.setValue(20.0)
        self._max_period.setDecimals(1); self._max_period.setMaximumWidth(70)
        r3.addWidget(self._max_period)
        r3.addWidget(QLabel("Phase bins:"))
        self._n_bins_spin = QSpinBox()
        self._n_bins_spin.setRange(64, 4096); self._n_bins_spin.setValue(N_PHASE_BINS)
        self._n_bins_spin.setSingleStep(64); self._n_bins_spin.setMaximumWidth(70)
        r3.addWidget(self._n_bins_spin)
        r3.addWidget(QLabel("Search ±(°):"))
        self._search_hw_spin = QDoubleSpinBox()
        self._search_hw_spin.setRange(1.0, 89.0)
        self._search_hw_spin.setValue(
            _default_half_width(self._wave_combo.currentText(),
                                self._duty_spin.value()))
        self._search_hw_spin.setDecimals(1); self._search_hw_spin.setSingleStep(5.0)
        self._search_hw_spin.setMaximumWidth(60)
        self._search_hw_spin.setToolTip(
            "Half-width (degrees) of the search region centred on\n"
            "0° and 180° (the velocity peaks).")
        r3.addWidget(self._search_hw_spin)

        r4 = QHBoxLayout()
        r4.addWidget(QLabel("Drive axis:"))
        self._drive_dir = QComboBox()
        self._drive_dir.addItems(list(_DIR_VECTORS.keys()))
        self._drive_dir.setCurrentText("+East")
        self._drive_dir.setMaximumWidth(90)
        self._drive_dir.setToolTip(
            "Compass direction of positive encoder displacement.")
        r4.addWidget(self._drive_dir)
        r4.addWidget(QLabel("Accel axis:"))
        self._accel_dir = QComboBox()
        self._accel_dir.addItems(list(_DIR_VECTORS.keys()))
        self._accel_dir.setCurrentText("+North")
        self._accel_dir.setMaximumWidth(90)
        self._accel_dir.setToolTip(
            "Compass direction of positive accelerometer reading.")
        r4.addWidget(self._accel_dir)
        self._sign_lbl = QLabel("")
        self._sign_lbl.setStyleSheet("color: #6d28d9; font-size: 10px;")
        r4.addWidget(self._sign_lbl)
        r4.addStretch()
        self._drive_dir.currentTextChanged.connect(self._update_sign_label)
        self._accel_dir.currentTextChanged.connect(self._update_sign_label)
        hw_lay.addLayout(r4)

        top.addWidget(hw_grp)

        # --- Acquisition & Template Control ---
        acq = QHBoxLayout()
        acq.addWidget(QLabel("N files:"))
        self._n_files_spin = QSpinBox()
        self._n_files_spin.setRange(1, 100000); self._n_files_spin.setValue(100)
        self._n_files_spin.setMaximumWidth(80)
        acq.addWidget(self._n_files_spin)
        self._record_btn = QPushButton("Start Recording")
        self._record_btn.setStyleSheet(
            "QPushButton { background-color: #16a34a; color: white; "
            "font-weight: bold; padding: 4px 12px; border-radius: 4px; }")
        self._record_btn.clicked.connect(self._start_recording)
        acq.addWidget(self._record_btn)
        self._stop_rec_btn = QPushButton("Stop Recording")
        self._stop_rec_btn.clicked.connect(self._stop_recording)
        acq.addWidget(self._stop_rec_btn)

        acq.addWidget(QLabel("  "))
        self._live_btn = QPushButton("Live: OFF")
        self._live_btn.setCheckable(True)
        self._live_btn.toggled.connect(self._toggle_live)
        self._apply_live_style(False)
        acq.addWidget(self._live_btn)
        self._reset_btn = QPushButton("Reset Template")
        self._reset_btn.clicked.connect(self._reset)
        acq.addWidget(self._reset_btn)
        self._cycle_lbl = QLabel("Cycles: 0")
        acq.addWidget(self._cycle_lbl)
        acq.addStretch()
        top.addLayout(acq)

        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(100)
        top.addWidget(self._log_box)

        # Initial computed-readout
        self._on_wave_or_param_changed()
        self._update_sign_label()

    # helpers
    def _log(self, msg: str):
        self._log_box.append(msg)

    def _get_sign(self) -> float:
        return coriolis_sign(
            self._drive_dir.currentText(),
            self._accel_dir.currentText())

    def _update_sign_label(self, _=None):
        s = self._get_sign()
        txt = "+" if s > 0 else "–"
        self._sign_lbl.setText(
            f"a_cor = {txt}κ·v   (sign = {s:+.0f})")

    # ---- ESP32 connection ----
    def _toggle_connect(self):
        if self._ctrl is not None:
            try:
                self._ctrl.stop(); self._ctrl.disable(); self._ctrl.disconnect()
            except Exception:
                pass
            self._ctrl = None
            self._connect_btn.setText("Connect")
            self._conn_status.setText("Disconnected")
            self._conn_status.setStyleSheet("color: gray;")
            self._pos_label.setText("Position: — mm")
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
            self._ctrl.set_line_callback(lambda line: None)
            self._ctrl.connect()
            self._connect_btn.setText("Disconnect")
            self._conn_status.setText(f"Connected ({port})")
            self._conn_status.setStyleSheet("color: green; font-weight: bold;")
            # Restore last known position
            last_pos = _load_esp_position()
            if last_pos != 0.0:
                self._ctrl.set_position(last_pos)
                self._log(f"Restored position: {last_pos:.4f} mm")
            self._pos_label.setText(f"Position: {last_pos:.4f} mm")
            self._log(f"Connected to ESP32 on {port}")
        except Exception as exc:
            self._log(f"Connection error: {exc}")
            self._ctrl = None

    # ---- Position / motion ----
    def _on_zero(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return
        self._ctrl.zero()
        self._pos_label.setText("Position: 0.0000 mm")
        _save_esp_position(0.0)
        self._log("Zeroed position.")

    def _on_go_home(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return
        self._ctrl.go_home()
        self._pos_label.setText("Position: 0.0000 mm (homing…)")
        _save_esp_position(0.0)
        self._log("Going home (moving to 0).")

    def _on_move_to(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return
        target = self._move_to_spin.value()
        # go_home then move — or just use MOVE as relative offset from current.
        # We read the current pos from the label, compute delta.
        cur = _load_esp_position()
        delta = target - cur
        if abs(delta) < 1e-6:
            self._log("Already at target position.")
            return
        self._ctrl.move(delta)
        self._pos_label.setText(f"Position: {target:.4f} mm (moving…)")
        _save_esp_position(target)
        self._log(f"Moving to {target:.4f} mm (delta={delta:+.4f} mm).")

    # ---- Drive mode (frequency vs speed) ----
    def _on_drive_mode_changed(self):
        freq_mode = self._freq_radio.isChecked()
        self._freq_spin.setEnabled(freq_mode)
        self._speed_spin.setEnabled(not freq_mode)
        self._on_wave_or_param_changed()

    def _on_wave_or_param_changed(self, _=None):
        wave = self._wave_combo.currentText()
        amp = self._amp_spin.value()
        duty = self._duty_spin.value()
        freq_mode = self._freq_radio.isChecked()
        is_triangle = wave in ("Triangle", "Rounded Triangle")

        if not is_triangle:
            self._speed_spin.setEnabled(False)
            self._speed_radio.setEnabled(False)
            self._freq_spin.setEnabled(True)
            if wave == "Sine":
                v_peak = 2.0 * math.pi * self._freq_spin.value() * amp
                self._computed_lbl.setText(
                    f"Sine peak velocity = {v_peak:.3f} mm/s")
            else:
                self._computed_lbl.setText("")
            return

        self._speed_radio.setEnabled(True)
        if freq_mode:
            freq = self._freq_spin.value()
            speed = _tri_speed_from_freq(wave, amp, freq, duty)
            self._computed_lbl.setText(
                f"↳ Constant-velocity speed = {speed:.3f} mm/s")
        else:
            speed = self._speed_spin.value()
            freq = _tri_freq_from_speed(wave, amp, speed, duty)
            self._computed_lbl.setText(
                f"↳ Oscillation frequency = {freq:.4f} Hz  "
                f"(period = {1.0/freq:.3f} s)" if freq > 0 else "")

    def _get_effective_freq(self) -> float:
        """Return the oscillation frequency, computing from speed if needed."""
        wave = self._wave_combo.currentText()
        if self._freq_radio.isChecked() or wave not in ("Triangle", "Rounded Triangle"):
            return self._freq_spin.value()
        amp = self._amp_spin.value()
        duty = self._duty_spin.value()
        speed = self._speed_spin.value()
        return _tri_freq_from_speed(wave, amp, speed, duty)

    # ---- Motor control ----
    def _apply_and_start_motor(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return
        wave = self._wave_combo.currentText()
        amp = self._amp_spin.value()
        freq = self._get_effective_freq()
        duty = self._duty_spin.value()
        try:
            self._ctrl.enable()
            _apply_waveform_params(self._ctrl, wave, amp, freq, duty)
            self._ctrl.start()
            self._motor_status.setText(
                f"{wave}  A={amp:.3f} mm  f={freq:.3f} Hz  duty={duty:.2f}")
            self._motor_status.setStyleSheet("color: green; font-size: 10px;")
            self._log(f"Motor started: {wave} A={amp:.3f} mm f={freq:.3f} Hz duty={duty:.2f}")
        except Exception as exc:
            self._log(f"Motor error: {exc}")

    def _stop_motor(self):
        if self._ctrl is None:
            return
        try:
            self._ctrl.stop()
            self._motor_status.setText("Stopped")
            self._motor_status.setStyleSheet("color: gray; font-size: 10px;")
            self._log("Motor stopped.")
        except Exception as exc:
            self._log(f"Stop error: {exc}")

    # ---- DAQ recording ----
    def _start_recording(self):
        if self._plugin.daq is None:
            self._log("ERROR: DAQ controller not available.")
            return
        if self._plugin.daq.is_recording():
            self._log("Already recording.")
            return
        n = self._n_files_spin.value()
        self._plugin.daq.start_recording(n_files=n)
        self._log(f"Recording started ({n} files).")

    def _stop_recording(self):
        if self._plugin.daq is None:
            return
        self._plugin.daq.stop_recording()
        self._log("Recording stopped.")

    def _apply_live_style(self, on: bool):
        if on:
            self._live_btn.setText("Live: ON")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #1e7e34; color: white; "
                "font-weight: bold; border-radius: 4px; padding: 4px 10px; }")
        else:
            self._live_btn.setText("Live: OFF")
            self._live_btn.setStyleSheet(
                "QPushButton { background-color: #555; color: white; "
                "font-weight: bold; border-radius: 4px; padding: 4px 10px; }")

    def _toggle_live(self, checked: bool):
        self._live = checked
        self._apply_live_style(checked)
        if checked:
            self._reset()
            self._log("Live mode ON — accumulating phase-folded cycles.")
        else:
            self._log("Live mode OFF.")

    def _reset(self):
        n_bins = self._n_bins_spin.value()
        self._accel_acc = TemplateAccumulator(n_bins)
        self._encoder_acc = TemplateAccumulator(n_bins)
        self._total_cycles = 0
        self._mean_period_s = 0.0
        self._period_count = 0
        self._cycle_lbl.setText("Cycles: 0")
        self._log("Template reset.")

    def _hwf(self, widget, default: float) -> float:
        try:
            return float(widget.text())
        except (ValueError, AttributeError):
            return default

    def _to_accel_g(self, V):
        return V / (self._hwf(self._sens_edit, 1000.0) * 1e-3)

    def _to_position_mm(self, V):
        v_fs = self._hwf(self._enc_vfs, 3.3)
        mm_lo = self._hwf(self._enc_mm_lo, -0.6375)
        mm_hi = self._hwf(self._enc_mm_hi, 0.6375)
        return (V / v_fs) * (mm_hi - mm_lo) + mm_lo

    # live data
    def on_file_written(self, filepath: str):
        if not self._live:
            return
        accel_ch = self._accel_ch.currentText()
        encoder_ch = self._encoder_ch.currentText()
        n_bins = self._n_bins_spin.value()
        try:
            accel_V, fs = daq_h5.read_channel(filepath, accel_ch)
            encoder_V, _ = daq_h5.read_channel(filepath, encoder_ch)
        except Exception as exc:
            self._log(f"Read error: {exc}")
            return
        accel_g = self._to_accel_g(accel_V)
        encoder_mm = self._to_position_mm(encoder_V)
        lp_hz = self._enc_lp.value()
        try:
            b, a = butter(4, lp_hz / (0.5 * fs), btype="low")
            encoder_filt = filtfilt(b, a, encoder_mm)
        except Exception:
            encoder_filt = encoder_mm
        cycles = segment_cycles(encoder_filt, fs,
                                self._min_period.value(), self._max_period.value())
        if not cycles:
            self._log(f"  {Path(filepath).name}: no complete cycles found.")
            return
        if self._accel_acc.n_bins != n_bins:
            self._reset()
        for s, e in cycles:
            self._accel_acc.add_cycle(phase_fold(accel_g, s, e, n_bins))
            self._encoder_acc.add_cycle(phase_fold(encoder_filt, s, e, n_bins))
            period = (e - s) / fs
            self._period_count += 1
            self._mean_period_s += (period - self._mean_period_s) / self._period_count
        self._total_cycles += len(cycles)
        self._cycle_lbl.setText(f"Cycles: {self._total_cycles}")
        self._log(f"  {Path(filepath).name}: +{len(cycles)} cycles "
                  f"(total {self._total_cycles})")
        self._update_plot()

    # plotting
    def _update_plot(self):
        if self._accel_acc.count < 2:
            return
        n_bins = self._accel_acc.n_bins
        phase = np.linspace(0.0, 1.0, n_bins, endpoint=False)
        phase_deg = phase * 360.0
        accel_mean = self._accel_acc.mean   # g
        accel_sem = self._accel_acc.sem     # g
        enc_mean = self._encoder_acc.mean   # mm
        n_cycles = self._accel_acc.count
        waveform = self._wave_combo.currentText()
        T_s = self._mean_period_s if self._mean_period_s > 0 else 1.0

        # Physical Coriolis prediction from encoder velocity
        a_cor_g, v_mms = coriolis_template(enc_mean, phase, T_s,
                                            sign=self._get_sign())

        accel_ug = accel_mean * 1e6       # g → µg
        sem_ug   = accel_sem * 1e6
        a_cor_ug = a_cor_g * 1e6          # g → µg
        cor_peak_ug = float(np.max(np.abs(a_cor_ug)))
        residual_ug = accel_ug - np.mean(accel_ug)
        search_hw = self._search_hw_spin.value()
        mean_abs, sem_avg, snr_region = compute_snr(
            accel_mean, accel_sem, phase, search_hw)
        mf_A, mf_sigma, snr_mf = matched_filter_snr(
            accel_mean, accel_sem, a_cor_g)
        v_peak = float(np.max(np.abs(v_mms)))

        self._plugin._plot_widget.update(
            phase_deg=phase_deg,
            enc_mean=enc_mean,
            v_mms=v_mms,
            accel_ug=accel_ug,
            sem_ug=sem_ug,
            a_cor_ug=a_cor_ug,
            residual_ug=residual_ug,
            cor_peak_ug=cor_peak_ug,
            search_hw=search_hw,
            waveform=waveform,
            n_cycles=n_cycles,
            T_s=T_s,
            v_peak=v_peak,
            mean_abs=mean_abs,
            sem_avg=sem_avg,
            snr_region=snr_region,
            mf_A_ug=mf_A * 1e6,
            mf_sigma_ug=mf_sigma * 1e6,
            snr_mf=snr_mf,
        )


# ===================================================================
#  PLOT WIDGET  (dedicated tab for continuous-mode plots)
# ===================================================================

class PlotWidget(QWidget):
    """Persistent 3-panel phase-folded template plot with shared x-axis
    and a log/linear y-axis toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)

        # Toolbar row: log toggle + SNR mode
        btn_row = QHBoxLayout()
        self._log_btn = QPushButton("Log Y")
        self._log_btn.setCheckable(True)
        self._log_btn.setChecked(False)
        self._log_btn.toggled.connect(self._toggle_log)
        btn_row.addWidget(self._log_btn)

        btn_row.addWidget(QLabel("  SNR mode:"))
        self._snr_mode_group = QButtonGroup(self)
        self._snr_region_rb = QRadioButton("Region avg")
        self._snr_mf_rb = QRadioButton("Matched filter")
        self._snr_region_rb.setChecked(True)
        self._snr_mode_group.addButton(self._snr_region_rb, 0)
        self._snr_mode_group.addButton(self._snr_mf_rb, 1)
        self._snr_mode_group.buttonToggled.connect(self._on_snr_mode_changed)
        btn_row.addWidget(self._snr_region_rb)
        btn_row.addWidget(self._snr_mf_rb)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        # Persistent figure & canvas
        self._fig = Figure(tight_layout=True)
        self._axes = self._fig.subplots(3, 1, sharex=True)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._toolbar = NavigationToolbar2QT(self._canvas, self)
        lay.addWidget(self._toolbar)
        lay.addWidget(self._canvas, stretch=1)

        self._log_scale = False
        self._last_kwargs = None   # stash for re-draw on mode toggle

    # --- public -----------------------------------------------------------
    def update(self, *, phase_deg, enc_mean, v_mms,
               accel_ug, sem_ug, a_cor_ug, residual_ug, cor_peak_ug,
               search_hw, waveform, n_cycles, T_s, v_peak,
               mean_abs, sem_avg, snr_region,
               mf_A_ug, mf_sigma_ug, snr_mf):
        self._last_kwargs = dict(
            phase_deg=phase_deg, enc_mean=enc_mean, v_mms=v_mms,
            accel_ug=accel_ug, sem_ug=sem_ug, a_cor_ug=a_cor_ug,
            residual_ug=residual_ug, cor_peak_ug=cor_peak_ug,
            search_hw=search_hw, waveform=waveform, n_cycles=n_cycles,
            T_s=T_s, v_peak=v_peak, mean_abs=mean_abs, sem_avg=sem_avg,
            snr_region=snr_region, mf_A_ug=mf_A_ug,
            mf_sigma_ug=mf_sigma_ug, snr_mf=snr_mf,
        )
        self._draw()

    def _draw(self):
        if self._last_kwargs is None:
            return
        kw = self._last_kwargs
        phase_deg = kw["phase_deg"]; enc_mean = kw["enc_mean"]
        v_mms = kw["v_mms"]; accel_ug = kw["accel_ug"]
        sem_ug = kw["sem_ug"]; a_cor_ug = kw["a_cor_ug"]
        residual_ug = kw["residual_ug"]; cor_peak_ug = kw["cor_peak_ug"]
        search_hw = kw["search_hw"]; waveform = kw["waveform"]
        n_cycles = kw["n_cycles"]; T_s = kw["T_s"]
        v_peak = kw["v_peak"]; mean_abs = kw["mean_abs"]
        sem_avg = kw["sem_avg"]; snr_region = kw["snr_region"]
        mf_A_ug = kw["mf_A_ug"]; mf_sigma_ug = kw["mf_sigma_ug"]
        snr_mf = kw["snr_mf"]
        use_mf = self._snr_mf_rb.isChecked()

        ax1, ax2, ax3 = self._axes
        for ax in self._axes:
            ax.clear()

        # --- Panel 1: encoder position + velocity twin axis ---
        ax1.plot(phase_deg, enc_mean, "b-", lw=1.2,
                 label="Position (mm)")
        ax1.set_ylabel("x pos\n(mm)", fontsize=9)
        ax1.set_title(
            f"Phase-folded template — {n_cycles} cycles — {waveform}"
            f" — T = {T_s:.3f} s ({1/T_s:.2f} Hz)",
            fontsize=11)
        ax1.legend(loc="upper right", fontsize=8)
        ax1.set_xlim(0, 360)
        ax1.grid(True, alpha=0.3)

        # Twin velocity axis (recreate each time since clear() removes it)
        for a in self._fig.axes:
            if a not in self._axes:
                a.remove()
        ax1v = ax1.twinx()
        ax1v.plot(phase_deg, v_mms, "r--", alpha=0.6, lw=0.9,
                  label="Velocity (mm/s)")
        ax1v.set_ylabel("ẋ\n(mm/s)", color="r", fontsize=9)
        ax1v.tick_params(axis="y", colors="r", labelsize=7)
        ax1v.legend(loc="lower right", fontsize=8)

        # --- Panel 2: measured accel y vs predicted Coriolis (µg) ---
        ax2.plot(phase_deg, accel_ug, "k-", lw=1.0,
                 label="Measured accel y")
        ax2.fill_between(phase_deg,
                         accel_ug - sem_ug, accel_ug + sem_ug,
                         alpha=0.3, color="C0",
                         label=f"±1 SEM (N={n_cycles})")
        ax2.plot(phase_deg,
                 a_cor_ug + np.mean(accel_ug),
                 "g-", lw=1.2, alpha=0.85,
                 label=f"Predicted Coriolis (peak {cor_peak_ug:.4f} µg)")
        ax2.set_ylabel("y accel\n(µg)", fontsize=9)
        ax2.grid(True, alpha=0.3)
        mark_coriolis_region(ax2, phase_deg, search_hw)
        ax2.legend(loc="upper right", fontsize=8)

        # --- Panel 3: residual + Coriolis prediction (µg) ---
        ax3.plot(phase_deg, residual_ug, "k-", lw=1.0,
                 label="Residual (mean-subtracted)")
        ax3.fill_between(phase_deg,
                         residual_ug - sem_ug, residual_ug + sem_ug,
                         alpha=0.3, color="C0", label="±1 SEM")
        if use_mf:
            # Scale template to matched-filter best-fit amplitude
            t = a_cor_ug.copy()
            t_norm = np.max(np.abs(t))
            if t_norm > 0:
                mf_fit_ug = t * (mf_A_ug / t_norm)
            else:
                mf_fit_ug = t
            ax3.plot(phase_deg, mf_fit_ug, "m-", lw=1.4, alpha=0.9,
                     label=f"MF best fit ({mf_A_ug:.4f} ± {mf_sigma_ug:.4f} µg)")
            ax3.plot(phase_deg, a_cor_ug, "g--", lw=0.8, alpha=0.45,
                     label=f"Predicted Coriolis ({cor_peak_ug:.4f} µg)")
            ax3.set_title(
                f"v_peak = {v_peak:.2f} mm/s — "
                f"MF amplitude = {mf_A_ug:.4f} ± {mf_sigma_ug:.4f} µg — "
                f"SNR_MF = {snr_mf:.1f}",
                fontsize=9, color="darkblue")
        else:
            ax3.plot(phase_deg, a_cor_ug, "g-", lw=1.2, alpha=0.85,
                     label=f"Predicted Coriolis ({cor_peak_ug:.4f} µg)")
            mark_coriolis_region(ax3, phase_deg, search_hw)
            ax3.set_title(
                f"v_peak = {v_peak:.2f} mm/s — "
                f"Coriolis peak = {cor_peak_ug:.4f} µg — "
                f"SEM = {sem_avg*1e6:.2e} µg — SNR = {snr_region:.1f}",
                fontsize=9, color="darkblue")
        ax3.set_ylabel("y res\n(µg)", fontsize=9)
        ax3.set_xlabel("Phase (degrees)")
        ax3.grid(True, alpha=0.3)
        ax3.legend(loc="upper right", fontsize=8)

        # Apply log/linear
        self._apply_scale()
        self._canvas.draw_idle()

    # --- internals --------------------------------------------------------
    def _toggle_log(self, checked: bool):
        self._log_scale = checked
        self._log_btn.setText("Linear Y" if checked else "Log Y")
        self._apply_scale()
        self._canvas.draw_idle()

    def _apply_scale(self):
        scale = "log" if self._log_scale else "linear"
        for ax in self._axes:
            ax.set_yscale(scale)

    def _on_snr_mode_changed(self):
        self._draw()


# ===================================================================
#  SCAN WORKER THREAD
# ===================================================================

class _ScanWorker(QThread):
    """Iterate over (waveform, amplitude, frequency) combinations."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)          # (step, total)
    point_started = pyqtSignal(str, float, float, float)  # waveform, amp, freq, duty
    request_recording = pyqtSignal(str)      # basename
    finished = pyqtSignal(bool)

    def __init__(self, ctrl, steps, n_files: int, settle_s: float, parent=None):
        super().__init__(parent)
        self._ctrl = ctrl
        # steps: list of (waveform_name, firmware_cmd, amp, freq, duty)
        self._steps = steps
        self._n_files = n_files
        self._settle_s = settle_s
        self._cancel = False
        self._file_event = False
        self._files_remaining = 0

    def cancel(self):
        self._cancel = True

    def notify_file_written(self, path: str):
        self._file_event = True

    def run(self):
        try:
            ctrl = self._ctrl
            n = len(self._steps)
            ctrl.enable()
            time.sleep(0.5)

            for i, (wave_name, wave_fw, amp, freq, duty) in enumerate(self._steps):
                if self._cancel:
                    break

                self.progress.emit(i + 1, n)
                self.log.emit(
                    f"--- Step {i+1}/{n}: {wave_name}  "
                    f"A={amp:.3f} mm  f={freq:.3f} Hz  duty={duty:.2f} ---"
                )

                _apply_waveform_params(ctrl, wave_name, amp, freq, duty)
                ctrl.start()

                # Settle
                self.log.emit(f"  Settling {self._settle_s:.1f} s ...")
                t0 = time.time()
                while time.time() - t0 < self._settle_s:
                    if self._cancel:
                        break
                    time.sleep(0.1)
                if self._cancel:
                    ctrl.stop(); break

                # Notify widget which scan point is active
                self.point_started.emit(wave_name, amp, freq, duty)

                # Record n_files sequentially
                for fi in range(self._n_files):
                    if self._cancel:
                        break
                    basename = (
                        f"cscan_{wave_name.replace(' ','')}_"
                        f"A{amp:.3f}_f{freq:.3f}_d{duty:.2f}_{fi}"
                    ).replace('.', '_')
                    self._file_event = False
                    self.request_recording.emit(basename)

                    t0 = time.time()
                    while not self._file_event:
                        if self._cancel:
                            break
                        if time.time() - t0 > 120:
                            self.log.emit("  ERROR: DAQ timeout.")
                            break
                        time.sleep(0.1)

                ctrl.stop()
                time.sleep(0.3)

            ctrl.stop()
            self.finished.emit(not self._cancel)

        except Exception as exc:
            self.log.emit(f"Scan error: {exc}")
            try:
                self._ctrl.stop()
            except Exception:
                pass
            self.finished.emit(False)


# ===================================================================
#  SCAN MODE WIDGET
# ===================================================================

class ScanWidget(QWidget):
    """Automated parameter scan across waveforms, amplitudes, frequencies."""

    def __init__(self, plugin: "Plugin", parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._ctrl = None
        self._worker: _ScanWorker | None = None
        # Per-point accumulators: key = (waveform, amp, freq)
        self._point_accs: dict[tuple[str, float, float], TemplateAccumulator] = {}
        self._point_enc_accs: dict[tuple[str, float, float], TemplateAccumulator] = {}
        self._point_cycles: dict[tuple[str, float, float], int] = {}
        self._point_periods: dict[tuple[str, float, float], tuple[float, int]] = {}
        self._active_key: tuple[str, float, float] | None = None
        self._active_waveform: str = "Sine"
        self._active_duty: float = 0.9
        self._n_bins = N_PHASE_BINS
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(6, 6, 6, 6)

        # --- ESP32 connection ---
        conn = QGroupBox("ESP32 Connection")
        cl = QVBoxLayout(conn)
        cr1 = QHBoxLayout()
        cr1.addWidget(QLabel("Port:"))
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("Auto-detect")
        self._port_edit.setMaximumWidth(120)
        cr1.addWidget(self._port_edit)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._toggle_connect)
        cr1.addWidget(self._connect_btn)
        self._conn_status = QLabel("Disconnected")
        self._conn_status.setStyleSheet("color: gray;")
        cr1.addWidget(self._conn_status)
        cr1.addStretch()
        cl.addLayout(cr1)
        cr2 = QHBoxLayout()
        self._pos_label = QLabel("Position: — mm")
        self._pos_label.setStyleSheet("font-weight: bold;")
        cr2.addWidget(self._pos_label)
        self._zero_btn = QPushButton("Zero")
        self._zero_btn.setToolTip("Set current position as zero reference")
        self._zero_btn.clicked.connect(self._on_zero)
        cr2.addWidget(self._zero_btn)
        self._go_home_btn = QPushButton("Go Home")
        self._go_home_btn.setToolTip("Move motor to position 0")
        self._go_home_btn.clicked.connect(self._on_go_home)
        cr2.addWidget(self._go_home_btn)
        cr2.addWidget(QLabel("Move to (mm):"))
        self._move_to_spin = QDoubleSpinBox()
        self._move_to_spin.setRange(-100.0, 100.0); self._move_to_spin.setValue(0.0)
        self._move_to_spin.setDecimals(4); self._move_to_spin.setSingleStep(0.1)
        self._move_to_spin.setMaximumWidth(90)
        cr2.addWidget(self._move_to_spin)
        self._move_to_btn = QPushButton("Move")
        self._move_to_btn.clicked.connect(self._on_move_to)
        cr2.addWidget(self._move_to_btn)
        cr2.addStretch()
        cl.addLayout(cr2)
        root.addWidget(conn)

        # --- Hardware / calibration ---
        hw = QGroupBox("Hardware && Calibration")
        hl = QVBoxLayout(hw)
        hr1 = QHBoxLayout()
        hr1.addWidget(QLabel("Accel ch:"))
        self._accel_ch = QComboBox()
        self._accel_ch.addItems(daq_h5.ALL_CHANNELS)
        self._accel_ch.setCurrentIndex(min(16, len(daq_h5.ALL_CHANNELS) - 1))
        hr1.addWidget(self._accel_ch)
        hr1.addWidget(QLabel("Encoder ch:"))
        self._encoder_ch = QComboBox()
        self._encoder_ch.addItems(daq_h5.ALL_CHANNELS)
        self._encoder_ch.setCurrentIndex(min(17, len(daq_h5.ALL_CHANNELS) - 1))
        hr1.addWidget(self._encoder_ch)
        hr1.addWidget(QLabel("Sensitivity (mV/g):"))
        self._sens_edit = QLineEdit("1000.0")
        self._sens_edit.setMaximumWidth(80)
        hr1.addWidget(self._sens_edit)
        hl.addLayout(hr1)

        hr2 = QHBoxLayout()
        hr2.addWidget(QLabel("Encoder V FS:"))
        self._enc_vfs = QLineEdit("3.3"); self._enc_vfs.setMaximumWidth(60)
        hr2.addWidget(self._enc_vfs)
        hr2.addWidget(QLabel("Enc mm low:"))
        self._enc_mm_lo = QLineEdit("-0.6375"); self._enc_mm_lo.setMaximumWidth(70)
        hr2.addWidget(self._enc_mm_lo)
        hr2.addWidget(QLabel("Enc mm hi:"))
        self._enc_mm_hi = QLineEdit("0.6375"); self._enc_mm_hi.setMaximumWidth(70)
        hr2.addWidget(self._enc_mm_hi)
        hr2.addWidget(QLabel("Encoder LP (Hz):"))
        self._enc_lp = QDoubleSpinBox()
        self._enc_lp.setRange(0.1, 1000.0); self._enc_lp.setValue(50.0)
        self._enc_lp.setDecimals(1); self._enc_lp.setMaximumWidth(70)
        hr2.addWidget(self._enc_lp)
        hr2.addWidget(QLabel("Phase bins:"))
        self._n_bins_spin = QSpinBox()
        self._n_bins_spin.setRange(64, 4096); self._n_bins_spin.setValue(N_PHASE_BINS)
        self._n_bins_spin.setSingleStep(64); self._n_bins_spin.setMaximumWidth(70)
        hr2.addWidget(self._n_bins_spin)
        hr2.addWidget(QLabel("Search ±(°):"))
        self._search_hw_spin = QDoubleSpinBox()
        self._search_hw_spin.setRange(1.0, 89.0); self._search_hw_spin.setValue(60.0)
        self._search_hw_spin.setDecimals(1); self._search_hw_spin.setSingleStep(5.0)
        self._search_hw_spin.setMaximumWidth(60)
        self._search_hw_spin.setToolTip(
            "Half-width (degrees) of the search region centred on\n"
            "0° and 180° (the velocity peaks).")
        hr2.addWidget(self._search_hw_spin)
        hl.addLayout(hr2)

        hr3 = QHBoxLayout()
        hr3.addWidget(QLabel("Min period (s):"))
        self._min_period = QDoubleSpinBox()
        self._min_period.setRange(0.01, 100.0); self._min_period.setValue(0.05)
        self._min_period.setDecimals(2); self._min_period.setMaximumWidth(70)
        hr3.addWidget(self._min_period)
        hr3.addWidget(QLabel("Max period (s):"))
        self._max_period = QDoubleSpinBox()
        self._max_period.setRange(0.1, 100.0); self._max_period.setValue(20.0)
        self._max_period.setDecimals(1); self._max_period.setMaximumWidth(70)
        hr3.addWidget(self._max_period)
        hr3.addWidget(QLabel("Settle time (s):"))
        self._settle_spin = QDoubleSpinBox()
        self._settle_spin.setRange(0.5, 120.0); self._settle_spin.setValue(5.0)
        self._settle_spin.setDecimals(1); self._settle_spin.setMaximumWidth(70)
        hr3.addWidget(self._settle_spin)
        hr3.addWidget(QLabel("Files per point:"))
        self._nfiles_spin = QSpinBox()
        self._nfiles_spin.setRange(1, 1000); self._nfiles_spin.setValue(10)
        self._nfiles_spin.setMaximumWidth(70)
        hr3.addWidget(self._nfiles_spin)
        hl.addLayout(hr3)
        root.addWidget(hw)

        # --- Scan parameter grid ---
        scan_grp = QGroupBox("Scan Parameters")
        sl = QVBoxLayout(scan_grp)

        # Waveform selection
        wf_row = QHBoxLayout()
        wf_row.addWidget(QLabel("Waveforms:"))
        self._cb_sine = QCheckBox("Sine"); self._cb_sine.setChecked(True)
        self._cb_trap = QCheckBox("Triangle"); self._cb_trap.setChecked(True)
        self._cb_scurve = QCheckBox("Rounded Triangle"); self._cb_scurve.setChecked(False)
        wf_row.addWidget(self._cb_sine)
        wf_row.addWidget(self._cb_trap)
        wf_row.addWidget(self._cb_scurve)
        wf_row.addStretch()
        sl.addLayout(wf_row)

        # Duty cycle for triangle waves
        duty_row = QHBoxLayout()
        duty_row.addWidget(QLabel("Triangle duty cycle:"))
        self._duty_spin = QDoubleSpinBox()
        self._duty_spin.setRange(0.1, 0.99); self._duty_spin.setValue(0.90)
        self._duty_spin.setDecimals(2); self._duty_spin.setSingleStep(0.01)
        self._duty_spin.setMaximumWidth(70)
        duty_row.addWidget(self._duty_spin)
        duty_row.addStretch()
        sl.addLayout(duty_row)

        # Amplitude range
        amp_row = QHBoxLayout()
        amp_row.addWidget(QLabel("Amplitude (mm):"))
        self._amp_lo = QDoubleSpinBox()
        self._amp_lo.setRange(0.01, 10.0); self._amp_lo.setValue(0.1)
        self._amp_lo.setDecimals(3); self._amp_lo.setSingleStep(0.1)
        amp_row.addWidget(self._amp_lo)
        amp_row.addWidget(QLabel("to"))
        self._amp_hi = QDoubleSpinBox()
        self._amp_hi.setRange(0.01, 10.0); self._amp_hi.setValue(1.0)
        self._amp_hi.setDecimals(3); self._amp_hi.setSingleStep(0.1)
        amp_row.addWidget(self._amp_hi)
        amp_row.addWidget(QLabel("Step:"))
        self._amp_step = QDoubleSpinBox()
        self._amp_step.setRange(0.001, 5.0); self._amp_step.setValue(0.3)
        self._amp_step.setDecimals(3); self._amp_step.setSingleStep(0.1)
        amp_row.addWidget(self._amp_step)
        amp_row.addStretch()
        sl.addLayout(amp_row)

        # Frequency range
        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Frequency (Hz):"))
        self._freq_lo = QDoubleSpinBox()
        self._freq_lo.setRange(0.01, 100.0); self._freq_lo.setValue(0.5)
        self._freq_lo.setDecimals(3); self._freq_lo.setSingleStep(0.1)
        freq_row.addWidget(self._freq_lo)
        freq_row.addWidget(QLabel("to"))
        self._freq_hi = QDoubleSpinBox()
        self._freq_hi.setRange(0.01, 100.0); self._freq_hi.setValue(2.0)
        self._freq_hi.setDecimals(3); self._freq_hi.setSingleStep(0.5)
        freq_row.addWidget(self._freq_hi)
        freq_row.addWidget(QLabel("Step:"))
        self._freq_step = QDoubleSpinBox()
        self._freq_step.setRange(0.001, 50.0); self._freq_step.setValue(0.5)
        self._freq_step.setDecimals(3); self._freq_step.setSingleStep(0.1)
        freq_row.addWidget(self._freq_step)
        freq_row.addStretch()
        sl.addLayout(freq_row)

        root.addWidget(scan_grp)

        # --- Controls ---
        ctrl_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Scan")
        self._start_btn.setMinimumWidth(120)
        self._start_btn.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; "
            "font-weight: bold; padding: 4px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3b82f6; }")
        self._start_btn.clicked.connect(self._start_scan)
        ctrl_row.addWidget(self._start_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_scan)
        ctrl_row.addWidget(self._cancel_btn)
        self._progress_lbl = QLabel("")
        self._progress_lbl.setStyleSheet("color: gray; font-size: 10px;")
        ctrl_row.addWidget(self._progress_lbl)
        ctrl_row.addStretch()
        root.addLayout(ctrl_row)

        # --- Log ---
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumHeight(120)
        self._log_box.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(self._log_box)

        # --- Plot area (scrollable) ---
        self._plot_layout = QVBoxLayout()
        plot_widget = QWidget()
        plot_widget.setLayout(self._plot_layout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(plot_widget)
        root.addWidget(scroll, stretch=1)

    # ------------------------------------------------------------------ helpers
    def _log(self, msg: str):
        self._log_box.append(msg)
        sb = self._log_box.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _hwf(self, widget, default: float) -> float:
        try:
            return float(widget.text())
        except (ValueError, AttributeError):
            return default

    def _to_accel_g(self, V):
        return V / (self._hwf(self._sens_edit, 1000.0) * 1e-3)

    def _to_position_mm(self, V):
        v_fs = self._hwf(self._enc_vfs, 3.3)
        mm_lo = self._hwf(self._enc_mm_lo, -0.6375)
        mm_hi = self._hwf(self._enc_mm_hi, 0.6375)
        return (V / v_fs) * (mm_hi - mm_lo) + mm_lo

    # ------------------------------------------------------------------ ESP32
    def _toggle_connect(self):
        if self._ctrl is not None:
            try:
                self._ctrl.stop(); self._ctrl.disable(); self._ctrl.disconnect()
            except Exception:
                pass
            self._ctrl = None
            self._connect_btn.setText("Connect")
            self._conn_status.setText("Disconnected")
            self._conn_status.setStyleSheet("color: gray;")
            self._pos_label.setText("Position: — mm")
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
            self._ctrl.set_line_callback(lambda line: None)
            self._ctrl.connect()
            self._connect_btn.setText("Disconnect")
            self._conn_status.setText(f"Connected ({port})")
            self._conn_status.setStyleSheet("color: green; font-weight: bold;")
            # Restore last known position
            last_pos = _load_esp_position()
            if last_pos != 0.0:
                self._ctrl.set_position(last_pos)
                self._log(f"Restored position: {last_pos:.4f} mm")
            self._pos_label.setText(f"Position: {last_pos:.4f} mm")
            self._log(f"Connected to ESP32 on {port}")
        except Exception as exc:
            self._log(f"Connection error: {exc}")
            self._ctrl = None

    def _on_zero(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first."); return
        self._ctrl.zero()
        self._pos_label.setText("Position: 0.0000 mm")
        _save_esp_position(0.0)
        self._log("Zeroed position.")

    def _on_go_home(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first."); return
        self._ctrl.go_home()
        self._pos_label.setText("Position: 0.0000 mm (homing…)")
        _save_esp_position(0.0)
        self._log("Going home (moving to 0).")

    def _on_move_to(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first."); return
        target = self._move_to_spin.value()
        cur = _load_esp_position()
        delta = target - cur
        if abs(delta) < 1e-6:
            self._log("Already at target position."); return
        self._ctrl.move(delta)
        self._pos_label.setText(f"Position: {target:.4f} mm (moving…)")
        _save_esp_position(target)
        self._log(f"Moving to {target:.4f} mm (delta={delta:+.4f} mm).")

    # ------------------------------------------------------------------ build steps
    def _build_range(self, lo_spin, hi_spin, step_spin) -> list[float]:
        lo, hi, step = lo_spin.value(), hi_spin.value(), step_spin.value()
        vals = []
        v = lo
        while v <= hi + 1e-9:
            vals.append(round(v, 4))
            v += step
        return vals

    def _build_steps(self):
        """Build list of (wave_name, firmware_cmd, amp, freq, duty)."""
        waves = []
        if self._cb_sine.isChecked():
            waves.append(("Sine", "SINE"))
        if self._cb_trap.isChecked():
            waves.append(("Triangle", "TRAP"))
        if self._cb_scurve.isChecked():
            waves.append(("Rounded Triangle", "SCURVE"))
        amps = self._build_range(self._amp_lo, self._amp_hi, self._amp_step)
        freqs = self._build_range(self._freq_lo, self._freq_hi, self._freq_step)
        duty = self._duty_spin.value()
        steps = []
        for wname, wfw in waves:
            for amp in amps:
                for freq in freqs:
                    steps.append((wname, wfw, amp, freq, duty))
        return steps

    # ------------------------------------------------------------------ scan control
    def _start_scan(self):
        if self._ctrl is None:
            self._log("ERROR: Connect to ESP32 first.")
            return
        if self._plugin.daq is None:
            self._log("ERROR: DAQ controller not available.")
            return
        if self._plugin.daq.is_recording():
            self._log("ERROR: A recording is already in progress.")
            return

        steps = self._build_steps()
        if not steps:
            self._log("ERROR: No scan points (check waveform/range settings).")
            return

        self._n_bins = self._n_bins_spin.value()
        self._point_accs.clear()
        self._point_enc_accs.clear()
        self._point_cycles.clear()
        self._point_periods.clear()
        self._active_key = None

        n_files = self._nfiles_spin.value()
        settle = self._settle_spin.value()

        self._log(f"Starting scan: {len(steps)} parameter points × {n_files} files")

        self._worker = _ScanWorker(self._ctrl, steps, n_files, settle)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self._on_progress)
        self._worker.point_started.connect(self._on_point_started)
        self._worker.request_recording.connect(self._on_request_recording)
        self._worker.finished.connect(self._on_scan_finished)

        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._worker.start()

    def _cancel_scan(self):
        if self._worker:
            self._worker.cancel()
            self._cancel_btn.setEnabled(False)
            self._log("Cancelling scan ...")

    def _on_progress(self, step, total):
        self._progress_lbl.setText(f"Step {step}/{total}")

    def _on_point_started(self, waveform, amp, freq, duty):
        key = (waveform, amp, freq)
        self._active_key = key
        self._active_waveform = waveform
        self._active_duty = duty
        if key not in self._point_accs:
            self._point_accs[key] = TemplateAccumulator(self._n_bins)
            self._point_enc_accs[key] = TemplateAccumulator(self._n_bins)
            self._point_cycles[key] = 0
            self._point_periods[key] = (0.0, 0)

    def _on_request_recording(self, basename):
        self._plugin.daq.start_recording(n_files=1, basename=basename)

    def _on_scan_finished(self, success):
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._progress_lbl.setText("Done" if success else "Cancelled/Error")
        self._log("Scan finished." if success else "Scan stopped.")
        self._update_summary_plot()

    # ------------------------------------------------------------------ on_file_written
    def on_file_written(self, filepath: str):
        # Notify worker that the file arrived
        if self._worker is not None and self._worker.isRunning():
            self._worker.notify_file_written(filepath)

        key = self._active_key
        if key is None:
            return

        accel_ch = self._accel_ch.currentText()
        encoder_ch = self._encoder_ch.currentText()
        try:
            accel_V, fs = daq_h5.read_channel(filepath, accel_ch)
            encoder_V, _ = daq_h5.read_channel(filepath, encoder_ch)
        except Exception as exc:
            self._log(f"Read error: {exc}")
            return

        accel_g = self._to_accel_g(accel_V)
        encoder_mm = self._to_position_mm(encoder_V)
        lp_hz = self._enc_lp.value()
        try:
            b, a = butter(4, lp_hz / (0.5 * fs), btype="low")
            encoder_filt = filtfilt(b, a, encoder_mm)
        except Exception:
            encoder_filt = encoder_mm

        cycles = segment_cycles(encoder_filt, fs,
                                self._min_period.value(), self._max_period.value())
        if not cycles:
            return

        acc = self._point_accs[key]
        enc_acc = self._point_enc_accs[key]
        mean_p, p_count = self._point_periods.get(key, (0.0, 0))
        for s, e in cycles:
            acc.add_cycle(phase_fold(accel_g, s, e, self._n_bins))
            enc_acc.add_cycle(phase_fold(encoder_filt, s, e, self._n_bins))
            period = (e - s) / fs
            p_count += 1
            mean_p += (period - mean_p) / p_count
        self._point_periods[key] = (mean_p, p_count)
        self._point_cycles[key] = self._point_cycles.get(key, 0) + len(cycles)

        self._log(f"  {Path(filepath).name}: +{len(cycles)} cycles for "
                  f"{key[0]} A={key[1]:.3f} f={key[2]:.3f}  "
                  f"(total {self._point_cycles[key]})")

        self._update_summary_plot()

    # ------------------------------------------------------------------ plotting
    def _clear_plots(self):
        while self._plot_layout.count():
            item = self._plot_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _update_summary_plot(self):
        self._clear_plots()
        if not self._point_accs:
            return

        phase = np.linspace(0.0, 1.0, self._n_bins, endpoint=False)

        # Collect SNR for every completed point
        snr_data: list[tuple[str, float, float, float, int]] = []
        for (wf, amp, freq), acc in self._point_accs.items():
            if acc.count < 2:
                continue
            _, _, snr = compute_snr(
                acc.mean, acc.sem, phase,
                self._search_hw_spin.value(),
            )
            n_cyc = self._point_cycles.get((wf, amp, freq), 0)
            snr_data.append((wf, amp, freq, snr, n_cyc))

        if not snr_data:
            return

        # Group by waveform
        waveforms = sorted(set(d[0] for d in snr_data),
                          key=lambda w: ["Sine", "Triangle", "Rounded Triangle"].index(w)
                          if w in ["Sine", "Triangle", "Rounded Triangle"] else 99)
        amps_all = sorted(set(d[1] for d in snr_data))
        freqs_all = sorted(set(d[2] for d in snr_data))

        n_waves = len(waveforms)
        # One row of heatmaps + one row for active template = n_waves + 1 panels
        fig = Figure(figsize=(max(8, 4 * len(freqs_all)), 4 * (n_waves + 1)),
                     tight_layout=True)

        # --- Heatmaps: one per waveform ---
        for wi, wf in enumerate(waveforms):
            ax = fig.add_subplot(n_waves + 1, 1, wi + 1)
            # Build 2D grid (amp × freq)
            wf_data = {(d[1], d[2]): (d[3], d[4]) for d in snr_data if d[0] == wf}
            grid = np.full((len(amps_all), len(freqs_all)), np.nan)
            annot = [['' for _ in freqs_all] for _ in amps_all]
            for ai, a in enumerate(amps_all):
                for fi, f in enumerate(freqs_all):
                    if (a, f) in wf_data:
                        snr_val, n_cyc = wf_data[(a, f)]
                        grid[ai, fi] = snr_val
                        annot[ai][fi] = f"{snr_val:.1f}\n({n_cyc})"

            im = ax.imshow(grid, aspect="auto", origin="lower",
                           cmap="RdYlGn", interpolation="nearest")
            ax.set_xticks(range(len(freqs_all)))
            ax.set_xticklabels([f"{f:.2f}" for f in freqs_all], fontsize=8)
            ax.set_yticks(range(len(amps_all)))
            ax.set_yticklabels([f"{a:.3f}" for a in amps_all], fontsize=8)
            ax.set_xlabel("Frequency (Hz)", fontsize=9)
            ax.set_ylabel("Amplitude (mm)", fontsize=9)
            ax.set_title(f"{wf} — SNR (cycles)", fontsize=10)

            # Annotate cells
            for ai in range(len(amps_all)):
                for fi in range(len(freqs_all)):
                    if annot[ai][fi]:
                        ax.text(fi, ai, annot[ai][fi], ha="center", va="center",
                                fontsize=7, color="black")
            fig.colorbar(im, ax=ax, label="SNR", shrink=0.8)

        # --- Active template panel ---
        if self._active_key and self._active_key in self._point_accs:
            acc = self._point_accs[self._active_key]
            enc_acc = self._point_enc_accs.get(self._active_key)
            mean_p, _ = self._point_periods.get(self._active_key, (1.0, 0))
            T_s = mean_p if mean_p > 0 else 1.0
            if acc.count >= 2:
                ax_t = fig.add_subplot(n_waves + 1, 1, n_waves + 1)
                phase_deg = phase * 360.0
                residual_ug = (acc.mean - np.mean(acc.mean)) * 1e6  # g → µg
                sem_ug = acc.sem * 1e6
                ax_t.plot(phase_deg, residual_ug, "k-", lw=1.0,
                          label="Residual y (µg)")
                ax_t.fill_between(phase_deg,
                                  residual_ug - sem_ug, residual_ug + sem_ug,
                                  alpha=0.3, color="C0", label="±1 SEM")
                # Overlay calibrated Coriolis prediction from encoder
                if enc_acc is not None and enc_acc.count >= 2:
                    sign = self._plugin._cont_widget._get_sign()
                    a_cor_g, v_mms = coriolis_template(
                        enc_acc.mean, phase, T_s, sign=sign)
                    a_cor_ug = a_cor_g * 1e6
                    cor_peak_ug = float(np.max(np.abs(a_cor_ug)))
                    ax_t.plot(phase_deg, a_cor_ug, "g-", lw=1.2,
                              alpha=0.85,
                              label=f"Predicted Coriolis ({cor_peak_ug:.4f} µg)")
                mark_coriolis_region(ax_t, phase_deg,
                                    self._search_hw_spin.value())
                wf, a, f = self._active_key
                n_cyc = self._point_cycles.get(self._active_key, 0)
                ax_t.set_title(
                    f"Active: {wf} A={a:.3f} mm f={f:.3f} Hz  "
                    f"({n_cyc} cycles) — T = {T_s:.3f} s", fontsize=10)
                ax_t.set_xlabel("Phase (degrees)")
                ax_t.set_ylabel("Residual accel y (µg)")
                ax_t.set_xlim(0, 360); ax_t.grid(True, alpha=0.3)
                ax_t.legend(loc="upper right", fontsize=8)

        canvas = FigureCanvasQTAgg(fig)
        toolbar = NavigationToolbar2QT(canvas, self)
        self._plot_layout.addWidget(toolbar)
        self._plot_layout.addWidget(canvas)


# ===================================================================
#  PLUGIN ENTRY POINT
# ===================================================================

class Plugin(AnalysisPlugin):
    NAME = "Coriolis Search"
    DESCRIPTION = (
        "Phase-folded template averaging to search for Coriolis forces. "
        "Continuous mode for single-parameter runs, or Scan mode for "
        "automated sweeps across waveforms, amplitudes, and frequencies."
    )

    def create_widget(self, parent=None):
        self._tabs = QTabWidget()
        self._cont_widget = ContinuousWidget(self)
        self._scan_widget = ScanWidget(self)
        self._plot_widget = PlotWidget()
        self._tabs.addTab(self._cont_widget, "Continuous")
        self._tabs.addTab(self._scan_widget, "Scan")
        self._tabs.addTab(self._plot_widget, "Plot")
        return self._tabs

    def on_file_written(self, filepath: str):
        if hasattr(self, "_cont_widget") and self._cont_widget is not None:
            self._cont_widget.on_file_written(filepath)
        if hasattr(self, "_scan_widget") and self._scan_widget is not None:
            self._scan_widget.on_file_written(filepath)
