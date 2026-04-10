# Coriolis Search Plugin — Analysis Documentation

## 1. Purpose

The Coriolis Search plugin performs **live phase-folded template averaging** to extract the Coriolis acceleration signature from continuous table oscillation data.  A motorised linear stage moves the optical table east/west (x-direction) while the encoder records the table position and an accelerometer records the perpendicular (y-direction) response.  Over many oscillation cycles the random noise averages down and the deterministic Coriolis signal emerges.

Two operating modes are provided:

| Mode | Description |
|---|---|
| **Continuous** | The motor runs at one fixed waveform / amplitude / frequency.  Each incoming HDF5 file is segmented into complete cycles, phase-folded, and accumulated into a running template. |
| **Scan** | The plugin steps through a grid of (waveform, amplitude, frequency) combinations, records a configurable number of files at each point, phase-folds the data, and builds a live SNR heatmap. |

---

## 2. Coordinate Convention

| Axis | Physical direction | Sensor |
|---|---|---|
| **x** | East / West | Motor encoder (table position, mm) |
| **y** | Perpendicular horizontal (≈ North / South) | Accelerometer (g) |

The table moves along x.  The Coriolis acceleration appears along y, perpendicular to the velocity and to Earth's rotation axis.

---

## 3. Core Physics

### 3.1 Coriolis Acceleration

In the rotating Earth frame, an object moving with velocity $\dot{x}$ along the east/west direction experiences a horizontal Coriolis acceleration in the perpendicular (y) direction ([coriolis_sensitivity.md §1](../analysis/Inertial%20Sensing/coriolis_sensitivity.md)):

$$a_{\mathrm{Cor},y}(t) = \kappa \;\dot{x}_{\mathrm{table}}(t) \tag{1}$$

where the coupling constant is

$$\kappa = 2\,\Omega_\oplus\,\sin\lambda \tag{2}$$

| Symbol | Value | Source |
|---|---|---|
| $\Omega_\oplus$ | $7.292 \times 10^{-5}$ rad/s | Earth's sidereal rotation rate |
| $\lambda$ | 41.3° | Yale University latitude |
| $\kappa$ | $9.61 \times 10^{-5}$ rad/s | Eq. (2) |
| $g$ | 9.80665 m/s² | Standard gravity (for unit conversion) |

**Reference**: Eq. (1) is the horizontal component of the Coriolis acceleration $\mathbf{a}_C = -2\,\boldsymbol{\Omega}\times\mathbf{v}$ projected onto the y-axis.  See [coriolis_sensitivity.md §1](../analysis/Inertial%20Sensing/coriolis_sensitivity.md) and [coriolis_analysis.md §1](../analysis/Inertial%20Sensing/coriolis_analysis.md) for the full vector derivation.

### 3.2 Numerical Scale

For a sinusoidal table oscillation at frequency $f$ and half-amplitude $A$ (mm):

$$\dot{x}_{\mathrm{peak}} = 2\pi f\,A \quad [\text{mm/s}]$$

$$a_{\mathrm{Cor,peak}} = \kappa \times \dot{x}_{\mathrm{peak}} \times 10^{-3} \quad [\text{m/s}^2]$$

| Example | $f$ | $A$ | $\dot{x}_{\mathrm{peak}}$ | $a_{\mathrm{Cor,peak}}$ |
|---|---|---|---|---|
| Slow | 0.5 Hz | 0.5 mm | 1.57 mm/s | 1.5 × 10⁻⁴ µg |
| Medium | 1.0 Hz | 0.5 mm | 3.14 mm/s | 3.1 × 10⁻⁴ µg |
| Fast | 2.0 Hz | 1.0 mm | 12.6 mm/s | 1.2 × 10⁻³ µg |

The expected Coriolis acceleration is extremely small (~10⁻⁴ µg), which is why long integration via phase folding is essential.

---

## 4. Signal Processing Pipeline

### 4.1 Data Ingestion

Each time the DAQ writes an HDF5 file, `on_file_written(filepath)` is called.  The plugin reads two channels:

- **Encoder channel** → raw voltage → converted to position in mm via a linear mapping:

$$x_{\mathrm{mm}} = \frac{V}{V_{\mathrm{FS}}} \times (x_{\mathrm{hi}} - x_{\mathrm{lo}}) + x_{\mathrm{lo}} \tag{3}$$

  where $V_{\mathrm{FS}}$, $x_{\mathrm{lo}}$, $x_{\mathrm{hi}}$ are user-configurable calibration parameters.

  **Implementation**: `ContinuousWidget._to_position_mm()`, `ScanWidget._to_position_mm()`.

- **Accelerometer channel** → raw voltage → converted to acceleration in g:

$$a_g = \frac{V}{S \times 10^{-3}} \tag{4}$$

  where $S$ is the accelerometer sensitivity in mV/g.

  **Implementation**: `ContinuousWidget._to_accel_g()`, `ScanWidget._to_accel_g()`.

### 4.2 Encoder Low-Pass Filtering

The encoder signal is low-pass filtered with a 4th-order Butterworth filter (configurable cutoff, default 50 Hz) before cycle segmentation.  This removes high-frequency quantisation noise from the encoder and prevents false zero crossings.

**Implementation**: `scipy.signal.butter` + `scipy.signal.filtfilt` in `on_file_written()`.

### 4.3 Cycle Segmentation

Complete oscillation cycles are identified by finding **rising zero crossings** of the mean-subtracted encoder signal.  A pair of consecutive crossings $(s_i, s_{i+1})$ defines one cycle if its duration falls within the configurable `[min_period, max_period]` range.

**Implementation**: `segment_cycles()` → calls `_find_zero_crossings()`.

### 4.4 Phase Folding

Each cycle of length $n$ samples is interpolated onto a uniform phase grid of $N_{\mathrm{bins}}$ points (default 512) spanning $\phi \in [0, 1)$:

$$\phi_k = \frac{k}{N_{\mathrm{bins}}}, \quad k = 0, 1, \ldots, N_{\mathrm{bins}} - 1 \tag{5}$$

This maps every cycle—regardless of its exact period—onto the same phase axis, enabling coherent averaging.

**Implementation**: `phase_fold(data, start, end, n_bins)`.

### 4.5 Template Accumulation (Welford's Algorithm)

Phase-folded cycles are accumulated into a running mean and variance using **Welford's online algorithm**, which is numerically stable for arbitrarily many cycles.  For each phase bin $k$ after $N$ cycles:

$$\bar{x}_k^{(N)} = \bar{x}_k^{(N-1)} + \frac{x_k^{(N)} - \bar{x}_k^{(N-1)}}{N} \tag{6}$$

$$M_{2,k}^{(N)} = M_{2,k}^{(N-1)} + \left(x_k^{(N)} - \bar{x}_k^{(N-1)}\right)\left(x_k^{(N)} - \bar{x}_k^{(N)}\right) \tag{7}$$

The standard error of the mean (SEM) in each bin is:

$$\mathrm{SEM}_k = \frac{\sigma_k}{\sqrt{N}} = \frac{\sqrt{M_{2,k}/(N-1)}}{\sqrt{N}} \tag{8}$$

**Implementation**: `TemplateAccumulator` class (`add_cycle()`, `.mean`, `.sem`).

**Reference**: Welford, B. P. (1962), "Note on a method for calculating corrected sums of squares and products," *Technometrics* 4(3), 419–420.

### 4.6 Cycle Period Tracking

The period of each cycle $T_i = (s_{i+1} - s_i) / f_s$ is accumulated using the same Welford running-mean formula.  The mean period $\bar{T}$ is needed to convert phase-domain derivatives to physical time derivatives (§5).

**Implementation**: `self._mean_period_s` and `self._period_count` in `ContinuousWidget.on_file_written()`; `self._point_periods` dict in `ScanWidget`.

---

## 5. Coriolis Prediction from Encoder

The predicted Coriolis acceleration template is computed entirely from the averaged encoder position and the measured cycle period—no free parameters.

### 5.1 Table Velocity

The phase-folded encoder gives position vs. phase: $x(\phi)$ in mm.  Each phase bin spans a time interval:

$$\Delta t = \Delta\phi \times \bar{T} \tag{9}$$

where $\Delta\phi = 1 / N_{\mathrm{bins}}$ and $\bar{T}$ is the mean cycle period in seconds.  The table velocity is:

$$\dot{x}(\phi) = \frac{dx}{d\phi}\,\frac{1}{\bar{T}} \quad [\text{mm/s}] \tag{10}$$

computed via `numpy.gradient(enc_mean_mm, dt)`.

### 5.2 Predicted Coriolis Acceleration

Substituting the velocity into Eq. (1) and converting units:

$$a_{\mathrm{Cor}}(\phi) = \kappa \times \dot{x}(\phi) \times 10^{-3} \;/\; g \quad [\text{in units of } g] \tag{11}$$

For display, the result is converted to µg ($\times 10^6$).

**Implementation**: `coriolis_template(enc_mean_mm, phase, cycle_period_s)` → returns `(a_cor_g, v_mms)`.

---

## 6. Plots

### 6.1 Continuous Mode — Three-Panel Display

| Panel | Content | Units |
|---|---|---|
| **Top** | Phase-folded encoder position (left axis) and table velocity (right axis) | mm, mm/s |
| **Middle** | Measured acceleration ± SEM, overlaid with predicted Coriolis | µg |
| **Bottom** | Residual acceleration (mean-subtracted) ± SEM, overlaid with predicted Coriolis.  Title shows $v_{\mathrm{peak}}$, Coriolis peak, SEM, SNR. | µg |

The **predicted Coriolis** curve (green) is plotted at its true physical amplitude—not rescaled to match the data.  This allows the user to see at a glance whether the measurement SEM has converged to the Coriolis scale.

### 6.2 Scan Mode

A heatmap of SNR vs. (amplitude, frequency) is shown for each waveform, plus an active-point template panel identical to the Continuous Mode bottom panel (in µg with calibrated Coriolis overlay).

### 6.3 Coriolis Regions

Green-shaded phase intervals mark the portions of the waveform where the table velocity (and hence Coriolis force) is largest:

- **Sine**: 60°–120° and 240°–300° (peak velocity phases).
- **Triangle / Rounded Triangle**: determined by the duty cycle parameter; the constant-velocity segments.

**Implementation**: `get_coriolis_phase_ranges()`, `mark_coriolis_region()`.

---

## 7. SNR Metric

The signal-to-noise ratio is computed within the Coriolis region:

$$\mathrm{SNR} = \frac{\langle |r(\phi)| \rangle_{\mathrm{Cor}}}{\langle \mathrm{SEM}(\phi) \rangle_{\mathrm{Cor}}} \tag{12}$$

where $r(\phi) = \bar{a}(\phi) - \langle\bar{a}\rangle$ is the mean-subtracted residual and the averages are over the phase bins inside the Coriolis regions.

**Implementation**: `compute_snr()`.

Note: This is a model-independent metric.  It measures whether the residual in the Coriolis region is statistically distinguishable from zero, without assuming the signal matches the predicted template shape.  A future extension could compute the matched-filter SNR against the predicted template.

---

## 8. Calibration Requirements

| Parameter | Where to set | Typical value | Notes |
|---|---|---|---|
| Accelerometer sensitivity | "Accel sensitivity (mV/g)" | 1000 mV/g | From sensor datasheet |
| Encoder voltage full-scale | "Encoder V FS" | 3.3 V | DAQ ADC reference |
| Encoder position range | "Enc mm low" / "Enc mm hi" | ±0.6375 mm | From encoder calibration |
| Encoder LP cutoff | "Encoder LP (Hz)" | 50 Hz | Removes encoder quantisation noise |
| Phase bins | "Phase bins" | 512 | Higher values capture finer waveform detail |

These feed into Eqs. (3)–(4) and are critical for the absolute calibration of the predicted Coriolis amplitude.  An error in the encoder-to-mm conversion propagates directly to the predicted velocity and hence to $a_{\mathrm{Cor}}$.

---

## 9. Constants Used

Defined at module level in `coriolis_search.py`:

```python
OMEGA_EARTH  = 7.292e-5      # rad/s
LATITUDE_DEG = 41.3           # degrees (Yale)
KAPPA        = 2 * OMEGA_EARTH * sin(radians(LATITUDE_DEG))  # ≈ 9.61e-5 rad/s
G_ACCEL      = 9.80665        # m/s²
```

---

## 10. References

1. **Coriolis coupling constant and sensitivity estimates**: [coriolis_sensitivity.md](../analysis/Inertial%20Sensing/coriolis_sensitivity.md) — derives $\kappa = 2\Omega_\oplus\sin\lambda$, minimum detectable velocity, and displacement amplitude.  Eqs. (1)–(2) in this document correspond to §1 of that reference.

2. **Table motion analysis method**: [coriolis_analysis.md §4](../analysis/Inertial%20Sensing/coriolis_analysis.md) — describes time-domain and frequency-domain approaches for extracting Coriolis from table oscillation.  The phase-folded template averaging in this plugin implements a variant of the time-domain approach (§4.2) where repeatable cycles are coherently averaged rather than fitted.

3. **Experiment methods (Method 3 — Table Motion)**: [experiment_methods.md §4](../analysis/Inertial%20Sensing/experiment_methods.md) — practical considerations for table motion including pneumatic leg resonance, motor vibrations, and sphere survival constraints.

4. **System overview and coordinate system**: [system_overview.md §3](../analysis/Inertial%20Sensing/system_overview.md) — defines the x (≈ North), y (≈ East/West), z (vertical) coordinate system and the sensor chain.

5. **Force noise calibration**: [calibration_overview.md](../analysis/Inertial%20Sensing/calibration_overview.md) — describes the frequency-comb calibration used to measure $|H(f)|$ and the force noise ASD.

6. **Welford's online algorithm**: Welford, B. P. (1962), "Note on a method for calculating corrected sums of squares and products," *Technometrics* 4(3), 419–420.  Used for numerically stable running mean and variance (Eqs. 6–8).
