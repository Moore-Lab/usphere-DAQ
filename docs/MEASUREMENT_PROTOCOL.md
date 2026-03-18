# Measurement Protocol: Optically Levitated Microsphere Experiments

**Project:** usphere-DAQ
**Document Type:** Measurement Protocol
**Date:** February 16, 2026
**Status:** Draft - Template for expansion

---

## 1. Overview

This document describes the measurement protocols for experiments using optically levitated dielectric microspheres in high vacuum. These techniques enable precision force and acceleration sensing, searches for new fundamental interactions, dark matter detection, and mechanical detection of nuclear decays.

---

## 2. Microsphere Preparation

### 2.1 Sphere Selection
- **Material:** <!-- e.g., SiO2 (amorphous silica) -->
- **Diameter range:** <!-- e.g., 3-20 um -->
- **Density:** <!-- e.g., 1.8 g/cm^3 -->
- **Supplier(s):** <!-- e.g., Bangs Laboratories, Corpuscular, Microparticles GmbH -->

### 2.2 Sample Implantation (if applicable)
<!-- Describe any implantation procedures, e.g., 212Pb implantation via 220Rn source for nuclear decay measurements -->

---

## 3. Optical Trapping

### 3.1 Trapping Beam Configuration
- **Laser wavelength:** <!-- e.g., 1064 nm -->
- **Numerical aperture:** <!-- e.g., NA = 0.03 (weak focus) or higher -->
- **Beam orientation:** <!-- e.g., vertical -->
- **Beam waist at trap location:** <!-- e.g., 25 um -->
- **Polarization:** <!-- e.g., circular for rotation, linear for stationary -->

### 3.2 Loading Procedure
<!-- Describe how microspheres are loaded into the trap, e.g., ejection from vibrating glass slide at ~1 mbar -->

### 3.3 Imaging Beams
- **Wavelength:** <!-- e.g., 532 nm -->
- **Number of beams:** <!-- e.g., 2 (vertical and horizontal) -->
- **Purpose:** Position detection of microsphere in 3 DOF (x, y, z)

### 3.4 Detection System
- **In-loop sensors:** <!-- e.g., balanced photodiodes (BPD), lateral effect position sensors (LSP) -->
- **Out-of-loop sensors:** <!-- e.g., independent BPD for noise-squashing-free measurement -->
- **Coordinate definitions:** <!-- e.g., Z = vertical, X = perpendicular to electrode face, Y = along beam -->

---

## 4. Vacuum Protocol

### 4.1 Pumpdown Procedure
| Stage | Pressure Range | Notes |
|-------|---------------|-------|
| Loading | ~1 mbar | Residual gas provides viscous damping |
| Intermediate | 1 - 0.1 mbar | Feedback cooling becomes necessary |
| High vacuum | < 10^-3 mbar | <!-- Describe turbopump engagement, etc. --> |
| Ultra-high vacuum | < 10^-7 mbar | Thermal noise minimized, data acquisition begins |

### 4.2 Pressure Monitoring
<!-- Describe gauges used and logging procedures -->

---

## 5. Feedback Cooling

### 5.1 Feedback Activation
- **Pressure threshold for activation:** <!-- e.g., < 0.1 mbar -->
- **Degrees of freedom cooled:** <!-- e.g., all 3 translational DOF -->

### 5.2 Feedback Implementation
- **Z (vertical):** <!-- e.g., AOM modulation of trapping beam power -->
- **X, Y (radial):** <!-- e.g., piezo deflection mirror displacing trapping beam, up to ~1 kHz -->

### 5.3 Cooling Performance
- **Target effective temperature:** <!-- e.g., ~50 uK for COM motion -->
- **Feedback gain settings:** <!-- Describe gain tuning procedure -->

---

## 6. Charge Management

### 6.1 Charge Measurement
<!-- Describe how net charge Q is measured, e.g., driving oscillating E-field with electrodes and measuring sphere response -->

### 6.2 Discharging / Neutralization
- **UV source:** <!-- e.g., xenon flash lamp -->
- **Electrode configuration:** <!-- e.g., 25.4 mm diameter gold electrodes, ~2-4 mm separation -->
- **Procedure for adding electrons:** <!-- e.g., reduce E-field amplitude, flash UV to eject photoelectrons from electrodes -->
- **Procedure for removing electrons:** <!-- e.g., increase E-field amplitude > 50 V/mm, flash UV -->
- **Target charge state:** <!-- e.g., Q = 0 (neutral) or Q = -e for calibration -->

### 6.3 Charge Monitoring During Data Acquisition
<!-- Describe continuous charge monitoring and automatic discharging protocols, e.g., maintain |Q| < 50e -->

---

## 7. Calibration

### 7.1 Position Calibration
<!-- Describe how photodiode voltage V(t) is converted to physical displacement x(t) using known charge and applied E-field -->

### 7.2 Force / Impulse Calibration
<!-- Describe in situ calibration using electric impulses of known amplitude applied via electrodes -->
- **Calibration pulse parameters:** <!-- e.g., square voltage pulses, delta_t = 100 us, amplitudes 20 V to 1.28 kV -->
- **Electrode spacing:** <!-- e.g., 3.99 +/- 0.05 mm -->

### 7.3 Mass Measurement
<!-- Describe procedure, e.g., microscope imaging at trap + diameter-to-mass conversion, or electrostatic co-levitation technique -->

### 7.4 Resonance Frequency and Damping
- **Resonance frequency f_0:** <!-- e.g., 50-200 Hz depending on sphere size and trap stiffness -->
- **Damping coefficient Gamma_0:** <!-- Measured from power spectrum fits -->

---

## 8. Data Acquisition

### 8.1 DAQ Hardware
- **FPGA:** <!-- Model and role, e.g., feedback control signals -->
- **Digitizer / ADC:** <!-- Sampling rate, resolution -->
- **Sensors recorded:** <!-- List all channels: in-loop BPD, out-of-loop BPD, accelerometer, etc. -->

### 8.2 Data Format and File Structure
- **Sampling rate:** <!-- e.g., 10 kHz (2^20 samples per ~10^5 s file) -->
- **File duration:** <!-- e.g., ~10^5 s per file -->
- **File format:** <!-- e.g., HDF5, binary, etc. -->
- **Naming convention:** <!-- Describe file naming scheme -->

### 8.3 Run Types
| Run Type | Purpose | Duration | Notes |
|----------|---------|----------|-------|
| Calibration | Force/impulse response calibration | <!-- --> | <!-- e.g., ~200 impulses per amplitude --> |
| Science | Primary data collection | <!-- e.g., days to weeks --> | <!-- --> |
| Noise characterization | Background measurement | <!-- --> | <!-- --> |
| Diagnostic | System health check | <!-- --> | <!-- --> |

### 8.4 Auxiliary Data
<!-- Describe any additional sensors or logs, e.g., commercial accelerometer (Wilcoxon 731A/P31), lab access log, environmental monitors -->

---

## 9. Data Quality and Selection Cuts

### 9.1 Live Time Cuts
- **Lab entry cut:** <!-- Exclude periods when someone is in the lab -->
- **Accelerometer cut:** <!-- Exclude files with excess vibrational noise -->
- **Anticoincidence cut:** <!-- Exclude clustered noise events -->

### 9.2 Event-Level Quality Cuts
<!-- Describe quality criteria for individual events, e.g., in-loop / out-of-loop consistency, chi^2 waveform fit -->

---

## 10. Analysis Overview

### 10.1 Signal Processing
<!-- Describe filtering, template matching, optimal filter, etc. -->

### 10.2 Key Measured Quantities
<!-- List primary observables: displacement PSD, impulse amplitudes, charge changes, force spectra, etc. -->

### 10.3 Systematic Uncertainties
<!-- List dominant systematics: sphere diameter, electrode spacing, position uncertainty, etc. -->

---

## 11. Safety and Operating Notes

<!-- Any lab safety protocols, laser safety, high voltage precautions, vacuum system interlocks, etc. -->

---

## 12. References

1. Monteiro et al., "Optical levitation of 10 nanogram spheres with nano-g acceleration sensitivity," Phys. Rev. A **96**, 063841 (2017).
2. Monteiro et al., "Force and acceleration sensing with optically levitated nanogram masses at microkelvin temperatures," arXiv:2001.10931 (2020).
3. Monteiro et al., "Search for Composite Dark Matter with Optically Levitated Sensors," Phys. Rev. Lett. **125**, 181102 (2020).
4. Wang et al., "Mechanical detection of nuclear decays," arXiv:2402.13257 (2024).
5. Blakemore et al., "Precision Mass and Density Measurement of Individual Optically Levitated Microspheres," Phys. Rev. Applied **12**, 024037 (2019).

---

**Document Status:** Template - To be expanded
**Last Updated:** February 16, 2026
